"""Rolling 24-hour submission budget (spec §11; decisions B13, B17).

* One ledger row per attempt; each attempt is counted exactly once.
* ``held`` (in-flight or uncertain) rows count regardless of age until resolved.
* ``used`` rows count while ``used_at > now - 24h``; ``used_at`` is the
  established actual submission time. There is deliberately no upper bound: a
  send time reported slightly ahead of the local clock (reconciliation accepts
  small clock skew) is real usage and must count immediately.
* ``released`` rows never count. A hold is released only on definite
  non-acceptance or on recorded evidence of definite non-submission.
* Live and transport_test share the ``real`` ledger; sandbox uses ``sandbox``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from marcelorian.db.models import BudgetLedger
from marcelorian.domain.enums import Actor, BudgetStatus, Ledger
from marcelorian.services.audit import audit

WINDOW = timedelta(hours=24)


class BudgetError(RuntimeError):
    pass


def count(session: Session, ledger: Ledger, now: datetime) -> int:
    held = and_(BudgetLedger.status == BudgetStatus.HELD.value)
    used = and_(
        BudgetLedger.status == BudgetStatus.USED.value,
        BudgetLedger.used_at > now - WINDOW,
    )
    stmt = select(func.count()).where(BudgetLedger.ledger == ledger.value, or_(held, used))
    return int(session.scalar(stmt) or 0)


def has_capacity(session: Session, ledger: Ledger, limit: int, now: datetime) -> bool:
    return count(session, ledger, now) + 1 <= limit


def capacity_from(session: Session, ledger: Ledger, limit: int, now: datetime) -> datetime | None:
    """Earliest instant ``has_capacity`` turns true by ageing alone (the send-button
    countdown, S1), or None when held entries alone fill the budget (they count until
    resolved, so no time is known)."""
    excess = count(session, ledger, now) + 1 - limit
    if excess <= 0:
        return now
    used_at = list(
        session.scalars(
            select(BudgetLedger.used_at)
            .where(
                BudgetLedger.ledger == ledger.value,
                BudgetLedger.status == BudgetStatus.USED.value,
                BudgetLedger.used_at > now - WINDOW,
            )
            .order_by(BudgetLedger.used_at)
        )
    )
    if len(used_at) < excess:
        return None
    oldest = used_at[excess - 1]
    assert oldest is not None
    return oldest + WINDOW  # counted while used_at > now - 24h


def hold(session: Session, ledger: Ledger, attempt_id: int, now: datetime) -> BudgetLedger:
    row = BudgetLedger(
        ledger=ledger.value, attempt_id=attempt_id, status=BudgetStatus.HELD.value, held_at=now
    )
    session.add(row)
    return row


def _row(session: Session, attempt_id: int) -> BudgetLedger:
    row = session.scalar(select(BudgetLedger).where(BudgetLedger.attempt_id == attempt_id))
    if row is None:
        raise BudgetError(f"no budget entry for attempt {attempt_id}")
    return row


def convert_to_used(session: Session, attempt_id: int, used_at: datetime, now: datetime) -> None:
    row = _row(session, attempt_id)
    if row.status == BudgetStatus.USED.value:
        return
    if row.status == BudgetStatus.RELEASED.value:
        # Later evidence shows the submission happened after all: account for it.
        audit(
            session,
            at=now,
            actor=Actor.SYSTEM,
            entity_type="budget_ledger",
            entity_id=row.id,
            action="release_contradicted",
            from_state=row.status,
            to_state=BudgetStatus.USED.value,
            reason="submission evidence found after the hold was released",
        )
    row.status = BudgetStatus.USED.value
    row.used_at = used_at


def release(session: Session, attempt_id: int, now: datetime, reason: str) -> None:
    row = _row(session, attempt_id)
    if row.status != BudgetStatus.HELD.value:
        raise BudgetError(f"only a held entry can be released (was {row.status})")
    row.status = BudgetStatus.RELEASED.value
    row.released_at = now
    row.release_reason = reason
