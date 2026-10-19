"""Migrations on a database that already holds data (hand-off review F1).

A 0001 database is populated by copying rows produced through the real services (a
Gmail-mode live send and a sandbox send) and then upgraded to head.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlalchemy import text

from marcelorian.clock import ManualClock
from marcelorian.db.engine import make_engine, make_session_factory
from marcelorian.db.migrate import downgrade, upgrade
from marcelorian.domain.enums import Mode
from tests.conftest import TUESDAY_0900_NY
from tests.factories import World


def _populated_head(path: Path) -> None:
    engine = make_engine(path)
    upgrade(engine)
    world = World(make_session_factory(engine), ManualClock(TUESDAY_0900_NY))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(camp)
    world.real_sync.sync_now()
    result = world.dispatch(mid)
    assert result.outcome.value == "sent", result
    engine.dispose()


def _copy_into_0001(src: Path, dst: Path) -> None:
    engine = make_engine(dst)
    upgrade(engine, "0001")
    engine.dispose()
    con = sqlite3.connect(dst)
    con.execute("PRAGMA foreign_keys=OFF")
    con.execute(f"ATTACH DATABASE '{src}' AS head")
    tables = [
        r[0]
        for r in con.execute(
            "select name from sqlite_master where type='table' and name != 'alembic_version'"
            " and name not like 'sqlite_%'"
        )
    ]
    for table in tables:
        cols_new = [r[1] for r in con.execute(f"PRAGMA main.table_info('{table}')")]
        cols_head = {r[1] for r in con.execute(f"PRAGMA head.table_info('{table}')")}
        cols = [c for c in cols_new if c in cols_head]
        joined = ",".join(f'"{c}"' for c in cols)
        # Table and column names come from this test's own schema, never from input.
        con.execute(f'DELETE FROM main."{table}"')  # noqa: S608
        con.execute(f'INSERT INTO main."{table}" ({joined}) SELECT {joined} FROM head."{table}"')  # noqa: S608
    con.commit()
    con.execute("DETACH DATABASE head")
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    assert con.execute("select count(*) from send_attempt").fetchone()[0] == 1
    con.close()


def test_0002_upgrades_a_database_with_data(tmp_path: Path) -> None:
    head = tmp_path / "head.db"
    old = tmp_path / "old.db"
    _populated_head(head)
    _copy_into_0001(head, old)
    engine = make_engine(old)
    upgrade(engine)  # rebuilt tables are referenced by other rows: must not fail or lose rows
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from send_attempt")).scalar_one() == 1
        assert conn.execute(text("select count(*) from message")).scalar_one() == 1
        assert conn.execute(text("select count(*) from approval_snapshot")).scalar_one() == 1
        assert conn.execute(text("select channel from send_attempt")).scalar_one() == "gmail"
        assert conn.execute(text("select kind from mailbox_account")).scalar_one() == "gmail"
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    engine.dispose()
    downgrade(engine, "0001")  # the same rebuilds in reverse
    with engine.connect() as conn:
        assert conn.execute(text("select count(*) from send_attempt")).scalar_one() == 1
        assert conn.execute(text("PRAGMA foreign_key_check")).fetchall() == []
    upgrade(engine)
    engine.dispose()
