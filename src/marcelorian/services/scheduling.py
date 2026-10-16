"""Dispatch evaluations, window rolling and the missed-window rule (spec §9.4; decision B7).

Missed-window rule
------------------
When an approved message's assigned window ``[ws, we)`` has ended unsent:

1. The relevant portion starts at the latest of ``ws``, the approval time and
   the due time, ends at ``we``, and contains only instants the *current*
   window policy permits (blackouts or narrowed hours added after approval
   are honoured). Intervals in which dispatch for the message's mode was
   deliberately off (persistent pause, another mode active), taken from
   ``runtime_state_log``, are removed (DECISIONS note 16; spec §9.4).
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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

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
from marcelorian.domain.snapshot import SnapshotError, verify_canonical
from marcelorian.domain.timing import (
    SchedulingError,
    WindowPolicy,
    load_zone,
    merge_intervals,
    next_window,
    window_on_local_date,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import runtime
from marcelorian.services import sync as sync_svc
from marcelorian.services.audit import audit

#: Evaluations with these reasons are not evidence that the message was considered.
_NOT_CONSIDERATION = (
    DeferralReason.PAUSED.value,
    DeferralReason.MODE_INACTIVE.value,
    DeferralReason.OUTSIDE_WINDOW.value,
    DeferralReason.NOT_DUE.value,
)


#: Gmail coverage counts as a *current sync* for an evaluation when it is at most this
#: old at the evaluation's start: three dispatcher polling cycles (spec §2, §9.4; note 3).
CURRENT_SYNC_TOLERANCE = timedelta(minutes=15)


def evaluation_functioning(
    session: Session, message: Message, evaluated_at: datetime, reason: DeferralReason | str | None
) -> bool:
    """Spec §9.4: functioning only with valid authorization and a current sync.

    Decided from the worker's recorded state, not from the deferral reason alone, so a
    local cap or pacing deferral while Gmail coverage is stale is not evidence that the
    message was considered (B7). Deferrals that mean the check itself could not run are
    never functioning.
    """
    if isinstance(reason, DeferralReason) and reason in NON_FUNCTIONING_DEFERRALS:
        return False
    mode = Mode(message.mode)
    if mode.is_real and runtime.get_runtime(session, evaluated_at).auth_blocked:
        return False
    coverage = sync_svc.coverage(session, sync_svc.mailbox_key_for(mode), evaluated_at)
    if coverage.gap_open or coverage.last_gmail_coverage_at is None:
        return False
    return coverage.last_gmail_coverage_at >= evaluated_at - CURRENT_SYNC_TOLERANCE


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
        functioning = evaluation_functioning(session, message, now, reason)
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


def permitted_intervals(
    start: datetime, end: datetime, tz: ZoneInfo, policy: WindowPolicy
) -> list[tuple[datetime, datetime]]:
    """Instants within [start, end) at which a send could start under ``policy``."""
    if start >= end:
        return []
    if policy.always_open:
        return [(start, end)]
    intervals: list[tuple[datetime, datetime]] = []
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    while day <= last:
        window = window_on_local_date(day, tz, policy)
        if window is not None:
            lo, hi = max(window.start, start), min(window.end, end)
            if lo < hi:
                intervals.append((lo, hi))
        day += timedelta(days=1)
    return merge_intervals(intervals)


def _intersect(
    a: list[tuple[datetime, datetime]], b: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    out = []
    for lo1, hi1 in a:
        for lo2, hi2 in b:
            lo, hi = max(lo1, lo2), min(hi1, hi2)
            if lo < hi:
                out.append((lo, hi))
    return merge_intervals(out)


def _was_considered(
    session: Session, message: Message, portions: list[tuple[datetime, datetime]]
) -> bool:
    if not portions:
        return True  # nothing could have been considered: not a miss
    start, end = portions[0][0], portions[-1][1]
    evaluations = session.scalars(
        select(DispatchEvaluation.evaluated_at).where(
            DispatchEvaluation.message_id == message.id,
            DispatchEvaluation.functioning.is_(True),
            DispatchEvaluation.evaluated_at >= start,
            DispatchEvaluation.evaluated_at < end,
            # Deferrals caused by deliberate unavailability prove nothing about consideration.
            or_(
                DispatchEvaluation.reason.is_(None),
                DispatchEvaluation.reason.not_in(_NOT_CONSIDERATION),
            ),
        )
    ).all()
    return any(lo <= at < hi for at in evaluations for lo, hi in portions)


def _considerable_portions(
    session: Session, message: Message, snapshot: ApprovalSnapshot, settings: AppSettings
) -> list[tuple[datetime, datetime]]:
    """The part of the assigned window in which the message could have been considered.

    It starts at the latest of the window start, the approval and the due time, keeps
    only instants the *current* window policy permits (blackouts, narrowed hours), and
    drops deliberate pause and inactive-mode time (B7).
    """
    assert message.eligible_window_start is not None and message.eligible_window_end is not None
    start = max(message.eligible_window_start, snapshot.approved_at)
    if message.due_at is not None:
        start = max(start, message.due_at)
    end = message.eligible_window_end
    policy = policy_for(snapshot, settings)
    permitted = permitted_intervals(start, end, load_zone(message.pi_timezone), policy)
    deliberate_free = runtime.considerable_intervals(session, Mode(message.mode), start, end)
    return _intersect(permitted, deliberate_free)


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
        assert snapshot is not None and message.eligible_window_end is not None
        end = message.eligible_window_end
        try:
            portions = _considerable_portions(session, message, snapshot, settings)
            if _was_considered(session, message, portions):
                tz = load_zone(message.pi_timezone)
                due = message.due_at or now
                window = next_window(max(now, due), tz, policy_for(snapshot, settings))
                message.eligible_window_start = window.start
                message.eligible_window_end = window.end
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
            reason = "missed_window"
        except SchedulingError:
            reason = "no_permitted_window"  # defensive: settings validation prevents this
        except SnapshotError:
            # Untrustworthy evidence never rolls a message forward; contained per message
            # so one bad snapshot cannot stop the rule (or startup recovery) for others.
            reason = "snapshot_invalid"
        _require_reapproval(session, message, snapshot, end, reason, actor=actor, now=now)
        results.append((message.id, "needs_reapproval"))
    return results


def _require_reapproval(
    session: Session,
    message: Message,
    snapshot: ApprovalSnapshot,
    window_end: datetime,
    reason: str,
    *,
    actor: Actor,
    now: datetime,
) -> None:
    snapshot.state, snapshot.ended_at, snapshot.end_reason = (
        SnapshotState.INVALIDATED.value,
        now,
        reason,
    )
    campaign_svc.set_message_state(
        session, message, MessageState.NEEDS_REAPPROVAL, actor=actor, now=now, reason=reason
    )
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    campaign_svc.open_pause(
        session,
        campaign,
        PauseReason.MISSED_WINDOW,
        actor=actor,
        now=now,
        source_ref=f"message:{message.id}:{window_end.isoformat()}",
        detail={"window_end": window_end.isoformat(), "reason": reason},
    )


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
