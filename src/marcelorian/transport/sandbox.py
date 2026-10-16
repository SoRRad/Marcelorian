"""Sandbox transport and sync: a fake mailbox, no network (spec §12).

Sandbox runs the same gateway, caps, states and scheduling as live; it is
isolated by its records, not by disabled checks. The in-memory mailbox here
is sufficient for P0; P2 adds persistence and the simulate-reply buttons.
"""

from __future__ import annotations

import itertools
import threading
from email import message_from_bytes
from email.policy import default as default_policy

from marcelorian.clock import Clock
from marcelorian.transport.base import (
    AccountInfo,
    MessageMeta,
    SearchResult,
    SendResult,
    TransportKind,
)

SANDBOX_SENDER = "sandbox-sender@marcelorian.invalid"


class SandboxTransport:
    kind = TransportKind.SANDBOX

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.messages: dict[str, MessageMeta] = {}

    def account(self) -> AccountInfo:
        return AccountInfo(email=SANDBOX_SENDER, scopes=frozenset())

    def send(self, raw_mime: bytes, thread_id: str | None) -> SendResult:
        parsed = message_from_bytes(raw_mime, policy=default_policy)
        with self._lock:
            number = next(self._ids)
            gid = f"sbx-msg-{number}"
            tid = thread_id or f"sbx-thr-{number}"
            headers = {k.lower(): str(v) for k, v in parsed.items()}
            self.messages[gid] = MessageMeta(
                gmail_message_id=gid,
                thread_id=tid,
                internal_date=self._clock.now(),
                label_ids=frozenset({"SENT"}),
                headers=headers,
            )
        return SendResult(gmail_message_id=gid, gmail_thread_id=tid)

    def search_message_ids(self, query: str, *, include_spam_trash: bool) -> SearchResult:
        # Sandbox has no pre-existing mail; only reconciliation lookups apply.
        if query.startswith("rfc822msgid:"):
            wanted = query.removeprefix("rfc822msgid:")
            with self._lock:
                ids = tuple(
                    m.gmail_message_id
                    for m in self.messages.values()
                    if (m.header("message-id") or "").strip("<>") == wanted
                )
            return SearchResult(ids=ids, complete=True)
        return SearchResult(ids=(), complete=True)

    def get_message_metadata(self, gmail_message_id: str) -> MessageMeta:
        with self._lock:
            return self.messages[gmail_message_id]

    def get_thread_metadata(self, thread_id: str) -> list[MessageMeta]:
        with self._lock:
            return [m for m in self.messages.values() if m.thread_id == thread_id]
