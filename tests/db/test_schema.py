"""Schema, migrations and SQLite configuration."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, StatementError

from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.models import Base, Campaign, Message, Reservation, WorkerHeartbeat
from marcelorian.domain.enums import Mode


def test_migration_matches_models(db_path) -> None:
    engine = make_engine(db_path)
    with engine.connect() as conn:
        diffs = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    engine.dispose()
    assert diffs == []


def test_sqlite_pragmas(db_path) -> None:
    engine = make_engine(db_path)
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        assert cursor.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert cursor.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert cursor.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        raw.close()
        engine.dispose()


def test_naive_datetimes_are_rejected(factory) -> None:
    with pytest.raises(StatementError), transaction(factory) as s:
        s.add(WorkerHeartbeat(at=datetime(2026, 1, 1), component="x"))
        s.flush()


def test_one_logical_message_per_step(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.draft(camp)
    with pytest.raises(IntegrityError), transaction(world.factory) as s:
        now = world.clock.now()
        s.add(
            Message(
                campaign_id=camp,
                mode="sandbox",
                step=1,
                state="draft",
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()


def test_one_active_reservation_per_kind(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.draft_and_approve(camp)
    with pytest.raises(IntegrityError), transaction(world.factory) as s:
        s.add(
            Reservation(
                campaign_id=camp,
                mode="sandbox",
                kind="pending_slot",
                status="active",
                created_at=world.clock.now(),
            )
        )
        s.flush()


def test_campaign_check_constraints(world) -> None:
    pi = world.pi(world.institution())
    with pytest.raises(IntegrityError), transaction(world.factory) as s:
        now = world.clock.now()
        s.add(
            Campaign(
                applicant_id=world.applicant_id,
                pi_id=pi,
                mode="live",
                state="candidate",
                test_recipient_id=1,
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()
    with pytest.raises(IntegrityError), transaction(world.factory) as s:
        now = world.clock.now()
        s.add(
            Campaign(
                applicant_id=world.applicant_id,
                pi_id=pi,
                mode="production",
                state="candidate",
                created_at=now,
                updated_at=now,
            )
        )
        s.flush()


def test_one_campaign_per_applicant_pi_mode(world) -> None:
    pi = world.pi(world.institution())
    world.campaign(pi)
    with pytest.raises(IntegrityError):
        world.campaign(pi)
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.campaign(pi, Mode.LIVE)  # another mode is a separate campaign


def test_begin_immediate_serializes_writers(db_path) -> None:
    engine = make_engine(db_path)
    factory = make_session_factory(engine)
    order: list[str] = []
    started = threading.Event()

    def first() -> None:
        with transaction(factory) as s:
            s.execute(text("SELECT 1"))
            started.set()
            time.sleep(0.3)
            order.append("first-commit")

    def second() -> None:
        started.wait()
        with transaction(factory) as s:
            s.execute(text("SELECT 1"))
            order.append("second-begin")

    threads = [threading.Thread(target=first), threading.Thread(target=second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    engine.dispose()
    assert order == ["first-commit", "second-begin"]
    assert datetime.now(UTC)  # keep the UTC import meaningful for linting


def test_send_channel_migration_round_trips(tmp_path) -> None:
    from marcelorian.db.migrate import downgrade, upgrade

    engine = make_engine(tmp_path / "m.db")
    upgrade(engine, "0001")
    upgrade(engine)
    downgrade(engine, "0001")
    with engine.connect() as conn:
        ddl = conn.execute(text("select sql from sqlite_master where name = 'send_attempt'"))
        assert "handed_off" not in ddl.scalar_one()
    upgrade(engine)
    with engine.connect() as conn:
        ddl = conn.execute(text("select sql from sqlite_master where name = 'send_attempt'"))
        assert "channel IN ('sandbox','gmail','outlook_handoff')" in ddl.scalar_one()
    engine.dispose()


def test_one_open_handoff_per_message(world) -> None:
    from marcelorian.db.models import SendAttempt

    world.connect_handoff_mailbox()
    world.set_mode(Mode.LIVE)
    world.signature_step()
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(camp)
    assert world.hand_off(mid).outcome == "handed_off"
    (attempt,) = world.attempts(mid)
    with pytest.raises(IntegrityError), transaction(world.factory) as s:
        s.add(
            SendAttempt(
                attempt_uid="b" * 32,
                message_id=mid,
                snapshot_id=attempt.snapshot_id,
                snapshot_sha256=attempt.snapshot_sha256,
                mode="live",
                channel="outlook_handoff",
                sender=attempt.sender,
                recipient=attempt.recipient,
                intended_recipient=attempt.intended_recipient,
                app_message_id="<" + "c" * 32 + "@example.test>",
                x_marcelorian_id="d" * 32,
                status="handed_off",
                claimed_at=world.clock.now(),
            )
        )
        s.flush()
