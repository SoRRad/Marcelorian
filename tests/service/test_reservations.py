"""Pending slots, new contacts and reservations (acceptance 1-8; decisions B3, B4)."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, InstitutionGroup, LabSuggestion, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    InboundClassification,
    MessageState,
    ReservationKind,
    ReservationStatus,
)
from marcelorian.services import approvals, campaigns, reservations
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from marcelorian.services.reservations import CapacityError, LabConversationConfirmationRequired

WEEK = timedelta(days=7)


def active(world, campaign_id):
    with transaction(world.factory) as s:
        return {
            r.kind: r.status
            for r in s.scalars(
                select(Reservation).where(
                    Reservation.campaign_id == campaign_id,
                    Reservation.status == ReservationStatus.ACTIVE.value,
                )
            )
        }


def reply(world, campaign_id):
    with transaction(world.factory) as s:
        campaign = s.get(Campaign, campaign_id)
        record_inbound(
            s,
            campaign,
            gmail_message_id=f"reply-{campaign_id}",
            thread_id=None,
            classification=InboundClassification.GENUINE_REPLY,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )


def test_email1_dispatches_from_outreach_pending_with_no_prior_thread(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    assert active(world, camp) == {"pending_slot": "active", "new_contact": "active"}
    assert world.dispatch(mid).outcome is Outcome.SENT  # acceptance 1
    with transaction(world.factory) as s:
        kinds = {
            r.kind: r.status
            for r in s.scalars(select(Reservation).where(Reservation.campaign_id == camp))
        }
    assert kinds == {"pending_slot": "active", "new_contact": "consumed"}


def test_campaign_holding_last_slot_can_still_send(world) -> None:
    inst = world.institution()
    first = world.campaign(world.pi(inst))
    world.draft_and_approve(first)
    second = world.campaign(world.pi(inst))
    mid = world.draft_and_approve(second)  # occupies the last (2nd) institution slot
    assert world.dispatch(mid).outcome is Outcome.SENT  # acceptance 3


def test_third_pending_pi_blocked(world) -> None:
    inst = world.institution()
    for _ in range(2):
        world.draft_and_approve(world.campaign(world.pi(inst)))
    third = world.campaign(world.pi(inst))
    mid = world.draft(third)
    with pytest.raises(CapacityError) as info:
        world.approve(mid)
    assert "institution_pending_full" in info.value.result.reasons  # acceptance 4
    assert world.campaign_row(third).state == CampaignState.CANDIDATE.value
    assert world.message_state(mid) is MessageState.AWAITING_APPROVAL


def test_fourth_new_contact_in_30_days_blocked_even_with_free_slots(world) -> None:
    inst = world.institution()
    for _ in range(3):
        camp = world.campaign(world.pi(inst))
        mid = world.draft_and_approve(camp)
        assert world.dispatch(mid).outcome is Outcome.SENT
        world.clock.advance(timedelta(minutes=7))
        reply(world, camp)  # frees the pending slot but not the 30-day history
    fourth = world.campaign(world.pi(inst))
    with pytest.raises(CapacityError) as info:
        world.approve(world.draft(fourth))
    assert info.value.result.reasons == ("new_contacts_30d_full",)


def test_new_contact_window_boundary_is_exclusive_at_30_days(world) -> None:
    inst = world.institution()
    sent_times = []
    for _ in range(3):
        camp = world.campaign(world.pi(inst))
        mid = world.draft_and_approve(camp)
        world.dispatch(mid)
        sent_times.append(world.message(mid).sent_at)
        world.clock.advance(timedelta(minutes=7))
        reply(world, camp)
    fourth = world.campaign(world.pi(inst))
    mid4 = world.draft(fourth)
    world.clock.set(sent_times[0] + timedelta(days=30) - timedelta(seconds=1))
    with pytest.raises(CapacityError):
        world.approve(mid4)
    world.clock.set(sent_times[0] + timedelta(days=30))  # exactly 30 days: no longer counted
    world.approve(mid4)


def test_followups_never_count_as_new_contacts_or_take_slots(world) -> None:
    inst = world.institution()
    camp = world.campaign(world.pi(inst))
    mid = world.draft_and_approve(camp)
    world.dispatch(mid)
    world.clock.advance(WEEK)
    world.draft_and_approve(camp, 2)  # acceptance 2: drafting/approving a follow-up keeps the slot
    with transaction(world.factory) as s:
        rows = s.scalars(select(Reservation).where(Reservation.campaign_id == camp)).all()
    assert sorted((r.kind, r.status) for r in rows) == [
        ("new_contact", "consumed"),
        ("pending_slot", "active"),
    ]


def test_concurrent_approvals_cannot_oversubscribe(world) -> None:
    inst = world.institution()
    campaigns_ = [world.campaign(world.pi(inst)) for _ in range(6)]
    drafts = [world.draft(c) for c in campaigns_]
    barrier = threading.Barrier(len(drafts))
    outcomes: list[str] = []

    def approve(mid: int) -> None:
        barrier.wait()
        try:
            world.approve(mid)
            outcomes.append("ok")
        except CapacityError:
            outcomes.append("blocked")

    threads = [threading.Thread(target=approve, args=(m,)) for m in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 2 and outcomes.count("blocked") == 4  # acceptance 5
    with transaction(world.factory) as s:
        assert (
            s.scalar(
                select(Reservation)
                .where(Reservation.kind == "pending_slot", Reservation.status == "active")
                .limit(1)
            )
            is not None
        )
        assert (
            len(
                s.scalars(
                    select(Reservation).where(
                        Reservation.kind == "pending_slot", Reservation.status == "active"
                    )
                ).all()
            )
            == 2
        )


def test_merged_institution_groups_do_not_double_count(world) -> None:
    a, b = world.institution(), world.institution()
    camp_a = world.campaign(world.pi(a))
    camp_b = world.campaign(world.pi(b))
    world.draft_and_approve(camp_a)
    world.draft_and_approve(camp_b)
    with transaction(world.factory) as s:
        group = InstitutionGroup(name="merged", created_at=world.clock.now())
        s.add(group)
        s.flush()
        from marcelorian.db.models import Institution

        for inst_id in (a, b):
            s.get(Institution, inst_id).group_id = group.id
        result = reservations.evaluate_initial(
            s, s.get(Campaign, camp_a), world_settings(s), world.clock.now()
        )
    assert result.counts["institution_pending"] == 2  # two campaigns, each counted once
    third = world.campaign(world.pi(a))
    with pytest.raises(CapacityError):
        world.approve(world.draft(third))  # acceptance 6: merged group is at capacity


def world_settings(session):
    from marcelorian.config.settings import load_settings

    return load_settings(session)


def test_unverified_lab_suggestion_blocks_second_contact_until_reviewed(world) -> None:
    inst = world.institution()
    pi_a, pi_b = world.pi(inst), world.pi(inst)
    world.draft_and_approve(world.campaign(pi_a))
    with transaction(world.factory) as s:
        s.add(
            LabSuggestion(
                institution_id=inst,
                pi_a_id=pi_a,
                pi_b_id=pi_b,
                status="unverified",
                created_at=world.clock.now(),
            )
        )
    camp_b = world.campaign(pi_b)
    mid = world.draft(camp_b)
    with pytest.raises(CapacityError) as info:
        world.approve(mid)
    assert info.value.result.reasons == ("lab_suggestion_unreviewed",)  # acceptance 7
    with transaction(world.factory) as s:
        s.scalars(select(LabSuggestion)).one().status = "dismissed"
    world.approve(mid)


def test_lab_conversation_requires_confirmation(world) -> None:
    inst = world.institution()
    pi_a = world.pi(inst)
    pi_b = world.pi(inst, lab_group_id=world.lab_group_of(pi_a))
    camp_a = world.campaign(pi_a)
    mid_a = world.draft_and_approve(camp_a)
    world.dispatch(mid_a)
    reply(world, camp_a)
    assert world.campaign_row(camp_a).state == CampaignState.CONVERSATION.value
    camp_b = world.campaign(pi_b)
    mid_b = world.draft(camp_b)
    with pytest.raises(LabConversationConfirmationRequired):
        world.approve(mid_b)
    world.approve(mid_b, confirm_lab_conversation=True)


def test_lab_cap_of_one(world) -> None:
    inst = world.institution()
    pi_a = world.pi(inst)
    pi_b = world.pi(inst, lab_group_id=world.lab_group_of(pi_a))
    world.draft_and_approve(world.campaign(pi_a))
    with pytest.raises(CapacityError) as info:
        world.approve(world.draft(world.campaign(pi_b)))
    assert "lab_pending_full" in info.value.result.reasons


def test_cancel_unsent_email1_releases_reservation(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        assert (
            approvals.cancel_initial(
                s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
            )
            == "cancelled"
        )
    assert active(world, camp) == {}  # acceptance 8
    assert world.campaign_row(camp).state == CampaignState.CANDIDATE.value
    assert world.message_state(mid) is MessageState.CANCELLED


def test_revoking_followup_does_not_release_slot(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(camp, 2)
    with transaction(world.factory) as s:
        approvals.revoke(
            s, s.get(type(world.message(m2)), m2), actor=Actor.APPLICANT, now=world.clock.now()
        )
    assert world.message_state(m2) is MessageState.AWAITING_APPROVAL
    assert active(world, camp) == {"pending_slot": "active"}


def test_reservations_survive_failed_definite_and_are_not_doubled(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    world.sandbox_reject = True
    with transaction(world.factory) as s:
        before = s.scalars(select(Reservation).where(Reservation.campaign_id == camp)).all()
    # Simulate a definite rejection through the state machine directly (sandbox never rejects).
    with transaction(world.factory) as s:
        message = s.get(type(world.message(mid)), mid)
        campaigns.set_message_state(
            s, message, MessageState.SENDING, actor=Actor.GATEWAY, now=world.clock.now()
        )
        campaigns.set_message_state(
            s, message, MessageState.FAILED_DEFINITE, actor=Actor.GATEWAY, now=world.clock.now()
        )
    assert active(world, camp) == {"pending_slot": "active", "new_contact": "active"}  # B4
    world.approve(mid)  # re-approval validates, never reserves twice
    with transaction(world.factory) as s:
        after = s.scalars(select(Reservation).where(Reservation.campaign_id == camp)).all()
    assert len(after) == len(before) == 2


def test_review_state_never_creates_a_reservation(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    with transaction(world.factory) as s, pytest.raises(campaigns.CampaignError):
        from marcelorian.domain.enums import PauseReason

        campaigns.open_pause(
            s,
            s.get(Campaign, camp),
            PauseReason.AUTO_REPLY,
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )
    assert active(world, camp) == {}


def test_approval_refused_when_reservation_unavailable_leaves_no_partial_state(world) -> None:
    inst = world.institution()
    world.settings(pending_per_institution=0)
    camp = world.campaign(world.pi(inst))
    mid = world.draft(camp)
    with pytest.raises((CapacityError, ApprovalError)):
        world.approve(mid)
    assert active(world, camp) == {}
    assert world.campaign_row(camp).state == CampaignState.CANDIDATE.value
    with transaction(world.factory) as s:
        assert s.scalars(select(Reservation).where(Reservation.campaign_id == camp)).all() == []
    assert ReservationKind.PENDING_SLOT.value == "pending_slot"
