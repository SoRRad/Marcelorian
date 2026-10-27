"""Outlook hand-off and the send-button countdown (spec §9.8; owner decisions S1-S6).

The app has no access to the Outlook mailbox. A hand-off runs every final check that
needs no mailbox (``gateway.final_checks``) at the instant the applicant presses
**Open in Outlook** / **Copy the text** / **Email file**, records a ``send_attempt`` with
status ``handed_off`` (budget held, pacing recorded), and returns the email
(``domain.handoff``). The applicant sends it from Outlook and records **Sent** or
**Not sent**. The mailbox searches the Gmail gateway runs are replaced by the
applicant's confirmations: no earlier contact found (Email 1) and no reply received
(follow-ups); a reply or an earlier contact can be reported at any time.

Every function runs inside the caller's (``BEGIN IMMEDIATE``) transaction; no
network I/O happens here.

Known limit: the checks hold at the hand-off instant; when the applicant presses
Send in Outlook is outside the app's control (spec §9.8).
"""

from __future__ import annotations

import dataclasses
import json
import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import (
    ApprovalSnapshot,
    AuditEvent,
    Blob,
    Campaign,
    Institution,
    Message,
    Pi,
    SendAttempt,
)
from marcelorian.domain.enums import (
    Actor,
    AttemptStatus,
    CampaignState,
    Channel,
    DeferralReason,
    InboundClassification,
    Ledger,
    MessageState,
    Mode,
    PauseReason,
    SnapshotState,
    ledger_for,
)
from marcelorian.domain.handoff import HandoffPackage, build_package
from marcelorian.domain.identifiers import new_x_marcelorian_id, opaque_token, rfc_message_id
from marcelorian.domain.snapshot import SnapshotContent, SnapshotError, verify_canonical
from marcelorian.domain.timing import SchedulingError, load_zone, next_window
from marcelorian.services import (
    approvals,
    budget,
    dnc,
    pacing,
    prior_contact,
    scheduling,
    send_results,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services.audit import audit
from marcelorian.services.gateway import Outcome, Verdict, apply_verdict, final_checks
from marcelorian.services.inbound import record_inbound

#: Owner decision S5: at most three hand-offs may be unresolved at once.
MAX_OPEN_HANDOFFS = 3
#: An unresolved hand-off older than this is shown as a reminder (spec §15).
REMIND_AFTER = timedelta(hours=24)

#: Deferrals that end by time alone, so the send button can show a countdown.
_TIMED = frozenset(
    {
        DeferralReason.NOT_DUE.value,
        DeferralReason.OUTSIDE_WINDOW.value,
        DeferralReason.PACING.value,
        DeferralReason.BUDGET.value,
    }
)

HANDED_OFF = "handed_off"
#: Readiness/hand-off reason until the first-use signature step is done (S8).
SIGNATURE_STEP = "signature_step_required"


class HandoffError(RuntimeError):
    pass


@dataclass(frozen=True)
class Confirmations:
    """The applicant's attestations, standing in for mailbox searches (spec §9.8)."""

    #: Email 1: searched Outlook for the PI's listed addresses; no earlier contact, or
    #: what was found has been reviewed in the app.
    prior_contact_checked: bool = False
    #: Follow-ups: the PI has not replied.
    no_reply: bool = False


@dataclass(frozen=True)
class HandoffResult:
    #: ``handed_off`` or a gateway ``Outcome`` value (deferred, blocked, not_eligible).
    outcome: str
    reason: str | None = None
    attempt_uid: str | None = None
    package: HandoffPackage | None = None


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason: str | None
    #: Earliest instant the button unlocks when only time stands in the way (countdown);
    #: None when another reason blocks it or no time is known.
    unlock_at: datetime | None
    #: Hand-off only: the confirmation the applicant must give at the click.
    confirmation: str | None = None


@dataclass(frozen=True)
class OpenHandoff:
    attempt_uid: str
    message_id: int
    campaign_id: int
    step: int
    handed_off_at: datetime
    needs_reminder: bool


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


def addresses_to_search(session: Session, message: Message) -> tuple[str, ...]:
    """The addresses the applicant searches Outlook for: the PI's whole identity (§3)."""
    snapshot = _active_snapshot(session, message)
    content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
    if Mode(message.mode) is Mode.TRANSPORT_TEST:
        return (content.to,)  # the test address stands in for the PI (spec §12)
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    return tuple(sorted(dnc.pi_identity(session, campaign.pi_id, (content.to,)).addresses))


def open_handoffs(session: Session, now: datetime) -> list[OpenHandoff]:
    rows = session.execute(
        select(SendAttempt, Message)
        .join(Message, Message.id == SendAttempt.message_id)
        .where(SendAttempt.status == AttemptStatus.HANDED_OFF.value)
        .order_by(SendAttempt.claimed_at)
    ).all()
    return [
        OpenHandoff(
            attempt_uid=attempt.attempt_uid,
            message_id=message.id,
            campaign_id=message.campaign_id,
            step=message.step,
            handed_off_at=attempt.claimed_at,
            needs_reminder=now - attempt.claimed_at >= REMIND_AFTER,
        )
        for attempt, message in rows
    ]


def _open_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count())
            .select_from(SendAttempt)
            .where(SendAttempt.status == AttemptStatus.HANDED_OFF.value)
        )
        or 0
    )


def _required_confirmation(message: Message, given: Confirmations | None) -> str | None:
    if message.step == 1:
        return None if given and given.prior_contact_checked else "prior_contact_checked"
    return None if given and given.no_reply else "no_reply"


def _hold(session: Session, message: Message) -> Verdict | None:
    """Hand-off-only holds after the shared final checks pass."""
    if _open_count(session) >= MAX_OPEN_HANDOFFS:
        return Verdict(Outcome.DEFERRED, "too_many_open_handoffs")
    addresses = set(addresses_to_search(session, message))
    if prior_contact.unresolved_attempt_to(session, addresses, Mode(message.mode)):
        return Verdict(Outcome.DEFERRED, DeferralReason.OWN_ATTEMPT_UNRESOLVED)
    return None


# --------------------------------------------------------------------------- #
# The send button (both channels)
# --------------------------------------------------------------------------- #


def readiness(
    session: Session, message: Message, settings: AppSettings, now: datetime
) -> Readiness:
    """Whether the send button is enabled now, and when it unlocks (owner decision S1).

    Read-only: uses the same final checks as the click, records nothing. For the Gmail
    channel the network preflight still runs at the click.
    """
    mode = Mode(message.mode)
    channel = approvals.message_channel(session, mode)
    if channel is None:
        return Readiness(False, "mailbox_not_connected", None)
    if message.state != MessageState.APPROVED_QUEUED.value:
        return Readiness(False, "not_approved_queued", None)
    confirmation = (
        _required_confirmation(message, None) if channel is Channel.OUTLOOK_HANDOFF else None
    )
    if channel is Channel.OUTLOOK_HANDOFF and not settings.signature_step_done:
        return Readiness(False, SIGNATURE_STEP, None, confirmation)
    verdict = final_checks(session, message, settings, now, preflight_started=None, channel=channel)
    timed = verdict is not None and verdict.outcome is Outcome.DEFERRED and verdict.reason in _TIMED
    if channel is Channel.OUTLOOK_HANDOFF and (verdict is None or timed):
        # A hold no clock ends (three open hand-offs, an unresolved attempt to this
        # person) wins over a countdown, which would otherwise promise a false unlock.
        verdict = _hold(session, message) or verdict
    if verdict is None:
        return Readiness(True, None, now, confirmation)
    reason = str(verdict.reason)
    if verdict.outcome is not Outcome.DEFERRED or reason not in _TIMED:
        return Readiness(False, reason, None, confirmation)
    return Readiness(False, reason, _unlock_at(session, message, settings, now), confirmation)


def _unlock_at(
    session: Session, message: Message, settings: AppSettings, now: datetime
) -> datetime | None:
    snapshot = _active_snapshot(session, message)
    try:
        content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
        tz = load_zone(content.pi_timezone)
    except (SnapshotError, SchedulingError):
        return None
    ledger = ledger_for(Mode(message.mode))
    limit = (
        settings.real_submissions_per_24h
        if ledger is Ledger.REAL
        else settings.sandbox_submissions_per_24h
    )
    free = budget.capacity_from(session, ledger, limit, now)
    if free is None:
        return None
    candidates = [now, free]
    candidates += [t for t in (message.due_at, message.eligible_window_start) if t is not None]
    allowed = pacing.next_allowed(session, ledger)
    if allowed is not None:
        candidates.append(allowed)
    at = max(candidates)
    try:
        window = next_window(at, tz, scheduling.policy_for(snapshot, settings))
    except SchedulingError:
        return None
    at = max(at, window.start)
    if message.eligible_window_end is None or at >= message.eligible_window_end:
        return None  # the assigned window ends first; the missed-window rule decides
    return at


# --------------------------------------------------------------------------- #
# Hand-off and its outcomes
# --------------------------------------------------------------------------- #


def hand_off(
    session: Session,
    message: Message,
    confirmations: Confirmations,
    *,
    settings: AppSettings,
    rng: random.Random,
    actor: Actor,
    now: datetime,
) -> HandoffResult:
    """Run the final checks and, if they pass, give the email to the applicant (§9.8)."""
    if message.state != MessageState.APPROVED_QUEUED.value:
        return HandoffResult(Outcome.NOT_ELIGIBLE.value, "not_approved_queued")
    mode = Mode(message.mode)
    if not mode.is_real:
        return HandoffResult(Outcome.NOT_ELIGIBLE.value, "handoff_real_modes_only")
    if approvals.active_channel(session) is Channel.OUTLOOK_HANDOFF and not (
        settings.signature_step_done
    ):
        # First use: the applicant adds a signature or skips it (S8); nothing recorded.
        return HandoffResult(Outcome.BLOCKED.value, SIGNATURE_STEP)
    missing = _required_confirmation(message, confirmations)
    if missing is not None:
        # Nothing is recorded: the applicant has not yet confirmed the mailbox check.
        return HandoffResult(Outcome.BLOCKED.value, f"confirmation_required:{missing}")
    verdict = final_checks(
        session, message, settings, now, preflight_started=None, channel=Channel.OUTLOOK_HANDOFF
    )
    if verdict is None:
        verdict = _hold(session, message)
    snapshot = _active_snapshot(session, message)
    package: HandoffPackage | None = None
    if verdict is None:
        content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
        signature = settings.email_signature
        try:  # built before anything is recorded, so a failure leaves no hand-off
            app_message_id = rfc_message_id(content.sender)
            package = build_package(
                content, _attachment_bytes(session, content), signature, app_message_id
            )
        except (ValueError, SnapshotError):
            verdict = Verdict(Outcome.BLOCKED, "email_not_buildable", invalidate=True)
    if verdict is not None:
        # The click itself proves the app was running: recorded as a functioning
        # evaluation (audit), as a Gmail evaluation with a current sync would be.
        result = apply_verdict(session, message, verdict, now, now, functioning=True)
        return HandoffResult(result.outcome.value, result.reason)
    assert package is not None
    attempt = SendAttempt(
        attempt_uid=opaque_token(),
        message_id=message.id,
        snapshot_id=snapshot.id,
        snapshot_sha256=snapshot.sha256,
        mode=message.mode,
        channel=Channel.OUTLOOK_HANDOFF.value,
        sender=content.sender,
        recipient=content.to,
        intended_recipient=content.intended_recipient,
        # In the .eml file (new Outlook needs one to save the draft); unique per attempt.
        app_message_id=app_message_id,
        x_marcelorian_id=new_x_marcelorian_id(),
        status=AttemptStatus.HANDED_OFF.value,
        claimed_at=now,
    )
    session.add(attempt)
    session.flush()
    snapshot.state = SnapshotState.CONSUMED.value
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.HANDED_OFF,
        actor=actor,
        now=now,
        reason=f"handed off {attempt.attempt_uid}",
    )
    message.cap_block_reason = None
    message.last_deferral_reason = None
    ledger = ledger_for(mode)
    budget.hold(session, ledger, attempt.id, now)
    # Spacing runs from the hand-off: the app cannot see when Outlook actually sends.
    pacing.record_submission(
        session, ledger, now, rng, settings.pacing_min_seconds, settings.pacing_max_seconds
    )
    scheduling.record_evaluation(
        session, message, now, outcome=HANDED_OFF, reason=None, functioning=True
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="send_attempt",
        entity_id=attempt.id,
        action=HANDED_OFF,
        detail={
            "attempt": attempt.attempt_uid,
            "snapshot_sha256": snapshot.sha256,
            "confirmed": ("prior_contact_checked" if message.step == 1 else "no_reply"),
            "searched_addresses": list(addresses_to_search(session, message)),
            # Reopening rebuilds the identical email even if the setting changes later.
            "signature": signature,
        },
        mode=message.mode,
    )
    return HandoffResult(HANDED_OFF, None, attempt.attempt_uid, package)


def package_for(session: Session, attempt_uid: str) -> HandoffPackage:
    """Reopen an unresolved hand-off: the same email, never a second hand-off."""
    attempt = _handed_off(session, attempt_uid)
    snapshot = session.get(ApprovalSnapshot, attempt.snapshot_id)
    assert snapshot is not None
    content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
    event = session.scalar(
        select(AuditEvent).where(
            AuditEvent.entity_type == "send_attempt",
            AuditEvent.entity_id == str(attempt.id),
            AuditEvent.action == HANDED_OFF,
        )
    )
    if event is None or not event.detail_json:
        raise HandoffError("the hand-off record is missing")
    signature = str(json.loads(event.detail_json).get("signature", ""))
    return build_package(
        content, _attachment_bytes(session, content), signature, attempt.app_message_id
    )


def record_sent(
    session: Session,
    attempt_uid: str,
    *,
    edited: bool,
    settings: AppSettings,
    actor: Actor,
    now: datetime,
) -> None:
    send_results.record_handoff_sent(
        session,
        _handed_off(session, attempt_uid),
        edited=edited,
        settings=settings,
        actor=actor,
        now=now,
    )


def record_not_sent(
    session: Session, attempt_uid: str, *, note: str | None, actor: Actor, now: datetime
) -> None:
    send_results.record_handoff_not_sent(
        session, _handed_off(session, attempt_uid), note=note, actor=actor, now=now
    )


def report_reply(
    session: Session, campaign: Campaign, *, note: str | None, actor: Actor, now: datetime
) -> None:
    """The applicant saw a reply from the PI in Outlook: a genuine reply (§9.8, §10).

    The campaign moves to ``conversation`` and unsent steps are cancelled; a hand-off
    still open gets a cancel request that takes effect if Not sent is recorded.
    """
    if not Mode(campaign.mode).is_real:
        raise HandoffError("sandbox replies are simulated, not reported")
    if CampaignState(campaign.state) is CampaignState.CANDIDATE:
        raise HandoffError("a reply cannot belong to a campaign with no outreach")
    event, _new = record_inbound(
        session,
        campaign,
        gmail_message_id=f"manual:{opaque_token()}",
        thread_id=None,
        classification=InboundClassification.GENUINE_REPLY,
        from_address=None,
        received_at=None,
        actor=actor,
        now=now,
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="reply_reported_by_applicant",
        reason=(note or "").strip()[:500] or None,
        detail={"inbound_event": event.id},
        mode=campaign.mode,
    )


def report_prior_contact(
    session: Session, campaign: Campaign, *, note: str | None, actor: Actor, now: datetime
) -> None:
    """The applicant found earlier contact in Outlook: a ``prior_contact_found`` review.

    The review is resolved as in §9.6 (acknowledge and proceed, or close); there are no
    mailbox IDs to acknowledge.
    """
    if not Mode(campaign.mode).is_real:
        raise HandoffError("prior contact is reported only for real-mode campaigns")
    campaign_svc.open_pause(
        session,
        campaign,
        PauseReason.PRIOR_CONTACT_FOUND,
        actor=actor,
        now=now,
        source_ref=f"prior_contact:reported:{opaque_token()[:12]}",
        detail={
            "gmail_ids": [],
            "reported_by_applicant": True,
            "note": (note or "").strip()[:500],
        },
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _active_snapshot(session: Session, message: Message) -> ApprovalSnapshot:
    snapshot = (
        session.get(ApprovalSnapshot, message.current_snapshot_id)
        if message.current_snapshot_id
        else None
    )
    if snapshot is None:
        raise HandoffError("the message has no approval snapshot")
    return snapshot


def _handed_off(session: Session, attempt_uid: str) -> SendAttempt:
    attempt = send_results.attempt_by_uid(session, attempt_uid)
    if attempt.channel != Channel.OUTLOOK_HANDOFF.value:
        raise send_results.ResolutionError("not an Outlook hand-off")
    if attempt.status != AttemptStatus.HANDED_OFF.value:
        raise send_results.ResolutionError("this hand-off is already resolved")
    return attempt


def _attachment_bytes(session: Session, content: SnapshotContent) -> dict[str, bytes]:
    blobs = {ref.sha256: session.get(Blob, ref.sha256) for ref in content.attachments}
    return {sha: blob.data for sha, blob in blobs.items() if blob is not None}


# --------------------------------------------------------------------------- #
# Suggested send time and the practice "send to myself" (owner decision 2026-10-27)
# --------------------------------------------------------------------------- #


def suggested_send_time(
    session: Session, message: Message, settings: AppSettings, now: datetime
) -> datetime | None:
    """The next moment inside the lab's working hours (PI local time), or now if open.

    Advice only when strict send rules are off: the applicant schedules the send in
    Outlook (Send later) for this time.
    """
    campaign = session.get(Campaign, message.campaign_id)
    pi = session.get(Pi, campaign.pi_id) if campaign else None
    institution = session.get(Institution, pi.institution_id) if pi else None
    if institution is None or not institution.iana_tz:
        return None
    try:
        window = next_window(now, load_zone(institution.iana_tz), settings.window_policy())
    except SchedulingError:
        return None
    return max(now, window.start)


def practice_package(session: Session, message: Message, settings: AppSettings) -> HandoffPackage:
    """Practice only: the approved email as an Outlook hand-off addressed to the
    applicant's own Outlook address, so the link, file and copy text can be tried for
    real without anything reaching a PI. Records nothing."""
    if Mode(message.mode) is not Mode.SANDBOX:
        raise HandoffError("practice hand-off is for practice mode only")
    if message.state != MessageState.APPROVED_QUEUED.value:
        raise HandoffError("approve the email first")
    mailbox = approvals._active_mailbox(session)
    if mailbox is None or not mailbox.email:
        raise HandoffError("add your Outlook address in Settings first")
    snapshot = _active_snapshot(session, message)
    content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
    own = mailbox.email.lower()
    mine = dataclasses.replace(content, to=own, intended_recipient=own, sender=own)
    return build_package(mine, _attachment_bytes(session, content), settings.email_signature)
