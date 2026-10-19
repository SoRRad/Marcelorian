"""Pending-slot and new-contact reservations (spec §11; decisions B3, B4, B6).

Counting rules (all per mode, using the *current* institution grouping and lab
assignment, so merged groups never double-count):

* pending occupancy = active ``pending_slot`` reservations (a campaign holds one
  while in ``outreach_pending``/``paused_review``; the optional 14-day hold keeps
  it counted until ``hold_until``, or until a genuine reply or closure ends it);
* new contacts in the rolling 30 days = ``new_contact`` reservations consumed at
  an Email 1 ``sent_at`` with ``sent_at > now - 30d`` (no upper bound: a
  slightly future-dated established time still counts) plus active
  (projected, i.e. approved-but-unsent, in-flight or uncertain) ones.

Same-place rule (owner decisions N1-N2, 2026-10-19): one active application per place,
where a place is the institution group (default), a department within it, or a lab
(``same_place_scope``). Another campaign occupies the place while it is pending,
paused for review or in a conversation, and for 14 days after its last unanswered
follow-up; closing it (a "no opening" reply, a bounce, do-not-contact or the
applicant's own close) frees the place. Checked at Email 1 approval and again at the
final dispatch check; follow-ups of a campaign already under way are never blocked.

A campaign that already owns reservations is *validated*, never reserved twice
(B4). Capacity is evaluated "including self": a campaign occupying the last
allowed slot can send (spec §11), while lowered limits block initials that are
over capacity without cancelling anything (B6).

Callers run inside a ``BEGIN IMMEDIATE`` transaction, so parallel approvals and
claims are serialized and cannot oversubscribe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from marcelorian.config.settings import AppSettings
from marcelorian.db.models import (
    Campaign,
    Institution,
    LabSuggestion,
    Message,
    Pi,
    Reservation,
)
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    Mode,
    ReservationKind,
    ReservationStatus,
)
from marcelorian.services.audit import audit

NEW_CONTACT_WINDOW = timedelta(days=30)
SLOT_HOLD_AFTER_EMAIL4 = timedelta(days=14)
#: A finished sequence with no reply keeps its place this long after the last email.
SAME_PLACE_NO_REPLY_GRACE = timedelta(days=14)
_OCCUPYING_STATES = (
    CampaignState.OUTREACH_PENDING.value,
    CampaignState.PAUSED_REVIEW.value,
    CampaignState.CONVERSATION.value,
)


class CapacityError(RuntimeError):
    def __init__(self, result: CapacityResult) -> None:
        super().__init__("capacity check failed: " + ", ".join(result.reasons))
        self.result = result


@dataclass(frozen=True)
class CapacityResult:
    ok: bool
    reasons: tuple[str, ...] = ()
    counts: dict[str, int] = field(default_factory=dict)


def institution_ids_in_group(session: Session, institution_id: int) -> list[int]:
    institution = session.get(Institution, institution_id)
    if institution is None:
        raise ValueError(f"unknown institution {institution_id}")
    if institution.group_id is None:
        return [institution.id]
    return list(
        session.scalars(select(Institution.id).where(Institution.group_id == institution.group_id))
    )


def _active_slot_filter(now: datetime):  # type: ignore[no-untyped-def]
    return (
        Reservation.kind == ReservationKind.PENDING_SLOT.value,
        Reservation.status == ReservationStatus.ACTIVE.value,
        or_(Reservation.hold_until.is_(None), Reservation.hold_until > now),
    )


def count_pending_institution(
    session: Session,
    mode: Mode,
    institution_ids: list[int],
    now: datetime,
    exclude_campaign_id: int | None = None,
) -> int:
    stmt = (
        select(func.count(func.distinct(Reservation.campaign_id)))
        .join(Campaign, Campaign.id == Reservation.campaign_id)
        .join(Pi, Pi.id == Campaign.pi_id)
        .where(
            Reservation.mode == mode.value,
            Pi.institution_id.in_(institution_ids),
            *_active_slot_filter(now),
        )
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(Reservation.campaign_id != exclude_campaign_id)
    return int(session.scalar(stmt) or 0)


def count_pending_lab(
    session: Session,
    mode: Mode,
    lab_group_id: int,
    now: datetime,
    exclude_campaign_id: int | None = None,
) -> int:
    stmt = (
        select(func.count(func.distinct(Reservation.campaign_id)))
        .join(Campaign, Campaign.id == Reservation.campaign_id)
        .join(Pi, Pi.id == Campaign.pi_id)
        .where(
            Reservation.mode == mode.value,
            Pi.lab_group_id == lab_group_id,
            *_active_slot_filter(now),
        )
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(Reservation.campaign_id != exclude_campaign_id)
    return int(session.scalar(stmt) or 0)


def count_new_contacts(
    session: Session,
    mode: Mode,
    institution_ids: list[int],
    now: datetime,
    exclude_campaign_id: int | None = None,
) -> int:
    in_window = (Reservation.status == ReservationStatus.CONSUMED.value) & (
        Reservation.consumed_at > now - NEW_CONTACT_WINDOW
    )
    projected = Reservation.status == ReservationStatus.ACTIVE.value
    stmt = (
        select(func.count(func.distinct(Reservation.campaign_id)))
        .join(Campaign, Campaign.id == Reservation.campaign_id)
        .join(Pi, Pi.id == Campaign.pi_id)
        .where(
            Reservation.mode == mode.value,
            Reservation.kind == ReservationKind.NEW_CONTACT.value,
            Pi.institution_id.in_(institution_ids),
            in_window | projected,
        )
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(Reservation.campaign_id != exclude_campaign_id)
    return int(session.scalar(stmt) or 0)


def active_reservations(session: Session, campaign_id: int) -> dict[ReservationKind, Reservation]:
    rows = session.scalars(
        select(Reservation).where(
            Reservation.campaign_id == campaign_id,
            Reservation.status == ReservationStatus.ACTIVE.value,
        )
    ).all()
    return {ReservationKind(r.kind): r for r in rows}


def _department(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def occupies_place(session: Session, campaign: Campaign, now: datetime) -> bool:
    """Whether this campaign holds its place under the same-place rule."""
    if campaign.state in _OCCUPYING_STATES:
        return True
    if campaign.state != CampaignState.SEQUENCE_COMPLETE.value:
        return False
    return place_free_from(session, campaign) > now


def place_free_from(session: Session, campaign: Campaign) -> datetime:
    """For a finished sequence: when its place frees (14 days after the last email)."""
    last = session.scalar(
        select(func.max(Message.sent_at)).where(Message.campaign_id == campaign.id)
    )
    return (last or campaign.updated_at) + SAME_PLACE_NO_REPLY_GRACE


def same_place_conflicts(
    session: Session, campaign: Campaign, scope: str, now: datetime
) -> list[Campaign]:
    """Other campaigns (same mode) occupying this campaign's place."""
    return place_conflicts_for_pi(
        session, campaign.pi_id, Mode(campaign.mode), scope, now, exclude_campaign_id=campaign.id
    )


def place_conflicts_for_pi(
    session: Session,
    pi_id: int,
    mode: Mode,
    scope: str,
    now: datetime,
    exclude_campaign_id: int | None = None,
) -> list[Campaign]:
    """Campaigns of other PIs (this mode) occupying this PI's place.

    A missing department is treated as the whole institution (conservative): it
    conflicts with every department there, and a PI without one conflicts with all.
    """
    pi = session.get(Pi, pi_id)
    assert pi is not None
    stmt = (
        select(Campaign, Pi)
        .join(Pi, Pi.id == Campaign.pi_id)
        .where(
            Campaign.mode == mode.value,
            Campaign.pi_id != pi_id,
            Campaign.state.in_((*_OCCUPYING_STATES, CampaignState.SEQUENCE_COMPLETE.value)),
        )
    )
    if exclude_campaign_id is not None:
        stmt = stmt.where(Campaign.id != exclude_campaign_id)
    if scope == "lab":
        stmt = stmt.where(Pi.lab_group_id == pi.lab_group_id)
    else:
        stmt = stmt.where(
            Pi.institution_id.in_(institution_ids_in_group(session, pi.institution_id))
        )
    own_department = _department(pi.department)
    found: list[Campaign] = []
    for other, other_pi in session.execute(stmt).all():
        if scope == "department" and own_department:
            theirs = _department(other_pi.department)
            if theirs and theirs != own_department:
                continue
        if occupies_place(session, other, now):
            found.append(other)
    return found


def evaluate_initial(
    session: Session, campaign: Campaign, settings: AppSettings, now: datetime
) -> CapacityResult:
    """Capacity for this campaign's Email 1, counting the campaign itself once."""
    mode = Mode(campaign.mode)
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    inst_ids = institution_ids_in_group(session, pi.institution_id)
    pending_inst = count_pending_institution(session, mode, inst_ids, now, campaign.id) + 1
    pending_lab = count_pending_lab(session, mode, pi.lab_group_id, now, campaign.id) + 1
    contacts = count_new_contacts(session, mode, inst_ids, now, campaign.id) + 1
    reasons: list[str] = []
    if pending_inst > settings.pending_per_institution:
        reasons.append("institution_pending_full")
    if pending_lab > settings.pending_per_lab:
        reasons.append("lab_pending_full")
    if contacts > settings.new_contacts_per_institution_30d:
        reasons.append("new_contacts_30d_full")
    busy = same_place_conflicts(session, campaign, settings.same_place_scope, now)
    if busy:
        reasons.append("same_place_active")
    return CapacityResult(
        ok=not reasons,
        reasons=tuple(reasons),
        counts={
            "institution_pending": pending_inst,
            "lab_pending": pending_lab,
            "new_contacts_30d": contacts,
            "same_place_active": len(busy),
        },
    )


def _lab_suggestion_block(session: Session, campaign: Campaign) -> bool:
    """An unverified shared-lab suggestion with an already-contacted/reserved PI blocks (§6)."""
    suggestions = session.scalars(
        select(LabSuggestion).where(
            LabSuggestion.status == "unverified",
            or_(LabSuggestion.pi_a_id == campaign.pi_id, LabSuggestion.pi_b_id == campaign.pi_id),
        )
    ).all()
    for suggestion in suggestions:
        other = suggestion.pi_b_id if suggestion.pi_a_id == campaign.pi_id else suggestion.pi_a_id
        has_outreach = session.scalar(
            select(func.count())
            .select_from(Reservation)
            .join(Campaign, Campaign.id == Reservation.campaign_id)
            .where(Campaign.pi_id == other, Campaign.mode == campaign.mode)
        )
        if has_outreach:
            return True
    return False


def reserve_or_validate_initial(
    session: Session,
    campaign: Campaign,
    settings: AppSettings,
    now: datetime,
    *,
    actor: Actor,
) -> CapacityResult:
    """At Email 1 approval: reserve atomically, or validate reservations already owned (B4)."""
    if _lab_suggestion_block(session, campaign):
        raise CapacityError(CapacityResult(ok=False, reasons=("lab_suggestion_unreviewed",)))
    existing = active_reservations(session, campaign.id)
    result = evaluate_initial(session, campaign, settings, now)
    if existing:
        if set(existing) != {ReservationKind.PENDING_SLOT, ReservationKind.NEW_CONTACT}:
            raise RuntimeError("campaign holds an incomplete reservation set")
        # Already owned: keep them; over-capacity is reported, not silently grandfathered (B6).
        return result
    if not result.ok:
        raise CapacityError(result)
    for kind in (ReservationKind.PENDING_SLOT, ReservationKind.NEW_CONTACT):
        session.add(
            Reservation(
                campaign_id=campaign.id,
                mode=campaign.mode,
                kind=kind.value,
                status=ReservationStatus.ACTIVE.value,
                created_at=now,
            )
        )
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="campaign",
        entity_id=campaign.id,
        action="reserved",
        detail=result.counts,
        mode=campaign.mode,
    )
    session.flush()
    return result


def validate_for_dispatch(
    session: Session, campaign: Campaign, settings: AppSettings, now: datetime
) -> CapacityResult:
    """Final check at the atomic claim for Email 1 (spec §11, B6)."""
    existing = active_reservations(session, campaign.id)
    if set(existing) != {ReservationKind.PENDING_SLOT, ReservationKind.NEW_CONTACT}:
        return CapacityResult(ok=False, reasons=("reservation_missing",))
    if _lab_suggestion_block(session, campaign):
        return CapacityResult(ok=False, reasons=("lab_suggestion_unreviewed",))
    return evaluate_initial(session, campaign, settings, now)


def _end(
    session: Session, row: Reservation, status: ReservationStatus, now: datetime, reason: str
) -> None:
    row.status = status.value
    if status is ReservationStatus.RELEASED:
        if row.hold_until is not None and row.hold_until <= now:
            # The optional hold already stopped counting on its own: record it as
            # lapsed then, not as ended by the event (reply, closure) that ends the row.
            now, reason = row.hold_until, "hold_lapsed"
        row.released_at, row.release_reason = now, reason


def release_pending_slot(
    session: Session,
    campaign: Campaign,
    now: datetime,
    reason: str,
    hold_until: datetime | None = None,
) -> None:
    row = active_reservations(session, campaign.id).get(ReservationKind.PENDING_SLOT)
    if row is None:
        return
    if hold_until is not None:
        row.hold_until = hold_until  # optional stricter setting: still counted until then
        row.release_reason = reason
        return
    _end(session, row, ReservationStatus.RELEASED, now, reason)


def release_projected_contact(
    session: Session, campaign: Campaign, now: datetime, reason: str
) -> None:
    row = active_reservations(session, campaign.id).get(ReservationKind.NEW_CONTACT)
    if row is not None:
        _end(session, row, ReservationStatus.RELEASED, now, reason)


def consume_new_contact(session: Session, campaign: Campaign, sent_at: datetime) -> None:
    row = active_reservations(session, campaign.id).get(ReservationKind.NEW_CONTACT)
    if row is None:
        return
    row.status = ReservationStatus.CONSUMED.value
    row.consumed_at = sent_at


def release_all(session: Session, campaign: Campaign, now: datetime, reason: str) -> None:
    for row in active_reservations(session, campaign.id).values():
        _end(session, row, ReservationStatus.RELEASED, now, reason)
