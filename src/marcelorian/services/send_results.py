"""Attempt outcomes shared by the gateway and reconciliation (spec §9.6 steps 4-7; B13, B17).

* Success: attempt + message ``sent`` at the established submission time; the
  budget hold converts to usage (counted once); Email 1 consumes its projected
  contact; the next step's due time is computed from that ``sent_at``. A
  success that arrives after the campaign stopped is recorded without changing
  the campaign (B13).
* Definite rejection: ``failed_definite``; the hold is released; reservations
  are kept for re-approval (B4).
* Uncertain: budget and reservations held; the campaign gets its own
  ``send_uncertain`` reason; never retried automatically.
* Recorded definite non-submission: hold released, message needs
  re-approval; only that attempt's reason is resolved and the campaign is
  never forced back to ``outreach_pending``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import ApprovalSnapshot, Campaign, Message, SendAttempt
from marcelorian.domain.enums import (
    HOLDING_STATES,
    Actor,
    AttemptStatus,
    CampaignState,
    MessageState,
    Mode,
    PauseReason,
    SentAtProvenance,
)
from marcelorian.domain.snapshot import verify_canonical
from marcelorian.domain.timing import due_for_step, load_zone
from marcelorian.services import budget
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services.audit import audit


class ResolutionError(RuntimeError):
    pass


def attempt_source(attempt: SendAttempt) -> str:
    return f"attempt:{attempt.attempt_uid}"


def _context(session: Session, attempt: SendAttempt) -> tuple[Message, Campaign, ApprovalSnapshot]:
    message = session.get(Message, attempt.message_id)
    snapshot = session.get(ApprovalSnapshot, attempt.snapshot_id)
    assert message is not None and snapshot is not None
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    return message, campaign, snapshot


def finalize_sent(
    session: Session,
    attempt: SendAttempt,
    *,
    sent_at: datetime,
    provenance: SentAtProvenance,
    gmail_message_id: str | None,
    gmail_thread_id: str | None,
    settings: AppSettings,
    actor: Actor,
    now: datetime,
) -> None:
    message, campaign, snapshot = _context(session, attempt)
    if attempt.status == AttemptStatus.SENT.value:
        return
    attempt.status = AttemptStatus.SENT.value
    attempt.sent_at, attempt.sent_at_provenance = sent_at, provenance.value
    attempt.gmail_message_id = gmail_message_id
    attempt.gmail_thread_id = gmail_thread_id
    if provenance is SentAtProvenance.GATEWAY_RESPONSE:
        attempt.submit_completed_at = now
    budget.convert_to_used(session, attempt.id, sent_at, now)
    message.sent_at, message.sent_at_provenance = sent_at, provenance.value
    message.gmail_message_id, message.gmail_thread_id = gmail_message_id, gmail_thread_id
    message.rfc_message_id = attempt.app_message_id
    message.cap_block_reason = None
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.SENT,
        actor=actor,
        now=now,
        reason=f"sent ({provenance.value})",
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action="sent",
        detail={"provenance": provenance.value, "sent_at": sent_at.isoformat()},
        mode=attempt.mode,
    )
    campaign_svc.resolve_reason_for_source(
        session,
        campaign,
        PauseReason.SEND_UNCERTAIN,
        attempt_source(attempt),
        resolution="confirmed_sent",
        actor=actor,
        now=now,
    )
    campaign_svc.after_step_sent(
        session, campaign, message.step, sent_at, settings, actor=actor, now=now
    )
    _schedule_next(session, campaign, message, snapshot, sent_at, settings)


def _schedule_next(
    session: Session,
    campaign: Campaign,
    message: Message,
    snapshot: ApprovalSnapshot,
    sent_at: datetime,
    settings: AppSettings,
) -> None:
    if (
        message.step >= 4
        or CampaignState(campaign.state) not in HOLDING_STATES
        or campaign.sequence_stopped
        or message.cancel_requested_at is not None
    ):
        if message.cancel_requested_at is not None:
            campaign.sequence_stopped = True
        return
    content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
    override = content.test_override.intervals() if content.test_override else None
    if override and Mode(campaign.mode) is not Mode.TRANSPORT_TEST:
        override = None  # defensive: overrides never apply outside transport_test
    tz = load_zone(content.pi_timezone)
    campaign.next_step = message.step + 1
    campaign.next_due_at = due_for_step(
        message.step + 1, sent_at, tz, settings.last_interval(), override
    )


def finalize_rejected(
    session: Session,
    attempt: SendAttempt,
    *,
    error_code: str,
    detail: str,
    actor: Actor,
    now: datetime,
) -> None:
    message, _campaign, _snapshot = _context(session, attempt)
    attempt.status = AttemptStatus.FAILED_DEFINITE.value
    attempt.error_code, attempt.error_detail = error_code, detail[:500]
    attempt.submit_completed_at = now
    budget.release(session, attempt.id, now, "definite_rejection")
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.FAILED_DEFINITE,
        actor=actor,
        now=now,
        reason=f"definite rejection {error_code}",
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action="failed_definite",
        reason=error_code,
        mode=attempt.mode,
    )


def mark_uncertain(
    session: Session, attempt: SendAttempt, *, detail: str, actor: Actor, now: datetime
) -> None:
    message, campaign, _snapshot = _context(session, attempt)
    if attempt.status == AttemptStatus.IN_FLIGHT.value:
        attempt.status = AttemptStatus.UNCERTAIN.value
        campaign_svc.set_message_state(
            session, message, MessageState.UNCERTAIN, actor=actor, now=now, reason=detail
        )
    attempt.error_detail = detail[:500]
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action="uncertain",
        reason=detail,
        mode=attempt.mode,
    )
    if CampaignState(campaign.state) is not CampaignState.CANDIDATE:
        campaign_svc.open_pause(
            session,
            campaign,
            PauseReason.SEND_UNCERTAIN,
            actor=actor,
            now=now,
            source_ref=attempt_source(attempt),
            detail={"detail": detail},
        )


def resolve_not_submitted(
    session: Session, attempt: SendAttempt, *, note: str, actor: Actor, now: datetime
) -> None:
    """Applicant recorded evidence of definite non-submission (B13)."""
    if attempt.status != AttemptStatus.UNCERTAIN.value:
        raise ResolutionError("only an uncertain attempt can be resolved as not submitted")
    if not note or not note.strip():
        raise ResolutionError("evidence of definite non-submission must be recorded")
    message, campaign, _snapshot = _context(session, attempt)
    attempt.status = AttemptStatus.NOT_SUBMITTED.value
    attempt.resolution, attempt.resolution_note = "definite_non_submission", note.strip()
    attempt.resolved_by, attempt.resolved_at = str(actor), now
    budget.release(session, attempt.id, now, "recorded_definite_non_submission")
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.NEEDS_REAPPROVAL,
        actor=actor,
        now=now,
        reason="uncertain attempt resolved: not submitted",
    )
    campaign_svc.resolve_reason_for_source(
        session,
        campaign,
        PauseReason.SEND_UNCERTAIN,
        attempt_source(attempt),
        resolution="definite_non_submission",
        actor=actor,
        now=now,
    )


def resolve_confirmed_sent(
    session: Session,
    attempt: SendAttempt,
    *,
    sent_at: datetime,
    settings: AppSettings,
    actor: Actor,
    now: datetime,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
) -> None:
    """Applicant confirmed the send in Gmail and reported the time it shows (B13)."""
    if attempt.status != AttemptStatus.UNCERTAIN.value:
        raise ResolutionError("only an uncertain attempt can be confirmed")
    if not attempt.claimed_at <= sent_at <= now:
        raise ResolutionError("the reported send time must lie between the claim and now")
    attempt.resolution, attempt.resolved_by, attempt.resolved_at = "confirmed_sent", str(actor), now
    finalize_sent(
        session,
        attempt,
        sent_at=sent_at,
        provenance=SentAtProvenance.APPLICANT_REPORTED,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=gmail_thread_id,
        settings=settings,
        actor=actor,
        now=now,
    )


def record_still_unresolved(
    session: Session, attempt: SendAttempt, *, note: str | None, actor: Actor, now: datetime
) -> None:
    if attempt.status != AttemptStatus.UNCERTAIN.value:
        raise ResolutionError("only an uncertain attempt can be marked still unresolved")
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action="still_unresolved",
        reason=note,
        mode=attempt.mode,
    )


def attempt_by_uid(session: Session, attempt_uid: str) -> SendAttempt:
    attempt = session.scalar(select(SendAttempt).where(SendAttempt.attempt_uid == attempt_uid))
    if attempt is None:
        raise ResolutionError(f"unknown attempt {attempt_uid}")
    return attempt
