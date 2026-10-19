"""Outlook hand-off package and snapshot channel rules (spec §9.8; S3; acceptance 85)."""

from __future__ import annotations

import email
import json
from dataclasses import replace
from email.policy import default as default_policy
from urllib.parse import parse_qs, urlsplit

import pytest

from marcelorian.domain.enums import Channel, Mode
from marcelorian.domain.handoff import (
    MAX_LINK_CHARS,
    OUTLOOK_COMPOSE_URL,
    body_with_signature,
    build_eml,
    build_package,
    compose_url,
)
from marcelorian.domain.snapshot import (
    SnapshotError,
    canonical_json,
    parse_canonical,
    validate_content,
)
from marcelorian.domain.subjects import normalize_reply_subject
from tests.unit.test_domain_pure import CV, CV_REF, content

SIGNATURE = "A. Applicant, MD\nResearch Fellow"


def _parse(raw: bytes) -> email.message.EmailMessage:
    parsed = email.message_from_bytes(raw, policy=default_policy)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


def test_eml_opens_as_an_unsent_message_with_the_approved_fields() -> None:
    c = content()
    parsed = _parse(build_eml(c, {CV_REF.sha256: CV}, SIGNATURE))
    assert parsed["X-Unsent"] == "1"
    for absent in ("From", "Date", "Message-ID", "Cc", "Bcc", "Reply-To", "X-Marcelorian-Id"):
        assert parsed[absent] is None
    assert parsed["To"] == c.to
    assert parsed["Subject"] == "Research Position Application – Ophthalmology"
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None
    # SMTP line endings on the wire; the text is the approved body plus the signature.
    assert body.get_content().replace("\r\n", "\n") == body_with_signature(c.body, SIGNATURE)
    (attachment,) = parsed.iter_attachments()
    assert attachment.get_filename() == "cv.pdf"
    assert attachment.get_content() == CV


def test_eml_is_deterministic_for_reopening() -> None:
    c = content()
    assert build_eml(c, {CV_REF.sha256: CV}, SIGNATURE) == build_eml(
        c, {CV_REF.sha256: CV}, SIGNATURE
    )


def test_eml_refuses_tampered_attachment_bytes() -> None:
    with pytest.raises(SnapshotError):
        build_eml(content(), {CV_REF.sha256: CV + b"x"}, "")


@pytest.mark.parametrize(
    "field,value",
    [
        ("subject", "Research Position Application\r\nBcc: other@example.test"),
        ("to", "pi@synthetic-univ.test\r\nBcc: other@example.test"),
        ("to", "pi@synthetic-univ.test, other@example.test"),
    ],
    ids=["subject-crlf", "to-crlf", "to-two-addresses"],
)
def test_eml_rejects_header_injection(field: str, value: str) -> None:
    with pytest.raises((ValueError, SnapshotError)):
        build_eml(replace(content(attachments=()), **{field: value}), {}, "")  # type: ignore[arg-type]


def test_link_prefills_outlook_without_the_signature() -> None:
    c = content()
    url = compose_url(c.to, c.subject, c.body)
    assert url is not None and url.startswith(OUTLOOK_COMPOSE_URL + "?")
    query = parse_qs(urlsplit(url).query)
    assert query == {"to": [c.to], "subject": [c.subject], "body": [c.body]}


def test_long_email_has_no_link() -> None:
    c = content(body="x" * MAX_LINK_CHARS)
    assert compose_url(c.to, c.subject, c.body) is None
    package = build_package(c, {CV_REF.sha256: CV}, "")
    assert package.compose_url is None and package.eml  # the file still works


def test_package_for_a_followup() -> None:
    c = content(
        step=2,
        subject=normalize_reply_subject("Research Position Application – Ophthalmology"),
        attachments=(),
        channel=Channel.OUTLOOK_HANDOFF,
        message_id=42,
    )
    package = build_package(c, {}, SIGNATURE)
    assert package.reply_in_outlook and not package.link_omits_attachments
    assert package.eml_filename == "marcelorian-email2-42.eml"  # no names or addresses
    assert package.body.endswith(SIGNATURE + "\n")


@pytest.mark.parametrize(
    "body,signature,expected",
    [
        ("Hi\n", "", "Hi\n"),
        ("Hi\n\n\n", "Sig", "Hi\n\nSig\n"),
        ("Hi", "\nSig\n", "Hi\n\nSig\n"),
    ],
)
def test_body_with_signature(body: str, signature: str, expected: str) -> None:
    assert body_with_signature(body, signature) == expected


# -- snapshot channel ---------------------------------------------------------------------


def test_snapshot_without_channel_parses_with_the_old_default() -> None:
    live = json.loads(canonical_json(content()))
    del live["channel"]
    assert parse_canonical(json.dumps(live)).channel is Channel.GMAIL
    sandbox = json.loads(canonical_json(content(mode=Mode.SANDBOX, channel=None)))
    del sandbox["channel"]
    assert parse_canonical(json.dumps(sandbox)).channel is Channel.SANDBOX


def test_channel_round_trips() -> None:
    c = content(channel=Channel.OUTLOOK_HANDOFF)
    assert parse_canonical(canonical_json(c)) == c


def test_handoff_followup_carries_no_gmail_headers() -> None:
    followup = content(
        step=2,
        subject=normalize_reply_subject("Research Position Application – Ophthalmology"),
        channel=Channel.OUTLOOK_HANDOFF,
    )
    validate_content(followup, "Ophthalmology")
    with pytest.raises(SnapshotError):
        validate_content(
            replace(followup, thread_id="t1", in_reply_to="<a@b>", references=("<a@b>",)),
            "Ophthalmology",
        )
    with pytest.raises(SnapshotError):  # a Gmail follow-up still needs them
        validate_content(replace(followup, channel=Channel.GMAIL), "Ophthalmology")


def test_sandbox_and_only_sandbox_uses_the_fake_transport() -> None:
    with pytest.raises(SnapshotError):
        validate_content(content(channel=Channel.SANDBOX), "Ophthalmology")
    with pytest.raises(SnapshotError):
        validate_content(
            content(mode=Mode.SANDBOX, channel=Channel.OUTLOOK_HANDOFF), "Ophthalmology"
        )
