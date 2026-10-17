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

import re
from datetime import datetime
from email.utils import getaddresses

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
from marcelorian.domain.identifiers import bare_message_id
from marcelorian.domain.snapshot import verify_canonical
from marcelorian.domain.timing import SchedulingError, due_for_step, load_zone
from marcelorian.services import budget
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services.audit import audit
from marcelorian.transport.base import MessageMeta


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
    # The Message-ID used for threading is the provider-observed one, recorded only once
    # the sent message's metadata has been read back and verified (M06). Until then the
    # next step cannot be approved or dispatched; the app id stays on the attempt.
    message.rfc_message_id = None
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
    """Record when the next step is due.

    It is recorded even when the sequence was stopped (Cancel/Stop during submission):
    ``sequence_stopped`` blocks automatic generation and dispatch, while the schedule
    keeps the B5 exit open - a deliberate re-draft of the next unsent step.
    """
    if message.cancel_requested_at is not None:
        campaign.sequence_stopped = True
    if message.step >= 4 or CampaignState(campaign.state) not in HOLDING_STATES:
        return
    try:
        # A snapshot failing its hash (SnapshotError is a ValueError) never undoes the
        # recorded send: the schedule goes to review like any unschedulable step.
        content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
        override = content.test_override.intervals() if content.test_override else None
        if override and Mode(campaign.mode) is not Mode.TRANSPORT_TEST:
            override = None  # defensive: overrides never apply outside transport_test
        tz = load_zone(content.pi_timezone)
        due = due_for_step(message.step + 1, sent_at, tz, settings.last_interval(), override)
    except (SchedulingError, OverflowError, ValueError) as exc:
        # The send itself must still commit (spec §9.6 step 4); the schedule needs review.
        campaign.next_step, campaign.next_due_at = None, None
        audit(
            session,
            at=sent_at,
            actor=Actor.SYSTEM,
            entity_type="campaign",
            entity_id=campaign.id,
            action="next_step_unschedulable",
            reason=type(exc).__name__,
            mode=campaign.mode,
        )
        return
    campaign.next_step = message.step + 1
    campaign.next_due_at = due


def finalize_rejected(
    session: Session,
    attempt: SendAttempt,
    *,
    error_code: str,
    detail: str,
    actor: Actor,
    now: datetime,
) -> None:
    message, campaign, _snapshot = _context(session, attempt)
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
    _after_non_submission(session, campaign, message, actor=actor, now=now)


def _after_non_submission(
    session: Session, campaign: Campaign, message: Message, *, actor: Actor, now: datetime
) -> None:
    """Definite non-submission: honour a recorded cancel and free unneeded capacity."""
    campaign_svc.complete_requested_cancel(session, campaign, message, actor=actor, now=now)
    campaign_svc.release_projected_contact_if_unneeded(
        session, campaign, now, "email 1 definitely not submitted"
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
    _after_non_submission(session, campaign, message, actor=actor, now=now)


def record_not_invoked(
    session: Session, attempt: SendAttempt, *, reason: str, actor: Actor, now: datetime
) -> None:
    """The gateway refused to call the transport for a claimed attempt (stale hand-off).

    ``send`` was provably never invoked for this attempt, so this is a definite
    non-submission recorded by the gateway itself, not an ambiguous outcome: the hold is
    released, the message needs re-approval (never a blind retry), and no
    ``send_uncertain`` review is opened. A cancel recorded after the claim takes effect.
    """
    if attempt.status != AttemptStatus.IN_FLIGHT.value:
        raise ResolutionError("only an in-flight attempt can be recorded as not invoked")
    message, campaign, _snapshot = _context(session, attempt)
    attempt.status = AttemptStatus.NOT_SUBMITTED.value
    attempt.submit_started_at = None
    attempt.error_code = reason[:64]
    attempt.resolution, attempt.resolution_note = "not_invoked", f"transport not called: {reason}"
    attempt.resolved_by, attempt.resolved_at = str(actor), now
    budget.release(session, attempt.id, now, "not_invoked")
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.NEEDS_REAPPROVAL,
        actor=actor,
        now=now,
        reason=f"claimed but not submitted: {reason}",
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action="not_invoked",
        reason=reason,
        mode=attempt.mode,
    )
    _after_non_submission(session, campaign, message, actor=actor, now=now)


def resolve_confirmed_sent(
    session: Session,
    attempt: SendAttempt,
    *,
    sent_at: datetime,
    settings: AppSettings,
    actor: Actor,
    now: datetime,
    gmail_message_id: str,
    gmail_thread_id: str,
) -> None:
    """Applicant confirmed the send in Gmail and reported what it shows (B13).

    The Gmail message and thread IDs are required: follow-ups must thread on the real
    conversation (§9.7), so a confirmation without them would strand the campaign.
    """
    if attempt.status != AttemptStatus.UNCERTAIN.value:
        raise ResolutionError("only an uncertain attempt can be confirmed")
    if not (gmail_message_id or "").strip() or not (gmail_thread_id or "").strip():
        raise ResolutionError("confirming a send requires its Gmail message and thread IDs")
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


# msg-id (RFC 5322 §3.6.4) as dot-atom text: printable ASCII only, no specials,
# whitespace or control characters, so a provider value cannot inject headers.
_RFC_ID = re.compile(r"^<[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9!#$%&'*+/=?^_`{|}~.\[\]-]+>$")


def sent_attempt_for(session: Session, message: Message) -> SendAttempt | None:
    return session.scalar(
        select(SendAttempt).where(
            SendAttempt.message_id == message.id,
            SendAttempt.status == AttemptStatus.SENT.value,
        )
    )


def thread_header_problem(
    meta: MessageMeta, attempt: SendAttempt, message: Message, expected_thread: str | None
) -> str | None:
    """Why the provider's metadata does not verify this sent step, or None (M06)."""
    if meta.gmail_message_id != message.gmail_message_id:
        return "gmail_id_mismatch"
    if "SENT" not in meta.label_ids:
        return "not_in_sent"
    if meta.thread_id != message.gmail_thread_id or (
        expected_thread is not None and meta.thread_id != expected_thread
    ):
        return "thread_mismatch"
    header_id = meta.header("message-id")
    header_x = meta.header("x-marcelorian-id")
    ours = (header_x is not None and header_x.strip() == attempt.x_marcelorian_id) or (
        header_id is not None
        and bare_message_id(header_id) == bare_message_id(attempt.app_message_id)
    )
    if not ours:
        return "not_corroborated"
    recipients = {a.lower() for _n, a in getaddresses([meta.header("to") or ""])}
    if attempt.recipient.lower() not in recipients:
        return "recipient_mismatch"
    if header_id is None or not _RFC_ID.match(header_id.strip()):
        return "message_id_missing"
    return None


def record_thread_headers(
    session: Session,
    message: Message,
    attempt: SendAttempt,
    meta: MessageMeta,
    expected_thread: str | None,
    *,
    actor: Actor,
    now: datetime,
) -> bool:
    """Store the provider-observed Message-ID if the metadata verifies the sent step."""
    problem = thread_header_problem(meta, attempt, message, expected_thread)
    if problem is not None:
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="message",
            entity_id=message.id,
            action="thread_headers_unverified",
            reason=problem,
            mode=message.mode,
        )
        return False
    observed = (meta.header("message-id") or "").strip()
    message.rfc_message_id = observed
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="message",
        entity_id=message.id,
        action="thread_headers_verified",
        detail={
            "message_id_preserved": bare_message_id(observed)
            == bare_message_id(attempt.app_message_id)
        },
        mode=message.mode,
    )
    return True


def verified_references(
    session: Session, campaign_id: int, before_step: int
) -> tuple[str, ...] | None:
    """Provider-observed Message-IDs of steps 1..before_step-1, or None if any is unverified."""
    sent = list(
        session.scalars(
            select(Message)
            .where(
                Message.campaign_id == campaign_id,
                Message.step < before_step,
                Message.state == MessageState.SENT.value,
            )
            .order_by(Message.step)
        )
    )
    if len(sent) != before_step - 1 or any(m.rfc_message_id is None for m in sent):
        return None
    return tuple(m.rfc_message_id for m in sent if m.rfc_message_id)
