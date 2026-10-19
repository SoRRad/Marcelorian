"""Replies, closing, tasks and review resolution (spec §10; Outlook-only pilot U3).

In real modes the applicant reads replies in Outlook and records them here
(``handoff.report_reply``: a genuine reply, so the sequence stops and the campaign is in
a conversation). In practice mode replies are simulated. A reply tag then decides what
follows: "No opening" closes the campaign (the place frees, N2); "Do not contact" closes
it and adds a do-not-contact entry (live scope for real modes, the practice mode's own
scope otherwise, C7); "Referral" and "Contact later" create manual tasks. The app never
reads or summarizes email content.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, CampaignPause, PiEmail, Task
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    CloseOutcome,
    InboundClassification,
    Mode,
    PauseReason,
)
from marcelorian.domain.identifiers import opaque_token
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import dnc, handoff
from marcelorian.services.audit import audit
from marcelorian.services.inbound import record_inbound
from marcelorian.services.recipients import send_ready_email

REPLY_TAGS: dict[str, str] = {
    "positive": "Positive",
    "requests_info": "Requests information",
    "no_opening": "No opening (rejection)",
    "referral": "Referral to a colleague",
    "contact_later": "Contact later",
    "do_not_contact": "Do not contact",
}
SIMULATIONS: dict[str, InboundClassification] = {
    "reply": InboundClassification.GENUINE_REPLY,
    "auto_reply": InboundClassification.AUTO_REPLY,
    "delivery_notice": InboundClassification.DELIVERY_NOTICE,
}
MAX_NOTE = 500


class ReplyError(ValueError):
    pass


def record_reply(
    session: Session,
    campaign: Campaign,
    tag: str,
    *,
    note: str = "",
    contact_later_on: date | None = None,
    now: datetime,
    actor: Actor = Actor.APPLICANT,
) -> None:
    if tag not in REPLY_TAGS:
        raise ReplyError("choose what the reply said")
    if tag == "contact_later" and contact_later_on is None:
        raise ReplyError("give the date to contact them again")
    state = CampaignState(campaign.state)
    if state is CampaignState.CANDIDATE:
        raise ReplyError("no email was sent in this campaign yet")
    if state is CampaignState.CLOSED:
        raise ReplyError("this campaign is closed")
    if state is not CampaignState.CONVERSATION:
        if Mode(campaign.mode).is_real:
            handoff.report_reply(session, campaign, note=note, actor=actor, now=now)
        else:
            simulate(session, campaign, "reply", now=now, actor=actor)
    campaign.reply_tag = tag
    clean = note.strip()[:MAX_NOTE]
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="reply_tagged",
        to_state=tag,
        reason=clean or None,
        mode=campaign.mode,
    )
    if tag == "no_opening":
        campaign_svc.close_campaign(
            session, campaign, CloseOutcome.NO_OPENING, actor=actor, now=now, reason=clean or None
        )
    elif tag == "do_not_contact":
        scope = Mode.LIVE if Mode(campaign.mode).is_real else Mode(campaign.mode)
        email = send_ready_email(session, campaign.pi_id)
        dnc.add_entry(
            session,
            scope_mode=scope,
            actor=actor,
            now=now,
            pi_id=campaign.pi_id,
            email=email.address if email else None,
            reason=clean or "Asked not to be contacted",
        )
        campaign_svc.close_campaign(
            session,
            campaign,
            CloseOutcome.DO_NOT_CONTACT,
            actor=actor,
            now=now,
            reason=clean or None,
        )
    elif tag in ("referral", "contact_later"):
        session.add(
            Task(
                campaign_id=campaign.id,
                kind=tag,
                due_on=contact_later_on.isoformat() if contact_later_on else None,
                details=clean,
                created_at=now,
            )
        )


def simulate(
    session: Session,
    campaign: Campaign,
    kind: str,
    *,
    now: datetime,
    actor: Actor = Actor.APPLICANT,
) -> None:
    """Practice mode only: a simulated PI reply, auto-reply or delivery notice (spec §12)."""
    if Mode(campaign.mode) is not Mode.SANDBOX:
        raise ReplyError("simulations exist only in practice mode")
    if kind not in SIMULATIONS:
        raise ReplyError("unknown simulation")
    if CampaignState(campaign.state) in (CampaignState.CANDIDATE, CampaignState.CLOSED):
        raise ReplyError("simulate after an email was sent")
    record_inbound(
        session,
        campaign,
        gmail_message_id=f"simulated:{opaque_token()}",
        thread_id=None,
        classification=SIMULATIONS[kind],
        from_address=None,
        received_at=now,
        actor=actor,
        now=now,
    )


def close(session: Session, campaign: Campaign, reason: str, *, now: datetime) -> None:
    if CampaignState(campaign.state) is CampaignState.CLOSED:
        raise ReplyError("already closed")
    if not reason.strip():
        raise ReplyError("give a short reason for closing")
    campaign_svc.close_campaign(
        session,
        campaign,
        CloseOutcome.APPLICANT_CLOSED,
        actor=Actor.APPLICANT,
        now=now,
        reason=reason.strip()[:MAX_NOTE],
    )


# --------------------------------------------------------------------------- #
# Reviews (paused campaigns)
# --------------------------------------------------------------------------- #

#: Choices per pause reason: action key -> label.
REVIEW_ACTIONS: dict[str, dict[str, str]] = {
    PauseReason.AUTO_REPLY.value: {"resume": "Resume the sequence", "close": "Close"},
    PauseReason.DELIVERY_NOTICE.value: {
        "bounce": "Bounce: the address is invalid (close)",
        "resume": "Only a delay: resume",
        "policy": "Policy rejection (close, address kept)",
    },
    PauseReason.PRIOR_CONTACT_FOUND.value: {
        "proceed": "Reviewed: proceed with this email",
        "close": "Close: do not contact again now",
    },
}
DEFAULT_ACTIONS = {"resume": "Resolved: continue", "close": "Close"}


def open_reviews(session: Session, campaign_id: int) -> list[CampaignPause]:
    return campaign_svc.open_reasons(session, campaign_id)


def actions_for(pause: CampaignPause) -> dict[str, str]:
    return REVIEW_ACTIONS.get(pause.reason, DEFAULT_ACTIONS)


def resolve_review(
    session: Session, pause_id: int, action: str, *, now: datetime, mailbox_email: str | None
) -> None:
    pause = session.get(CampaignPause, pause_id)
    if pause is None or pause.resolved_at is not None:
        raise ReplyError("this review is already resolved")
    if action not in actions_for(pause):
        raise ReplyError("choose one of the listed actions")
    campaign = session.get(Campaign, pause.campaign_id)
    assert campaign is not None
    actor = Actor.APPLICANT
    if action in ("resume", "proceed"):
        if pause.reason == PauseReason.PRIOR_CONTACT_FOUND.value:
            from marcelorian.services import prior_contact

            prior_contact.acknowledge_and_proceed(
                session, pause, mailbox_email=mailbox_email or "", actor=actor, now=now
            )
        else:
            campaign_svc.resolve_pause(session, pause, resolution=action, actor=actor, now=now)
        return
    campaign_svc.resolve_pause(session, pause, resolution=action, actor=actor, now=now)
    if action == "bounce":
        email = send_ready_email(session, campaign.pi_id)
        if email is not None:
            from marcelorian.services import approvals

            approvals.update_pi_email(session, email, actor=actor, now=now, invalid=True)
        outcome = CloseOutcome.BOUNCE_CONFIRMED
    else:
        outcome = CloseOutcome.APPLICANT_CLOSED
    if CampaignState(campaign.state) is not CampaignState.CLOSED:
        campaign_svc.close_campaign(
            session,
            campaign,
            outcome,
            actor=actor,
            now=now,
            reason=f"review: {pause.reason} -> {action}",
        )


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #


def open_tasks(session: Session) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.done_at.is_(None))
            .order_by(Task.due_on.is_(None), Task.due_on, Task.created_at)
        )
    )


def complete_task(session: Session, task_id: int, *, now: datetime) -> None:
    task = session.get(Task, task_id)
    if task is None:
        raise LookupError(task_id)
    task.done_at = now


def invalid_addresses(session: Session, pi_id: int) -> list[PiEmail]:
    return list(
        session.scalars(select(PiEmail).where(PiEmail.pi_id == pi_id, PiEmail.invalid.is_(True)))
    )
