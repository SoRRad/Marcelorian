"""M04 additional regression cases: extra boundaries, negatives, atomicity and late-result cases.

Desired safe outcomes; synthetic data, fake transports, injected clock only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    CloseOutcome,
    InboundClassification,
    Mode,
)
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from tests.service.repair.test_m04_hold_release import (
    HOLD,
    _close,
    _complete_with_hold,
    _counts,
    _inbound,
    _lab_of,
    _new_contacts,
    _rows,
)


def _assert_history(world, inst, camp, m1):
    sent1 = world.message(m1).sent_at
    c = _rows(world, camp)["new_contact"]
    assert (c.status, c.consumed_at) == ("consumed", sent1)
    assert _new_contacts(world, inst, sent1 + timedelta(days=30) - timedelta(microseconds=1)) == 1
    assert _new_contacts(world, inst, sent1 + timedelta(days=30)) == 0


@pytest.mark.parametrize(
    "cls",
    [
        InboundClassification.DELIVERY_NOTICE,
        InboundClassification.MANUAL_APPLICANT_MESSAGE,
        InboundClassification.UNKNOWN_PARTICIPANT,
        InboundClassification.AMBIGUOUS_THREAD,
    ],
)
def test_doubtful_inbound_during_hold_keeps_slot(world, cls):
    inst = world.institution()
    camp, _, m4 = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=2))
    _inbound(world, camp, cls, f"doubt-{cls.value}")
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    assert _counts(world, inst, lab, world.clock.now()) == (1, 1)
    slot = _rows(world, camp)["pending_slot"]
    assert (slot.status, slot.hold_until) == ("active", world.message(m4).sent_at + HOLD)


def test_reply_rolled_back_keeps_hold(world):
    """Crash inside the reply transaction: nothing partial is committed."""
    inst = world.institution()
    camp, _, m4 = _complete_with_hold(world, inst)
    world.clock.advance(timedelta(days=1))
    with pytest.raises(RuntimeError), transaction(world.factory) as s:
        record_inbound(
            s,
            s.get(Campaign, camp),
            gmail_message_id="crash-reply",
            thread_id=None,
            classification=InboundClassification.GENUINE_REPLY,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )
        raise RuntimeError("simulated crash before commit")
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    slot = _rows(world, camp)["pending_slot"]
    assert (slot.status, slot.hold_until) == ("active", world.message(m4).sent_at + HOLD)
    # After restart the same reply re-arrives and is applied once.
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "crash-reply")
    assert world.campaign_row(camp).state == CampaignState.CONVERSATION.value
    assert _rows(world, camp)["pending_slot"].status == "released"


def test_reply_duplicate_and_followups_idempotent(world):
    inst = world.institution()
    camp, m1, _ = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=3))
    t_reply = world.clock.now()
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "dup")
    world.clock.advance(timedelta(hours=1))
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "dup")  # same id: no-op
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "second")  # conversation: no-op
    _close(world, camp)
    slot = _rows(world, camp)["pending_slot"]
    assert (slot.status, slot.released_at) == ("released", t_reply)
    assert _counts(world, inst, lab, world.clock.now()) == (0, 0)
    assert world.campaign_row(camp).state == CampaignState.CLOSED.value
    _assert_history(world, inst, camp, m1)


@pytest.mark.parametrize("delta_us", [-1, 0])
def test_reply_at_hold_boundary(world, delta_us):
    inst = world.institution()
    camp, m1, m4 = _complete_with_hold(world, inst)
    hold_until = world.message(m4).sent_at + HOLD
    world.clock.set(hold_until + timedelta(microseconds=delta_us))
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "edge")
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "released"
    assert slot.released_at <= hold_until
    with transaction(world.factory) as s:
        n = s.scalar(
            select(Reservation.id).where(
                Reservation.campaign_id == camp, Reservation.status == "active"
            )
        )
    assert n is None
    _assert_history(world, inst, camp, m1)


def test_bounce_confirmed_close_during_hold_releases(world):
    inst = world.institution()
    camp, m1, _ = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=4))
    _close(world, camp, outcome=CloseOutcome.BOUNCE_CONFIRMED, reason=None)
    assert _counts(world, inst, lab, world.clock.now()) == (0, 0)
    _assert_history(world, inst, camp, m1)


def test_close_with_long_reason_during_hold_keeps_reason_within_column(world):
    inst = world.institution()
    camp, _, _ = _complete_with_hold(world, inst)
    world.clock.advance(timedelta(days=1))
    _close(world, camp, reason="x" * 300)
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "released"
    assert slot.release_reason is not None and len(slot.release_reason) <= 64


def test_late_email4_success_after_reply_creates_no_hold(world):
    """Reply lands while Email 4 is in flight; the late SENT result must not open a hold."""
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.settings(hold_slot_14d_after_email4=True)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    m1 = world.draft_and_approve(camp)
    assert world.dispatch(m1).outcome is Outcome.SENT
    from tests.service.repair.test_m04_hold_release import _to_next_window

    for step in (2, 3):
        world.clock.set(max(world.clock.now(), world.campaign_row(camp).next_due_at))
        _to_next_window(world)
        mid = world.draft_and_approve(camp, step)
        assert world.dispatch(mid).outcome is Outcome.SENT
    world.clock.set(max(world.clock.now(), world.campaign_row(camp).next_due_at))
    _to_next_window(world)
    m4 = world.draft_and_approve(camp, 4)

    def _reply(_raw):
        with transaction(world.factory) as s:
            record_inbound(
                s,
                s.get(Campaign, camp),
                gmail_message_id="inflight",
                thread_id=None,
                classification=InboundClassification.GENUINE_REPLY,
                from_address=None,
                received_at=world.clock.now(),
                actor=Actor.SYSTEM,
                now=world.clock.now(),
            )

    world.gmail.on_send = _reply
    assert world.dispatch(m4).outcome is Outcome.SENT
    assert world.campaign_row(camp).state == CampaignState.CONVERSATION.value
    slot = _rows(world, camp)["pending_slot"]
    assert (slot.status, slot.hold_until) == ("released", None)
