"""Whole sequences in sandbox (acceptance 2, 12, 45, 46; optional 14-day hold)."""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    InboundClassification,
    MessageState,
    Mode,
)
from marcelorian.domain.timing import WindowPolicy, next_window
from marcelorian.services import reservations
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound

NY = ZoneInfo("America/New_York")


def to_next_window(world) -> None:
    window = next_window(world.clock.now(), NY, WindowPolicy())
    world.clock.set(max(world.clock.now(), window.start))


def run_step(world, camp, step) -> int:
    world.clock.set(max(world.clock.now(), world.campaign_row(camp).next_due_at))
    to_next_window(world)
    mid = world.draft_and_approve(camp, step)
    assert world.dispatch(mid).outcome is Outcome.SENT
    return mid


def pending_slot(world, camp):
    with transaction(world.factory) as s:
        return s.scalars(
            select(Reservation).where(
                Reservation.campaign_id == camp, Reservation.kind == "pending_slot"
            )
        ).one()


def test_no_reply_sequence_reaches_email4(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    m1 = world.draft_and_approve(camp)
    world.dispatch(m1)
    sent = {1: world.message(m1).sent_at}
    for step in (2, 3, 4):
        due = world.campaign_row(camp).next_due_at
        if step in (2, 3):
            assert due == sent[step - 1] + timedelta(hours=168)
        mid = run_step(world, camp, step)
        sent[step] = world.message(mid).sent_at
        assert pending_slot(world, camp).status == ("active" if step < 4 else "released")
    local3, local_due4 = sent[3].astimezone(NY), due.astimezone(NY)
    assert (local_due4.month - local3.month) % 12 == 1  # calendar month from actual sent_at
    campaign = world.campaign_row(camp)
    assert campaign.state == CampaignState.SEQUENCE_COMPLETE.value  # acceptance 45
    assert campaign.next_step is None
    try:
        world.draft(camp, 5)
    except (ApprovalError, ValueError):
        pass
    else:  # pragma: no cover
        raise AssertionError("a fifth email must be impossible")


def test_reply_stops_before_later_emails(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    run_step(world, camp, 2)
    with transaction(world.factory) as s:
        record_inbound(
            s,
            s.get(Campaign, camp),
            gmail_message_id="r",
            thread_id=None,
            classification=InboundClassification.GENUINE_REPLY,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )
    world.clock.advance(timedelta(days=8))
    try:
        world.draft(camp, 3)
    except ApprovalError as exc:
        assert "campaign_not_draftable" in exc.reasons  # acceptance 46
    else:  # pragma: no cover
        raise AssertionError("no further step after a reply")
    assert world.message_for(camp, 3) is None


def test_optional_14_day_hold_after_email4(world) -> None:
    world.settings(hold_slot_14d_after_email4=True)
    inst = world.institution()
    camp = world.campaign(world.pi(inst))
    world.dispatch(world.draft_and_approve(camp))
    for step in (2, 3, 4):
        m4 = run_step(world, camp, step)
    slot = pending_slot(world, camp)
    assert slot.status == "active" and slot.hold_until == world.message(m4).sent_at + timedelta(14)
    with transaction(world.factory) as s:
        count_now = reservations.count_pending_institution(
            s, Mode.SANDBOX, [inst], world.clock.now()
        )
        count_later = reservations.count_pending_institution(
            s, Mode.SANDBOX, [inst], world.clock.now() + timedelta(days=15)
        )
    assert (count_now, count_later) == (1, 0)


def test_followup_drafting_keeps_slot(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    world.clock.advance(timedelta(hours=168))
    m2 = world.draft(camp, 2)  # drafted, not approved
    assert world.message_state(m2) is MessageState.AWAITING_APPROVAL
    assert pending_slot(world, camp).status == "active"  # acceptance 2
