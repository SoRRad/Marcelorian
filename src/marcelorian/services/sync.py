"""Mailbox sync contract and coverage state (spec §10; decisions B9, B10).

The full Gmail history sync with gap reconciliation is P3 (its live behaviour
is checked in the P0.3 spike). P0 defines the contract the gateway relies on:

* ``MailboxSync.sync_now()`` runs an incremental sync and records coverage
  through ``record_sync_success`` / ``record_sync_gap``.
* The real mailbox has one coverage record (key ``real``) shared by live and
  transport_test; it may advance only after both real modes' full range has
  been processed. Sandbox has its own record (key ``sandbox``).
* An open gap blocks all real sends.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.db.engine import transaction
from marcelorian.db.models import SyncState
from marcelorian.domain.enums import Ledger, Mode, ledger_for


@dataclass(frozen=True)
class SyncOutcome:
    ok: bool
    gap: bool = False
    detail: str | None = None


class MailboxSync(Protocol):
    mailbox_key: str

    def sync_now(self) -> SyncOutcome: ...


def mailbox_key_for(mode: Mode) -> str:
    return ledger_for(mode).value


def _row(session: Session, key: str, now: datetime) -> SyncState:
    row = session.get(SyncState, key)
    if row is None:
        row = SyncState(mailbox_key=key, gap_open=False, updated_at=now)
        session.add(row)
        session.flush()
    return row


def record_sync_success(
    session: Session, key: str, coverage_at: datetime, cursor: str | None, now: datetime
) -> None:
    row = _row(session, key, now)
    if row.gap_open:
        raise RuntimeError("an open sync gap must be closed by gap reconciliation first")
    if row.last_gmail_coverage_at is None or coverage_at > row.last_gmail_coverage_at:
        row.last_gmail_coverage_at = coverage_at
    if cursor is not None:
        row.history_cursor = cursor
    row.last_error = None
    row.updated_at = now


def record_sync_gap(session: Session, key: str, detail: str, now: datetime) -> None:
    row = _row(session, key, now)
    if not row.gap_open:
        row.gap_open = True
        row.gap_since = now
    row.last_error = detail
    row.updated_at = now


def close_sync_gap(session: Session, key: str, coverage_at: datetime, now: datetime) -> None:
    """Called only after full gap reconciliation (P3) has covered the range."""
    row = _row(session, key, now)
    row.gap_open = False
    row.gap_since = None
    row.last_gmail_coverage_at = coverage_at
    row.last_error = None
    row.updated_at = now


def reset_coverage(session: Session, key: str, now: datetime) -> None:
    """Forget all coverage for a mailbox key (setup-only identity switch, M02).

    The new identity inherits no history cursor, coverage time or gap from the old
    one; the claim's freshness rule then requires a new successful sync before any
    real send.
    """
    row = _row(session, key, now)
    row.history_cursor = None
    row.last_gmail_coverage_at = None
    row.gap_open = False
    row.gap_since = None
    row.last_error = None
    row.updated_at = now


def coverage(session: Session, key: str, now: datetime) -> SyncState:
    return _row(session, key, now)


class SandboxSync:
    """Sandbox has no external mailbox: a sync is always complete."""

    mailbox_key = Ledger.SANDBOX.value

    def __init__(self, session_factory: sessionmaker[Session], clock: Clock) -> None:
        self._factory = session_factory
        self._clock = clock

    def sync_now(self) -> SyncOutcome:
        now = self._clock.now()
        with transaction(self._factory) as session:
            record_sync_success(session, self.mailbox_key, now, None, now)
        return SyncOutcome(ok=True)
