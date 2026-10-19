"""Backups and restore (spec §14).

* Copies are made with the SQLite **online backup API** (never a raw copy of a live WAL
  database), checked with ``PRAGMA integrity_check``, then swapped in atomically.
* Daily (worker) and before every database update (launcher); the newest 14 are kept.
* **Restore** never replaces the database while the app runs: the chosen backup is checked
  and staged as ``restore-pending.db``, and the next start (before the database is
  opened) backs up the current database, removes its WAL/SHM files, swaps the staged copy
  in, and starts with **sending paused** (``restored_backup``) until the applicant resumes.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

KEEP = 14
DAILY = timedelta(hours=24)
PENDING_NAME = "restore-pending.db"
_NAME = re.compile(r"^marcelorian-\d{8}-\d{6}-[a-z-]+\.db$")


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BackupInfo:
    name: str
    path: Path
    size_bytes: int
    created_at: datetime
    label: str


def _integrity_ok(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.DatabaseError:
        return False
    finally:
        connection.close()


def backup_now(db_path: Path, backups_dir: Path, now: datetime, *, label: str = "daily") -> Path:
    if not db_path.is_file():
        raise BackupError("there is no database to back up yet")
    backups_dir.mkdir(parents=True, exist_ok=True)
    name = f"marcelorian-{now:%Y%m%d-%H%M%S}-{label}.db"
    final = backups_dir / name
    tmp = backups_dir / f".{name}.tmp"
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(tmp)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    if not _integrity_ok(tmp):
        tmp.unlink(missing_ok=True)
        raise BackupError("the backup copy failed its integrity check")
    os.replace(tmp, final)
    prune(backups_dir)
    return final


def list_backups(backups_dir: Path) -> list[BackupInfo]:
    if not backups_dir.is_dir():
        return []
    found = []
    for path in backups_dir.iterdir():
        if not _NAME.match(path.name) or not path.is_file():
            continue
        stamp = datetime.strptime(path.name[12:27], "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
        label = path.name[28:-3]
        found.append(BackupInfo(path.name, path, path.stat().st_size, stamp, label))
    return sorted(found, key=lambda b: b.name, reverse=True)


def prune(backups_dir: Path, keep: int = KEEP) -> list[str]:
    removed = []
    for info in list_backups(backups_dir)[keep:]:
        try:
            info.path.unlink(missing_ok=True)
        except OSError:  # open elsewhere (Windows locks open files): try again next time
            continue
        removed.append(info.name)
    return removed


def due_daily(backups_dir: Path, now: datetime) -> bool:
    newest = list_backups(backups_dir)
    return not newest or now - newest[0].created_at >= DAILY


def daily(db_path: Path, backups_dir: Path, now: datetime) -> Path | None:
    """The worker's daily copy; nothing when the newest backup is under a day old."""
    if not db_path.is_file() or not due_daily(backups_dir, now):
        return None
    return backup_now(db_path, backups_dir, now, label="daily")


def stage_restore(backups_dir: Path, name: str, data_dir: Path) -> Path:
    """Check the chosen backup and stage it for the next start."""
    chosen = {b.name: b for b in list_backups(backups_dir)}.get(name)
    if chosen is None:
        raise BackupError("that backup does not exist")
    if not _integrity_ok(chosen.path):
        raise BackupError("that backup failed its integrity check; choose another one")
    pending = data_dir / PENDING_NAME
    tmp = data_dir / f".{PENDING_NAME}.tmp"
    shutil.copyfile(chosen.path, tmp)
    os.replace(tmp, pending)
    return pending


def cancel_restore(data_dir: Path) -> None:
    (data_dir / PENDING_NAME).unlink(missing_ok=True)


def restore_pending(data_dir: Path) -> bool:
    return (data_dir / PENDING_NAME).is_file()


def apply_pending_restore(db_path: Path, backups_dir: Path, now: datetime) -> bool:
    """At start, before the database is opened: swap a staged backup in. True if done."""
    pending = db_path.parent / PENDING_NAME
    if not pending.is_file():
        return False
    if not _integrity_ok(pending):
        pending.unlink()
        raise BackupError("the staged backup failed its integrity check and was discarded")
    if db_path.is_file():
        backup_now(db_path, backups_dir, now, label="before-restore")
    for suffix in ("-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    os.replace(pending, db_path)
    return True
