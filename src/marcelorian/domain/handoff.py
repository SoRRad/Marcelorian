"""What the applicant receives for an Outlook hand-off (spec §9.8; decisions S3, S6).

Built only from the verified approval snapshot, like the Gmail MIME:

* a link that opens a pre-filled new message in Outlook on the web (no signature:
  Outlook adds the account's own), or ``None`` when the URL would be too long;
* an ``.eml`` file Outlook opens as an editable unsent message (``X-Unsent: 1``),
  with the approved To, subject, body, signature setting and attachments, and no
  From/Date/Message-ID (Outlook supplies them for the signed-in account);
* the same To, subject and body as copy text.

Follow-ups keep their thread only when the applicant replies to the earlier email in
Outlook and pastes the body; the link and file carry the ``Re:`` subject but start a
new thread (owner test pending, P0.3b).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage
from email.policy import SMTP
from urllib.parse import quote, urlencode

from marcelorian.domain.mime import MimeAddressError, _single_mailbox
from marcelorian.domain.snapshot import SnapshotContent, SnapshotError, sha256_hex

OUTLOOK_COMPOSE_URL = "https://outlook.office.com/mail/deeplink/compose"
#: Browsers and Outlook truncate long URLs; beyond this only the file and copy text work.
MAX_LINK_CHARS = 2000


@dataclass(frozen=True)
class HandoffPackage:
    to: str
    subject: str
    #: The approved body with the signature setting appended (file and copy text).
    body: str
    #: Opens a pre-filled new message in Outlook on the web; None when too long.
    compose_url: str | None
    eml: bytes
    eml_filename: str
    #: The link cannot carry attachments; the applicant attaches them in Outlook.
    link_omits_attachments: bool
    #: A follow-up: reply to the earlier email in Outlook and paste the body (§9.7).
    reply_in_outlook: bool


def body_with_signature(body: str, signature: str) -> str:
    sig = signature.strip("\n")
    if not sig:
        return body
    return body.rstrip("\n") + "\n\n" + sig + "\n"


def compose_url(to: str, subject: str, body: str) -> str | None:
    query = urlencode({"to": to, "subject": subject, "body": body}, quote_via=quote)
    url = f"{OUTLOOK_COMPOSE_URL}?{query}"
    return url if len(url) <= MAX_LINK_CHARS else None


def build_eml(
    content: SnapshotContent, attachment_bytes: Mapping[str, bytes], signature: str
) -> bytes:
    msg = EmailMessage(policy=SMTP)  # refuses CR/LF in header values
    msg["X-Unsent"] = "1"
    msg["To"] = content.to
    msg["Subject"] = content.subject
    _single_mailbox(msg, "To", content.to)
    for forbidden in ("From", "Cc", "Bcc", "Reply-To"):
        if msg.get(forbidden) is not None:
            raise MimeAddressError(f"{forbidden} is never set")
    msg.set_content(
        body_with_signature(content.body, signature),
        subtype="plain",
        charset="utf-8",
        cte="quoted-printable",
    )
    for ref in content.attachments:
        data = attachment_bytes.get(ref.sha256)
        if data is None or sha256_hex(data) != ref.sha256 or len(data) != ref.size:
            raise SnapshotError("attachment bytes do not match the approved snapshot")
        maintype, _, subtype = ref.content_type.partition("/")
        if not maintype or not subtype:
            raise SnapshotError("attachment content type is invalid")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=ref.filename)
    if msg.is_multipart():
        # Deterministic, so reopening a hand-off yields the identical file.
        boundary = (
            "marcelorian-"
            + sha256_hex(
                "\n".join(
                    [
                        content.subject,
                        content.body,
                        signature,
                        *(a.sha256 for a in content.attachments),
                    ]
                )
            )[:40]
        )
        if boundary in content.body or boundary in signature:
            raise SnapshotError("the email text collides with the MIME boundary")
        msg.set_boundary(boundary)
    return msg.as_bytes()


def build_package(
    content: SnapshotContent, attachment_bytes: Mapping[str, bytes], signature: str
) -> HandoffPackage:
    return HandoffPackage(
        to=content.to,
        subject=content.subject,
        body=body_with_signature(content.body, signature),
        compose_url=compose_url(content.to, content.subject, content.body),
        eml=build_eml(content, attachment_bytes, signature),
        # No names or addresses in the file name: it lands in the Downloads folder.
        eml_filename=f"marcelorian-email{content.step}-{content.message_id}.eml",
        link_omits_attachments=bool(content.attachments),
        reply_in_outlook=content.step > 1,
    )
