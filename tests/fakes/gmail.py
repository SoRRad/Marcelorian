"""Fake Gmail transport for tests: no network, scripted outcomes, full call capture.

It emulates the parts of Gmail the gateway relies on: message/thread ids, the
SENT label, header preservation (or rewriting, to test the fallback), the
three query shapes built by ``marcelorian.domain.gmail_params``, and the
failure modes that matter for safety (definite rejection, timeouts before or
after acceptance, process death after acceptance, failed/incomplete searches).
"""

from __future__ import annotations

import itertools
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email import message_from_bytes
from email.policy import default as default_policy
from email.utils import getaddresses

from marcelorian.clock import Clock
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.transport.base import (
    AccountInfo,
    AmbiguousOutcome,
    DefiniteRejection,
    MessageMeta,
    SearchResult,
    SendResult,
    TransportKind,
    TransportUnavailable,
)


class SimulatedCrash(BaseException):
    """Raised to model the process dying at a precise point (not caught by the gateway)."""


@dataclass
class StoredMessage:
    meta: MessageMeta
    raw: bytes | None = None


@dataclass
class FakeGmailTransport:
    clock: Clock
    account_email: str = "applicant@example.test"
    scopes: frozenset[str] = REQUIRED_SCOPES
    rewrite_message_id: bool = False
    drop_internal_date: bool = False
    kind: TransportKind = TransportKind.GMAIL
    send_script: list[str] = field(default_factory=list)
    search_script: list[str] = field(default_factory=list)
    account_error: Exception | None = None
    on_account: Callable[[], None] | None = None
    on_search: Callable[[str], None] | None = None
    on_send: Callable[[bytes], None] | None = None
    send_calls: list[bytes] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    thread_requests: list[str] = field(default_factory=list)
    metadata_requests: list[str] = field(default_factory=list)
    store: dict[str, StoredMessage] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # -- helpers for tests -------------------------------------------------
    def _next_id(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids):05d}"

    def inject(
        self,
        *,
        sender: str,
        to: str,
        thread_id: str | None = None,
        labels: frozenset[str] = frozenset({"INBOX"}),
        when: datetime | None = None,
        headers: dict[str, str] | None = None,
    ) -> MessageMeta:
        """Place a message in the fake mailbox (an inbound reply, prior mail, a draft...)."""
        with self._lock:
            gid = self._next_id("in")
            hdrs = {"from": sender, "to": to}
            hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
            meta = MessageMeta(
                gmail_message_id=gid,
                thread_id=thread_id or self._next_id("t"),
                internal_date=when or self.clock.now(),
                label_ids=labels,
                headers=hdrs,
            )
            self.store[gid] = StoredMessage(meta)
            return meta

    def sent_messages(self) -> list[StoredMessage]:
        return [m for m in self.store.values() if "SENT" in m.meta.label_ids]

    # -- MailTransport ----------------------------------------------------------
    def account(self) -> AccountInfo:
        if self.on_account:
            self.on_account()
        if self.account_error is not None:
            raise self.account_error
        return AccountInfo(email=self.account_email, scopes=self.scopes)

    def _store_sent(self, raw: bytes, thread_id: str | None) -> SendResult:
        parsed = message_from_bytes(raw, policy=default_policy)
        headers = {k.lower(): str(v) for k, v in parsed.items()}
        if self.rewrite_message_id:
            headers["message-id"] = f"<rewritten-{next(self._ids)}@mail.gmail.test>"
        with self._lock:
            gid = self._next_id("s")
            tid = thread_id or self._next_id("t")
            self.store[gid] = StoredMessage(
                MessageMeta(
                    gmail_message_id=gid,
                    thread_id=tid,
                    internal_date=None if self.drop_internal_date else self.clock.now(),
                    label_ids=frozenset({"SENT"}),
                    headers=headers,
                ),
                raw,
            )
        return SendResult(gmail_message_id=gid, gmail_thread_id=tid)

    def send(self, raw_mime: bytes, thread_id: str | None) -> SendResult:
        self.send_calls.append(raw_mime)
        if self.on_send:
            self.on_send(raw_mime)
        action = self.send_script.pop(0) if self.send_script else "ok"
        if action == "ok":
            return self._store_sent(raw_mime, thread_id)
        if action.startswith("definite:"):
            status = int(action.split(":")[1])
            raise DefiniteRejection(status, "invalidArgument", auth_failure=status == 401)
        if action == "timeout_before_accept":
            raise AmbiguousOutcome("timed out before the request reached Gmail")
        if action == "timeout_after_accept":
            self._store_sent(raw_mime, thread_id)
            raise AmbiguousOutcome("timed out waiting for the response")
        if action == "server_error_after_accept":
            self._store_sent(raw_mime, thread_id)
            raise AmbiguousOutcome("HTTP 503")
        if action == "crash_after_accept":
            self._store_sent(raw_mime, thread_id)
            raise SimulatedCrash("process died after Gmail accepted the message")
        if action == "crash_before_send":
            raise SimulatedCrash("process died before the request was sent")
        raise AssertionError(f"unknown send action {action}")

    def search_message_ids(self, query: str, *, include_spam_trash: bool) -> SearchResult:
        self.search_queries.append(query)
        if self.on_search:
            self.on_search(query)
        action = self.search_script.pop(0) if self.search_script else "ok"
        if action == "fail":
            raise TransportUnavailable("search failed")
        with self._lock:
            ids = tuple(
                gid
                for gid, stored in self.store.items()
                if _matches(query, stored.meta, include_spam_trash)
            )
        return SearchResult(ids=ids, complete=action != "incomplete")

    def get_message_metadata(self, gmail_message_id: str) -> MessageMeta:
        self.metadata_requests.append(gmail_message_id)
        return self.store[gmail_message_id].meta

    def get_thread_metadata(self, thread_id: str) -> list[MessageMeta]:
        self.thread_requests.append(thread_id)
        with self._lock:
            return [s.meta for s in self.store.values() if s.meta.thread_id == thread_id]


_FIELD = re.compile(r'(from|to|cc|bcc):"([^"]+)"')
_AFTER = re.compile(r"after:(\d+)")


def _addresses(meta: MessageMeta, header: str) -> set[str]:
    return {a.lower() for _n, a in getaddresses([meta.header(header) or ""]) if a}


def _matches(query: str, meta: MessageMeta, include_spam_trash: bool) -> bool:
    if not include_spam_trash and meta.label_ids & {"SPAM", "TRASH"}:
        return False
    if query.startswith("rfc822msgid:"):
        wanted = query.removeprefix("rfc822msgid:")
        return (meta.header("message-id") or "").strip().strip("<>") == wanted
    if "-in:drafts" in query and "DRAFT" in meta.label_ids:
        return False
    after = _AFTER.search(query)
    if after and (
        meta.internal_date is None or meta.internal_date.timestamp() < int(after.group(1))
    ):
        return False
    for field_name, address in _FIELD.findall(query):
        if address.lower() in _addresses(meta, field_name):
            return True
    return False
