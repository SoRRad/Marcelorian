"""Pure domain rules: subjects, identifiers, snapshots, MIME, Gmail params, outcomes,
recipients, transitions."""

from __future__ import annotations

import socket
from dataclasses import replace
from datetime import UTC, datetime
from email import message_from_bytes
from email.policy import default as default_policy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marcelorian.domain.enums import CampaignState, MessageState, Mode
from marcelorian.domain.gmail_params import (
    METADATA_HEADERS,
    MetadataPolicyError,
    assert_metadata_only,
    message_get_params,
    message_list_params,
    messages_from_since_query,
    prior_contact_query,
    sent_search_query,
    thread_get_params,
)
from marcelorian.domain.identifiers import (
    bare_message_id,
    new_x_marcelorian_id,
    rfc_message_id,
    sender_domain,
)
from marcelorian.domain.mime import build_mime
from marcelorian.domain.outcomes import OutcomeKind, classify_send_error
from marcelorian.domain.recipients import (
    RecipientContext,
    RecipientError,
    normalize_address,
    validate_recipient,
)
from marcelorian.domain.snapshot import (
    AttachmentRef,
    SnapshotContent,
    SnapshotError,
    TestOverride,
    canonical_json,
    parse_canonical,
    sha256_hex,
    validate_content,
    verify_canonical,
)
from marcelorian.domain.subjects import (
    EN_DASH,
    expected_subject,
    initial_subject,
    normalize_reply_subject,
)
from marcelorian.domain.transitions import (
    CAMPAIGN_TRANSITIONS,
    MESSAGE_TRANSITIONS,
    InvalidTransition,
    check_campaign,
    check_message,
)

CV = b"%PDF-1.4 synthetic"
CV_REF = AttachmentRef("cv.pdf", "application/pdf", sha256_hex(CV), len(CV), 1)


def content(**overrides: object) -> SnapshotContent:
    base = SnapshotContent(
        campaign_id=1,
        message_id=1,
        step=1,
        mode=Mode.LIVE,
        version=1,
        sender="applicant@example.test",
        to="pi@synthetic-univ.test",
        intended_recipient="pi@synthetic-univ.test",
        pi_id=1,
        pi_display_name="Pat Example",
        pi_is_synthetic=False,
        subject=initial_subject("Ophthalmology"),
        body="Dear Dr. Example,\n\nSynthetic body.\n",
        attachments=(CV_REF,),
        thread_id=None,
        in_reply_to=None,
        references=(),
        pi_timezone="America/New_York",
        test_override=None,
        approved_at=datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


# -- subjects (spec §1, C5) -------------------------------------------------------------


def test_subject_has_exact_en_dash() -> None:
    assert initial_subject("Ophthalmology") == "Research Position Application – Ophthalmology"
    assert EN_DASH in initial_subject("Ophthalmology")


@pytest.mark.parametrize("raw", ["Re: X", "RE: re: X", "  re :Re: X", "X"])
def test_reply_subject_has_exactly_one_re(raw: str) -> None:
    assert normalize_reply_subject(raw) == "Re: X"


def test_expected_subjects_per_step() -> None:
    assert expected_subject(1, "Ophthalmology").startswith("Research")
    for step in (2, 3, 4):
        assert expected_subject(step, "Ophthalmology") == "Re: " + initial_subject("Ophthalmology")
    with pytest.raises(ValueError):
        expected_subject(5, "Ophthalmology")


# -- identifiers (B12, acceptance 65) -------------------------------------------------------


def test_message_id_uses_sender_domain_never_hostname() -> None:
    mid = rfc_message_id("Applicant@Example.TEST")
    local, domain = bare_message_id(mid).split("@")
    assert mid.startswith("<") and mid.endswith(">")
    assert domain == "example.test"  # exactly the sender's domain, so never the hostname
    assert len(local) == 32 and set(local) <= set("0123456789abcdef")


def test_identifiers_are_unique_and_opaque() -> None:
    ids = {rfc_message_id("a@example.test") for _ in range(200)}
    xs = {new_x_marcelorian_id() for _ in range(200)}
    assert len(ids) == 200 and len(xs) == 200
    assert all(set(x) <= set("0123456789abcdef") for x in xs)


@pytest.mark.parametrize("bad", ["no-at-sign", "@example.test", "a@", "a@localhost"])
def test_sender_domain_validation(bad: str) -> None:
    with pytest.raises(ValueError):
        sender_domain(bad)


# -- snapshots (spec §9.1, B2) ----------------------------------------------------------------


def test_canonical_snapshot_round_trips_and_hash_is_stable() -> None:
    text = canonical_json(content())
    assert canonical_json(parse_canonical(text)) == text
    assert verify_canonical(text, sha256_hex(text)) == content()


def test_any_tampering_breaks_the_hash() -> None:
    text = canonical_json(content())
    tampered = text.replace("Synthetic body", "Different body")
    with pytest.raises(SnapshotError):
        verify_canonical(tampered, sha256_hex(text))


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("body", "Changed"),
        ("to", "other@synthetic-univ.test"),
        ("sender", "b@example.test"),
        ("mode", Mode.SANDBOX),
        ("attachments", ()),
    ],
)
def test_every_bound_field_changes_the_hash(field_name: str, value: object) -> None:
    assert sha256_hex(canonical_json(content())) != sha256_hex(
        canonical_json(content(**{field_name: value}))
    )


def test_snapshot_has_exactly_one_to_and_no_cc_bcc() -> None:
    data = canonical_json(content())
    assert '"cc":[]' in data and '"bcc":[]' in data and '"to":["pi@synthetic-univ.test"]' in data


def test_override_rejected_outside_transport_test() -> None:
    override = TestOverride(window_always_open=True, step_intervals_seconds=((2, 600),))
    with pytest.raises(SnapshotError):
        validate_content(content(test_override=override), "Ophthalmology")
    validate_content(
        content(mode=Mode.TRANSPORT_TEST, to="tester@example.test", test_override=override),
        "Ophthalmology",
    )


def test_live_never_targets_synthetic_identity() -> None:
    with pytest.raises(SnapshotError):
        validate_content(content(pi_is_synthetic=True), "Ophthalmology")


def test_subject_must_be_exact() -> None:
    with pytest.raises(SnapshotError):
        validate_content(
            content(subject="Research Position Application - Ophthalmology"), "Ophthalmology"
        )


def test_followups_need_thread_and_reply_headers() -> None:
    reply = expected_subject(2, "Ophthalmology")
    with pytest.raises(SnapshotError):
        validate_content(content(step=2, subject=reply), "Ophthalmology")
    validate_content(
        content(
            step=2,
            subject=reply,
            thread_id="t1",
            in_reply_to="<a@b.test>",
            references=("<a@b.test>",),
            attachments=(),
        ),
        "Ophthalmology",
    )


def test_live_actual_recipient_must_equal_intended() -> None:
    with pytest.raises(SnapshotError):
        validate_content(content(to="someone-else@synthetic-univ.test"), "Ophthalmology")


# -- MIME (spec §9.6 step 3, B15) ---------------------------------------------------------------


def build(**overrides: object) -> bytes:
    return build_mime(
        content(**overrides),
        {CV_REF.sha256: CV},
        rfc_message_id="<" + "a" * 32 + "@example.test>",
        x_marcelorian_id="b" * 32,
        date=datetime(2026, 10, 20, 13, 0, tzinfo=UTC),
    )


def test_mime_is_plain_text_with_bound_headers() -> None:
    raw = build()
    parsed = message_from_bytes(raw, policy=default_policy)
    assert parsed["To"] == "pi@synthetic-univ.test"
    assert parsed["Cc"] is None and parsed["Bcc"] is None
    assert parsed["Subject"] == initial_subject("Ophthalmology")
    assert parsed["Message-ID"] == "<" + "a" * 32 + "@example.test>"
    assert parsed["X-Marcelorian-Id"] == "b" * 32
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None and "Synthetic body" in body.get_content()
    assert parsed.get_body(preferencelist=("html",)) is None
    assert b"=?utf-8?" in raw.split(b"\r\n\r\n")[0].lower()  # en dash RFC 2047-encoded
    attachments = list(parsed.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["cv.pdf"]
    assert attachments[0].get_content() == CV


def test_mime_followup_reply_headers() -> None:
    parsed = message_from_bytes(
        build(
            step=2,
            subject=expected_subject(2, "Ophthalmology"),
            thread_id="t1",
            in_reply_to="<m1@example.test>",
            references=("<m1@example.test>",),
            attachments=(),
        ),
        policy=default_policy,
    )
    assert parsed["In-Reply-To"] == "<m1@example.test>"
    assert parsed["References"] == "<m1@example.test>"


def test_mime_refuses_attachment_bytes_that_do_not_match() -> None:
    with pytest.raises(SnapshotError):
        build_mime(
            content(),
            {CV_REF.sha256: b"different bytes"},
            rfc_message_id="<x@y.test>",
            x_marcelorian_id="z",
            date=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_mime_never_contains_hostname() -> None:
    hostname = socket.gethostname().encode()
    raw = build()
    if len(hostname) > 4:
        assert hostname not in raw


# -- Gmail parameters (spec §10, acceptance 33) --------------------------------------------------


def test_read_requests_are_metadata_only() -> None:
    for params in (message_get_params("m1"), thread_get_params("t1")):
        assert params["format"] == "metadata"
        assert "snippet" not in params["fields"]
        assert "body" not in params["fields"] and "parts" not in params["fields"]
        assert_metadata_only(params)
    assert_metadata_only(message_list_params("q"))


@pytest.mark.parametrize(
    "params",
    [
        {"format": "full", "fields": "id"},
        {"format": "raw", "fields": "id"},
        {"format": "metadata", "fields": "id,snippet"},
        {"format": "metadata", "fields": "payload/body"},
        {"format": "metadata", "fields": "payload/parts"},
        {"format": "metadata"},
        {"format": "metadata", "fields": "id", "metadataHeaders": ["X-Secret"]},
    ],
)
def test_metadata_policy_rejects_content_requests(params: dict[str, object]) -> None:
    with pytest.raises(MetadataPolicyError):
        assert_metadata_only(params)


def test_x_marcelorian_id_is_in_header_allowlist() -> None:
    assert "X-Marcelorian-Id" in METADATA_HEADERS
    assert "Message-ID" in METADATA_HEADERS


def test_prior_contact_query_excludes_drafts_and_covers_all_directions() -> None:
    query = prior_contact_query(["PI@Synthetic-Univ.test"])
    for prefix in ("from:", "to:", "cc:", "bcc:"):
        assert f'{prefix}"pi@synthetic-univ.test"' in query
    assert query.endswith("-in:drafts")
    assert message_list_params(query)["includeSpamTrash"] is True


def test_queries_reject_injection() -> None:
    with pytest.raises(ValueError):
        prior_contact_query(['a@b.test" OR from:"x'])
    with pytest.raises(ValueError):
        prior_contact_query([])


def test_new_thread_reply_and_sent_queries() -> None:
    assert "after:1700000000" in messages_from_since_query(["a@b.test"], 1_700_000_000)
    assert sent_search_query("<abc@example.test>") == "rfc822msgid:abc@example.test"


# -- send outcome classification ----------------------------------------------------------------


@pytest.mark.parametrize("status", [None, 500, 502, 503, 504, 408, 418])
def test_ambiguous_outcomes(status: int | None) -> None:
    assert classify_send_error(status).kind is OutcomeKind.AMBIGUOUS


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 429])
def test_definite_rejections(status: int) -> None:
    assert classify_send_error(status).kind is OutcomeKind.DEFINITE_REJECTION


def test_auth_failures_flagged() -> None:
    assert classify_send_error(401).auth_failure
    assert classify_send_error(403, "insufficientPermissions").auth_failure
    assert not classify_send_error(403, "userRateLimitExceeded").auth_failure


# -- recipients (acceptance 44, B11) ---------------------------------------------------------------


def ctx(**overrides: object) -> RecipientContext:
    base = RecipientContext(
        mode=Mode.LIVE,
        transport_kind="gmail",
        snapshot_to="pi@synthetic-univ.test",
        snapshot_intended="pi@synthetic-univ.test",
        has_test_override=False,
        pi_is_synthetic=False,
        send_ready_address="pi@synthetic-univ.test",
        allowlist=frozenset({"tester@example.test"}),
        campaign_test_recipient=None,
        real_pi_addresses=frozenset({"pi@synthetic-univ.test"}),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def test_live_accepts_only_verified_pi_address() -> None:
    validate_recipient(ctx())
    for bad in (
        ctx(snapshot_to="tester@example.test"),
        ctx(send_ready_address=None),
        ctx(has_test_override=True),
        ctx(pi_is_synthetic=True),
        ctx(transport_kind="sandbox"),
    ):
        with pytest.raises(RecipientError):
            validate_recipient(bad)


def test_transport_test_requires_allowlisted_mapped_address() -> None:
    good = ctx(
        mode=Mode.TRANSPORT_TEST,
        snapshot_to="tester@example.test",
        campaign_test_recipient="tester@example.test",
    )
    validate_recipient(good)
    with pytest.raises(RecipientError):
        validate_recipient(replace(good, allowlist=frozenset()))
    with pytest.raises(RecipientError):
        validate_recipient(replace(good, campaign_test_recipient="other@example.test"))
    with pytest.raises(RecipientError):
        validate_recipient(
            replace(
                good,
                snapshot_to="pi@synthetic-univ.test",
                allowlist=frozenset({"pi@synthetic-univ.test"}),
                campaign_test_recipient="pi@synthetic-univ.test",
            )
        )


def test_sandbox_only_uses_fake_transport() -> None:
    validate_recipient(ctx(mode=Mode.SANDBOX, transport_kind="sandbox"))
    with pytest.raises(RecipientError):
        validate_recipient(ctx(mode=Mode.SANDBOX, transport_kind="gmail"))


@pytest.mark.parametrize("bad", ["a@b", "two@a.test,three@a.test", "x <a@b.test>", "a@b.test\n"])
def test_normalize_address_rejects_non_single_addresses(bad: str) -> None:
    with pytest.raises(RecipientError):
        normalize_address(bad if not bad.endswith("\n") else bad + "x")


# -- transitions ---------------------------------------------------------------------------------


def test_sent_and_closed_are_terminal() -> None:
    assert MESSAGE_TRANSITIONS[MessageState.SENT] == frozenset()
    assert CAMPAIGN_TRANSITIONS[CampaignState.CLOSED] == frozenset()
    with pytest.raises(InvalidTransition):
        check_message(MessageState.SENT, MessageState.APPROVED_QUEUED)
    with pytest.raises(InvalidTransition):
        check_campaign(CampaignState.CONVERSATION, CampaignState.OUTREACH_PENDING)


def test_uncertain_never_returns_to_queue_directly() -> None:
    assert MessageState.APPROVED_QUEUED not in MESSAGE_TRANSITIONS[MessageState.UNCERTAIN]
    assert MessageState.SENDING not in MESSAGE_TRANSITIONS[MessageState.UNCERTAIN]


@given(st.lists(st.sampled_from(list(MessageState)), max_size=30))
def test_random_walks_never_leave_sent(path: list[MessageState]) -> None:
    state = MessageState.SENT
    for target in path:
        if target in MESSAGE_TRANSITIONS[state]:
            state = target
    assert state is MessageState.SENT
