""" "Delete all my data" (spec §14; Outlook-only pilot U3).

The request is recorded while the app runs: sending is paused, approved emails go back to
review (so nothing can be opened in Outlook), and a marker file is written. The files are
removed after the app has stopped and closed the database and logs, because Windows
cannot delete files that are still open. A marker left by an interrupted run is finished
at the next start, before anything else opens.

Removed: the Claude key in the credential store, the database with its WAL/SHM files,
staged restores, backups, logs, sessions (in memory) and the Marcelorian exports folder.
CVs live inside the database, so they go with it.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Message
from marcelorian.domain.enums import Actor, MessageState, RuntimePauseReason
from marcelorian.lookups.keys import SECRET_NAMES as LOOKUP_SECRET_NAMES
from marcelorian.platform.paths import AppPaths
from marcelorian.platform.secrets import SecretStore, SecretStoreUnavailable
from marcelorian.services import approvals, runtime
from marcelorian.services.ai import KEY_NAME
from marcelorian.services.audit import audit

MARKER = "delete-pending"
CONFIRM_WORD = "DELETE"

#: What stays outside the app after deletion (shown to the applicant).
REMAINS = (
    "Emails you sent from Outlook stay in your Outlook Sent folder and in the PIs' inboxes.",
    "Export files you copied elsewhere, or sent to the program owner, stay where you put them.",
    "Windows or OneDrive backups of your computer may still hold older copies.",
    "If you used Claude suggestions, revoke the API key in the Anthropic console.",
)


@dataclass
class DeletionReport:
    removed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def requested(data_dir: Path) -> bool:
    return (data_dir / MARKER).is_file()


def request_deletion(session: Session, data_dir: Path, *, now: datetime) -> None:
    """Step 1 (in the running app): stop sending, recall approvals, write the marker."""
    runtime.pause_dispatch(
        session, actor=Actor.APPLICANT, now=now, reason=RuntimePauseReason.APPLICANT
    )
    queued = session.scalars(
        select(Message).where(Message.state == MessageState.APPROVED_QUEUED.value)
    ).all()
    for message in queued:
        try:
            approvals.revoke(session, message, actor=Actor.APPLICANT, now=now)
        except (approvals.ApprovalError, approvals.CannotRecall):
            continue  # everything is removed after shutdown anyway; sending is paused
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="app",
        entity_id="data",
        action="delete_all_requested",
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / MARKER).write_text("delete all data at the next stop\n", encoding="utf-8")


def _remove(path: Path, report: DeletionReport, label: str) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
        else:
            return
        report.removed.append(label)
    except OSError:
        report.failed.append(label)


def perform_deletion(
    paths: AppPaths, secrets: Callable[[], SecretStore] | None = None
) -> DeletionReport:
    """Step 2 (app stopped, database and logs closed). The instance lock file stays."""
    report = DeletionReport()
    if secrets is not None:
        try:
            store = secrets()
            store.delete(KEY_NAME)
            for name in LOOKUP_SECRET_NAMES.values():
                store.delete(name)
            report.removed.append("API keys (Windows Credential Manager)")
        except SecretStoreUnavailable:
            report.failed.append("API keys (credential store unavailable)")
    db = paths.db_path
    for suffix, label in (
        ("", "database"),
        ("-wal", "database journal"),
        ("-shm", "database index"),
    ):
        _remove(Path(f"{db}{suffix}"), report, label)
    _remove(paths.data_dir / "restore-pending.db", report, "staged restore")
    _remove(paths.backups_dir, report, "backups")
    _remove(paths.logs_dir, report, "logs")
    _remove(paths.exports_dir, report, "exports")
    if not report.failed:
        _remove(paths.data_dir / MARKER, report, "deletion marker")
    return report
