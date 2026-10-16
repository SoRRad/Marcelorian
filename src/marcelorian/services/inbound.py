"""State effects of classified inbound events (spec §10).

Classification heuristics (auto-reply headers, delivery reports, participant
matching) arrive in P3. P0 implements the transitions an already-classified
event causes, idempotently: an event for the same Gmail message and campaign
is stored once and never re-applied, so an acknowledged auto-reply never
re-pauses the campaign (acceptance test 26).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, InboundEvent
from marcelorian.domain.enums import Actor, InboundClassification, PauseReason
from marcelorian.services import campaigns as campaign_svc

_PAUSES: dict[InboundClassification, PauseReason] = {
    InboundClassification.AUTO_REPLY: PauseReason.AUTO_REPLY,
    InboundClassification.DELIVERY_NOTICE: PauseReason.DELIVERY_NOTICE,
    InboundClassification.MANUAL_APPLICANT_MESSAGE: PauseReason.MANUAL_APPLICANT_MESSAGE,
    InboundClassification.UNKNOWN_PARTICIPANT: PauseReason.UNKNOWN_PARTICIPANT,
    InboundClassification.AMBIGUOUS_THREAD: PauseReason.AMBIGUOUS_THREAD,
}


def known_event(session: Session, campaign_id: int, gmail_message_id: str) -> InboundEvent | None:
    return session.scalar(
        select(InboundEvent).where(
            InboundEvent.campaign_id == campaign_id,
            InboundEvent.gmail_message_id == gmail_message_id,
        )
    )


def record_inbound(
    session: Session,
    campaign: Campaign,
    *,
    gmail_message_id: str,
    thread_id: str | None,
    classification: InboundClassification,
    from_address: str | None,
    received_at: datetime | None,
    actor: Actor,
    now: datetime,
) -> tuple[InboundEvent, bool]:
    existing = known_event(session, campaign.id, gmail_message_id)
    if existing is not None:
        return existing, False
    event = InboundEvent(
        mode=campaign.mode,
        campaign_id=campaign.id,
        gmail_message_id=gmail_message_id,
        gmail_thread_id=thread_id,
        from_address=from_address.lower() if from_address else None,
        classification=classification.value,
        received_at=received_at,
        recorded_at=now,
    )
    session.add(event)
    session.flush()
    source = f"inbound:{gmail_message_id}"
    if classification is InboundClassification.GENUINE_REPLY:
        campaign_svc.apply_genuine_reply(session, campaign, actor=actor, now=now, source_ref=source)
    elif classification in _PAUSES:
        campaign_svc.open_pause(
            session, campaign, _PAUSES[classification], actor=actor, now=now, source_ref=source
        )
    return event, True
