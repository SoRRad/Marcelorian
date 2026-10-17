"""Lowering limits below occupancy (acceptance 6, 59; decision B6)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign
from marcelorian.domain.enums import Actor, CampaignState, InboundClassification, MessageState
from marcelorian.services import approvals, scheduling
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from marcelorian.services.reservations import CapacityError

WEEK = timedelta(hours=168)


def cancel(world, camp):
    with transaction(world.factory) as s:
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )


def test_two_reserved_initials_under_new_limit_of_one(world) -> None:
    inst = world.institution()
    a, b = world.campaign(world.pi(inst)), world.campaign(world.pi(inst))
    ma, mb = world.draft_and_approve(a), world.draft_and_approve(b)
    world.settings(pending_per_institution=1)  # lowered below occupancy
    ra, rb = world.dispatch(ma), world.dispatch(mb)
    assert (ra.reason, rb.reason) == ("over_capacity", "over_capacity")  # both blocked
    assert world.message(ma).cap_block_reason == "over_capacity:institution_pending_full"
    assert world.message_state(ma) is MessageState.APPROVED_QUEUED  # approvals preserved
    assert world.campaign_row(a).state == CampaignState.OUTREACH_PENDING.value  # no auto-cancel
    world.clock.advance(timedelta(minutes=1))
    assert world.dispatch(ma).reason == "over_capacity"  # no hidden resolution by retrying
    cancel(world, b)  # applicant action releases b's reservations...
    assert world.dispatch(ma).outcome is Outcome.SENT  # ...which permits a


def test_either_cancellation_unblocks_the_other(world) -> None:
    inst = world.institution()
    a, b = world.campaign(world.pi(inst)), world.campaign(world.pi(inst))
    ma, mb = world.draft_and_approve(a), world.draft_and_approve(b)
    world.settings(pending_per_institution=1)
    cancel(world, a)
    assert world.dispatch(mb).outcome is Outcome.SENT
    assert world.message_state(ma) is MessageState.CANCELLED


def test_restoring_the_limit_unblocks(world) -> None:
    inst = world.institution()
    a, b = world.campaign(world.pi(inst)), world.campaign(world.pi(inst))
    ma, _mb = world.draft_and_approve(a), world.draft_and_approve(b)
    world.settings(pending_per_institution=1)
    assert world.dispatch(ma).reason == "over_capacity"
    world.settings(pending_per_institution=2)
    assert world.dispatch(ma).outcome is Outcome.SENT


def test_unsent_initial_waits_for_contacted_campaign_to_release(world) -> None:
    inst = world.institution()
    contacted, waiting = world.campaign(world.pi(inst)), world.campaign(world.pi(inst))
    world.dispatch(world.draft_and_approve(contacted))
    mw = world.draft_and_approve(waiting)
    world.settings(pending_per_institution=1)
    world.clock.advance(timedelta(minutes=10))
    assert world.dispatch(mw).reason == "over_capacity"
    with transaction(world.factory) as s:
        record_inbound(
            s,
            s.get(Campaign, contacted),
            gmail_message_id="r1",
            thread_id=None,
            classification=InboundClassification.GENUINE_REPLY,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )
    assert world.dispatch(mw).outcome is Outcome.SENT


def test_new_reservations_blocked_while_over_capacity(world) -> None:
    inst = world.institution()
    for _ in range(2):
        world.draft_and_approve(world.campaign(world.pi(inst)))
    world.settings(pending_per_institution=1)
    with pytest.raises(CapacityError):
        world.approve(world.draft(world.campaign(world.pi(inst))))


def test_lowered_30_day_limit_blocks_reserved_initial(world) -> None:
    inst = world.institution()
    first = world.campaign(world.pi(inst))
    world.dispatch(world.draft_and_approve(first))
    second = world.campaign(world.pi(inst))
    m2 = world.draft_and_approve(second)
    world.settings(new_contacts_per_institution_30d=1)
    world.clock.advance(timedelta(minutes=10))
    assert world.message(m2).state == MessageState.APPROVED_QUEUED.value
    assert world.dispatch(m2).reason == "over_capacity"
    assert "new_contacts_30d_full" in world.message(m2).cap_block_reason


def test_followups_unaffected_by_pending_caps_but_obey_lowered_budget(world) -> None:
    inst = world.institution()
    camp = world.campaign(world.pi(inst))
    world.dispatch(world.draft_and_approve(camp))
    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(camp, 2)
    world.settings(pending_per_institution=0, new_contacts_per_institution_30d=0)
    world.settings(sandbox_submissions_per_24h=0)  # lowered 24 h budget
    assert world.dispatch(m2).reason == "budget"  # not over_capacity: no new slot needed
    world.settings(sandbox_submissions_per_24h=25)
    assert world.dispatch(m2).outcome is Outcome.SENT


def test_cap_only_deferral_is_not_missed_window(world) -> None:
    inst = world.institution()
    a, b = world.campaign(world.pi(inst)), world.campaign(world.pi(inst))
    ma = world.draft_and_approve(a)
    world.draft_and_approve(b)
    world.settings(pending_per_institution=1)
    world.worker_cycle(ma)
    world.clock.advance(timedelta(hours=3))  # past 11:00 New York
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert (ma, "rolled") in actions
    assert world.message_state(ma) is MessageState.APPROVED_QUEUED
