"""Turn a confirmed research record into a contact (PI + address) through review.

Spec §6 rules apply: an address becomes send-ready only with confidence A (published on
the PI's current institutional or lab page) or B (a recent publication explicitly
attributing it to this investigator) **and** recorded ownership evidence (source, who,
when). The applicant's own check must have confirmed the person as current faculty or
an investigator within the last 90 days. Seed claims never become recipients.

Promotion creates or reuses the institution (with the applicant-confirmed time zone),
a lab group of the PI's own, the PI with current-role evidence, and the address. It
never creates a campaign, draft, approval or reservation.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from marcelorian.db.models import (
    DoNotContact,
    Institution,
    LabGroup,
    Pi,
    PiEmail,
    ResearchRecord,
)
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.recipients import RecipientError, normalize_address
from marcelorian.domain.research import confirmation_expired, is_web_url
from marcelorian.domain.scholarly_ids import (
    IdentifierError,
    canonical_openalex_author_id,
    canonical_orcid,
)
from marcelorian.domain.timing import SchedulingError, load_zone
from marcelorian.services import dnc
from marcelorian.services.audit import audit
from marcelorian.services.research_seed import record_value

#: Where the applicant saw the address -> confidence (spec §6).
EMAIL_SOURCES = {
    "institutional_page": ("A", "Published on the PI's current institutional or lab page"),
    "recent_publication": (
        "B",
        "A publication from the last 2 years explicitly attributing it to this investigator",
    ),
}
_NOT_A_PI = {"program_contact", "shared_mailbox"}


class PromotionError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class PromotionInput:
    last_name: str
    salutation: str
    email_source: str  # key of EMAIL_SOURCES
    email_source_url: str
    timezone: str
    timezone_confirmed: bool
    department: str = ""
    not_same_person: bool = False  # the applicant checked a name+institution match


def blockers(session: Session, record: ResearchRecord, today: date) -> list[str]:
    """Why this record cannot be turned into a contact yet (empty when it can)."""
    found: list[str] = []
    if record.promoted_pi_id is not None:
        found.append("Already turned into a contact.")
    if record.record_type != "contact":
        found.append("Opportunities are not contacts; use the opportunity's application route.")
    if record.contact_kind in _NOT_A_PI or record.email_is_shared:
        found.append("A program or shared mailbox is not a PI.")
    if record.excluded_reason:
        found.append(f"Out of scope ({record.excluded_reason}).")
    if record.local_verification != "confirmed_current":
        found.append("Confirm the person yourself first (current faculty or investigator).")
    else:
        verified = record.local_verified_at.date() if record.local_verified_at else None
        if confirmation_expired(record.local_verification, verified, today):
            found.append("Your confirmation is over 90 days old: confirm again.")
    if not record.email:
        found.append("No address on record (claimed addresses are never used).")
    if record.rereview_reason:
        found.append("The seed changed since your check: review the record again.")
    return found


def possible_duplicates(session: Session, record: ResearchRecord) -> tuple[list[Pi], list[Pi]]:
    """(same identity by address/ORCID/OpenAlex, same name at the same institution)."""
    identity = _identity_matches(session, record)
    name = " ".join(record.name.split()).casefold()
    same_name = [
        pi
        for pi in session.scalars(select(Pi).join(Institution, Institution.id == Pi.institution_id))
        if " ".join(pi.full_name.split()).casefold() == name
        and pi.id not in {p.id for p in identity}
        and _same_institution(session, pi, record)
    ]
    return identity, same_name


def promote(
    session: Session,
    record_id: int,
    data: PromotionInput,
    *,
    now: datetime,
    actor: Actor = Actor.APPLICANT,
) -> Pi:
    record = session.get(ResearchRecord, record_id)
    if record is None:
        raise LookupError(record_id)
    reasons = blockers(session, record, now.date())
    last_name = " ".join(data.last_name.split())
    salutation = " ".join(data.salutation.split())
    if not last_name:
        reasons.append("Enter the PI's last name.")
    if not salutation:
        reasons.append("Enter the salutation (e.g. Dr. Lastname).")
    if data.email_source not in EMAIL_SOURCES:
        reasons.append("Say where you saw the address.")
    if not is_web_url(data.email_source_url):
        reasons.append("Give the web address (https://…) where the email address is published.")
    try:
        load_zone(data.timezone.strip())
    except SchedulingError:
        reasons.append("Choose a valid time zone.")
    if not data.timezone_confirmed:
        reasons.append("Confirm the institution's time zone.")
    try:
        address = normalize_address(record.email)
    except RecipientError:
        reasons.append("The address on record is not a single plain email address.")
        address = ""
    if address and _dnc_hit(session, record, address):
        reasons.append("This person or address is on your do-not-contact list.")
    identity, same_name = possible_duplicates(session, record)
    if same_name and not data.not_same_person:
        reasons.append(
            "A contact with the same name at this institution exists: confirm it is a "
            "different person, or use the existing contact."
        )
    if reasons:
        raise PromotionError(reasons)

    if identity:
        pi = identity[0]  # the same person (address/ORCID/OpenAlex): reuse, never duplicate
        _ensure_email(session, pi, address, data, now)
        action = "linked_existing"
    else:
        institution = _institution(session, record, data, now)
        lab = LabGroup(institution_id=institution.id, name=f"{record.name} lab", created_at=now)
        session.add(lab)
        session.flush()
        pi = Pi(
            full_name=" ".join(record.name.split()),
            last_name=last_name,
            salutation=salutation,
            salutation_reviewed=True,
            salutation_reviewed_at=now,
            institution_id=institution.id,
            lab_group_id=lab.id,
            current_role_verified=True,
            current_role_evidence=_role_evidence(record),
            department=" ".join(data.department.split()) or None,
            created_at=now,
        )
        _set_identifiers(pi, record)
        session.add(pi)
        session.flush()
        _ensure_email(session, pi, address, data, now)
        action = "created"
    record.promoted_pi_id = pi.id
    record.promoted_at = now
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="research_record",
        entity_id=record.id,
        action="promoted",
        detail={
            "pi_id": pi.id,
            "result": action,
            "email_confidence": EMAIL_SOURCES[data.email_source][0],
        },
    )
    return pi


def _role_evidence(record: ResearchRecord) -> str:
    when = record.local_verified_at.date().isoformat() if record.local_verified_at else "unknown"
    return f"Applicant confirmed current role on {when}: {record.local_verification_source or ''}"


def _identity_matches(session: Session, record: ResearchRecord) -> list[Pi]:
    try:
        address = normalize_address(record.email) if record.email else ""
    except RecipientError:
        address = ""
    orcid = _canonical(canonical_orcid, record_value(record, "orcid"))
    openalex = _canonical(canonical_openalex_author_id, record_value(record, "openalex_id"))
    ids: set[int] = set()
    if address:
        ids.update(session.scalars(select(PiEmail.pi_id).where(PiEmail.address == address)))
    if orcid:
        ids.update(session.scalars(select(Pi.id).where(Pi.orcid == orcid)))
    if openalex:
        ids.update(session.scalars(select(Pi.id).where(Pi.openalex_id == openalex)))
    if not ids:
        return []
    closure = dnc.identity_closure(session, pi_ids=ids)
    pis = session.scalars(select(Pi).where(Pi.id.in_(closure.pi_ids)).order_by(Pi.id)).all()
    return [p for p in pis if p.merged_into_id is None] or list(pis)


def _dnc_hit(session: Session, record: ResearchRecord, address: str) -> bool:
    orcid = _canonical(canonical_orcid, record_value(record, "orcid"))
    conditions = [DoNotContact.email == address]
    if orcid:
        conditions.append(DoNotContact.orcid == orcid)
    return bool(
        session.scalar(
            select(func.count())
            .select_from(DoNotContact)
            .where(DoNotContact.scope_mode == Mode.LIVE.value, or_(*conditions))
        )
    )


def _same_institution(session: Session, pi: Pi, record: ResearchRecord) -> bool:
    institution = session.get(Institution, pi.institution_id)
    if institution is None:
        return False
    ror = record_value(record, "institution_ror_id").strip()
    if ror and institution.ror_id == ror:
        return True
    return (
        " ".join(institution.name.split()).casefold()
        == " ".join(record.institution.split()).casefold()
    )


def _institution(
    session: Session, record: ResearchRecord, data: PromotionInput, now: datetime
) -> Institution:
    from marcelorian.services import approvals

    ror = record_value(record, "institution_ror_id").strip() or None
    institution = None
    if ror:
        institution = session.scalar(select(Institution).where(Institution.ror_id == ror))
    if institution is None:
        name = " ".join(record.institution.split())
        institution = next(
            (
                i
                for i in session.scalars(select(Institution))
                if " ".join(i.name.split()).casefold() == name.casefold()
            ),
            None,
        )
    if institution is None:
        institution = Institution(
            ror_id=ror,
            name=" ".join(record.institution.split()) or "Unknown institution",
            city=record.city or None,
            state=record.state_code or None,
            created_at=now,
        )
        session.add(institution)
        session.flush()
    if institution.iana_tz != data.timezone.strip() or not institution.tz_verified:
        approvals.set_institution_timezone(
            session,
            institution,
            data.timezone.strip(),
            verified=True,
            actor=Actor.APPLICANT,
            now=now,
        )
    return institution


def _ensure_email(
    session: Session, pi: Pi, address: str, data: PromotionInput, now: datetime
) -> PiEmail:
    from marcelorian.services import approvals

    confidence, label = EMAIL_SOURCES[data.email_source]
    evidence = f"{label}: {data.email_source_url.strip()}"
    email = session.scalar(
        select(PiEmail).where(PiEmail.pi_id == pi.id, PiEmail.address == address)
    )
    if email is not None and email.ownership_verified and email.confidence <= confidence:
        return email  # already send-ready at this or a better confidence: keep it as is
    if email is None:
        email = PiEmail(
            pi_id=pi.id,
            address=address,
            confidence=confidence,
            source=data.email_source_url.strip(),
            created_at=now,
        )
        session.add(email)
        session.flush()
    else:
        # A change to an existing address goes through the invalidating update (B18).
        approvals.update_pi_email(
            session,
            email,
            actor=Actor.APPLICANT,
            now=now,
            confidence=confidence,
            ownership_verified=True,
        )
    email.ownership_verified = True
    email.verified_by = str(Actor.APPLICANT)
    email.verified_at = now
    email.verification_evidence = evidence
    session.flush()
    return email


def _set_identifiers(pi: Pi, record: ResearchRecord) -> None:
    orcid = _canonical(canonical_orcid, record_value(record, "orcid"))
    openalex = _canonical(canonical_openalex_author_id, record_value(record, "openalex_id"))
    pi.orcid = orcid
    pi.openalex_id = openalex


def _canonical(fn: Callable[[str], str], value: str) -> str | None:
    if not value:
        return None
    try:
        return fn(value)
    except IdentifierError:
        return None
