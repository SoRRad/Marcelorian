"""Shared fixtures: migrated temporary SQLite DB, injected clock, synthetic world.

No test uses the network. ``socket`` connections to anything but loopback are
refused for the whole session as a guard.
"""

from __future__ import annotations

import shutil
import socket
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import ManualClock
from marcelorian.db.engine import make_engine, make_session_factory
from marcelorian.db.migrate import upgrade
from tests.factories import World

#: Tuesday 2026-10-20 13:00 UTC == 09:00 America/New_York (inside the default window).
TUESDAY_0900_NY = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    original = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"network access attempted in tests: {address!r}")
        return original(self, address)  # type: ignore[arg-type]

    socket.socket.connect = guarded  # type: ignore[method-assign]
    yield
    socket.socket.connect = original  # type: ignore[method-assign]


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("template") / "template.db"
    engine = make_engine(path)
    upgrade(engine)
    raw = engine.raw_connection()
    try:
        raw.cursor().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        raw.close()
    engine.dispose()
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    path = tmp_path / "marcelorian.db"
    shutil.copy(template_db, path)
    return path


@pytest.fixture
def factory(db_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = make_engine(db_path)
    yield make_session_factory(engine)
    engine.dispose()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(TUESDAY_0900_NY)


@pytest.fixture
def world(
    factory: sessionmaker[Session], clock: ManualClock, request: pytest.FixtureRequest
) -> World:
    world = World(factory, clock)
    if request.node.get_closest_marker("narrow_window"):
        world.narrow_window()
    return world
