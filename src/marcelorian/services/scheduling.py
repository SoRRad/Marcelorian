"""Dispatch evaluations, window rolling and the missed-window rule (spec §9.4).

Missed-window rule (owner decision S7, 2026-10-19; replaces B7's re-approval)
-----------------------------------------------------------------------------
Nothing is sent without the applicant's click (S1). When an approved message's
assigned window has ended unsent, that was the applicant's choice or one of the
app's own holds, so the approval simply waits: the message rolls to the next
permitted window (from now and its due time), with no re-approval and no review.
The click re-runs every final check against the current state. Only a snapshot
that fails its hash, or a policy with no permitted window, invalidates the
approval (``missed_window`` review), contained per message.

Dispatch evaluations are still recorded (with their ``functioning`` flag) as an
audit of each click and deferral.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import select
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
    next_window,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import runtime
from marcelorian.services import sync as sync_svc
from marcelorian.services.audit import audit

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
        first_end = message.eligible_window_end
        try:
            # Owner decision S7: nothing is sent without the applicant's click (S1), so a
            # window that passes unsent was the applicant's choice or the app's own hold;
            # the approval waits for the next permitted window, with no re-approval. The
            # click re-runs every check against the current state.
            verify_canonical(snapshot.canonical_json, snapshot.sha256)
            tz = load_zone(message.pi_timezone)
            policy = policy_for(snapshot, settings)
            window = next_window(max(now, message.due_at or now), tz, policy)
        except SchedulingError:
            reason = "no_permitted_window"  # defensive: settings validation prevents this
        except SnapshotError:
            # Untrustworthy content is never carried forward; contained per message so one
            # bad snapshot cannot stop the rule (or startup recovery) for others.
            reason = "snapshot_invalid"
        else:
            message.eligible_window_start = window.start
            message.eligible_window_end = window.end
            audit(
                session,
                at=now,
                actor=actor,
                entity_type="message",
                entity_id=message.id,
                action="window_rolled",
                detail={"from_end": first_end.isoformat(), "to_start": window.start.isoformat()},
                mode=message.mode,
            )
            results.append((message.id, "rolled"))
            continue
        _require_reapproval(session, message, snapshot, first_end, reason, actor=actor, now=now)
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
