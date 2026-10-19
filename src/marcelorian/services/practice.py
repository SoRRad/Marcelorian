"""Practice time and the live readiness checklist (spec §12; Outlook-only pilot U3).

Practice time is stored as a forward-only offset (seconds) so a restart keeps it.
Live mode is allowed only after the readiness checklist: the profile is confirmed, a
CV is uploaded, the signature step and the Outlook address are done, and the owner has
recorded that the real Outlook test (P0.3b) passed and the limits were reviewed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from marcelorian.config.settings import load_settings
from marcelorian.db.models import Setting
from marcelorian.domain.enums import Actor
from marcelorian.services import approvals, profile, runtime
from marcelorian.services.audit import audit

OFFSET_KEY = "practice_time_offset_seconds"
ATTESTATIONS = {
    "outlook_test_passed": "The real Outlook test (P0.3b) passed on this computer",
    "limits_reviewed": "I reviewed the sending limits and the send window",
}


def _get(session: Session, key: str, default: object) -> object:
    row = session.get(Setting, key)
    return json.loads(row.value_json) if row is not None else default


def _put(session: Session, key: str, value: object, now: datetime) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value_json=json.dumps(value), updated_at=now))
    else:
        row.value_json, row.updated_at = json.dumps(value), now


def load_offset(session: Session) -> timedelta:
    value = _get(session, OFFSET_KEY, 0)
    seconds = value if isinstance(value, int | float) and value >= 0 else 0
    return timedelta(seconds=seconds)


def save_offset(session: Session, offset: timedelta, *, now: datetime) -> None:
    if offset < load_offset(session):
        raise ValueError("practice time never moves backwards")
    _put(session, OFFSET_KEY, int(offset.total_seconds()), now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="practice",
        entity_id="clock",
        action="practice_time_advanced",
        detail={"offset_seconds": int(offset.total_seconds())},
        mode="sandbox",
    )


@dataclass(frozen=True)
class ReadinessItem:
    key: str
    label: str
    done: bool
    attestation: bool = False


def readiness(session: Session, now: datetime) -> list[ReadinessItem]:
    me = profile.applicant(session, now)
    settings = load_settings(session)
    items = [
        ReadinessItem(
            "profile", "Profile answers confirmed", not profile.missing_required(session, me.id)
        ),
        ReadinessItem("cv", "CV uploaded", bool(profile.cv_versions(session, me.id))),
        ReadinessItem("signature", "Signature set up (or skipped)", settings.signature_step_done),
        ReadinessItem(
            "outlook", "Outlook address saved", approvals._active_mailbox(session) is not None
        ),
    ]
    for key, label in ATTESTATIONS.items():
        items.append(ReadinessItem(key, label, bool(_get(session, key, False)), attestation=True))
    return items


def set_attestation(session: Session, key: str, value: bool, *, now: datetime) -> None:
    if key not in ATTESTATIONS:
        raise ValueError("unknown checklist item")
    _put(session, key, value, now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="readiness",
        entity_id=key,
        action="attested" if value else "attestation_withdrawn",
    )
    complete = all(item.done for item in readiness(session, now))
    runtime.set_live_readiness(session, complete, actor=Actor.APPLICANT, now=now)


def refresh_readiness(session: Session, now: datetime) -> bool:
    complete = all(item.done for item in readiness(session, now))
    runtime.set_live_readiness(session, complete, actor=Actor.APPLICANT, now=now)
    return complete
