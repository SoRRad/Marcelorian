"""MIME construction from snapshot bytes only (spec §9.6 step 3; decision B15).

Plain-text body, no tracking, no HTML. The only inputs are the verified
snapshot, the attachment bytes (re-hashed here) and the per-attempt
identifiers. Never Cc/Bcc.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime

from marcelorian.clock import ensure_utc
from marcelorian.domain.snapshot import SnapshotContent, SnapshotError, sha256_hex


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
