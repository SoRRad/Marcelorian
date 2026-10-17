"""Mode and recipient validation (spec §9.6, §12; decisions B2, B11, B20).

Pure function used both at approval and in the gateway preflight/claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from marcelorian.domain.enums import Mode

# A strict RFC 5322 addr-spec, ASCII only. The local part is a dot-atom built
# from atext minus "=" and "?" (so it can never resemble an RFC 2047 encoded
# word) and minus "%" and "/" (so a percent-encoded or "mailto://" URI remainder
# is rejected instead of being stored as a different mailbox); quoted strings,
# comments, group syntax (":" / ";"), domain literals and non-ASCII are all
# rejected. Every comparison in the app (allowlist,
# real-PI, do-not-contact, duplicate outreach, prior contact) runs on this
# canonical form, and build_mime re-parses the rendered header to confirm the
# delivered mailbox is exactly this address.
_ATEXT = r"[a-z0-9!#$&'*+^_`{|}~-]"
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_ADDRESS = re.compile(
    rf"{_ATEXT}+(?:\.{_ATEXT}+)*@(?:{_LABEL}\.)+[a-z]{{2,63}}",
    re.ASCII,
)
_MAILTO = "mailto:"
MAX_LOCAL_PART = 64
MAX_ADDRESS = 254


class RecipientError(ValueError):
    pass


def normalize_address(address: str) -> str:
    """Return the canonical lower-case addr-spec or raise ``RecipientError``.

    A pasted ``mailto:`` link prefix is removed first; the remainder must then
    be a single plain address with nothing else (no display name, no query).
    """
    stripped = address.strip()
    if not stripped.isascii():
        # Before case folding: str.lower() maps some non-ASCII letters (e.g. U+212A
        # KELVIN SIGN) to ASCII, which would silently rewrite the address.
        raise RecipientError("not a single plain email address")
    cleaned = stripped.lower()
    if cleaned.startswith(_MAILTO):
        cleaned = cleaned[len(_MAILTO) :].strip()
    if (
        len(cleaned) > MAX_ADDRESS
        or len(cleaned.partition("@")[0]) > MAX_LOCAL_PART
        or not _ADDRESS.fullmatch(cleaned)
    ):
        raise RecipientError("not a single plain email address")
    return cleaned


@dataclass(frozen=True)
class RecipientContext:
    mode: Mode
    transport_kind: str  # "gmail" or "sandbox"
    snapshot_to: str
    snapshot_intended: str
    has_test_override: bool
    pi_is_synthetic: bool
    #: The PI's current send-ready address (A/B + ownership + not invalid), if any.
    send_ready_address: str | None
    #: Active allowlist entries (transport_test).
    allowlist: frozenset[str]
    #: The campaign's mapped test recipient (transport_test).
    campaign_test_recipient: str | None
    #: Every address belonging to a non-synthetic PI.
    real_pi_addresses: frozenset[str]


def validate_recipient(ctx: RecipientContext) -> None:
    to = normalize_address(ctx.snapshot_to)
    intended = normalize_address(ctx.snapshot_intended)
    if ctx.mode is Mode.SANDBOX:
        if ctx.transport_kind != "sandbox":
            raise RecipientError("sandbox messages may only use the fake transport")
        if ctx.has_test_override:
            raise RecipientError("schedule overrides exist only in transport_test")
        if ctx.send_ready_address is None or to != ctx.send_ready_address:
            raise RecipientError("sandbox recipient is not the PI's send-ready address")
        return
    if ctx.transport_kind != "gmail":
        raise RecipientError("real modes require the Gmail transport")
    if ctx.mode is Mode.LIVE:
        if ctx.has_test_override:
            raise RecipientError("live mode rejects transport-test overrides")
        if ctx.pi_is_synthetic:
            raise RecipientError("synthetic identities are never contacted in live mode")
        if ctx.send_ready_address is None:
            raise RecipientError("the PI has no send-ready verified address")
        if to != intended or to != ctx.send_ready_address:
            raise RecipientError("live recipient must be the campaign's verified PI address")
        if to in ctx.allowlist:
            raise RecipientError("a transport-test address cannot be a live recipient")
        return
    # transport_test
    if to not in ctx.allowlist:
        raise RecipientError("transport_test recipient is not on the allowlist")
    if to in ctx.real_pi_addresses:
        raise RecipientError("a known real-PI address can never be a test recipient")
    if ctx.campaign_test_recipient is None or to != ctx.campaign_test_recipient:
        raise RecipientError("recipient differs from the campaign's mapped test address")
