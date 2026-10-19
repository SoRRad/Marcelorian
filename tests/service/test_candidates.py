"""Candidates: dimensions shown separately, skip, fit, do-not-contact, place status."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import DoNotContact, GrantEvidence, Pi
from marcelorian.domain.enums import Mode
from marcelorian.services import candidates, dnc
from tests.service.test_same_place import started


def listed(world, **filters) -> list[candidates.Candidate]:
    with transaction(world.factory) as s:
        return candidates.list_candidates(
            s, candidates.Filters(**filters), Mode.SANDBOX, "institution", world.clock.now()
        )


def grant(world, pi_id: int, end: str) -> None:
    with transaction(world.factory) as s:
        s.add(
            GrantEvidence(
                pi_id=pi_id,
                project_number=f"R01EY{pi_id}-{end}",
                title="t",
                end_date=end,
                retrieved_at=world.clock.now(),
            )
        )


def test_sorted_by_grants_then_fit_then_address(world) -> None:
    inst = world.institution()
    plain = world.pi(inst, name="Ann Plain")
    funded = world.pi(world.institution(), name="Bea Funded")
    strong = world.pi(world.institution(), name="Cy Strong")
    grant(world, funded, "2030-01-01")
    grant(world, plain, "2020-01-01")  # ended: not active
    with transaction(world.factory) as s:
        candidates.set_fit(s, strong, "strong", "methods match", now=world.clock.now())
    names = [c.pi.full_name for c in listed(world)]
    assert names == ["Bea Funded", "Cy Strong", "Ann Plain"]
    funded_row = listed(world)[0]
    assert funded_row.active_grants == 1 and funded_row.confidence == "A"
    assert "active NIH grant evidence" in funded_row.summary
    assert "No opening verified" in funded_row.summary
    assert [c.pi.full_name for c in listed(world, fit="strong")] == ["Cy Strong"]
    assert [c.pi.full_name for c in listed(world, grants="yes")] == ["Bea Funded"]
    assert len(listed(world, fit="none")) == 2


def test_skip_hides_until_unskipped(world) -> None:
    pi = world.pi(world.institution(), name="Dee Skip")
    with transaction(world.factory) as s:
        candidates.set_skipped(s, pi, True, "not my field", now=world.clock.now())
    assert listed(world) == []
    assert [c.skipped for c in listed(world, show_skipped=True)] == [True]
    with transaction(world.factory) as s:
        candidates.set_skipped(s, pi, False, "", now=world.clock.now())
    assert len(listed(world)) == 1


def test_place_busy_and_campaign_state(world) -> None:
    inst = world.institution()
    first = world.pi(inst, name="Eve First")
    world.pi(inst, name="Fay Second")
    started(world, first)
    rows = {c.pi.full_name: c for c in listed(world)}
    assert rows["Fay Second"].place_busy and not rows["Eve First"].place_busy
    assert rows["Eve First"].campaign_state == "outreach_pending"


def test_do_not_contact_from_a_candidate(world) -> None:
    pi = world.pi(world.institution(), name="Gus Never")
    with transaction(world.factory) as s:
        candidates.add_to_dnc(s, pi, "asked not to be contacted", now=world.clock.now())
        entry = s.scalars(select(DoNotContact)).one()
        assert entry.scope_mode == "live" and entry.pi_id == pi and entry.email
        assert dnc.matches(s, pi, Mode.LIVE)


def test_invalid_fit_is_refused(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        with pytest.raises(ValueError):
            candidates.set_fit(s, pi, "perfect", "", now=world.clock.now())
        with pytest.raises(LookupError):
            candidates.set_fit(s, 999, "strong", "", now=world.clock.now())
        assert s.scalar(select(func.count()).select_from(Pi)) == 1
