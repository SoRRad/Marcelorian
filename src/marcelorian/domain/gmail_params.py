"""Gmail request parameter builders: metadata only (spec §10; decisions B12, B14).

These pure builders are what the real Gmail adapter (P0.3) must use, so the
metadata-only rule can be unit-tested by inspecting parameters. Nothing here
performs network access. Documentation for ``users.messages.get`` /
``users.threads.get`` (format=metadata, metadataHeaders, fields) to be
rechecked against Google's docs at the P0.3 gate before the first live call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from marcelorian.domain.identifiers import bare_message_id

#: Header allowlist (spec §10, plus X-Marcelorian-Id per B12).
METADATA_HEADERS: tuple[str, ...] = (
    "From",
    "To",
    "Cc",
    "Date",
    "Subject",
    "Message-ID",
    "In-Reply-To",
    "References",
    "Auto-Submitted",
    "X-Autoreply",
    "X-Autorespond",
    "Precedence",
    "Content-Type",
    "X-Marcelorian-Id",
)

#: Partial-response masks: ids, labels, dates and allowlisted headers only.
MESSAGE_FIELDS = "id,threadId,labelIds,internalDate,historyId,payload/headers"
THREAD_FIELDS = "id,historyId,messages(id,threadId,labelIds,internalDate,payload/headers)"
LIST_FIELDS = "messages(id,threadId),nextPageToken,resultSizeEstimate"

REQUIRED_SCOPES: frozenset[str] = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)

_FORBIDDEN_FIELD_TOKENS = ("snippet", "raw", "body", "parts", "attachment")


class MetadataPolicyError(ValueError):
    """A Gmail read request would fetch content beyond permitted metadata."""


def message_get_params(gmail_message_id: str) -> dict[str, Any]:
    return {
        "userId": "me",
        "id": gmail_message_id,
        "format": "metadata",
        "metadataHeaders": list(METADATA_HEADERS),
        "fields": MESSAGE_FIELDS,
    }


def thread_get_params(thread_id: str) -> dict[str, Any]:
    return {
        "userId": "me",
        "id": thread_id,
        "format": "metadata",
        "metadataHeaders": list(METADATA_HEADERS),
        "fields": THREAD_FIELDS,
    }


def message_list_params(query: str, page_token: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {
        "userId": "me",
        "q": query,
        "includeSpamTrash": True,
        "fields": LIST_FIELDS,
        "maxResults": 500,
    }
    if page_token:
        params["pageToken"] = page_token
    return params


def assert_metadata_only(params: Mapping[str, Any]) -> None:
    """Reject any read request that could return bodies, snippets or attachments."""
    if "format" in params and params["format"] != "metadata":
        raise MetadataPolicyError("only format=metadata is permitted")
    fields = str(params.get("fields", ""))
    if not fields:
        raise MetadataPolicyError("a fields mask is required")
    lowered = fields.lower()
    for token in _FORBIDDEN_FIELD_TOKENS:
        if token in lowered:
            raise MetadataPolicyError(f"fields mask must not include {token!r}")
    headers = params.get("metadataHeaders")
    if headers is not None and not set(headers) <= set(METADATA_HEADERS):
        raise MetadataPolicyError("metadataHeaders must stay within the allowlist")


def _quote(address: str) -> str:
    cleaned = address.strip().lower()
    if not cleaned or any(c in cleaned for c in ' "()'):
        raise ValueError("addresses in Gmail queries must be plain addresses")
    return f'"{cleaned}"'


def prior_contact_query(addresses: Iterable[str]) -> str:
    """Messages to/from the exact addresses, drafts excluded (B14).

    Spam/trash inclusion is the ``includeSpamTrash`` list parameter.
    """
    clauses: list[str] = []
    for address in sorted({a.strip().lower() for a in addresses}):
        q = _quote(address)
        clauses.extend([f"from:{q}", f"to:{q}", f"cc:{q}", f"bcc:{q}"])
    if not clauses:
        raise ValueError("at least one address is required for a prior-contact search")
    return "(" + " OR ".join(clauses) + ") -in:drafts"


def messages_from_since_query(addresses: Iterable[str], since_epoch_seconds: int) -> str:
    """Possible replies from known identities in *any* thread since the last send (B11)."""
    clauses = [f"from:{_quote(a)}" for a in sorted({a.strip().lower() for a in addresses})]
    if not clauses:
        raise ValueError("at least one address is required")
    return "(" + " OR ".join(clauses) + f") after:{int(since_epoch_seconds)} -in:drafts"


def sent_search_query(rfc_message_id: str) -> str:
    """Reconciliation lookup by our Message-ID across all mail (spam/trash via list param)."""
    return f"rfc822msgid:{bare_message_id(rfc_message_id)}"
