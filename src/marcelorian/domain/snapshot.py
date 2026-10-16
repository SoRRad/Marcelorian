"""Immutable approval snapshots (spec §9.1; decisions B2, B12, B19).

The canonical form is deterministic JSON. Its SHA-256 is stored at approval
and rechecked at the atomic claim and before MIME construction. Attachment
bytes are bound through their SHA-256 values in the canonical form; the bytes
themselves live in a content-addressed blob table and are re-hashed before
sending.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from marcelorian.clock import ensure_utc
from marcelorian.domain.enums import Mode
from marcelorian.domain.subjects import expected_subject

SCHEMA = "marcelorian.snapshot.v1"
_SHA256_HEX = frozenset("0123456789abcdef")


class SnapshotError(ValueError):
    """The snapshot content violates a binding rule; approval is refused."""


@dataclass(frozen=True)
class AttachmentRef:
    filename: str
    content_type: str
    sha256: str
    size: int
    cv_version_id: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "sha256": self.sha256,
            "size": self.size,
            "cv_version_id": self.cv_version_id,
        }


@dataclass(frozen=True)
class TestOverride:
    """Transport-test schedule override (decision B2). Never valid in live."""

    __test__ = False  # not a pytest test class despite the name

    window_always_open: bool
    step_intervals_seconds: tuple[tuple[int, int], ...] = ()

    def intervals(self) -> dict[int, int]:
        return dict(self.step_intervals_seconds)

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_always_open": self.window_always_open,
            "step_intervals_seconds": [list(p) for p in self.step_intervals_seconds],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TestOverride | None:
        if data is None:
            return None
        pairs = tuple(sorted((int(s), int(v)) for s, v in data.get("step_intervals_seconds", [])))
        return cls(bool(data["window_always_open"]), pairs)


@dataclass(frozen=True)
class SnapshotContent:
    campaign_id: int
    message_id: int
    step: int
    mode: Mode
    version: int
    sender: str
    to: str
    intended_recipient: str
    pi_id: int
    pi_display_name: str
    pi_is_synthetic: bool
    subject: str
    body: str
    attachments: tuple[AttachmentRef, ...]
    thread_id: str | None
    in_reply_to: str | None
    references: tuple[str, ...]
    pi_timezone: str
    test_override: TestOverride | None
    approved_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "campaign_id": self.campaign_id,
            "message_id": self.message_id,
            "step": self.step,
            "mode": self.mode.value,
            "version": self.version,
            "sender": self.sender,
            "to": [self.to],  # exactly one To, no Cc/Bcc in v1
            "cc": [],
            "bcc": [],
            "intended_recipient": self.intended_recipient,
            "pi_id": self.pi_id,
            "pi_display_name": self.pi_display_name,
            "pi_is_synthetic": self.pi_is_synthetic,
            "subject": self.subject,
            "body": self.body,
            "attachments": [a.to_dict() for a in self.attachments],
            "thread_id": self.thread_id,
            "in_reply_to": self.in_reply_to,
            "references": list(self.references),
            "pi_timezone": self.pi_timezone,
            "test_override": self.test_override.to_dict() if self.test_override else None,
            "approved_at": ensure_utc(self.approved_at).isoformat(),
        }


def canonical_json(content: SnapshotContent) -> str:
    return json.dumps(content.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def parse_canonical(text: str) -> SnapshotContent:
    data = json.loads(text)
    if data.get("schema") != SCHEMA:
        raise SnapshotError("unknown snapshot schema")
    if len(data["to"]) != 1 or data["cc"] or data["bcc"]:
        raise SnapshotError("a snapshot has exactly one To and no Cc/Bcc")
    return SnapshotContent(
        campaign_id=int(data["campaign_id"]),
        message_id=int(data["message_id"]),
        step=int(data["step"]),
        mode=Mode(data["mode"]),
        version=int(data["version"]),
        sender=data["sender"],
        to=data["to"][0],
        intended_recipient=data["intended_recipient"],
        pi_id=int(data["pi_id"]),
        pi_display_name=data["pi_display_name"],
        pi_is_synthetic=bool(data["pi_is_synthetic"]),
        subject=data["subject"],
        body=data["body"],
        attachments=tuple(
            AttachmentRef(
                filename=a["filename"],
                content_type=a["content_type"],
                sha256=a["sha256"],
                size=int(a["size"]),
                cv_version_id=a["cv_version_id"],
            )
            for a in data["attachments"]
        ),
        thread_id=data["thread_id"],
        in_reply_to=data["in_reply_to"],
        references=tuple(data["references"]),
        pi_timezone=data["pi_timezone"],
        test_override=TestOverride.from_dict(data["test_override"]),
        approved_at=datetime.fromisoformat(data["approved_at"]),
    )


def verify_canonical(text: str, expected_sha256: str) -> SnapshotContent:
    """Parse a stored canonical snapshot after checking its hash."""
    if sha256_hex(text) != expected_sha256:
        raise SnapshotError("snapshot hash mismatch; the approved content was altered")
    return parse_canonical(text)


def validate_content(content: SnapshotContent, specialty_display_name: str) -> None:
    """Structural binding rules checked before an approval is stored."""
    if content.step not in (1, 2, 3, 4):
        raise SnapshotError("step must be 1-4")
    if content.subject != expected_subject(content.step, specialty_display_name):
        raise SnapshotError("subject is not the exact fixed subject for this step")
    if not content.body.strip():
        raise SnapshotError("body is empty")
    if "\r" in content.to or "\n" in content.to or "," in content.to:
        raise SnapshotError("exactly one recipient address is allowed")
    if content.test_override is not None and content.mode is not Mode.TRANSPORT_TEST:
        raise SnapshotError("a schedule override is only allowed in transport_test")
    if content.mode is not Mode.TRANSPORT_TEST and content.to != content.intended_recipient:
        raise SnapshotError("outside transport_test the actual recipient is the intended recipient")
    if content.mode is Mode.LIVE and content.pi_is_synthetic:
        raise SnapshotError("synthetic identities can never be contacted in live mode")
    if content.step == 1:
        if content.thread_id or content.in_reply_to or content.references:
            raise SnapshotError("Email 1 starts a new thread")
    else:
        if not content.thread_id or not content.in_reply_to or not content.references:
            raise SnapshotError("follow-ups need the campaign thread and reply headers")
        if content.references[-1] != content.in_reply_to:
            raise SnapshotError("In-Reply-To must be the last References entry")
    for attachment in content.attachments:
        if len(attachment.sha256) != 64 or not set(attachment.sha256) <= _SHA256_HEX:
            raise SnapshotError("attachment hash must be a lowercase SHA-256 hex digest")
        if attachment.size <= 0:
            raise SnapshotError("attachment is empty")
