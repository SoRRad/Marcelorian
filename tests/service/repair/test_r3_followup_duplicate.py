"""Round-3 #2: an identity link found after both Email 1s went out holds both sequences.

Owner decision (2026-10-17): the duplicate-outreach rule also covers follow-ups. Two
unlinked records of one person each get a legitimate Email 1; enrichment then links them
(same ORCID). Neither Email 2 may be approved or dispatched until the applicant keeps one
sequence by closing the other. Fake Gmail, synthetic data, injected clock only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Pi
from marcelorian.domain.enums import Actor, CloseOutcome, MessageState, Mode
from marcelorian.services import campaigns
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from tests.factories import World

ORCID = "0000-0002-0000-0778"  # valid check digit


def _two_sent_records(world: World) -> tuple[int, int, int, int]:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    hospital = world.pi(world.institution(), email="dr.twin@synthetic-hospital.test")
    univ = world.pi(world.institution(), email="dr.twin@synthetic-univ.test")
    camp_a = world.campaign(hospital, Mode.LIVE)
    assert world.dispatch(world.draft_and_approve(camp_a)).outcome is Outcome.SENT
    world.clock.advance(timedelta(minutes=10))
    camp_b = world.campaign(univ, Mode.LIVE)
    assert world.dispatch(world.draft_and_approve(camp_b)).outcome is Outcome.SENT
    return hospital, univ, camp_a, camp_b


def _link(world: World, *pi_ids: int) -> None:
    with transaction(world.factory) as s:
        for pi_id in pi_ids:
            s.get(Pi, pi_id).orcid = ORCID


def _due(world: World, *camps: int) -> None:
    world.clock.set(max(world.campaign_row(c).next_due_at for c in camps) + timedelta(minutes=1))


def _close(world: World, camp: int) -> None:
    with transaction(world.factory) as s:
        campaigns.close_campaign(
            s,
            s.get(Campaign, camp),
            CloseOutcome.APPLICANT_CLOSED,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            reason="kept the other sequence",
        )


def test_link_found_before_followup_approval_refuses_both(world: World) -> None:
    hospital, univ, camp_a, camp_b = _two_sent_records(world)
    _link(world, hospital, univ)
    _due(world, camp_a, camp_b)
    for camp in (camp_a, camp_b):
        with pytest.raises(ApprovalError, match="duplicate_outreach"):
            world.draft_and_approve(camp, 2)
    assert len(world.gmail.send_calls) == 2  # only the two Email 1s


def test_link_found_after_followup_approval_blocks_both_at_dispatch(world: World) -> None:
    hospital, univ, camp_a, camp_b = _two_sent_records(world)
    _due(world, camp_a, camp_b)
    queued = [world.draft_and_approve(c, 2) for c in (camp_a, camp_b)]
    _link(world, hospital, univ)
    for mid in queued:
        result = world.dispatch(mid)
        assert (result.outcome, result.reason) == (Outcome.BLOCKED, "duplicate_outreach")
        assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert len(world.gmail.send_calls) == 2


def test_closing_one_sequence_lets_the_kept_one_continue_once(world: World) -> None:
    hospital, univ, camp_a, camp_b = _two_sent_records(world)
    _link(world, hospital, univ)
    _due(world, camp_a, camp_b)
    _close(world, camp_a)
    mid = world.draft_and_approve(camp_b, 2)
    assert world.dispatch(mid).outcome is Outcome.SENT
    assert len(world.gmail.send_calls) == 3
    with pytest.raises(ApprovalError):
        world.draft_and_approve(camp_a, 2)  # the closed sequence never resumes


def test_unsent_candidate_of_the_same_person_does_not_hold_a_followup(world: World) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    hospital = world.pi(world.institution(), email="dr.twin@synthetic-hospital.test")
    univ = world.pi(world.institution(), email="dr.twin@synthetic-univ.test")
    camp_a = world.campaign(hospital, Mode.LIVE)
    assert world.dispatch(world.draft_and_approve(camp_a)).outcome is Outcome.SENT
    world.campaign(univ, Mode.LIVE)  # never drafted: reached no one
    _link(world, hospital, univ)
    _due(world, camp_a)
    assert world.dispatch(world.draft_and_approve(camp_a, 2)).outcome is Outcome.SENT


def test_unlinked_records_keep_independent_followups(world: World) -> None:
    _hospital, _univ, camp_a, camp_b = _two_sent_records(world)
    _due(world, camp_a, camp_b)
    for camp in (camp_a, camp_b):
        world.clock.advance(timedelta(minutes=10))  # pacing
        assert world.dispatch(world.draft_and_approve(camp, 2)).outcome is Outcome.SENT
