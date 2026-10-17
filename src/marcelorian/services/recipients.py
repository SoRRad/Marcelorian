"""Transport-test allowlist and recipient context (spec §12; decisions B11, B20)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, Pi, PiEmail, RecipientAllowlist
from marcelorian.domain.enums import Actor, CampaignState, EmailConfidence, Mode
from marcelorian.domain.recipients import RecipientContext, RecipientError, normalize_address
from marcelorian.services.audit import audit


def real_pi_addresses(session: Session) -> frozenset[str]:
    rows = session.scalars(
        select(func.lower(PiEmail.address))
        .join(Pi, Pi.id == PiEmail.pi_id)
        .where(Pi.is_synthetic.is_(False))
    )
    return frozenset(rows)


def active_allowlist(session: Session) -> frozenset[str]:
    return frozenset(
        session.scalars(
            select(RecipientAllowlist.address).where(RecipientAllowlist.active.is_(True))
        )
    )


def add_allowlist_address(
    session: Session,
    address: str,
    label: str,
    *,
    actor: Actor,
    now: datetime,
    consent_note: str | None = None,
) -> RecipientAllowlist:
    normalized = normalize_address(address)
    if normalized in real_pi_addresses(session):
        raise RecipientError("a known real-PI address can never be a transport-test recipient")
    entry = RecipientAllowlist(
        address=normalized,
        label=label,
        consent_note=consent_note,
        active=True,
        added_at=now,
        added_by=str(actor),
    )
    session.add(entry)
    session.flush()
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="allowlist",
        entity_id=entry.id,
        action="added",
        detail={"label": label},
    )
    return entry


def deactivate_allowlist_address(
    session: Session, entry: RecipientAllowlist, *, actor: Actor, now: datetime
) -> None:
    entry.active = False
    entry.version += 1
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="allowlist",
        entity_id=entry.id,
        action="deactivated",
    )


def send_ready_email(session: Session, pi_id: int) -> PiEmail | None:
    """A or B confidence, ownership attributable, not invalid (spec §6). Unique or none."""
    rows = session.scalars(
        select(PiEmail)
        .where(
            PiEmail.pi_id == pi_id,
            PiEmail.confidence.in_([EmailConfidence.A.value, EmailConfidence.B.value]),
            PiEmail.ownership_verified.is_(True),
            PiEmail.invalid.is_(False),
        )
        .order_by(PiEmail.confidence, PiEmail.id)
    ).all()
    return rows[0] if rows else None


def other_active_test_campaign(session: Session, recipient_id: int, campaign_id: int) -> bool:
    return bool(
        session.scalar(
            select(func.count())
            .select_from(Campaign)
            .where(
                Campaign.mode == Mode.TRANSPORT_TEST.value,
                Campaign.test_recipient_id == recipient_id,
                Campaign.id != campaign_id,
                Campaign.state.in_(
                    [CampaignState.OUTREACH_PENDING.value, CampaignState.PAUSED_REVIEW.value]
                ),
            )
        )
    )


def build_context(
    session: Session,
    *,
    campaign: Campaign,
    pi: Pi,
    transport_kind: str,
    snapshot_to: str,
    snapshot_intended: str,
    has_test_override: bool,
) -> RecipientContext:
    ready = send_ready_email(session, pi.id)
    test_recipient: str | None = None
    if campaign.test_recipient_id is not None:
        entry = session.get(RecipientAllowlist, campaign.test_recipient_id)
        test_recipient = entry.address if entry is not None and entry.active else None
    return RecipientContext(
        mode=Mode(campaign.mode),
        transport_kind=transport_kind,
        snapshot_to=snapshot_to,
        snapshot_intended=snapshot_intended,
        has_test_override=has_test_override,
        pi_is_synthetic=pi.is_synthetic,
        send_ready_address=ready.address.lower() if ready else None,
        allowlist=active_allowlist(session),
        campaign_test_recipient=test_recipient,
        real_pi_addresses=real_pi_addresses(session),
    )
