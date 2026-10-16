"""Persisted spacing between actual submissions (spec §9.4; decision B8).

The ``real`` ledger serializes spacing across live and transport_test regardless
of PI time zone; sandbox has its own. The next allowed instant is chosen at
random in [min, max] when a submission is authorized and stored in the
database, so restarts and mode switches never reset it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from marcelorian.db.models import PacingState
from marcelorian.domain.enums import Ledger


def _row(session: Session, ledger: Ledger) -> PacingState:
    row = session.get(PacingState, ledger.value)
    if row is None:
        row = PacingState(ledger=ledger.value)
        session.add(row)
        session.flush()
    return row


def next_allowed(session: Session, ledger: Ledger) -> datetime | None:
    return _row(session, ledger).next_allowed_at


def allows(session: Session, ledger: Ledger, now: datetime) -> bool:
    allowed = next_allowed(session, ledger)
    return allowed is None or now >= allowed


def record_submission(
    session: Session,
    ledger: Ledger,
    now: datetime,
    rng: random.Random,
    min_seconds: int,
    max_seconds: int,
) -> datetime:
    if min_seconds <= 0 or max_seconds < min_seconds:
        raise ValueError("pacing bounds must be positive and ordered")
    row = _row(session, ledger)
    row.last_submission_at = now
    row.next_allowed_at = now + timedelta(seconds=rng.randint(min_seconds, max_seconds))
    return row.next_allowed_at
