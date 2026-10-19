"""Applicant profile facts and CV checks (spec §4)."""

from __future__ import annotations

import pytest

from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Actor
from marcelorian.services import profile


def save(world, key: str, value: str, *, confirm: bool = True):
    with transaction(world.factory) as s:
        return profile.set_fact(
            s,
            world.applicant_id,
            key,
            value,
            confirm=confirm,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )


def current(world) -> dict:
    with transaction(world.factory) as s:
        return {
            k: (f.value, f.status, f.version)
            for k, f in profile.facts(s, world.applicant_id).items()
        }


def test_draft_then_confirm_bumps_the_version(world) -> None:
    save(world, "full_name", "Alex Synthetic", confirm=False)
    assert current(world)["full_name"] == ("Alex Synthetic", "proposed", 1)
    save(world, "full_name", "Alex Synthetic")
    assert current(world)["full_name"] == ("Alex Synthetic", "confirmed", 2)
    save(world, "full_name", "Alex Synthetic")  # unchanged: no new version
    assert current(world)["full_name"][2] == 2


def test_empty_draft_creates_nothing_but_confirmed_empty_is_kept(world) -> None:
    assert save(world, "recent_news", "", confirm=False) is None
    assert "recent_news" not in current(world)
    save(world, "recent_news", "")
    assert current(world)["recent_news"] == ("", "confirmed", 1)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("earliest_start", "July 2027"),
        ("earliest_start", "2027-13"),
        ("funding_preference", "paid_only"),
        ("tone", "casual"),
        ("skills", "OCT {analysis}"),
        ("full_name", "x" * 2001),
        ("full_name", "bad\x07bell"),
        ("unknown_field", "x"),
    ],
)
def test_invalid_values_are_refused(world, key: str, value: str) -> None:
    with pytest.raises(profile.ProfileError):
        save(world, key, value)


def test_missing_required_lists_unconfirmed_fields(world) -> None:
    with transaction(world.factory) as s:
        before = profile.missing_required(s, world.applicant_id)
    assert "Full name" in before and "Methods and skills you offer" in before
    for key, value in {
        "full_name": "Alex Synthetic",
        "current_role_phrase": "a research fellow at Synthetic Eye Institute",
        "earliest_start": "2027-07",
        "funding_preference": "paid_preferred",
        "skills": "OCT image analysis",
    }.items():
        save(world, key, value)
    save(world, "skills", "OCT image analysis and Python", confirm=False)  # withdrawn
    with transaction(world.factory) as s:
        assert profile.missing_required(s, world.applicant_id) == ["Methods and skills you offer"]


def test_cv_check() -> None:
    assert profile.check_cv("C:\\Users\\x\\My CV (final).pdf", b"%PDF-1.4 x") == "My CV _final_.pdf"
    assert profile.check_cv("cv", b"%PDF-1.7") == "cv.pdf"
    for name, data in (
        ("cv.pdf", b""),
        ("cv.pdf", b"PK\x03\x04"),
        ("cv.pdf", b"%PDF-" + b"0" * (10 * 1024 * 1024)),
    ):
        with pytest.raises(profile.ProfileError):
            profile.check_cv(name, data)
