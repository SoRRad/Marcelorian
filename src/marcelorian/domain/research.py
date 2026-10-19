"""Research-catalog confidence, local verification and freshness (docs/SEED_IMPORT.md).

Pure functions. A research record is a lead, not a recipient: these labels tell the
applicant how much is actually known about the person and the address, so that the
review before any email is made with the confidence in view. Nothing here authorizes,
drafts or sends anything.

Two questions are judged separately and the weaker answer wins:

* **person** -- is this a current faculty member / investigator at that institution,
  rather than a fellow, resident, author who has since moved, or a program route?
* **email** -- is this address published by an official source for that person?

From the seed alone the best possible answer is ``source_checked`` (an official
source was observed on some date). Only the applicant's own check can make a person
``confirmed``; any negative finding by the applicant makes the record ``blocked``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

#: Applicant's own finding about the person, never written by an import.
LOCAL_VERIFICATIONS: dict[str, str] = {
    "unchecked": "Not checked by you yet",
    "confirmed_current": "Confirmed by you: current faculty / investigator there",
    "fellow_or_trainee": "Found by you: fellow, resident or other trainee",
    "left_institution": "Found by you: left or moved institution",
    "not_faculty": "Found by you: not faculty / not a consultant",
    "retired_or_emeritus": "Found by you: retired or emeritus",
    "wrong_person": "Found by you: wrong or ambiguous person",
}
BLOCKING_VERIFICATIONS = frozenset(LOCAL_VERIFICATIONS) - {"unchecked", "confirmed_current"}

#: Applicant's own triage, never written by an import.
LOCAL_STATUSES: dict[str, str] = {
    "unreviewed": "Not reviewed",
    "shortlisted": "Shortlisted for later review",
    "set_aside": "Set aside",
}

LEVELS = ("blocked", "none", "unverified", "partial", "source_checked", "confirmed")
LEVEL_LABELS: dict[str, str] = {
    "blocked": "Blocked",
    "none": "No usable address",
    "unverified": "Unverified",
    "partial": "Partial evidence",
    "source_checked": "Official source seen (not confirmed by you)",
    "confirmed": "Confirmed by you",
}

_TRAINEE = re.compile(
    r"\b(fellow|fellowship|resident|residency|trainee|post-?doc|postdoctoral|student|intern)\b",
    re.IGNORECASE,
)
_EMERITUS = re.compile(r"\b(emerit\w*|retired)\b", re.IGNORECASE)
_EMAIL_LEVEL = {
    "official_published": "source_checked",
    "official_program_mailbox": "source_checked",
    "publication_attribution_observed": "partial",
    "secondary_directory_observed": "partial",
    "claim_only": "unverified",
    "not_published_or_not_reverified": "unverified",
    "missing": "none",
}
#: A "confirmed current" check returns to the queue after this many days (owner, R3).
CONFIRMATION_VALID_DAYS = 90
FRESHNESS = ("recent", "aging", "stale", "unknown")
RECENT_DAYS = 30
AGING_DAYS = 180


@dataclass(frozen=True)
class Facts:
    """The fields confidence depends on, copied from a staged record."""

    record_type: str
    contact_kind: str
    role: str
    role_status: str
    verification_status: str
    email: str
    email_status: str
    email_is_shared: bool | None
    excluded_reason: str | None
    local_verification: str
    evidence_count: int
    hiring_authority: str = ""
    freshness: str = "unknown"
    local_verified_on: date | None = None
    confirmation_expired: bool = False


@dataclass(frozen=True)
class Confidence:
    level: str
    person: str
    email: str
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LEVEL_LABELS[self.level]


def _rank(level: str) -> int:
    return LEVELS.index(level)


def person_level(facts: Facts) -> tuple[str, str]:
    if facts.local_verification in BLOCKING_VERIFICATIONS:
        return "blocked", LOCAL_VERIFICATIONS[facts.local_verification]
    if facts.local_verification == "confirmed_current":
        if facts.confirmation_expired:
            when = facts.local_verified_on.isoformat() if facts.local_verified_on else "earlier"
            return "source_checked", (
                f"You confirmed this on {when}, over {CONFIRMATION_VALID_DAYS} days ago: "
                "confirm again"
            )
        return "confirmed", LOCAL_VERIFICATIONS["confirmed_current"]
    if facts.role_status == "official_source_checked":
        return "source_checked", "Role observed on an official source (seed)"
    if facts.verification_status == "partial_evidence":
        return "partial", "Some evidence for the person, role not checked on an official source"
    return "unverified", "Current role not verified; only imported claims"


def email_level(facts: Facts, person: str) -> tuple[str, str]:
    if not facts.email:
        if facts.record_type == "opportunity":
            # A program is reached through its application route, judged with the role.
            return person, "No email: use the official application route in the record"
        if facts.email_status == "claim_only":
            return "none", "Only claimed addresses (unverified); never used as a recipient"
        return "none", "No email address"
    level = _EMAIL_LEVEL.get(facts.email_status, "unverified")
    reason = f"Email status: {facts.email_status or 'unknown'}"
    if level == "source_checked" and person == "confirmed" and not facts.email_is_shared:
        return "confirmed", reason + "; person confirmed by you"
    return level, reason


def confidence(facts: Facts) -> Confidence:
    person, person_reason = person_level(facts)
    email, email_reason = email_level(facts, person)
    reasons = [person_reason, email_reason]
    level = min(person, email, key=_rank)
    if facts.excluded_reason:
        level = "blocked"
        reasons.insert(0, f"Out of scope: {facts.excluded_reason}")
    return Confidence(level, person, email, reasons, warnings(facts))


def warnings(facts: Facts) -> list[str]:
    """Signals that the person may not be who the record implies (applicant review)."""
    found: list[str] = []
    if _TRAINEE.search(facts.role) or facts.contact_kind in {"fellow", "trainee"}:
        found.append(
            "Role text suggests a trainee (fellow/resident/postdoc), not an independent PI"
        )
    if _EMERITUS.search(facts.role) or facts.contact_kind == "emeritus":
        found.append("Emeritus or retired: may not take new trainees")
    if facts.contact_kind == "author_unverified":
        found.append(
            "Known only as a publication author: authors include fellows, residents and "
            "people who have since moved"
        )
    if facts.contact_kind == "research_contact_unverified":
        found.append("Research contact from imported data; role not established")
    if facts.contact_kind in {"program_contact", "shared_mailbox"} or facts.record_type == (
        "opportunity"
    ):
        found.append("Program or application route, not a PI: do not personalize as a lab PI")
    if facts.email_is_shared:
        found.append("Shared mailbox: not a personal address")
    if facts.email_status == "publication_attribution_observed":
        found.append("Email taken from a publication; the affiliation may be out of date")
    if facts.evidence_count == 0:
        found.append("No observed evidence: everything about this record is a claim")
    if facts.freshness in {"stale", "unknown"}:
        found.append(f"Source freshness: {facts.freshness}")
    if facts.record_type == "contact" and facts.hiring_authority in {"", "not_established"}:
        found.append("Hiring authority not established")
    return found


def confirmation_expired(verification: str, verified_on: date | None, today: date) -> bool:
    """A confirmation without a date, or older than the limit, has expired."""
    if verification != "confirmed_current":
        return False
    return verified_on is None or (today - verified_on).days > CONFIRMATION_VALID_DAYS


# --------------------------------------------------------------------------- #
# Verification queue order (owner, R2: most sendable first)
# --------------------------------------------------------------------------- #

QUEUE_TIERS: dict[int, str] = {
    1: "Faculty with role and email seen on an official source",
    2: "Partial evidence",
    3: "Opportunities and program routes",
    4: "Claims only (publication authors, research contacts, no address)",
}


def queue_tier(facts: Facts) -> int:
    if facts.record_type == "opportunity" or facts.contact_kind in {
        "program_contact",
        "shared_mailbox",
    }:
        return 3
    email = _EMAIL_LEVEL.get(facts.email_status, "unverified") if facts.email else "none"
    official_role = facts.role_status == "official_source_checked"
    if (
        official_role
        and email == "source_checked"
        and not facts.email_is_shared
        and facts.contact_kind not in {"author_unverified", "research_contact_unverified"}
    ):
        return 1
    if facts.email and (
        official_role
        or email in {"source_checked", "partial"}
        or facts.verification_status == "partial_evidence"
    ):
        return 2
    return 4


def freshness(observed_on: date | None, today: date) -> str:
    if observed_on is None:
        return "unknown"
    age = (today - observed_on).days
    if age <= RECENT_DAYS:
        return "recent"
    if age <= AGING_DAYS:
        return "aging"
    return "stale"


def parse_iso(value: str | None) -> date | None:
    """Lenient ISO date for evidence ``checked_on`` (``YYYY-MM-DD`` prefix); else None."""
    if not value:
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def is_web_url(value: str | None) -> bool:
    """Only http(s) URLs are rendered as links; anything else is shown as text."""
    return bool(value) and str(value).lower().startswith(("https://", "http://"))
