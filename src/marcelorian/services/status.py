"""Outreach status for the dashboard: where every application stands (read-only).

Counts the active mode's campaigns by where they stand, the applicant's response
rate, and the places currently in use under the same-place rule. Nothing here
changes state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, InboundEvent, Institution, LabGroup, Message, Pi
from marcelorian.domain.enums import CampaignState, CloseOutcome, InboundClassification, Mode
from marcelorian.services import reservations

#: Dashboard rows, in display order.
STATUS_LABELS: dict[str, str] = {
    "waiting": "Sent, waiting for a reply",
    "needs_attention": "Needs your attention",
    "replied": "Replied, conversation open",
    "rejected": "Rejected (no opening)",
    "no_reply": "No reply after all follow-ups",
    "approved_not_sent": "Approved, not sent yet",
    "not_started": "Not started",
    "closed_other": "Closed for another reason",
}


@dataclass(frozen=True)
class PlaceInUse:
    place: str
    pi_name: str
    state: str
    free_from: datetime | None  # set for a finished sequence with no reply


@dataclass
class OutreachStatus:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STATUS_LABELS, 0))
    contacted: int = 0
    responded: int = 0
    places: list[PlaceInUse] = field(default_factory=list)

    @property
    def pending(self) -> int:
        return self.counts["waiting"] + self.counts["needs_attention"]

    @property
    def response_rate(self) -> int | None:
        """Percent of contacted applications that got a reply, or None before any."""
        if not self.contacted:
            return None
        return round(100 * self.responded / self.contacted)


def outreach_status(session: Session, mode: Mode, scope: str, now: datetime) -> OutreachStatus:
    status = OutreachStatus()
    sent = set(
        session.scalars(
            select(Message.campaign_id).where(
                Message.mode == mode.value, Message.sent_at.is_not(None)
            )
        )
    )
    replied = set(
        session.scalars(
            select(InboundEvent.campaign_id).where(
                InboundEvent.mode == mode.value,
                InboundEvent.classification == InboundClassification.GENUINE_REPLY.value,
            )
        )
    )
    rows = session.execute(
        select(Campaign, Pi).join(Pi, Pi.id == Campaign.pi_id).where(Campaign.mode == mode.value)
    ).all()
    for campaign, pi in rows:
        key = _category(campaign, campaign.id in sent)
        status.counts[key] += 1
        if campaign.id in sent:
            status.contacted += 1
            if campaign.id in replied or key in {"replied", "rejected"}:
                status.responded += 1
        if reservations.occupies_place(session, campaign, now):
            free_from = (
                reservations.place_free_from(session, campaign)
                if campaign.state == CampaignState.SEQUENCE_COMPLETE.value
                else None
            )
            status.places.append(
                PlaceInUse(_place_label(session, pi, scope), pi.full_name, key, free_from)
            )
    status.places.sort(key=lambda p: (p.place.casefold(), p.pi_name))
    return status


def category(campaign: Campaign, any_sent: bool) -> str:
    """The dashboard bucket for one campaign (also used by the exports)."""
    return _category(campaign, any_sent)


def _category(campaign: Campaign, any_sent: bool) -> str:
    state = CampaignState(campaign.state)
    if state is CampaignState.CANDIDATE:
        return "not_started"
    if state is CampaignState.OUTREACH_PENDING:
        return "waiting" if any_sent else "approved_not_sent"
    if state is CampaignState.PAUSED_REVIEW:
        return "needs_attention"
    if state is CampaignState.CONVERSATION:
        return "replied"
    if state is CampaignState.SEQUENCE_COMPLETE:
        return "no_reply"
    if campaign.close_outcome == CloseOutcome.NO_OPENING.value:
        return "rejected"
    return "closed_other"


def _place_label(session: Session, pi: Pi, scope: str) -> str:
    institution = session.get(Institution, pi.institution_id)
    name = institution.name if institution else "Unknown institution"
    if scope == "department":
        return f"{name} — {pi.department}" if pi.department else f"{name} (whole institution)"
    if scope == "lab":
        lab = session.get(LabGroup, pi.lab_group_id)
        return f"{name} — {lab.name}" if lab else name
    return name
