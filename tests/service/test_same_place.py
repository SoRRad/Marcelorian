"""Same-place rule: one active application per place (owner decision, 2026-10-19).

A place is the institution group (default), a department within it, or a lab. Another
campaign occupies it while pending, paused, in a conversation, or within 14 days after
its last unanswered follow-up; a close (e.g. "no opening") frees it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from marcelorian.config.settings import ConfirmationRequired, SettingsError, update_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Institution, InstitutionGroup, Pi
from marcelorian.domain.enums import Actor, CampaignState, CloseOutcome
from marcelorian.services import campaigns
from marcelorian.services.gateway import Outcome
from marcelorian.services.reservations import CapacityError
from tests.service.repair.test_m04_hold_release import _run_step
from tests.service.test_reservations import reply


def blocked(world, campaign_id: int) -> list[str]:
    try:
        world.draft_and_approve(campaign_id)
    except CapacityError as exc:
        return list(exc.result.reasons)
    return []


def started(world, pi_id: int) -> int:
    camp = world.campaign(pi_id)
    assert world.dispatch(world.draft_and_approve(camp)).outcome is Outcome.SENT
    return camp


def close(world, campaign_id: int, outcome: CloseOutcome) -> None:
    with transaction(world.factory) as s:
        campaigns.close_campaign(
            s,
            s.get(Campaign, campaign_id),
            outcome,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            reason="synthetic",
        )


def set_department(world, pi_id: int, department: str | None) -> None:
    with transaction(world.factory) as s:
        s.get(Pi, pi_id).department = department


def test_default_is_one_active_application_per_institution(world) -> None:
    inst = world.institution()
    world.draft_and_approve(world.campaign(world.pi(inst)))  # approved, not yet sent
    other_lab = world.campaign(world.pi(inst))
    assert "same_place_active" in blocked(world, other_lab)
    elsewhere = world.campaign(world.pi(world.institution()))
    assert blocked(world, elsewhere) == []


def test_institution_group_is_one_place_and_splitting_frees_it(world) -> None:
    with transaction(world.factory) as s:
        group = InstitutionGroup(name="Synthetic Health System", created_at=world.clock.now())
        s.add(group)
        s.flush()
        group_id = group.id
    university = world.institution(group_id=group_id)
    hospital = world.institution(group_id=group_id)
    started(world, world.pi(university))
    at_hospital = world.campaign(world.pi(hospital))
    assert "same_place_active" in blocked(world, at_hospital)
    with transaction(world.factory) as s:
        s.get(Institution, hospital).group_id = None  # the applicant splits it off
    assert blocked(world, at_hospital) == []


def test_a_reply_keeps_the_place_until_a_rejection(world) -> None:
    inst = world.institution()
    first = started(world, world.pi(inst))
    reply(world, first)
    assert world.campaign_row(first).state == CampaignState.CONVERSATION.value
    second = world.campaign(world.pi(inst))
    assert "same_place_active" in blocked(world, second)
    close(world, first, CloseOutcome.NO_OPENING)
    assert blocked(world, second) == []


@pytest.mark.parametrize("outcome", [CloseOutcome.BOUNCE_CONFIRMED, CloseOutcome.APPLICANT_CLOSED])
def test_any_close_frees_the_place(world, outcome) -> None:
    inst = world.institution()
    first = started(world, world.pi(inst))
    close(world, first, outcome)
    assert blocked(world, world.campaign(world.pi(inst))) == []


def test_no_reply_frees_the_place_14_days_after_the_last_email(world) -> None:
    inst = world.institution()
    first = started(world, world.pi(inst))
    last = None
    for step in (2, 3, 4):
        last = _run_step(world, first, step)
    assert world.campaign_row(first).state == CampaignState.SEQUENCE_COMPLETE.value
    last_sent = world.message(last).sent_at
    second = world.campaign(world.pi(inst))
    world.clock.set(last_sent + timedelta(days=13))
    assert "same_place_active" in blocked(world, second)
    world.clock.set(last_sent + timedelta(days=14))
    assert blocked(world, second) == []


def test_department_scope(world) -> None:
    world.settings(same_place_scope="department")
    inst = world.institution()
    retina = world.pi(inst)
    set_department(world, retina, "Retina Service")
    started(world, retina)
    glaucoma = world.pi(inst)
    set_department(world, glaucoma, "Glaucoma Service")
    assert blocked(world, world.campaign(glaucoma)) == []
    same = world.pi(world.institution())
    set_department(world, same, "  retina   service ")  # other institution: not the same
    assert blocked(world, world.campaign(same)) == []


def test_department_scope_treats_missing_department_as_whole_institution(world) -> None:
    world.settings(same_place_scope="department")
    inst = world.institution()
    retina = world.pi(inst)
    set_department(world, retina, "Retina Service")
    started(world, retina)
    same_dept = world.pi(inst)
    set_department(world, same_dept, "RETINA service")
    assert "same_place_active" in blocked(world, world.campaign(same_dept))
    unknown = world.pi(inst)  # no department recorded
    assert "same_place_active" in blocked(world, world.campaign(unknown))


def test_lab_scope_allows_other_labs_but_not_the_same_lab(world) -> None:
    world.settings(same_place_scope="lab")
    inst = world.institution()
    first_pi = world.pi(inst)
    first = started(world, first_pi)
    reply(world, first)  # a conversation holds no pending slot, but holds the lab
    other_lab = world.campaign(world.pi(inst))
    assert blocked(world, other_lab) == []
    same_lab = world.campaign(world.pi(inst, lab_group_id=world.lab_group_of(first_pi)))
    assert blocked(world, same_lab) == ["same_place_active"]


def test_widening_the_scope_defers_an_approved_initial_but_never_a_followup(world) -> None:
    world.settings(same_place_scope="lab")
    inst = world.institution()
    first = started(world, world.pi(inst))
    second = world.campaign(world.pi(inst))
    m_second = world.draft_and_approve(second)
    world.settings(same_place_scope="institution")
    world.clock.advance(timedelta(minutes=10))  # past the pacing gap after the first send
    result = world.dispatch(m_second)
    assert result.outcome is not Outcome.SENT
    assert "same_place_active" in (world.message(m_second).cap_block_reason or "")
    _run_step(world, first, 2)  # the campaign already under way continues


def test_narrowing_the_scope_needs_confirmation(world) -> None:
    with transaction(world.factory) as s:
        with pytest.raises(ConfirmationRequired) as info:
            update_settings(
                s, {"same_place_scope": "lab"}, actor=Actor.APPLICANT, now=world.clock.now()
            )
        assert info.value.keys == ["same_place_scope"]
        with pytest.raises(SettingsError):
            update_settings(
                s,
                {"same_place_scope": "city"},
                actor=Actor.APPLICANT,
                now=world.clock.now(),
                confirm_less_conservative=True,
            )
    world.settings(same_place_scope="lab")
    with transaction(world.factory) as s:  # back to a stricter scope: no confirmation
        update_settings(
            s, {"same_place_scope": "department"}, actor=Actor.APPLICANT, now=world.clock.now()
        )
