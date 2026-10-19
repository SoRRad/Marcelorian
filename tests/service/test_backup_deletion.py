"""Backups, restore and "Delete all my data" (spec §14)."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from marcelorian.app_main import main, prepare_database
from marcelorian.clock import ManualClock
from marcelorian.db.engine import make_engine, transaction
from marcelorian.db.migrate import downgrade, needs_upgrade
from marcelorian.db.models import Message, Setting
from marcelorian.domain.enums import MessageState
from marcelorian.platform.instance_lock import InstanceLock
from marcelorian.platform.paths import AppPaths
from marcelorian.services import backup, deletion, runtime
from marcelorian.services.ai import KEY_NAME
from tests.conftest import TUESDAY_0900_NY
from tests.fakes.secrets import InMemorySecretStore


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock(TUESDAY_0900_NY)


@pytest.fixture
def paths(tmp_path: Path) -> AppPaths:
    return AppPaths(
        data_dir=tmp_path / "data",
        exports_dir=tmp_path / "docs" / "exports",
        exports_synced_by_onedrive=False,
    )


def _name(factory) -> str:
    with transaction(factory) as s:
        row = s.get(Setting, "synthetic_marker")
        return json.loads(row.value_json) if row else ""


def _set_name(factory, value: str) -> None:
    with transaction(factory) as s:
        row = s.get(Setting, "synthetic_marker")
        if row is None:
            s.add(
                Setting(
                    key="synthetic_marker", value_json=json.dumps(value), updated_at=TUESDAY_0900_NY
                )
            )
        else:
            row.value_json = json.dumps(value)


def test_backup_is_consistent_and_pruned(paths, clock) -> None:
    factory = prepare_database(paths.db_path, clock, paths.backups_dir)
    _set_name(factory, "Synthetic Before")
    made = backup.backup_now(paths.db_path, paths.backups_dir, clock.now(), label="manual")
    with sqlite3.connect(made) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        row = copy.execute("SELECT value_json FROM settings WHERE key = 'synthetic_marker'")
        assert json.loads(row.fetchone()[0]) == "Synthetic Before"  # WAL content included
    for hours in range(1, 20):
        backup.backup_now(paths.db_path, paths.backups_dir, clock.now() + timedelta(hours=hours))
    assert len(backup.list_backups(paths.backups_dir)) == backup.KEEP
    assert not backup.due_daily(paths.backups_dir, clock.now() + timedelta(hours=20))
    assert backup.daily(paths.db_path, paths.backups_dir, clock.now() + timedelta(hours=44))


def test_restore_is_staged_then_applied_at_start_with_sending_paused(paths, clock) -> None:
    factory = prepare_database(paths.db_path, clock, paths.backups_dir)
    _set_name(factory, "Old Name")
    made = backup.backup_now(paths.db_path, paths.backups_dir, clock.now(), label="manual")
    _set_name(factory, "New Name")
    with pytest.raises(backup.BackupError):
        backup.stage_restore(paths.backups_dir, "../marcelorian.db", paths.data_dir)
    backup.stage_restore(paths.backups_dir, made.name, paths.data_dir)
    assert backup.restore_pending(paths.data_dir)
    factory.kw["bind"].dispose()  # the app has stopped
    clock.advance(timedelta(minutes=5))
    restarted = prepare_database(paths.db_path, clock, paths.backups_dir)
    assert _name(restarted) == "Old Name" and not backup.restore_pending(paths.data_dir)
    with transaction(restarted) as s:
        view = runtime.get_runtime(s, clock.now())
        assert view.dispatch_paused and view.pause_reason == "restored_backup"
    labels = [b.label for b in backup.list_backups(paths.backups_dir)]
    assert "before-restore" in labels  # the replaced data was kept


def test_backup_before_update(paths, clock) -> None:
    factory = prepare_database(paths.db_path, clock, paths.backups_dir)
    engine = factory.kw["bind"]
    downgrade(engine, "0008")
    assert needs_upgrade(engine)
    engine.dispose()
    prepare_database(paths.db_path, clock, paths.backups_dir)
    assert "before-update" in [b.label for b in backup.list_backups(paths.backups_dir)]
    assert not needs_upgrade(make_engine(paths.db_path))


def test_request_deletion_stops_sending_and_recalls_approvals(world, tmp_path) -> None:
    message_id = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    with transaction(world.factory) as s:
        deletion.request_deletion(s, tmp_path, now=world.clock.now())
    assert deletion.requested(tmp_path)
    with transaction(world.factory) as s:
        assert runtime.get_runtime(s, world.clock.now()).dispatch_paused
        assert s.get(Message, message_id).state != MessageState.APPROVED_QUEUED.value


def test_perform_deletion_removes_app_files_and_key(paths, clock) -> None:
    factory = prepare_database(paths.db_path, clock, paths.backups_dir)
    backup.backup_now(paths.db_path, paths.backups_dir, clock.now())
    paths.logs_dir.mkdir(parents=True)
    (paths.logs_dir / "app.log").write_text("synthetic log\n")
    paths.exports_dir.mkdir(parents=True)
    (paths.exports_dir / "owner_report.csv").write_text("x\n")
    (paths.data_dir / deletion.MARKER).write_text("x")
    store = InMemorySecretStore()
    store.set(KEY_NAME, "sk-ant-synthetic-key-0000")
    factory.kw["bind"].dispose()
    report = deletion.perform_deletion(paths, lambda: store)
    assert not report.failed
    assert store.get(KEY_NAME) is None
    for gone in (paths.db_path, paths.backups_dir, paths.logs_dir, paths.exports_dir):
        assert not gone.exists(), gone
    assert not deletion.requested(paths.data_dir)


def test_launcher_finishes_an_interrupted_deletion(tmp_path, monkeypatch, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    (data / "marcelorian.db").write_text("synthetic")
    (data / deletion.MARKER).write_text("x")

    class Free(InstanceLock):
        def acquire(self) -> InstanceLock:
            return self

        def release(self) -> None:
            return None

    monkeypatch.setattr("marcelorian.app_main.InstanceLock", Free)
    monkeypatch.setattr("marcelorian.app_main.configure_logging", lambda *a, **k: None)
    monkeypatch.setattr("marcelorian.app_main.open_secret_store", InMemorySecretStore)
    monkeypatch.setattr(
        "marcelorian.app_main._serve", lambda *a, **k: pytest.fail("must not serve")
    )
    assert main(["--data-dir", str(data), "--no-browser"]) == 0
    assert not (data / "marcelorian.db").exists()
    assert "has been deleted" in capsys.readouterr().out
