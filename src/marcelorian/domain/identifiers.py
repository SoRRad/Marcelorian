"""Opaque correlation identifiers (decision B12).

* The RFC Message-ID is ``<opaque@verified-sender-domain>``. The laptop hostname
  is never used (``email.utils.make_msgid`` would use it, so it is not used).
* ``X-Marcelorian-Id`` is an opaque random value: no applicant name, file path or
  other personal data. It is a visible correlation header, not a secret.
* A new pair is generated for every separately authorized attempt and is kept
  for that attempt's whole reconciliation.
"""

from __future__ import annotations

import re
import secrets

_DOMAIN = re.compile(
    r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$"
)


def opaque_token() -> str:
    return secrets.token_hex(16)


def sender_domain(sender_email: str) -> str:
    local, sep, domain = sender_email.strip().rpartition("@")
    domain = domain.lower()
    if not sep or not local or not _DOMAIN.match(domain):
        raise ValueError("sender address has no valid domain for a Message-ID")
    return domain


def rfc_message_id(sender_email: str, token: str | None = None) -> str:
    value = token or opaque_token()
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise ValueError("Message-ID token must be 32 lowercase hex characters")
    return f"<{value}@{sender_domain(sender_email)}>"


def new_x_marcelorian_id() -> str:
    return opaque_token()


def bare_message_id(message_id: str) -> str:
    """``<abc@d>`` -> ``abc@d`` (the form Gmail's ``rfc822msgid:`` search takes)."""
    return message_id.strip().removeprefix("<").removesuffix(">")
