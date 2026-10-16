"""SQLite engine and transactions.

SQLite (WAL) is the durable queue (spec §2). Every transaction starts with
``BEGIN IMMEDIATE`` so writers are serialized and reservation/claim checks
cannot interleave (spec §11 "Concurrency"). ``synchronous=FULL`` keeps a
committed claim durable across power loss. The pysqlite driver's implicit
transaction handling is disabled as recommended by the SQLAlchemy docs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

BUSY_TIMEOUT_MS = 15_000


def make_engine(db_path: Path | str) -> Engine:
    url = f"sqlite+pysqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 15})

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # we emit BEGIN ourselves
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=FULL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _on_begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=True)


@contextmanager
def transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One short ``BEGIN IMMEDIATE`` transaction; commits on success."""
    with factory() as session, session.begin():
        yield session
