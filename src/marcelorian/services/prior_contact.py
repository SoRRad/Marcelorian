"""Prior-contact review for Email 1 (spec §9.6; decision B14).

* The app's own attempts are reconciled first: an unresolved attempt to the
  same address makes the check incomplete.
* Search results exclude only *this campaign's* own messages (by stored Gmail
  IDs) and the hits already acknowledged for this mailbox + campaign. Messages
  the app sent for any other campaign are real prior contact (re-discovery or
  a later merge must never permit a duplicate sequence), so anything else
  pauses the campaign as ``prior_contact_found``.
* Unresolved attempts are only those in the same mailbox: live and
  transport_test share the real mailbox; sandbox only sees sandbox (C9).
* Acknowledgment covers only the exact reviewed Gmail IDs. It never ignores
  future mail, bypasses do-not-contact, or permits a duplicate sequence.
* An incomplete search is never a clean result.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marcelorian.db.models import (
    Campaign,
    CampaignPause,
    InboundEvent,
    Message,
    PriorContactAck,
    SendAttempt,
)
from marcelorian.domain.enums import (
    UNRESOLVED_ATTEMPT_STATES,
    Actor,
    InboundClassification,
    Mode,
    PauseReason,
    ledger_for,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services.audit import audit
from marcelorian.transport.base import SearchResult


@dataclass(frozen=True)
class PriorContactCheck:
    complete: bool
    unacknowledged: tuple[str, ...] = ()
    reason: str | None = None


def campaign_gmail_ids(session: Session, campaign_id: int) -> set[str]:
    """Gmail IDs of messages this campaign itself sent (or recorded as its own)."""
    ids = set(
        session.scalars(
            select(SendAttempt.gmail_message_id)
            .join(Message, Message.id == SendAttempt.message_id)
            .where(Message.campaign_id == campaign_id, SendAttempt.gmail_message_id.is_not(None))
        )
    )
    ids |= set(
        session.scalars(
            select(Message.gmail_message_id).where(
                Message.campaign_id == campaign_id, Message.gmail_message_id.is_not(None)
            )
        )
    )
    ids |= set(
        session.scalars(
            select(InboundEvent.gmail_message_id).where(
                InboundEvent.campaign_id == campaign_id,
                InboundEvent.classification == InboundClassification.OWN_MESSAGE.value,
            )
        )
    )
    return {i for i in ids if i}


def same_mailbox_modes(mode: Mode) -> list[str]:
    return [m.value for m in Mode if ledger_for(m) is ledger_for(mode)]


def unresolved_attempt_to(session: Session, addresses: set[str], mode: Mode) -> bool:
    return bool(
        session.scalar(
            select(func.count())
            .select_from(SendAttempt)
            .where(
                func.lower(SendAttempt.recipient).in_({a.lower() for a in addresses}),
                SendAttempt.status.in_([s.value for s in UNRESOLVED_ATTEMPT_STATES]),
                SendAttempt.mode.in_(same_mailbox_modes(mode)),
            )
        )
    )


def acknowledged_ids(session: Session, mailbox_email: str, campaign_id: int) -> set[str]:
    return set(
        session.scalars(
            select(PriorContactAck.gmail_message_id).where(
                PriorContactAck.mailbox_email == mailbox_email.lower(),
                PriorContactAck.campaign_id == campaign_id,
            )
        )
    )


def evaluate(
    session: Session, *, campaign_id: int, mailbox_email: str, search: SearchResult
) -> PriorContactCheck:
    if not search.complete:
        return PriorContactCheck(complete=False, reason="search_incomplete")
    excluded = campaign_gmail_ids(session, campaign_id) | acknowledged_ids(
        session, mailbox_email, campaign_id
    )
    hits = tuple(sorted({i for i in search.ids if i not in excluded}))
    return PriorContactCheck(complete=True, unacknowledged=hits)


def open_review(
    session: Session, campaign: Campaign, hits: tuple[str, ...], *, actor: Actor, now: datetime
) -> CampaignPause:
    return campaign_svc.open_pause(
        session,
        campaign,
        PauseReason.PRIOR_CONTACT_FOUND,
        actor=actor,
        now=now,
        source_ref="prior_contact:" + ",".join(hits)[:100],
        detail={
            "gmail_ids": list(hits),
            "note": "A bounded scan does not prove there was no prior contact.",
        },
    )


def acknowledge_and_proceed(
    session: Session, pause: CampaignPause, *, mailbox_email: str, actor: Actor, now: datetime
) -> list[str]:
    if pause.reason != PauseReason.PRIOR_CONTACT_FOUND.value or pause.resolved_at is not None:
        raise ValueError("not an open prior-contact review")
    ids = list(json.loads(pause.detail_json or "{}").get("gmail_ids", []))
    for gmail_id in ids:
        session.add(
            PriorContactAck(
                mailbox_email=mailbox_email.lower(),
                campaign_id=pause.campaign_id,
                gmail_message_id=gmail_id,
                acknowledged_at=now,
                acknowledged_by=str(actor),
            )
        )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=pause.campaign_id,
        action="prior_contact_acknowledged",
        detail={"count": len(ids)},
    )
    campaign_svc.resolve_pause(
        session, pause, resolution="acknowledged_and_proceed", actor=actor, now=now
    )
    return ids
