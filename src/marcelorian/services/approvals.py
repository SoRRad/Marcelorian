"""Drafts, approval snapshots, revoke/cancel, and dependency invalidation.

Spec §3, §9.1; decisions B2-B5, B18-B20.

* Approval binds one immutable snapshot (canonical JSON + SHA-256) and records
  the dependencies it actually uses with the versions seen (B18).
* Approving Email 1 atomically reserves capacity, or validates reservations the
  campaign already owns (B4). Follow-ups never reserve.
* Approval prerequisites: verified time zone, send-ready exact recipient,
  reviewed salutation (B20), plus do-not-contact, duplicate-outreach and mode
  checks. The gateway rechecks the mutable ones at dispatch.
* Revoke keeps the slot (B5). Cancel-before-first-send returns the campaign to
  ``candidate`` only when nothing was or might have been submitted (B3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import (
    ApprovalDependency,
    ApprovalSnapshot,
    Blob,
    Campaign,
    CvVersion,
    Institution,
    MailboxAccount,
    Message,
    Pi,
    PiEmail,
    ProfileFact,
    RecipientAllowlist,
    SnapshotAttachment,
)
from marcelorian.domain.enums import (
    HOLDING_STATES,
    Actor,
    CampaignState,
    DependencyKind,
    FactStatus,
    MessageState,
    Mode,
    SnapshotState,
)
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.domain.recipients import RecipientError, validate_recipient
from marcelorian.domain.snapshot import (
    AttachmentRef,
    SnapshotContent,
    SnapshotError,
    TestOverride,
    canonical_json,
    sha256_hex,
    validate_content,
)
from marcelorian.domain.timing import (
    SchedulingError,
    Window,
    eligible_window,
    in_window,
    load_zone,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import dnc, recipients, reservations, runtime, scheduling
from marcelorian.services.audit import audit
from marcelorian.services.reservations import CapacityResult
from marcelorian.transport.sandbox import SANDBOX_SENDER


class ApprovalError(RuntimeError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("approval refused: " + ", ".join(reasons))
        self.reasons = reasons


class CannotRecall(RuntimeError):
    """The message was already claimed for submission; it may have been submitted."""


class CancelRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class DraftContent:
    subject: str
    body: str
    cv_version_ids: tuple[int, ...] = ()
    fact_ids: tuple[int, ...] = ()
    test_override: TestOverride | None = None


@dataclass(frozen=True)
class ApprovalOutcome:
    message_id: int
    snapshot_id: int
    snapshot_sha256: str
    button_label: str
    window: Window
    expected_dispatch_at: datetime
    capacity: CapacityResult | None


# --------------------------------------------------------------------------- #
# Campaigns and drafts
# --------------------------------------------------------------------------- #


def create_campaign(
    session: Session,
    *,
    applicant_id: int,
    pi_id: int,
    mode: Mode,
    actor: Actor,
    now: datetime,
    test_recipient_id: int | None = None,
) -> Campaign:
    pi = session.get(Pi, pi_id)
    if pi is None or pi.merged_into_id is not None:
        raise ApprovalError(["pi_not_found_or_merged"])
    if mode is Mode.LIVE and pi.is_synthetic:
        raise ApprovalError(["synthetic_pi_in_live"])
    if mode is Mode.TRANSPORT_TEST:
        entry = session.get(RecipientAllowlist, test_recipient_id) if test_recipient_id else None
        if entry is None or not entry.active:
            raise ApprovalError(["test_recipient_not_allowlisted"])
        if entry.address in recipients.real_pi_addresses(session):
            raise ApprovalError(["test_recipient_is_real_pi"])
    elif test_recipient_id is not None:
        raise ApprovalError(["test_recipient_outside_transport_test"])
    campaign = Campaign(
        applicant_id=applicant_id,
        pi_id=pi_id,
        mode=mode.value,
        state=CampaignState.CANDIDATE.value,
        sequence_stopped=False,
        test_recipient_id=test_recipient_id,
        created_at=now,
        updated_at=now,
    )
    session.add(campaign)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="created",
        to_state=campaign.state,
        mode=mode.value,
    )
    return campaign


def _message(session: Session, campaign: Campaign, step: int) -> Message | None:
    return session.scalar(
        select(Message).where(
            Message.campaign_id == campaign.id, Message.step == step, Message.mode == campaign.mode
        )
    )


def _sent_messages(session: Session, campaign: Campaign) -> list[Message]:
    return list(
        session.scalars(
            select(Message)
            .where(Message.campaign_id == campaign.id, Message.state == MessageState.SENT.value)
            .order_by(Message.step)
        )
    )


def save_draft(
    session: Session,
    campaign: Campaign,
    step: int,
    content: DraftContent,
    *,
    actor: Actor,
    now: datetime,
) -> Message:
    """Create or revise the logical message for ``step``. Revisions never create new messages."""
    state = CampaignState(campaign.state)
    if step == 1:
        if state not in (CampaignState.CANDIDATE, *HOLDING_STATES):
            raise ApprovalError(["campaign_not_draftable"])
    else:
        previous = _message(session, campaign, step - 1)
        if previous is None or previous.state != MessageState.SENT.value:
            raise ApprovalError(["previous_step_not_sent"])
        if state not in HOLDING_STATES:
            raise ApprovalError(["campaign_not_draftable"])
        if _message(session, campaign, step) is None:
            # Only the next step is ever generated, and only once it is due (§9.3, D1).
            if campaign.next_step != step or campaign.next_due_at is None:
                raise ApprovalError(["step_not_next"])
            if now < campaign.next_due_at:
                raise ApprovalError(["step_not_due_yet"])
    for cv_id in content.cv_version_ids:
        cv = session.get(CvVersion, cv_id)
        if cv is None or cv.applicant_id != campaign.applicant_id:
            raise ApprovalError(["unknown_cv_version"])
    for fact_id in content.fact_ids:
        fact = session.get(ProfileFact, fact_id)
        if fact is None or fact.applicant_id != campaign.applicant_id:
            raise ApprovalError(["unknown_fact"])
    if content.test_override is not None and Mode(campaign.mode) is not Mode.TRANSPORT_TEST:
        raise ApprovalError(["override_outside_transport_test"])

    message = _message(session, campaign, step)
    if message is None:
        message = Message(
            campaign_id=campaign.id,
            mode=campaign.mode,
            step=step,
            state=MessageState.DRAFT.value,
            created_at=now,
            updated_at=now,
            due_at=campaign.next_due_at if step > 1 else None,
        )
        session.add(message)
        session.flush()
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="message",
            entity_id=message.id,
            action="created",
            to_state=message.state,
            mode=message.mode,
        )
    else:
        current = MessageState(message.state)
        if current in (MessageState.SENDING, MessageState.SENT, MessageState.UNCERTAIN):
            raise ApprovalError(["message_not_editable"])
        if current is MessageState.CANCELLED and step == 1 and state is not CampaignState.CANDIDATE:
            raise ApprovalError(["campaign_not_draftable"])
        if current is MessageState.APPROVED_QUEUED:
            _end_snapshot(session, message, SnapshotState.INVALIDATED, now, "content_changed")
    if step > 1 and campaign.sequence_stopped:
        campaign.sequence_stopped = False  # deliberate re-draft of the next unsent step (B5)
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="campaign",
            entity_id=campaign.id,
            action="sequence_resumed_by_redraft",
            detail={"step": step},
            mode=campaign.mode,
        )
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    message.draft_subject = content.subject
    message.draft_body = content.body
    message.draft_attachments_json = json.dumps(list(content.cv_version_ids))
    # The versions the draft text was written from (B18): approval refuses if they moved.
    message.draft_dependencies_json = json.dumps(
        {
            "facts": [
                [fact_id, _fact_version(session, fact_id)] for fact_id in content.fact_ids
            ],
            "pi_version": pi.version,
        }
    )
    message.cancel_requested_at = None  # a deliberate revision supersedes an old cancel request
    message.draft_test_override_json = (
        json.dumps(content.test_override.to_dict()) if content.test_override else None
    )
    if MessageState(message.state) is not MessageState.AWAITING_APPROVAL:
        campaign_svc.set_message_state(
            session,
            message,
            MessageState.AWAITING_APPROVAL,
            actor=actor,
            now=now,
            reason="draft saved",
        )
    message.updated_at = now
    return message


def _fact_version(session: Session, fact_id: int) -> int:
    fact = session.get(ProfileFact, fact_id)
    assert fact is not None
    return fact.version


def draft_dependencies(message: Message) -> tuple[list[tuple[int, int]], int | None]:
    """(fact id, version drafted from) pairs and the PI version the draft text used."""
    data = json.loads(message.draft_dependencies_json or "{}")
    if isinstance(data, list):  # no recorded versions: nothing can be verified
        return [(int(f), -1) for f in data], -1
    facts = [(int(f), int(v)) for f, v in data.get("facts", [])]
    pi_version = data.get("pi_version")
    return facts, int(pi_version) if pi_version is not None else None


def _end_snapshot(
    session: Session, message: Message, state: SnapshotState, now: datetime, reason: str
) -> None:
    if message.current_snapshot_id is None:
        return
    snapshot = session.get(ApprovalSnapshot, message.current_snapshot_id)
    if snapshot is not None and snapshot.state == SnapshotState.ACTIVE.value:
        snapshot.state, snapshot.ended_at, snapshot.end_reason = state.value, now, reason
        audit(
            session,
            at=now,
            actor=Actor.SYSTEM,
            entity_type="approval_snapshot",
            entity_id=snapshot.id,
            action=state.value,
            reason=reason,
            mode=snapshot.mode,
        )


# --------------------------------------------------------------------------- #
# Approval
# --------------------------------------------------------------------------- #


def duplicate_outreach(session: Session, campaign: Campaign, address: str | None) -> bool:
    """Another campaign in this mode already reached (or holds outreach for) this identity."""
    cluster = dnc.merge_cluster(session, campaign.pi_id)
    active_states = [
        CampaignState.OUTREACH_PENDING.value,
        CampaignState.PAUSED_REVIEW.value,
        CampaignState.CONVERSATION.value,
        CampaignState.SEQUENCE_COMPLETE.value,
    ]
    others = session.scalars(
        select(Campaign).where(
            Campaign.mode == campaign.mode, Campaign.id != campaign.id, Campaign.pi_id.in_(cluster)
        )
    ).all()
    for other in others:
        if other.state in active_states or _sent_messages(session, other):
            return True
    if address and Mode(campaign.mode) is not Mode.TRANSPORT_TEST:
        same_address = session.scalars(
            select(Campaign)
            .join(PiEmail, PiEmail.pi_id == Campaign.pi_id)
            .where(
                Campaign.mode == campaign.mode,
                Campaign.id != campaign.id,
                func.lower(PiEmail.address) == address.lower(),
            )
        ).all()
        for other in same_address:  # whatever its state, once it reached the address
            if other.state in active_states or _sent_messages(session, other):
                return True
    return False


def approval_blockers(
    session: Session, campaign: Campaign, message: Message, now: datetime
) -> list[str]:
    reasons: list[str] = []
    mode = Mode(campaign.mode)
    view = runtime.get_runtime(session, now)
    if view.active_mode is not mode:
        reasons.append("mode_not_active")
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    institution = session.get(Institution, pi.institution_id)
    assert institution is not None
    if not institution.tz_verified:
        reasons.append("timezone_unverified")
    else:
        try:
            load_zone(institution.iana_tz)
        except SchedulingError:
            reasons.append("timezone_invalid")
    ready = recipients.send_ready_email(session, pi.id)
    if ready is None:
        reasons.append("recipient_not_send_ready")
    if not pi.salutation_reviewed or not (pi.salutation and pi.salutation.strip()):
        reasons.append("salutation_not_reviewed")
    if mode is Mode.LIVE and pi.is_synthetic:
        reasons.append("synthetic_pi_in_live")
    if mode is Mode.TRANSPORT_TEST:
        entry = (
            session.get(RecipientAllowlist, campaign.test_recipient_id)
            if campaign.test_recipient_id
            else None
        )
        if entry is None or not entry.active:
            reasons.append("test_recipient_not_allowlisted")
        else:
            if entry.address in recipients.real_pi_addresses(session):
                reasons.append("test_recipient_is_real_pi")
            if recipients.other_active_test_campaign(session, entry.id, campaign.id):
                reasons.append("test_recipient_busy")
    extra: tuple[str, ...] = ()
    if mode is Mode.TRANSPORT_TEST and campaign.test_recipient_id:
        entry = session.get(RecipientAllowlist, campaign.test_recipient_id)
        extra = (entry.address,) if entry else ()
    if dnc.matches(session, pi.id, mode, extra):
        reasons.append("do_not_contact")
    state = CampaignState(campaign.state)
    if message.step == 1:
        if state not in (CampaignState.CANDIDATE, *HOLDING_STATES):
            reasons.append("campaign_not_sendable")
        if duplicate_outreach(session, campaign, ready.address if ready else None):
            reasons.append("duplicate_outreach")
    else:
        if state not in HOLDING_STATES:
            reasons.append("campaign_not_sendable")
        if campaign.sequence_stopped:
            reasons.append("sequence_stopped")
        if message.due_at is None:
            reasons.append("due_time_missing")
    facts, pi_version = draft_dependencies(message)
    for fact_id, drafted_version in facts:
        fact = session.get(ProfileFact, fact_id)
        if fact is None or fact.status != FactStatus.CONFIRMED.value:
            reasons.append("fact_not_confirmed")
            break
        if fact.version != drafted_version:
            reasons.append("dependency_changed_since_draft")
            break
    if pi_version is not None and pi_version != pi.version:
        reasons.append("dependency_changed_since_draft")
    if message.cancel_requested_at is not None:
        reasons.append("cancel_pending")
    if mode.is_real and _active_mailbox(session) is None:
        reasons.append("mailbox_not_connected")
    return reasons


def _active_mailbox(session: Session) -> MailboxAccount | None:
    return session.scalar(select(MailboxAccount).where(MailboxAccount.active.is_(True)))


def _attachment_refs(session: Session, cv_ids: list[int]) -> list[tuple[AttachmentRef, CvVersion]]:
    refs = []
    for cv_id in cv_ids:
        cv = session.get(CvVersion, cv_id)
        if cv is None:
            raise ApprovalError(["unknown_cv_version"])
        refs.append(
            (
                AttachmentRef(
                    filename=cv.filename,
                    content_type=cv.content_type,
                    sha256=cv.sha256,
                    size=cv.size_bytes,
                    cv_version_id=cv.id,
                ),
                cv,
            )
        )
    return refs


def approve(
    session: Session,
    message: Message,
    *,
    actor: Actor,
    now: datetime,
    settings: AppSettings,
    specialty_display_name: str,
    confirm_lab_conversation: bool = False,
) -> ApprovalOutcome:
    current = MessageState(message.state)
    if current not in (
        MessageState.AWAITING_APPROVAL,
        MessageState.NEEDS_REAPPROVAL,
        MessageState.FAILED_DEFINITE,
    ):
        raise ApprovalError(["message_not_approvable"])
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    reasons = approval_blockers(session, campaign, message, now)
    if reasons:
        raise ApprovalError(reasons)
    mode = Mode(campaign.mode)
    pi = session.get(Pi, campaign.pi_id)
    institution = session.get(Institution, pi.institution_id) if pi else None
    ready = recipients.send_ready_email(session, campaign.pi_id)
    assert pi is not None and institution is not None and ready is not None

    mailbox = _active_mailbox(session) if mode.is_real else None
    sender = mailbox.email if mailbox is not None else SANDBOX_SENDER
    allow_entry = (
        session.get(RecipientAllowlist, campaign.test_recipient_id)
        if mode is Mode.TRANSPORT_TEST and campaign.test_recipient_id
        else None
    )
    to_address = allow_entry.address if allow_entry is not None else ready.address.lower()
    override = TestOverride.from_dict(
        json.loads(message.draft_test_override_json) if message.draft_test_override_json else None
    )
    sent = _sent_messages(session, campaign)
    thread_id = sent[0].gmail_thread_id if sent and message.step > 1 else None
    references = (
        tuple(m.rfc_message_id for m in sent if m.rfc_message_id) if message.step > 1 else ()
    )
    attachments = _attachment_refs(session, json.loads(message.draft_attachments_json or "[]"))
    version = (
        int(
            session.scalar(
                select(func.coalesce(func.max(ApprovalSnapshot.version), 0)).where(
                    ApprovalSnapshot.message_id == message.id
                )
            )
            or 0
        )
        + 1
    )
    content = SnapshotContent(
        campaign_id=campaign.id,
        message_id=message.id,
        step=message.step,
        mode=mode,
        version=version,
        sender=sender,
        to=to_address,
        intended_recipient=ready.address.lower(),
        pi_id=pi.id,
        pi_display_name=pi.full_name,
        pi_is_synthetic=pi.is_synthetic,
        subject=message.draft_subject or "",
        body=message.draft_body or "",
        attachments=tuple(ref for ref, _ in attachments),
        thread_id=thread_id,
        in_reply_to=references[-1] if references else None,
        references=references,
        pi_timezone=str(institution.iana_tz),
        test_override=override,
        approved_at=now,
    )
    try:
        validate_content(content, specialty_display_name)
    except SnapshotError as exc:
        raise ApprovalError([f"snapshot_invalid: {exc}"]) from exc
    try:
        validate_recipient(
            recipients.build_context(
                session,
                campaign=campaign,
                pi=pi,
                transport_kind="gmail" if mode.is_real else "sandbox",
                snapshot_to=content.to,
                snapshot_intended=content.intended_recipient,
                has_test_override=override is not None,
            )
        )
    except RecipientError as exc:
        raise ApprovalError([f"recipient_invalid: {exc}"]) from exc

    tz = load_zone(institution.iana_tz)
    policy = settings.window_policy()
    if override is not None and override.window_always_open:
        policy = replace(policy, always_open=True)
    due_at = now if message.step == 1 else message.due_at
    assert due_at is not None
    window = eligible_window(due_at, now, tz, policy)

    capacity: CapacityResult | None = None
    if message.step == 1:
        capacity = reservations.reserve_or_validate_initial(
            session,
            campaign,
            settings,
            now,
            actor=actor,
            confirm_lab_conversation=confirm_lab_conversation,
        )

    canonical = canonical_json(content)
    digest = sha256_hex(canonical)
    if message.current_snapshot_id is not None:
        _end_snapshot(session, message, SnapshotState.SUPERSEDED, now, "re-approved")
    snapshot = ApprovalSnapshot(
        message_id=message.id,
        version=version,
        state=SnapshotState.ACTIVE.value,
        canonical_json=canonical,
        sha256=digest,
        mode=mode.value,
        sender=sender,
        to_address=content.to,
        intended_recipient=content.intended_recipient,
        subject=content.subject,
        has_test_override=override is not None,
        approved_at=now,
        approved_by=str(actor),
    )
    session.add(snapshot)
    session.flush()
    for position, (ref, _cv) in enumerate(attachments):
        if session.get(Blob, ref.sha256) is None:
            raise ApprovalError(["attachment_bytes_missing"])
        session.add(
            SnapshotAttachment(
                snapshot_id=snapshot.id,
                position=position,
                filename=ref.filename,
                content_type=ref.content_type,
                sha256=ref.sha256,
                size=ref.size,
                cv_version_id=ref.cv_version_id,
            )
        )
    deps: list[tuple[DependencyKind, int, int]] = [
        (DependencyKind.PI, pi.id, pi.version),
        (DependencyKind.PI_EMAIL, ready.id, ready.version),
        (DependencyKind.INSTITUTION, institution.id, institution.version),
    ]
    deps += [(DependencyKind.CV_VERSION, cv.id, 1) for _ref, cv in attachments]
    for fact_id, drafted_version in draft_dependencies(message)[0]:
        deps.append((DependencyKind.PROFILE_FACT, fact_id, drafted_version))
    if mailbox is not None:
        deps.append((DependencyKind.MAILBOX_ACCOUNT, mailbox.id, mailbox.version))
    if allow_entry is not None:
        deps.append((DependencyKind.ALLOWLIST_ENTRY, allow_entry.id, allow_entry.version))
    for kind, ref_id, ref_version in deps:
        session.add(
            ApprovalDependency(
                snapshot_id=snapshot.id, kind=kind.value, ref_id=ref_id, ref_version=ref_version
            )
        )

    if message.step == 1 and CampaignState(campaign.state) is CampaignState.CANDIDATE:
        campaign_svc.set_campaign_state(
            session,
            campaign,
            CampaignState.OUTREACH_PENDING,
            actor=actor,
            now=now,
            reason="Email 1 approved",
        )
    # A fresh approval of this message resolves its own missed-window reason, whatever
    # route (direct re-approval, edit, cancel/stop and re-draft) led back here (B7).
    scheduling.resolve_missed_window_on_reapproval(session, message, actor=actor, now=now)
    message.current_snapshot_id = snapshot.id
    message.due_at = due_at
    message.pi_timezone = institution.iana_tz
    message.eligible_window_start, message.eligible_window_end = window.start, window.end
    message.cap_block_reason = None
    if capacity is not None and not capacity.ok:
        message.cap_block_reason = "over_capacity"
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.APPROVED_QUEUED,
        actor=actor,
        now=now,
        reason=f"approved v{version}",
    )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="approval_snapshot",
        entity_id=snapshot.id,
        action="approved",
        detail={"sha256": digest, "version": version, "override": override is not None},
        mode=mode.value,
    )
    in_window_now = in_window(now, tz, policy) and now >= due_at
    return ApprovalOutcome(
        message_id=message.id,
        snapshot_id=snapshot.id,
        snapshot_sha256=digest,
        button_label="Approve and send" if in_window_now else "Approve and queue",
        window=window,
        expected_dispatch_at=max(now, window.start, due_at),
        capacity=capacity,
    )


# --------------------------------------------------------------------------- #
# Revoke / cancel / stop
# --------------------------------------------------------------------------- #


def revoke(session: Session, message: Message, *, actor: Actor, now: datetime) -> None:
    """Approved, unclaimed message -> awaiting approval. Reservations and slot are kept (B5)."""
    state = MessageState(message.state)
    if state is MessageState.SENDING:
        raise CannotRecall("this message was already claimed for submission and cannot be recalled")
    if state is not MessageState.APPROVED_QUEUED:
        raise ApprovalError(["message_not_revocable"])
    _end_snapshot(session, message, SnapshotState.REVOKED, now, "revoked")
    campaign_svc.set_message_state(
        session,
        message,
        MessageState.AWAITING_APPROVAL,
        actor=actor,
        now=now,
        reason="approval revoked",
    )


def request_cancel_claimed(
    session: Session, message: Message, *, actor: Actor, now: datetime
) -> None:
    """Cancel pressed on a claimed message: not a recall (B5). Future steps stop."""
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    message.cancel_requested_at = message.cancel_requested_at or now
    campaign.sequence_stopped = True
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="message",
        entity_id=message.id,
        action="cancel_requested_not_recalled",
        reason="submission may already have started; the attempt will be reconciled",
        mode=message.mode,
    )


def cancel_initial(session: Session, campaign: Campaign, *, actor: Actor, now: datetime) -> str:
    """Cancel an unsent Email 1 (B3). Returns 'cancelled' or 'not_recalled'."""
    message = _message(session, campaign, 1)
    if message is None:
        raise CancelRefused("there is no Email 1 to cancel")
    state = MessageState(message.state)
    if state is MessageState.SENDING:
        request_cancel_claimed(session, message, actor=actor, now=now)
        return "not_recalled"
    if state is MessageState.UNCERTAIN or campaign_svc.has_unresolved_attempt(session, campaign.id):
        raise CancelRefused(
            "an uncertain submission must be resolved first; cancelling never clears it"
        )
    if _sent_messages(session, campaign):
        raise CancelRefused("Email 1 was already sent; close the campaign instead")
    if state is MessageState.CANCELLED:
        raise CancelRefused("Email 1 is already cancelled")
    if CampaignState(campaign.state) not in (CampaignState.CANDIDATE, *HOLDING_STATES):
        raise CancelRefused("the campaign is already stopped; nothing remains to cancel")
    campaign_svc.cancel_before_first_send(session, campaign, message, actor=actor, now=now)
    return "cancelled"


def stop_sequence(session: Session, campaign: Campaign, *, actor: Actor, now: datetime) -> None:
    for message in session.scalars(
        select(Message).where(Message.campaign_id == campaign.id, Message.step >= 2)
    ):
        if MessageState(message.state) is MessageState.SENDING:
            request_cancel_claimed(session, message, actor=actor, now=now)
    campaign_svc.stop_followup_sequence(session, campaign, actor=actor, now=now)


# --------------------------------------------------------------------------- #
# Dependency changes (B18, B19)
# --------------------------------------------------------------------------- #


def invalidate_dependents(
    session: Session,
    kind: DependencyKind,
    ref_id: int,
    *,
    actor: Actor,
    now: datetime,
    reason: str,
) -> list[int]:
    rows = session.execute(
        select(ApprovalSnapshot, Message)
        .join(ApprovalDependency, ApprovalDependency.snapshot_id == ApprovalSnapshot.id)
        .join(Message, Message.id == ApprovalSnapshot.message_id)
        .where(
            ApprovalDependency.kind == kind.value,
            ApprovalDependency.ref_id == ref_id,
            ApprovalSnapshot.state == SnapshotState.ACTIVE.value,
        )
    ).all()
    affected: list[int] = []
    for snapshot, message in rows:
        if MessageState(message.state) is MessageState.APPROVED_QUEUED:
            snapshot.state, snapshot.ended_at, snapshot.end_reason = (
                SnapshotState.INVALIDATED.value,
                now,
                reason,
            )
            campaign_svc.set_message_state(
                session, message, MessageState.NEEDS_REAPPROVAL, actor=actor, now=now, reason=reason
            )
            affected.append(message.id)
    return affected


def dependencies_current(session: Session, snapshot_id: int) -> bool:
    """Final check at the claim: every recorded dependency still has the approved version."""
    models = {
        DependencyKind.PI: Pi,
        DependencyKind.PI_EMAIL: PiEmail,
        DependencyKind.INSTITUTION: Institution,
        DependencyKind.PROFILE_FACT: ProfileFact,
        DependencyKind.MAILBOX_ACCOUNT: MailboxAccount,
        DependencyKind.ALLOWLIST_ENTRY: RecipientAllowlist,
    }
    for dep in session.scalars(
        select(ApprovalDependency).where(ApprovalDependency.snapshot_id == snapshot_id)
    ):
        kind = DependencyKind(dep.kind)
        if kind is DependencyKind.CV_VERSION:
            if session.get(CvVersion, dep.ref_id) is None:
                return False
            continue
        row: Any = session.get(models[kind], dep.ref_id)
        if row is None or row.version != dep.ref_version:
            return False
        if kind is DependencyKind.MAILBOX_ACCOUNT and not row.active:
            return False
        if kind is DependencyKind.ALLOWLIST_ENTRY and not row.active:
            return False
    return True


def update_fact(
    session: Session, fact: ProfileFact, value: str, *, confirm: bool, actor: Actor, now: datetime
) -> list[int]:
    """Edit a fact; bumps its version and invalidates only approvals that use it (B18)."""
    if value == fact.value and (not confirm or fact.status == FactStatus.CONFIRMED.value):
        return []
    fact.value = value
    fact.version += 1
    fact.updated_at = now
    if confirm:
        fact.status, fact.confirmed_at = FactStatus.CONFIRMED.value, now
    else:
        fact.status = FactStatus.PROPOSED.value
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="profile_fact",
        entity_id=fact.id,
        action="updated",
        detail={"version": fact.version, "status": fact.status},
    )
    return invalidate_dependents(
        session,
        DependencyKind.PROFILE_FACT,
        fact.id,
        actor=actor,
        now=now,
        reason="profile fact changed",
    )


def set_salutation(
    session: Session, pi: Pi, salutation: str, *, actor: Actor, now: datetime
) -> list[int]:
    changed = salutation != pi.salutation
    pi.salutation = salutation
    pi.salutation_reviewed, pi.salutation_reviewed_at = True, now
    if not changed:
        return []
    pi.version += 1
    audit(session, at=now, actor=actor, entity_type="pi", entity_id=pi.id, action="salutation")
    return invalidate_dependents(
        session, DependencyKind.PI, pi.id, actor=actor, now=now, reason="salutation changed"
    )


def set_institution_timezone(
    session: Session,
    institution: Institution,
    tz_name: str | None,
    *,
    verified: bool,
    actor: Actor,
    now: datetime,
) -> list[int]:
    if tz_name is not None:
        load_zone(tz_name)
    institution.iana_tz = tz_name
    institution.tz_verified = verified and tz_name is not None
    institution.tz_verified_by = str(actor) if institution.tz_verified else None
    institution.tz_verified_at = now if institution.tz_verified else None
    institution.version += 1
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="institution",
        entity_id=institution.id,
        action="timezone",
        detail={"verified": institution.tz_verified},
    )
    return invalidate_dependents(
        session,
        DependencyKind.INSTITUTION,
        institution.id,
        actor=actor,
        now=now,
        reason="time zone changed",
    )


def update_pi_email(
    session: Session,
    email: PiEmail,
    *,
    actor: Actor,
    now: datetime,
    invalid: bool | None = None,
    confidence: str | None = None,
    ownership_verified: bool | None = None,
) -> list[int]:
    if invalid is not None:
        email.invalid = invalid
    if confidence is not None:
        email.confidence = confidence
    if ownership_verified is not None:
        email.ownership_verified = ownership_verified
    email.version += 1
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="pi_email",
        entity_id=email.id,
        action="updated",
        detail={"invalid": email.invalid, "confidence": email.confidence},
    )
    return invalidate_dependents(
        session,
        DependencyKind.PI_EMAIL,
        email.id,
        actor=actor,
        now=now,
        reason="recipient address changed",
    )


def upload_cv(
    session: Session,
    *,
    applicant_id: int,
    filename: str,
    content_type: str,
    data: bytes,
    actor: Actor,
    now: datetime,
) -> CvVersion:
    """A new CV version never changes or invalidates an approved frozen attachment (B19)."""
    if not data:
        raise ValueError("empty CV file")
    digest = sha256_hex(data)
    if session.get(Blob, digest) is None:
        session.add(Blob(sha256=digest, data=data, size=len(data), created_at=now))
    cv = CvVersion(
        applicant_id=applicant_id,
        filename=filename,
        content_type=content_type,
        sha256=digest,
        size_bytes=len(data),
        uploaded_at=now,
    )
    session.add(cv)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="cv_version",
        entity_id=cv.id,
        action="uploaded",
        detail={"sha256": digest, "size": len(data)},
    )
    return cv


def connect_mailbox(
    session: Session, email: str, scopes: frozenset[str], *, actor: Actor, now: datetime
) -> MailboxAccount:
    """Record the verified connected account. A different account invalidates its approvals.

    A verified (re)connection with the required scopes clears the global Gmail auth
    block (spec §15); a connection missing a scope keeps real dispatch halted.
    """
    normalized = email.strip().lower()
    if scopes >= REQUIRED_SCOPES:
        runtime.set_auth_block(session, False, None, now=now)
    else:
        runtime.set_auth_block(session, True, "required_scopes_missing", now=now)
    current = _active_mailbox(session)
    if current is not None and current.email == normalized:
        current.scopes_json = json.dumps(sorted(scopes))
        current.verified_at = now
        return current
    if current is not None:
        current.active = False
        current.version += 1
        invalidate_dependents(
            session,
            DependencyKind.MAILBOX_ACCOUNT,
            current.id,
            actor=actor,
            now=now,
            reason="connected Gmail account changed",
        )
    account = MailboxAccount(
        email=normalized, scopes_json=json.dumps(sorted(scopes)), active=True, verified_at=now
    )
    session.add(account)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="mailbox_account",
        entity_id=account.id,
        action="connected",
    )
    return account
