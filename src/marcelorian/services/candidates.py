"""Candidates: contacts with their assessment dimensions, never one score (spec §7).

Dimensions shown separately: research fit (applicant-set, Strong/Moderate/Weak), contact
quality (address confidence, ownership, current role), research funding evidence (active
NIH grants), appointment funding (Unknown unless an opportunity says otherwise; P2),
opportunity (No opening verified unless a posting is recorded), and place status under
the same-place rule. Default sort follows spec §7: funding, grant evidence, fit, contact.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Assessment, Campaign, Institution, Pi, ResearchRecord
from marcelorian.domain.enums import Actor, Mode
from marcelorian.lookups import enrich
from marcelorian.services import dnc, reservations
from marcelorian.services.audit import audit
from marcelorian.services.recipients import send_ready_email

FITS = {"strong": "Strong", "moderate": "Moderate", "weak": "Weak"}
_FIT_RANK = {"strong": 0, "moderate": 1, "weak": 2, None: 3}
MAX_NOTE = 2000


@dataclass(frozen=True)
class Filters:
    fit: str = ""  # strong | moderate | weak | none
    grants: str = ""  # yes | no
    state: str = ""
    institution_id: int | None = None
    contact: str = ""  # A | B | none
    show_skipped: bool = False
    query: str = ""


@dataclass(frozen=True)
class Candidate:
    pi: Pi
    institution: Institution
    email: str | None
    confidence: str | None
    fit: str | None
    skipped: bool
    active_grants: int
    place_busy: bool
    campaign_state: str | None
    summary: str


def assessment(session: Session, pi_id: int) -> Assessment | None:
    return session.scalar(select(Assessment).where(Assessment.pi_id == pi_id))


def _assessment_for_update(session: Session, pi_id: int, now: datetime) -> Assessment:
    row = assessment(session, pi_id)
    if row is None:
        if session.get(Pi, pi_id) is None:
            raise LookupError(pi_id)
        row = Assessment(pi_id=pi_id, updated_at=now)
        session.add(row)
    return row


def set_fit(session: Session, pi_id: int, fit: str, note: str, *, now: datetime) -> Assessment:
    if fit and fit not in FITS:
        raise ValueError("research fit must be strong, moderate or weak")
    row = _assessment_for_update(session, pi_id, now)
    row.research_fit = fit or None
    row.fit_source = "applicant"
    row.fit_note = note.strip()[:MAX_NOTE] or None
    row.updated_at = now
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="pi",
        entity_id=pi_id,
        action="research_fit",
        to_state=fit or "unassessed",
    )
    return row


def set_skipped(
    session: Session, pi_id: int, skipped: bool, reason: str, *, now: datetime
) -> Assessment:
    row = _assessment_for_update(session, pi_id, now)
    row.skipped = skipped
    row.skip_reason = reason.strip()[:MAX_NOTE] or None
    row.updated_at = now
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="pi",
        entity_id=pi_id,
        action="skipped" if skipped else "unskipped",
    )
    return row


def add_to_dnc(session: Session, pi_id: int, reason: str, *, now: datetime) -> None:
    """Live-scope do-not-contact entry for this person and their send-ready address."""
    pi = session.get(Pi, pi_id)
    if pi is None:
        raise LookupError(pi_id)
    email = send_ready_email(session, pi_id)
    dnc.add_entry(
        session,
        scope_mode=Mode.LIVE,
        actor=Actor.APPLICANT,
        now=now,
        pi_id=pi.id,
        email=email.address if email else None,
        orcid=pi.orcid,
        openalex_id=pi.openalex_id,
        reason=reason.strip()[:MAX_NOTE] or "Applicant decision",
    )


def summary(*, fit: str | None, role_verified: bool, grants: int, confidence: str | None) -> str:
    parts = [
        f"{FITS[fit]} research fit." if fit else "Research fit not assessed.",
        "Current role verified." if role_verified else "Current role not verified.",
        "No opening verified.",
        "Appointment funding unknown"
        + ("; active NIH grant evidence." if grants else "; no active grant evidence found."),
    ]
    if confidence is None:
        parts.append("No send-ready address.")
    return " ".join(parts)


def list_candidates(
    session: Session, filters: Filters, mode: Mode, scope: str, now: datetime
) -> list[Candidate]:
    today = now.date()
    rows: list[Candidate] = []
    query = (
        select(Pi, Institution)
        .join(Institution, Institution.id == Pi.institution_id)
        .where(Pi.merged_into_id.is_(None), Pi.is_synthetic.is_(False))
    )
    if filters.institution_id:
        query = query.where(Pi.institution_id == filters.institution_id)
    if filters.state:
        query = query.where(Institution.state == filters.state)
    needle = filters.query.strip().casefold()
    for pi, institution in session.execute(query).all():
        if (
            needle
            and needle not in pi.full_name.casefold()
            and needle not in institution.name.casefold()
        ):
            continue
        row = assessment(session, pi.id)
        fit = row.research_fit if row else None
        skipped = bool(row and row.skipped)
        if skipped and not filters.show_skipped:
            continue
        email = send_ready_email(session, pi.id)
        confidence = email.confidence if email else None
        grants = len(enrich.active_grants(session, pi.id, today))
        if filters.fit and (fit or "none") != filters.fit:
            continue
        if filters.grants in {"yes", "no"} and (grants > 0) != (filters.grants == "yes"):
            continue
        if filters.contact and (confidence or "none") != filters.contact:
            continue
        campaign = session.scalar(
            select(Campaign).where(Campaign.pi_id == pi.id, Campaign.mode == mode.value)
        )
        busy = bool(reservations.place_conflicts_for_pi(session, pi.id, mode, scope, now))
        rows.append(
            Candidate(
                pi=pi,
                institution=institution,
                email=email.address if email else None,
                confidence=confidence,
                fit=fit,
                skipped=skipped,
                active_grants=grants,
                place_busy=busy,
                campaign_state=campaign.state if campaign else None,
                summary=summary(
                    fit=fit,
                    role_verified=pi.current_role_verified,
                    grants=grants,
                    confidence=confidence,
                ),
            )
        )
    rows.sort(
        key=lambda c: (
            -min(c.active_grants, 1),
            _FIT_RANK[c.fit],
            c.confidence or "Z",
            c.pi.full_name.casefold(),
        )
    )
    return rows


def research_records_for(session: Session, pi_id: int) -> list[ResearchRecord]:
    return list(
        session.scalars(select(ResearchRecord).where(ResearchRecord.promoted_pi_id == pi_id))
    )


def institutions(session: Session) -> list[Institution]:
    return list(session.scalars(select(Institution).order_by(Institution.name)))
