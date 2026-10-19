"""Research confidence labels: what is actually known about the person and the address."""

from __future__ import annotations

from datetime import date

import pytest

from marcelorian.domain.research import Facts, confidence, freshness, is_web_url


def facts(**changes: object) -> Facts:
    base: dict[str, object] = dict(
        record_type="contact",
        contact_kind="faculty_or_investigator",
        role="Associate professor",
        role_status="official_source_checked",
        verification_status="official_contact_evidence",
        email="pi@synthetic-eye.test",
        email_status="official_published",
        email_is_shared=False,
        excluded_reason=None,
        local_verification="unchecked",
        evidence_count=1,
        hiring_authority="not_established",
        freshness="recent",
    )
    base.update(changes)
    return Facts(**base)  # type: ignore[arg-type]


def test_seed_alone_never_reaches_confirmed() -> None:
    result = confidence(facts())
    assert result.level == "source_checked"
    assert "Confirmed" not in result.label


def test_applicant_confirmation_raises_to_confirmed() -> None:
    assert confidence(facts(local_verification="confirmed_current")).level == "confirmed"


@pytest.mark.parametrize(
    "finding",
    ["fellow_or_trainee", "left_institution", "not_faculty", "retired_or_emeritus", "wrong_person"],
)
def test_negative_findings_block(finding: str) -> None:
    result = confidence(facts(local_verification=finding))
    assert result.level == "blocked" and result.person == "blocked"


def test_claim_only_address_is_not_usable() -> None:
    result = confidence(
        facts(
            contact_kind="author_unverified",
            role="",
            role_status="unverified",
            verification_status="not_verified",
            email="",
            email_status="claim_only",
            evidence_count=0,
            freshness="unknown",
        )
    )
    assert result.level == "none"
    assert any("never used as a recipient" in r for r in result.reasons)
    assert any("publication author" in w for w in result.warnings)
    assert any("No observed evidence" in w for w in result.warnings)


def test_unverified_role_caps_an_official_address() -> None:
    result = confidence(facts(role_status="unverified", verification_status="not_verified"))
    assert result.level == "unverified"


def test_publication_address_is_partial_and_warned() -> None:
    result = confidence(facts(email_status="publication_attribution_observed"))
    assert result.level == "partial"
    assert any("publication" in w for w in result.warnings)


def test_shared_mailbox_never_confirmed_as_personal() -> None:
    result = confidence(
        facts(
            contact_kind="shared_mailbox",
            email_status="official_program_mailbox",
            email_is_shared=True,
            local_verification="confirmed_current",
        )
    )
    assert result.email == "source_checked" and result.level == "source_checked"
    assert any("Shared mailbox" in w for w in result.warnings)
    assert any("not a PI" in w for w in result.warnings)


@pytest.mark.parametrize("role", ["Clinical fellow", "PGY-3 resident", "Postdoctoral researcher"])
def test_trainee_role_text_is_warned(role: str) -> None:
    assert any("trainee" in w for w in confidence(facts(role=role)).warnings)


def test_emeritus_is_warned() -> None:
    assert any("Emeritus" in w for w in confidence(facts(contact_kind="emeritus")).warnings)


def test_excluded_record_is_blocked() -> None:
    assert confidence(facts(excluded_reason="excluded_non_us")).level == "blocked"


def test_opportunity_without_email_follows_its_route_evidence() -> None:
    result = confidence(
        facts(record_type="opportunity", contact_kind="", email="", email_status="missing")
    )
    assert result.level == "source_checked"


def test_freshness_buckets() -> None:
    today = date(2026, 10, 19)
    assert freshness(date(2026, 10, 1), today) == "recent"
    assert freshness(date(2026, 6, 1), today) == "aging"
    assert freshness(date(2025, 1, 1), today) == "stale"
    assert freshness(None, today) == "unknown"


def test_only_web_urls_are_links() -> None:
    assert is_web_url("https://synthetic-eye.test/x")
    assert not is_web_url("javascript:alert(1)") and not is_web_url("") and not is_web_url(None)
