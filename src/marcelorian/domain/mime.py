"""MIME construction from snapshot bytes only (spec §9.6 step 3; decision B15).

Plain-text body, no tracking, no HTML. The only inputs are the verified
snapshot, the attachment bytes (re-hashed here) and the per-attempt
identifiers. Never Cc/Bcc.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from email.headerregistry import AddressHeader
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from marcelorian.clock import ensure_utc
from marcelorian.domain.recipients import RecipientError, normalize_address
from marcelorian.domain.snapshot import SnapshotContent, SnapshotError, sha256_hex


class MimeAddressError(SnapshotError):
    """The rendered address header would not deliver to exactly the approved mailbox."""


def _single_mailbox(msg: EmailMessage, header: str, expected: str) -> None:
    """Re-parse a rendered header: exactly one plain mailbox, no group, equal to ``expected``."""
    values = msg.get_all(header) or []
    if len(values) != 1:
        raise MimeAddressError(f"{header} must appear exactly once")
    parsed = values[0]
    if not isinstance(parsed, AddressHeader):
        raise MimeAddressError(f"{header} is not an address header")
    groups = parsed.groups
    if len(groups) != 1 or groups[0].display_name is not None or len(groups[0].addresses) != 1:
        raise MimeAddressError(f"{header} must hold exactly one mailbox and no group")
    address = groups[0].addresses[0]
    if address.display_name:
        raise MimeAddressError(f"{header} must not carry a display name")
    try:
        delivered = normalize_address(address.addr_spec)
        approved = normalize_address(expected)
    except RecipientError as exc:
        raise MimeAddressError(f"{header} is not a single plain email address") from exc
    if delivered != approved:
        raise MimeAddressError(f"{header} does not match the approved address")


def build_mime(
    content: SnapshotContent,
    attachment_bytes: Mapping[str, bytes],
    *,
    rfc_message_id: str,
    x_marcelorian_id: str,
    date: datetime,
) -> bytes:
    msg = EmailMessage(policy=SMTP)
    msg["From"] = content.sender
    msg["To"] = content.to
    msg["Subject"] = content.subject
    msg["Date"] = format_datetime(ensure_utc(date))
    msg["Message-ID"] = rfc_message_id
    msg["X-Marcelorian-Id"] = x_marcelorian_id
    if content.in_reply_to:
        msg["In-Reply-To"] = content.in_reply_to
        msg["References"] = " ".join(content.references)
    _single_mailbox(msg, "To", content.to)
    _single_mailbox(msg, "From", content.sender)
    for forbidden in ("Cc", "Bcc", "Reply-To"):
        if msg.get(forbidden) is not None:
            raise MimeAddressError(f"{forbidden} is never set")
    msg.set_content(content.body, subtype="plain", charset="utf-8", cte="quoted-printable")
    for ref in content.attachments:
        data = attachment_bytes.get(ref.sha256)
        if data is None or sha256_hex(data) != ref.sha256 or len(data) != ref.size:
            raise SnapshotError("attachment bytes do not match the approved snapshot")
        maintype, _, subtype = ref.content_type.partition("/")
        if not maintype or not subtype:
            raise SnapshotError("attachment content type is invalid")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=ref.filename)
    return msg.as_bytes()
