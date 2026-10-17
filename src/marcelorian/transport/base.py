"""Mail transport interface used by the single send gateway.

Implementations:
* ``SandboxTransport`` (production sandbox mode; fake, no network).
* The Gmail adapter (P0.3, after authorization). It must make exactly one
  ``messages.send`` call per ``send()`` with automatic retries disabled, and
  must build every read request with ``marcelorian.domain.gmail_params``.

Contract points the gateway relies on (DECISIONS notes 28, 31, 32):

* ``send()`` performs no network I/O before its single request (credentials are
  refreshed during the preflight ``account()`` call), so the gateway's final
  clock check really is immediately before the request. ``SendResult`` carries
  ids only; the app never assumes the submitted Message-ID survived.
* ``get_message_metadata()`` returns the headers the provider actually stored
  (lower-cased ``message-id``, ``x-marcelorian-id``, ``to``), using
  ``format=metadata``, and raises on any failure.
* ``account().email`` is the provider-reported primary address; its canonical
  form is the mailbox identity key.
* Test doubles under ``tests/fakes``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class TransportKind(StrEnum):
    SANDBOX = "sandbox"
    GMAIL = "gmail"


@dataclass(frozen=True)
class AccountInfo:
    email: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class SendResult:
    gmail_message_id: str
    gmail_thread_id: str


@dataclass(frozen=True)
class MessageMeta:
    """Allowlisted metadata only; never bodies, snippets or attachments."""

    gmail_message_id: str
    thread_id: str
    internal_date: datetime | None
    label_ids: frozenset[str] = field(default_factory=frozenset)
    headers: Mapping[str, str] = field(default_factory=dict)  # lower-cased header names

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


@dataclass(frozen=True)
class SearchResult:
    ids: tuple[str, ...]
    complete: bool  # False if any page failed or pagination was cut short


class DefiniteRejection(Exception):
    """The provider proved it did not accept the submission."""

    def __init__(self, status: int, reason: str | None, *, auth_failure: bool = False) -> None:
        super().__init__(f"definite rejection {status} {reason or ''}".strip())
        self.status = status
        self.reason = reason
        self.auth_failure = auth_failure


class AmbiguousOutcome(Exception):
    """Timeout, connection loss, 5xx or anything else that does not prove non-acceptance."""


class TransportUnavailable(Exception):
    """A read-only call (preflight, reconciliation search) could not be completed."""


class MailTransport(Protocol):
    kind: TransportKind

    def account(self) -> AccountInfo: ...

    def send(self, raw_mime: bytes, thread_id: str | None) -> SendResult: ...

    def search_message_ids(self, query: str, *, include_spam_trash: bool) -> SearchResult: ...

    def get_message_metadata(self, gmail_message_id: str) -> MessageMeta: ...

    def get_thread_metadata(self, thread_id: str) -> list[MessageMeta]: ...
