"""Optional keys for the free lookups, and the NCBI contact address (checked 2026-10-22).

* PubMed works without a key (3 requests a second); a free NCBI key raises that to 10.
* OpenAlex works without a key on a small daily budget (about 1,000 searches); a free
  key gives about ten times that. The old ``mailto`` "polite pool" is gone.
* NCBI asks callers to send ``tool`` (always ``marcelorian``) and the developer's
  ``email``, registered with NCBI by the program owner (OWNER_SETUP.md).

Keys live in the Windows Credential Manager, like the Claude key; the database only
records *whether* each one is saved, so page loads never open the credential store.
The contact address is not a secret and is kept in the settings table.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from marcelorian.db.engine import transaction
from marcelorian.db.models import Setting
from marcelorian.domain.enums import Actor
from marcelorian.lookups.enrich import NO_KEYS, Keys
from marcelorian.platform.secrets import SecretStore, SecretStoreUnavailable
from marcelorian.services.audit import audit

#: Credential Manager entry names.
SECRET_NAMES = {"ncbi": "ncbi_api_key", "openalex": "openalex_api_key"}
LABELS = {"ncbi": "PubMed (NCBI)", "openalex": "OpenAlex"}
SAVED_FLAG = "lookup_keys_saved"
EMAIL_SETTING = "ncbi_contact_email"
_KEY_SHAPE = re.compile(r"[A-Za-z0-9_\-.]{16,200}")
_EMAIL_SHAPE = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")


class KeyInputError(ValueError):
    """A key or address the applicant typed is not usable; the message is safe to show."""


def _read(session: Session, key: str, default: object) -> object:
    row = session.get(Setting, key)
    return default if row is None else json.loads(row.value_json)


def _write(session: Session, key: str, value: object, now: datetime) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value_json=json.dumps(value), updated_at=now))
    else:
        row.value_json, row.updated_at = json.dumps(value), now


def saved(session: Session) -> dict[str, bool]:
    flags = _read(session, SAVED_FLAG, {})
    found = flags if isinstance(flags, dict) else {}
    return {name: bool(found.get(name)) for name in SECRET_NAMES}


def contact_email(session: Session) -> str:
    value = _read(session, EMAIL_SETTING, "")
    return value if isinstance(value, str) else ""


def check_and_store(store: SecretStore, name: str, value: str) -> None:
    """Credential store only; call outside a database transaction."""
    if name not in SECRET_NAMES:
        raise KeyInputError("Unknown service.")
    value = value.strip()
    if not _KEY_SHAPE.fullmatch(value):
        raise KeyInputError(f"That does not look like a {LABELS[name]} API key.")
    store.set(SECRET_NAMES[name], value)


def record(session: Session, name: str, is_saved: bool, *, now: datetime) -> None:
    flags = saved(session)
    flags[name] = is_saved
    _write(session, SAVED_FLAG, flags, now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="setting",
        entity_id=SAVED_FLAG,
        action=f"lookup_key_{'saved' if is_saved else 'removed'}",
        detail={"service": name},
    )


def set_contact_email(session: Session, value: str, *, now: datetime) -> None:
    value = value.strip()
    if value and (len(value) > 254 or not _EMAIL_SHAPE.fullmatch(value)):
        raise KeyInputError("Enter an email address, or leave it empty.")
    _write(session, EMAIL_SETTING, value, now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="setting",
        entity_id=EMAIL_SETTING,
        action="ncbi_contact_email_saved" if value else "ncbi_contact_email_removed",
    )


def load(session_factory: sessionmaker[Session], secrets: Callable[[], SecretStore]) -> Keys:
    """The keys for one lookup run. Never fails: without a store, lookups run keyless."""
    with transaction(session_factory) as session:
        flags = saved(session)
        email = contact_email(session)
    values = dict.fromkeys(SECRET_NAMES, "")
    if any(flags.values()):
        try:
            store = secrets()
            for name, is_saved in flags.items():
                if is_saved:
                    values[name] = store.get(SECRET_NAMES[name]) or ""
        except SecretStoreUnavailable:
            pass
    if not email and not any(values.values()):
        return NO_KEYS
    return Keys(ncbi=values["ncbi"], openalex=values["openalex"], ncbi_email=email)
