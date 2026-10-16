"""Mode and recipient validation (spec §9.6, §12; decisions B2, B11, B20).

Pure function used both at approval and in the gateway preflight/claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from marcelorian.domain.enums import Mode

_ADDRESS = re.compile(r"^[^@\s,;<>\"]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")


class RecipientError(ValueError):
    pass


def normalize_address(address: str) -> str:
    cleaned = address.strip().lower()
    if not _ADDRESS.match(cleaned):
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
