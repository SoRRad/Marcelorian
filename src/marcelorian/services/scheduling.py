"""Dispatch evaluations, window rolling and the missed-window rule (spec §9.4; decision B7).

Missed-window rule
------------------
When an approved message's assigned window ``[ws, we)`` has ended unsent:

1. The relevant portion is ``[max(ws, approved_at), we)`` minus intervals in
   which dispatch for the message's mode was deliberately off (persistent
   pause, another mode active), taken from ``runtime_state_log``.
2. If that portion is empty, the message simply rolls to the next window.
3. If any *functioning* evaluation of the message (worker ran with valid
   authorization and a current sync) falls inside the portion, it was
   considered and deferred (caps, pacing, review...): it rolls.
4. Otherwise the worker or connectivity/sync was unavailable for the whole
   portion: the snapshot is invalidated, the message needs re-approval and the
   campaign gets a ``missed_window`` reason. Missing evidence counts as
   unavailability; no successful check is ever fabricated.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import ApprovalSnapshot, Campaign, DispatchEvaluation, Message
from marcelorian.domain.enums import (
    NON_FUNCTIONING_DEFERRALS,
    Actor,
    DeferralReason,
    MessageState,
    Mode,
    PauseReason,
    SnapshotState,
)
from marcelorian.domain.snapshot import verify_canonical
from marcelorian.domain.timing import WindowPolicy, load_zone, next_window
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import runtime
from marcelorian.services.audit import audit

_DELIBERATE_REASONS = (DeferralReason.PAUSED.value, DeferralReason.MODE_INACTIVE.value)


def record_evaluation(
    session: Session,
    message: Message,
    now: datetime,
    *,
    outcome: str,
    reason: DeferralReason | str | None,
    functioning: bool | None = None,
) -> DispatchEvaluation:
    if functioning is None:
        functioning = not (
            isinstance(reason, DeferralReason) and reason in NON_FUNCTIONING_DEFERRALS
        )
    row = DispatchEvaluation(
        message_id=message.id,
        mode=message.mode,
        evaluated_at=now,
        outcome=outcome,
        reason=str(reason) if reason is not None else None,
        functioning=functioning,
    )
    session.add(row)
    return row


def policy_for(snapshot: ApprovalSnapshot, settings: AppSettings) -> WindowPolicy:
    content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
    policy = settings.window_policy()
    if (
        content.test_override is not None
        and content.test_override.window_always_open
        and content.mode is Mode.TRANSPORT_TEST
    ):
        policy = replace(policy, always_open=True)
    return policy


def _was_considered(session: Session, message: Message, start: datetime, end: datetime) -> bool:
    portions = runtime.considerable_intervals(session, Mode(message.mode), start, end)
    if not portions:
        return True  # deliberately unavailable for the whole portion: not a miss
    evaluations = session.scalars(
        select(DispatchEvaluation.evaluated_at).where(
            DispatchEvaluation.message_id == message.id,
            DispatchEvaluation.functioning.is_(True),
            DispatchEvaluation.evaluated_at >= start,
            DispatchEvaluation.evaluated_at < end,
            # Deferrals caused by deliberate unavailability prove nothing about consideration.
            or_(
                DispatchEvaluation.reason.is_(None),
                DispatchEvaluation.reason.not_in(_DELIBERATE_REASONS),
            ),
        )
    ).all()
    return any(lo <= at < hi for at in evaluations for lo, hi in portions)


def apply_missed_window_rule(
    session: Session, now: datetime, settings: AppSettings, *, actor: Actor = Actor.DISPATCHER
) -> list[tuple[int, str]]:
    results: list[tuple[int, str]] = []
    messages = session.scalars(
        select(Message).where(
            Message.state == MessageState.APPROVED_QUEUED.value,
            Message.eligible_window_end.is_not(None),
            Message.eligible_window_end <= now,
        )
    ).all()
    for message in messages:
        snapshot = session.get(ApprovalSnapshot, message.current_snapshot_id)
        assert snapshot is not None and message.eligible_window_start is not None
        assert message.eligible_window_end is not None
        start = max(message.eligible_window_start, snapshot.approved_at)
        end = message.eligible_window_end
        if _was_considered(session, message, start, end):
            policy = policy_for(snapshot, settings)
            tz = load_zone(message.pi_timezone)
            due = message.due_at or now
            window = next_window(max(now, due), tz, policy)
            message.eligible_window_start, message.eligible_window_end = window.start, window.end
            audit(
                session,
                at=now,
                actor=actor,
                entity_type="message",
                entity_id=message.id,
                action="window_rolled",
                detail={"from_end": end.isoformat(), "to_start": window.start.isoformat()},
                mode=message.mode,
            )
            results.append((message.id, "rolled"))
            continue
        snapshot.state, snapshot.ended_at, snapshot.end_reason = (
            SnapshotState.INVALIDATED.value,
            now,
            "missed_window",
        )
        campaign_svc.set_message_state(
            session,
            message,
            MessageState.NEEDS_REAPPROVAL,
            actor=actor,
            now=now,
            reason="missed_window",
        )
        campaign = session.get(Campaign, message.campaign_id)
        assert campaign is not None
        campaign_svc.open_pause(
            session,
            campaign,
            PauseReason.MISSED_WINDOW,
            actor=actor,
            now=now,
            source_ref=f"message:{message.id}:{end.isoformat()}",
            detail={"window_start": start.isoformat(), "window_end": end.isoformat()},
        )
        results.append((message.id, "needs_reapproval"))
    return results


def resolve_missed_window_on_reapproval(
    session: Session, message: Message, *, actor: Actor, now: datetime
) -> None:
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    prefix = f"message:{message.id}:"
    for pause in campaign_svc.open_reasons(session, campaign.id):
        if pause.reason == PauseReason.MISSED_WINDOW.value and (pause.source_ref or "").startswith(
            prefix
        ):
            campaign_svc.resolve_pause(
                session, pause, resolution="re-approved", actor=actor, now=now
            )


def dispatchable_message_ids(session: Session, mode: Mode, now: datetime) -> list[int]:
    """Approved messages whose assigned window is open now, oldest first."""
    return list(
        session.scalars(
            select(Message.id)
            .where(
                Message.mode == mode.value,
                Message.state == MessageState.APPROVED_QUEUED.value,
                Message.eligible_window_start <= now,
                Message.eligible_window_end > now,
            )
            .order_by(Message.eligible_window_start, Message.due_at, Message.id)
        )
    )
