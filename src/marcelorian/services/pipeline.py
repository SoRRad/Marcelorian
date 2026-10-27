"""The Home board: one card per person, each with a progress bar (owner request 2026-10-27).

A card moves to the next stage by itself as soon as the step is done; nothing here
changes data. Stages, left to right:

1. **Lead**: imported, not yet checked.
2. **Checked**: you confirmed the person is current; turn them into a contact next.
3. **Contact**: ready to draft Email 1.
4. **Drafted**: Email 1 is being written or waits for your approval.
5. **Ready to send**: approved; open it in Outlook (or practice-send it).
6. **Sent**: Email 1 went out; follow-ups come due by themselves.
7. **Replied**: the PI answered, or the sequence is closed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, Institution, Message, Pi, ResearchRecord
from marcelorian.domain.enums import CampaignState, MessageState, Mode
from marcelorian.domain.research import BLOCKING_VERIFICATIONS

STAGES: tuple[tuple[str, str], ...] = (
    ("lead", "Lead"),
    ("checked", "Checked"),
    ("contact", "Contact"),
    ("drafted", "Drafted"),
    ("ready", "Ready to send"),
    ("sent", "Sent"),
    ("replied", "Replied"),
)
STAGE_INDEX = {key: i for i, (key, _label) in enumerate(STAGES)}
#: Lead and Checked can hold hundreds of rows; the board shows the first few.
COLUMN_LIMIT = 12


@dataclass(frozen=True)
class Card:
    key: str
    name: str
    place: str
    stage: str
    url: str
    action: str
    detail: str = ""
    warn: bool = False

    @property
    def percent(self) -> int:
        return round(100 * (STAGE_INDEX[self.stage] + 1) / len(STAGES))

    @property
    def step(self) -> int:
        return STAGE_INDEX[self.stage] + 1


@dataclass
class Column:
    key: str
    label: str
    cards: list[Card] = field(default_factory=list)
    total: int = 0


@dataclass
class Board:
    columns: list[Column]

    @property
    def total(self) -> int:
        return sum(c.total for c in self.columns)


def _campaign_card(session: Session, pi: Pi, place: str, campaign: Campaign) -> Card | None:
    messages = {
        m.step: m
        for m in session.scalars(select(Message).where(Message.campaign_id == campaign.id))
    }
    state = CampaignState(campaign.state)
    first = messages.get(1)
    sent = [s for s, m in messages.items() if m.state == MessageState.SENT.value]
    pending = next(
        (
            m
            for _s, m in sorted(messages.items(), reverse=True)
            if m.state in (MessageState.APPROVED_QUEUED.value, MessageState.HANDED_OFF.value)
        ),
        None,
    )
    if state is CampaignState.CONVERSATION:
        return Card(
            f"pi-{pi.id}",
            pi.full_name,
            place,
            "replied",
            f"/campaigns/{campaign.id}",
            "Open the conversation",
            "The PI replied",
        )
    if state is CampaignState.CLOSED:
        outcome = (campaign.close_outcome or "closed").replace("_", " ")
        return Card(
            f"pi-{pi.id}",
            pi.full_name,
            place,
            "replied",
            f"/campaigns/{campaign.id}",
            "Open",
            f"Closed: {outcome}",
        )
    if pending is not None:
        label = "Open in Outlook" if Mode(campaign.mode).is_real else "Send (practice)"
        detail = (
            "Open in Outlook: mark Sent or Not sent"
            if pending.state == MessageState.HANDED_OFF.value
            else f"Email {pending.step} approved"
        )
        return Card(
            f"pi-{pi.id}",
            pi.full_name,
            place,
            "ready",
            f"/messages/{pending.id}#send",
            label,
            detail,
        )
    if sent:
        due = campaign.next_step if not campaign.sequence_stopped else None
        detail = f"Email {max(sent)} of 4 sent" + (
            f" · Email {due} due {campaign.next_due_at:%d %b}"
            if due and campaign.next_due_at
            else ""
        )
        return Card(
            f"pi-{pi.id}",
            pi.full_name,
            place,
            "sent",
            f"/campaigns/{campaign.id}",
            "Follow-ups and replies",
            detail,
        )
    if first is not None and first.state in (
        MessageState.DRAFT.value,
        MessageState.AWAITING_APPROVAL.value,
        MessageState.NEEDS_REAPPROVAL.value,
        MessageState.FAILED_DEFINITE.value,
    ):
        return Card(
            f"pi-{pi.id}",
            pi.full_name,
            place,
            "drafted",
            f"/campaigns/{campaign.id}/draft",
            "Review and approve",
            "Needs re-approval"
            if first.state != MessageState.AWAITING_APPROVAL.value
            else "Waiting for your approval",
            warn=first.state != MessageState.AWAITING_APPROVAL.value,
        )
    return None


def board(session: Session, mode: Mode, today: date, query: str = "") -> Board:
    columns = {key: Column(key, label) for key, label in STAGES}
    needle = query.strip().casefold()

    def add(card: Card) -> None:
        if needle and needle not in f"{card.name} {card.place}".casefold():
            return
        column = columns[card.stage]
        column.total += 1
        if len(column.cards) < COLUMN_LIMIT:
            column.cards.append(card)

    institutions = {i.id: i.name for i in session.scalars(select(Institution))}
    campaigns = {
        c.pi_id: c for c in session.scalars(select(Campaign).where(Campaign.mode == mode.value))
    }
    for pi in session.scalars(select(Pi).where(Pi.merged_into_id.is_(None)).order_by(Pi.id.desc())):
        place = institutions.get(pi.institution_id, "")
        campaign = campaigns.get(pi.id)
        card = _campaign_card(session, pi, place, campaign) if campaign else None
        add(
            card
            or Card(
                f"pi-{pi.id}",
                pi.full_name,
                place,
                "contact",
                f"/candidates/{pi.id}",
                "Draft Email 1",
            )
        )
    for record in session.scalars(
        select(ResearchRecord)
        .where(
            ResearchRecord.record_type == "contact",
            ResearchRecord.promoted_pi_id.is_(None),
            ResearchRecord.excluded_reason.is_(None),
        )
        .order_by(ResearchRecord.priority_band, ResearchRecord.name)
    ):
        if record.local_verification in BLOCKING_VERIFICATIONS:
            continue  # left, retired, wrong person: off the board
        checked = record.local_verification == "confirmed_current"
        add(
            Card(
                f"rec-{record.id}",
                record.name or record.external_record_id,
                record.institution,
                "checked" if checked else "lead",
                f"/research/{record.id}" + ("#promote" if checked else ""),
                "Make a contact" if checked else "Check",
                record.role or "",
            )
        )
    return Board([columns[key] for key, _label in STAGES])
