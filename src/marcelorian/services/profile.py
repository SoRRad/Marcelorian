"""Applicant profile: one applicant, fields stored as versioned facts (spec §4).

Every profile field is a ``profile_fact`` keyed by a fixed name. Saving is either a
draft (``proposed``) or an explicit **Save and confirm** (``confirmed``); only confirmed
facts can appear in drafts, and a change invalidates only the approvals that used the
fact (``approvals.update_fact``, B18). Self-reported qualifications are never labelled
externally verified (C2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import ApplicantProfile, CvVersion, ProfileFact
from marcelorian.domain.enums import Actor, FactStatus
from marcelorian.domain.scholarly_ids import IdentifierError, canonical_orcid
from marcelorian.services import approvals
from marcelorian.services.audit import audit

MAX_FACT_CHARS = 2000
MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
FUNDING_CHOICES = {
    "paid_preferred": "Paid preferred, unpaid acceptable",
    "exclude_unpaid": "Exclude explicitly unpaid opportunities; unknown funding allowed",
}
TONE_CHOICES = {"formal": "Formal", "warm_formal": "Warm and formal", "concise": "Concise"}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    help: str = ""
    kind: str = "text"  # text | long | month | choice | orcid
    choices: dict[str, str] | None = None
    required: bool = False


#: Profile fields in display order (spec §4).
FIELDS: tuple[Field, ...] = (
    Field("full_name", "Full name", "As used in the first email's introduction.", required=True),
    Field(
        "signature_name",
        "Name for closing follow-ups",
        "Defaults to your full name; never shortened automatically.",
    ),
    Field("orcid", "Your ORCID iD (optional)", "Used to import your publications.", "orcid"),
    Field(
        "pubmed_name",
        "Your name as on your papers (optional)",
        'As PubMed lists it, e.g. "Shahriari R". Helps find your publications.',
    ),
    Field("degrees", "Degree(s)", "For example MD, MPH."),
    Field("medical_school", "Medical school"),
    Field("graduation_year", "Graduation year"),
    Field(
        "current_role_phrase",
        "Current role",
        'Completes "I am …", e.g. "a research fellow at X".',
        required=True,
    ),
    Field(
        "us_training_status", "US training status", "USMLE steps, ECFMG, any US residency.", "long"
    ),
    Field("visa_status", "Visa status or need", "Shown factually, never interpreted.", "long"),
    Field(
        "earliest_start",
        "Earliest start (YYYY-MM, optional)",
        "Not mentioned in the default emails; only a custom template with {availability} uses it.",
        "month",
    ),
    Field("desired_duration", "Desired duration", "Included only when confirmed."),
    Field("preferred_states", "Preferred US states (optional)"),
    Field(
        "funding_preference",
        "Funding requirement",
        "Unknown funding always stays eligible and marked.",
        "choice",
        FUNDING_CHOICES,
        required=True,
    ),
    Field(
        "funding_line",
        "Funding sentence (optional)",
        "Added to Email 1 only if you write one. Never mention unpaid work unless you want to.",
        "long",
    ),
    Field("subspecialty_interests", "Subspecialty interests", "Leave empty for all."),
    Field("research_interests", "Research interests", kind="long"),
    Field("skills", "Methods and skills you offer", kind="long", required=True),
    Field("tone", "Tone", kind="choice", choices=TONE_CHOICES),
    Field("recent_news", "Recent news", "Used only in Email 3; leave empty for none.", "long"),
)
FIELD_BY_KEY = {f.key: f for f in FIELDS}


class ProfileError(ValueError):
    pass


def applicant(session: Session, now: datetime) -> ApplicantProfile:
    """The single applicant profile, created on first use."""
    profile = session.scalars(select(ApplicantProfile).order_by(ApplicantProfile.id)).first()
    if profile is None:
        profile = ApplicantProfile(created_at=now, updated_at=now)
        session.add(profile)
        session.flush()
    return profile


def facts(session: Session, applicant_id: int) -> dict[str, ProfileFact]:
    return {
        f.key: f
        for f in session.scalars(
            select(ProfileFact).where(ProfileFact.applicant_id == applicant_id)
        )
    }


def validate(key: str, value: str) -> str:
    field = FIELD_BY_KEY.get(key)
    if field is None:
        raise ProfileError(f"unknown profile field: {key}")
    text = value.replace("\r\n", "\n").strip()
    if len(text) > MAX_FACT_CHARS:
        raise ProfileError(f"{field.label} is too long (max {MAX_FACT_CHARS} characters)")
    if any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        raise ProfileError(f"{field.label} contains control characters")
    if "{" in text or "}" in text:
        raise ProfileError(f"{field.label} must not contain {{ or }} (template markers)")
    if text and field.kind == "month" and not MONTH.match(text):
        raise ProfileError(f"{field.label} must look like 2027-07")
    if text and field.kind == "orcid":
        try:
            text = canonical_orcid(text)
        except IdentifierError as exc:
            raise ProfileError(f"{field.label}: {exc}") from exc
    if text and field.kind == "choice" and field.choices and text not in field.choices:
        raise ProfileError(f"{field.label}: choose one of the listed options")
    return text


def set_fact(
    session: Session,
    applicant_id: int,
    key: str,
    value: str,
    *,
    confirm: bool,
    actor: Actor,
    now: datetime,
    provenance: str = "applicant_entered",
) -> ProfileFact | None:
    """Create or change a fact. Empty text removes an unconfirmed fact, or records an
    explicit empty answer when confirmed (e.g. "no recent news")."""
    text = validate(key, value)
    existing = facts(session, applicant_id).get(key)
    if existing is None:
        if not text and not confirm:
            return None
        fact = ProfileFact(
            applicant_id=applicant_id,
            key=key,
            value=text,
            status=(FactStatus.CONFIRMED if confirm else FactStatus.PROPOSED).value,
            provenance=provenance,
            version=1,
            confirmed_at=now if confirm else None,
            updated_at=now,
        )
        session.add(fact)
        session.flush()
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="profile_fact",
            entity_id=fact.id,
            action="created",
            to_state=fact.status,
            detail={"key": key, "provenance": provenance},
        )
        return fact
    approvals.update_fact(session, existing, text, confirm=confirm, actor=actor, now=now)
    return existing


def missing_required(session: Session, applicant_id: int) -> list[str]:
    """Required fields not yet confirmed (drafting needs them)."""
    current = facts(session, applicant_id)
    return [
        f.label
        for f in FIELDS
        if f.required
        and (
            f.key not in current
            or current[f.key].status != FactStatus.CONFIRMED.value
            or not current[f.key].value
        )
    ]


def cv_versions(session: Session, applicant_id: int) -> list[CvVersion]:
    return list(
        session.scalars(
            select(CvVersion)
            .where(CvVersion.applicant_id == applicant_id)
            .order_by(CvVersion.uploaded_at.desc(), CvVersion.id.desc())
        )
    )


MAX_CV_BYTES = 10 * 1024 * 1024


def check_cv(filename: str, data: bytes) -> str:
    """Return a safe display filename or raise ``ProfileError``. PDF only (spec §4)."""
    if not data:
        raise ProfileError("The file is empty.")
    if len(data) > MAX_CV_BYTES:
        raise ProfileError("The CV is larger than 10 MB.")
    if not data.startswith(b"%PDF-"):
        raise ProfileError("Please upload the CV as a PDF file.")
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
    name = name.strip(" .") or "cv.pdf"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[:120]
