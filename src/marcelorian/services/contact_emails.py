"""Change or add a contact's email address after it was confirmed (owner request 2026-10-27).

* The applicant states where the address is published (as at promotion), which sets its
  confidence (A: the PI's current institutional or lab page; B: a recent publication)
  and attests ownership.
* "Use instead of the current one" retires the old send-ready address through the
  invalidating update (B18): any approved, unsent email to it needs approval again.
* Removing an address marks it invalid (never deleted, so history and the do-not-email-
  twice checks still see it).
* An address that belongs to another contact is refused: one address, one person, so
  nobody is emailed twice under two names.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Pi, PiEmail
from marcelorian.domain.enums import Actor
from marcelorian.services import approvals
from marcelorian.services.audit import audit
from marcelorian.services.promotion import EMAIL_SOURCES
from marcelorian.services.recipients import send_ready_email


class AddressError(ValueError):
    """The address cannot be used; the message is safe to show."""


def _canonical(address: str) -> str:
    text = address.strip().lower()
    local, sep, domain = text.rpartition("@")
    if not sep or not local or "." not in domain or any(c.isspace() for c in text):
        raise AddressError("Enter one email address, like name@university.edu.")
    if len(text) > 320 or "," in text or ";" in text:
        raise AddressError("Enter one email address, like name@university.edu.")
    return text


def add_address(
    session: Session,
    pi_id: int,
    address: str,
    *,
    source: str,
    source_url: str,
    replace_current: bool,
    now: datetime,
) -> PiEmail:
    pi = session.get(Pi, pi_id)
    if pi is None:
        raise AddressError("Unknown contact.")
    if source not in EMAIL_SOURCES:
        raise AddressError("Choose where the address is published.")
    if not source_url.strip():
        raise AddressError("Paste the page where you found the address.")
    canonical = _canonical(address)
    owner = session.scalar(
        select(PiEmail).where(PiEmail.address == canonical, PiEmail.pi_id != pi_id)
    )
    if owner is not None:
        other = session.get(Pi, owner.pi_id)
        raise AddressError(
            f"That address already belongs to {other.full_name if other else 'another contact'}."
        )
    confidence, label = EMAIL_SOURCES[source]
    previous = send_ready_email(session, pi_id)
    email = session.scalar(
        select(PiEmail).where(PiEmail.pi_id == pi_id, PiEmail.address == canonical)
    )
    if email is None:
        email = PiEmail(
            pi_id=pi_id,
            address=canonical,
            confidence=confidence,
            source=source_url.strip(),
            created_at=now,
        )
        session.add(email)
        session.flush()
    else:
        approvals.update_pi_email(
            session,
            email,
            actor=Actor.APPLICANT,
            now=now,
            invalid=False,
            confidence=confidence,
            ownership_verified=True,
        )
        email.invalid_reason = None
    email.ownership_verified = True
    email.verified_by = str(Actor.APPLICANT)
    email.verified_at = now
    email.verification_evidence = f"{label}: {source_url.strip()}"
    if replace_current and previous is not None and previous.id != email.id:
        retire(session, pi_id, previous.id, reason="replaced by the applicant", now=now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="pi_email",
        entity_id=email.id,
        action="address_added",
        detail={"pi_id": pi_id, "confidence": confidence, "replaced": bool(replace_current)},
    )
    session.flush()
    return email


def retire(session: Session, pi_id: int, email_id: int, *, reason: str, now: datetime) -> None:
    email = session.get(PiEmail, email_id)
    if email is None or email.pi_id != pi_id:
        raise AddressError("Unknown address.")
    approvals.update_pi_email(session, email, actor=Actor.APPLICANT, now=now, invalid=True)
    email.invalid_reason = reason
