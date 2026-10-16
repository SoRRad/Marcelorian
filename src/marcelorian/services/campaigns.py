"""Campaign transitions and the pause-reason policy (spec §3; decisions B3, B5, B13).

Policy, kept deliberately small and explicit:

* Each pause reason is its own ``campaign_pause`` row. Resolving one never
  clears another. ``paused_review`` is shown while at least one reason is open
  for a campaign whose base state is ``outreach_pending``.
* ``conversation``, ``sequence_complete`` and ``closed`` are never overwritten by
  recovery, late results or pause bookkeeping. Open reasons on such campaigns
  remain as attention items but cannot restart a stopped sequence.
* Leaving the holding states releases the pending slot. The new-contact ledger
  and any unresolved attempt (with its budget hold) are untouched (B13).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import ApprovalSnapshot, Campaign, CampaignPause, Message, SendAttempt
from marcelorian.domain.enums import (
    HOLDING_STATES,
    UNRESOLVED_ATTEMPT_STATES,
    UNSENT_CANCELLABLE_STATES,
    Actor,
    CampaignState,
    CloseOutcome,
    MessageState,
    PauseReason,
    SnapshotState,
)
from marcelorian.domain.transitions import check_campaign, check_message
from marcelorian.services import reservations
from marcelorian.services.audit import audit


class CampaignError(RuntimeError):
    pass


def set_campaign_state(
    session: Session,
    campaign: Campaign,
    target: CampaignState,
    *,
    actor: Actor,
    now: datetime,
    reason: str | None = None,
    hold_slot_until: datetime | None = None,
) -> None:
    current = CampaignState(campaign.state)
    if current is target:
        return
    check_campaign(current, target)
    campaign.state = target.value
    campaign.updated_at = now
    if current in HOLDING_STATES and target not in HOLDING_STATES:
        reservations.release_pending_slot(
            session, campaign, now, reason or f"left {current.value}", hold_until=hold_slot_until
        )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="state",
        from_state=current.value,
        to_state=target.value,
        reason=reason,
        mode=campaign.mode,
    )


def set_message_state(
    session: Session,
    message: Message,
    target: MessageState,
    *,
    actor: Actor,
    now: datetime,
    reason: str | None = None,
) -> None:
    current = MessageState(message.state)
    if current is target:
        return
    check_message(current, target)
    message.state = target.value
    message.updated_at = now
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="message",
        entity_id=message.id,
        action="state",
        from_state=current.value,
        to_state=target.value,
        reason=reason,
        mode=message.mode,
    )


def open_reasons(session: Session, campaign_id: int) -> list[CampaignPause]:
    return list(
        session.scalars(
            select(CampaignPause).where(
                CampaignPause.campaign_id == campaign_id, CampaignPause.resolved_at.is_(None)
            )
        )
    )


def open_pause(
    session: Session,
    campaign: Campaign,
    reason: PauseReason,
    *,
    actor: Actor,
    now: datetime,
    source_ref: str | None = None,
    detail: dict[str, Any] | None = None,
) -> CampaignPause:
    for row in open_reasons(session, campaign.id):
        if row.reason == reason.value and row.source_ref == source_ref:
            return row  # idempotent: the same reason from the same source stays one row
    if CampaignState(campaign.state) is CampaignState.CANDIDATE:
        raise CampaignError("a candidate has no outreach to pause")
    row = CampaignPause(
        campaign_id=campaign.id,
        reason=reason.value,
        source_ref=source_ref,
        detail_json=json.dumps(detail, sort_keys=True) if detail else None,
        opened_at=now,
    )
    session.add(row)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="pause_opened",
        reason=reason.value,
        detail={"source": source_ref},
        mode=campaign.mode,
    )
    if CampaignState(campaign.state) is CampaignState.OUTREACH_PENDING:
        set_campaign_state(
            session,
            campaign,
            CampaignState.PAUSED_REVIEW,
            actor=actor,
            now=now,
            reason=reason.value,
        )
    return row


def resolve_pause(
    session: Session, pause: CampaignPause, *, resolution: str, actor: Actor, now: datetime
) -> None:
    if pause.resolved_at is not None:
        return
    pause.resolved_at, pause.resolution, pause.resolved_by = now, resolution, str(actor)
    campaign = session.get(Campaign, pause.campaign_id)
    assert campaign is not None
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="pause_resolved",
        reason=pause.reason,
        detail={"resolution": resolution},
        mode=campaign.mode,
    )
    session.flush()
    if CampaignState(campaign.state) is CampaignState.PAUSED_REVIEW and not open_reasons(
        session, campaign.id
    ):
        set_campaign_state(
            session,
            campaign,
            CampaignState.OUTREACH_PENDING,
            actor=actor,
            now=now,
            reason=f"all review reasons resolved ({pause.reason})",
        )


def resolve_reason_for_source(
    session: Session,
    campaign: Campaign,
    reason: PauseReason,
    source_ref: str,
    *,
    resolution: str,
    actor: Actor,
    now: datetime,
) -> None:
    for row in open_reasons(session, campaign.id):
        if row.reason == reason.value and row.source_ref == source_ref:
            resolve_pause(session, row, resolution=resolution, actor=actor, now=now)


def has_unresolved_attempt(session: Session, campaign_id: int) -> bool:
    return bool(
        session.scalar(
            select(SendAttempt.id)
            .join(Message, Message.id == SendAttempt.message_id)
            .where(
                Message.campaign_id == campaign_id,
                SendAttempt.status.in_([s.value for s in UNRESOLVED_ATTEMPT_STATES]),
            )
            .limit(1)
        )
    )


def _end_snapshot(
    session: Session, message: Message, state: SnapshotState, now: datetime, reason: str
) -> None:
    if message.current_snapshot_id is None:
        return
    snapshot = session.get(ApprovalSnapshot, message.current_snapshot_id)
    if snapshot is not None and snapshot.state == SnapshotState.ACTIVE.value:
        snapshot.state, snapshot.ended_at, snapshot.end_reason = state.value, now, reason


def cancel_unsent_messages(
    session: Session,
    campaign: Campaign,
    *,
    actor: Actor,
    now: datetime,
    reason: str,
    steps_from: int = 1,
    clear_schedule: bool = True,
) -> list[int]:
    """Cancel queued and not-yet-sent messages; claimed/uncertain ones are reconciled instead."""
    cancelled: list[int] = []
    for message in session.scalars(
        select(Message).where(Message.campaign_id == campaign.id, Message.step >= steps_from)
    ):
        state = MessageState(message.state)
        if state in UNSENT_CANCELLABLE_STATES:
            _end_snapshot(session, message, SnapshotState.REVOKED, now, reason)
            set_message_state(
                session, message, MessageState.CANCELLED, actor=actor, now=now, reason=reason
            )
            cancelled.append(message.id)
        elif state is MessageState.SENDING:
            message.cancel_requested_at = message.cancel_requested_at or now
    if clear_schedule:
        campaign.next_step, campaign.next_due_at = None, None
    return cancelled


def apply_genuine_reply(
    session: Session, campaign: Campaign, *, actor: Actor, now: datetime, source_ref: str
) -> None:
    state = CampaignState(campaign.state)
    if state in (CampaignState.CONVERSATION, CampaignState.CLOSED):
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="campaign",
            entity_id=campaign.id,
            action="reply_recorded",
            reason="campaign already stopped",
            detail={"source": source_ref},
            mode=campaign.mode,
        )
        return
    if state is CampaignState.CANDIDATE:
        raise CampaignError("a reply cannot belong to a campaign with no outreach")
    campaign.sequence_stopped = True
    cancel_unsent_messages(session, campaign, actor=actor, now=now, reason="genuine reply")
    set_campaign_state(
        session, campaign, CampaignState.CONVERSATION, actor=actor, now=now, reason="genuine reply"
    )


def close_campaign(
    session: Session,
    campaign: Campaign,
    outcome: CloseOutcome,
    *,
    actor: Actor,
    now: datetime,
    reason: str | None = None,
) -> None:
    if outcome is CloseOutcome.APPLICANT_CLOSED and not (reason and reason.strip()):
        raise CampaignError("closing a campaign requires a reason")
    campaign.sequence_stopped = True
    cancel_unsent_messages(session, campaign, actor=actor, now=now, reason=f"closed:{outcome}")
    email1_sent = bool(
        session.scalar(
            select(Message.id).where(
                Message.campaign_id == campaign.id,
                Message.step == 1,
                Message.state == MessageState.SENT.value,
            )
        )
    )
    if not email1_sent and not has_unresolved_attempt(session, campaign.id):
        reservations.release_projected_contact(session, campaign, now, f"closed:{outcome}")
    campaign.close_outcome, campaign.close_reason = outcome.value, reason
    set_campaign_state(
        session,
        campaign,
        CampaignState.CLOSED,
        actor=actor,
        now=now,
        reason=f"{outcome.value}: {reason or ''}".strip(),
    )


def stop_followup_sequence(
    session: Session, campaign: Campaign, *, actor: Actor, now: datetime
) -> list[int]:
    """'Stop this follow-up sequence' (B5): no further generation/dispatch; slot kept."""
    campaign.sequence_stopped = True
    # The schedule is kept so the applicant can deliberately re-draft the next unsent step.
    cancelled = cancel_unsent_messages(
        session,
        campaign,
        actor=actor,
        now=now,
        reason="follow-up sequence stopped",
        steps_from=2,
        clear_schedule=False,
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="sequence_stopped",
        detail={"cancelled": cancelled},
        mode=campaign.mode,
    )
    return cancelled


def after_step_sent(
    session: Session,
    campaign: Campaign,
    step: int,
    sent_at: datetime,
    settings: AppSettings,
    *,
    actor: Actor,
    now: datetime,
) -> None:
    """Campaign effects of a successful send. Never overwrites a newer stopped state (B13)."""
    state = CampaignState(campaign.state)
    if step == 1:
        reservations.consume_new_contact(session, campaign, sent_at)
    if state not in HOLDING_STATES:
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="campaign",
            entity_id=campaign.id,
            action="late_send_recorded",
            reason=f"step {step} sent while {state.value}",
            mode=campaign.mode,
        )
        return
    if step == 4:
        hold_until = (
            sent_at + reservations.SLOT_HOLD_AFTER_EMAIL4
            if settings.hold_slot_14d_after_email4
            else None
        )
        campaign.next_step, campaign.next_due_at = None, None
        set_campaign_state(
            session,
            campaign,
            CampaignState.SEQUENCE_COMPLETE,
            actor=actor,
            now=now,
            reason="Email 4 sent",
            hold_slot_until=hold_until,
        )
