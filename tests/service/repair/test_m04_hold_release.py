"""M04 regression tests: the optional post-Email-4 pending-slot hold.

Each test asserts the DESIRED SAFE OUTCOME from the owner's brief:

* a genuine reply, or an explicit closure, during the 14-day hold releases the held
  pending slot (institution and lab occupancy drop to zero immediately);
* the consumed new-contact reservation (rolling 30-day history) stays consumed with its
  original ``consumed_at``;
* ordinary expiry frees the slot exactly at ``hold_until`` (t > now - W boundary style);
* an auto-reply during the hold does NOT release it (negative case).

Synthetic data, sandbox transport, injected clock only.
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Pi, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    CloseOutcome,
    InboundClassification,
    Mode,
)
from marcelorian.domain.timing import WindowPolicy, next_window
from marcelorian.services import campaigns, reservations
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from marcelorian.services.reservations import CapacityError

NY = ZoneInfo("America/New_York")
HOLD = timedelta(days=14)


# --------------------------------------------------------------------------- helpers
def _to_next_window(world) -> None:
    window = next_window(world.clock.now(), NY, WindowPolicy())
    world.clock.set(max(world.clock.now(), window.start))


def _run_step(world, camp, step) -> int:
    world.clock.set(max(world.clock.now(), world.campaign_row(camp).next_due_at))
    _to_next_window(world)
    mid = world.draft_and_approve(camp, step)
    assert world.dispatch(mid).outcome is Outcome.SENT
    return mid


def _complete_with_hold(world, inst, **pi_kwargs):
    """Run a full no-reply sequence with the 14-day hold on and institution cap = 1."""
    world.settings(hold_slot_14d_after_email4=True, pending_per_institution=1)
    camp = world.campaign(world.pi(inst, **pi_kwargs))
    m1 = world.draft_and_approve(camp)
    assert world.dispatch(m1).outcome is Outcome.SENT
    m4 = None
    for step in (2, 3, 4):
        m4 = _run_step(world, camp, step)
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    return camp, m1, m4


def _rows(world, camp) -> dict[str, Reservation]:
    with transaction(world.factory) as s:
        return {
            r.kind: r for r in s.scalars(select(Reservation).where(Reservation.campaign_id == camp))
        }


def _counts(world, inst, lab_group_id, at):
    with transaction(world.factory) as s:
        return (
            reservations.count_pending_institution(s, Mode.SANDBOX, [inst], at),
            reservations.count_pending_lab(s, Mode.SANDBOX, lab_group_id, at),
        )


def _new_contacts(world, inst, at) -> int:
    with transaction(world.factory) as s:
        return reservations.count_new_contacts(s, Mode.SANDBOX, [inst], at)


def _inbound(world, camp, classification, gmail_id) -> None:
    with transaction(world.factory) as s:
        record_inbound(
            s,
            s.get(Campaign, camp),
            gmail_message_id=gmail_id,
            thread_id=None,
            classification=classification,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )


def _close(world, camp, outcome=CloseOutcome.APPLICANT_CLOSED, reason="applicant decided"):
    with transaction(world.factory) as s:
        campaigns.close_campaign(
            s,
            s.get(Campaign, camp),
            outcome,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            reason=reason,
        )


def _lab_of(world, pi_camp) -> int:
    with transaction(world.factory) as s:
        campaign = s.get(Campaign, pi_camp)
        return s.get(Pi, campaign.pi_id).lab_group_id


def _assert_history_preserved(world, inst, camp, m1) -> None:
    sent1 = world.message(m1).sent_at
    contact = _rows(world, camp)["new_contact"]
    assert (contact.status, contact.consumed_at) == ("consumed", sent1)
    # Rolling-30-day history still counts this Email 1 when evaluated inside its window.
    assert _new_contacts(world, inst, sent1 + timedelta(days=29)) == 1
    # ...and stops exactly at the 30-day boundary (t > now - 30d).
    assert _new_contacts(world, inst, sent1 + timedelta(days=30)) == 0


# ------------------------------------------------------------------- regression tests
def test_genuine_reply_during_hold_releases_pending_slot(world) -> None:
    inst = world.institution()
    camp, m1, m4 = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    hold_until = world.message(m4).sent_at + HOLD
    assert _rows(world, camp)["pending_slot"].hold_until == hold_until
    world.clock.advance(timedelta(days=3))  # well inside the hold
    assert _counts(world, inst, lab, world.clock.now()) == (1, 1)  # sanity: hold active

    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "m04-reply")

    assert world.campaign_row(camp).state == CampaignState.CONVERSATION.value
    # Desired: the reply ended the hold right away (spec §3: conversation holds no slot).
    assert _counts(world, inst, lab, world.clock.now()) == (0, 0)
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "released"
    assert slot.released_at == world.clock.now()
    _assert_history_preserved(world, inst, camp, m1)


def test_genuine_reply_during_hold_lets_next_initial_reserve(world) -> None:
    inst = world.institution()
    camp, _, _ = _complete_with_hold(world, inst)
    world.clock.advance(timedelta(days=1))
    other = world.campaign(world.pi(inst))
    m_other = world.draft(other)
    with pytest.raises(CapacityError):  # sanity: the hold blocks the cap-1 institution
        world.approve(m_other)
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "m04-reply-2")
    world.approve(m_other)  # desired: capacity freed by the reply, no CapacityError


def test_applicant_close_during_hold_releases_pending_slot(world) -> None:
    inst = world.institution()
    camp, m1, _ = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=5))
    assert _counts(world, inst, lab, world.clock.now()) == (1, 1)

    _close(world, camp)

    campaign = world.campaign_row(camp)
    assert campaign.state == CampaignState.CLOSED.value
    assert campaign.close_outcome == CloseOutcome.APPLICANT_CLOSED.value
    assert _counts(world, inst, lab, world.clock.now()) == (0, 0)
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "released"
    assert slot.released_at == world.clock.now()
    _assert_history_preserved(world, inst, camp, m1)


@pytest.mark.parametrize("outcome", [CloseOutcome.NO_OPENING, CloseOutcome.DO_NOT_CONTACT])
def test_other_close_outcomes_during_hold_release_pending_slot(world, outcome) -> None:
    inst = world.institution()
    camp, m1, _ = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=2))
    _close(world, camp, outcome=outcome, reason="tagged by applicant")
    assert _counts(world, inst, lab, world.clock.now()) == (0, 0)
    assert _rows(world, camp)["pending_slot"].status == "released"
    _assert_history_preserved(world, inst, camp, m1)


def test_ordinary_expiry_frees_slot_exactly_at_hold_until(world) -> None:
    inst = world.institution()
    camp, m1, m4 = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    hold_until = world.message(m4).sent_at + HOLD
    assert _counts(world, inst, lab, hold_until - timedelta(microseconds=1)) == (1, 1)
    assert _counts(world, inst, lab, hold_until) == (0, 0)  # boundary: no longer held

    other = world.campaign(world.pi(inst))
    m_other = world.draft(other)
    world.clock.set(hold_until - timedelta(seconds=1))
    with pytest.raises(CapacityError) as info:
        world.approve(m_other)
    assert "institution_pending_full" in info.value.result.reasons
    world.clock.set(hold_until)
    _assert_history_preserved(world, inst, camp, m1)
    world.approve(m_other)  # ordinary expiry: capacity available again
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    # consumed history (camp) + the new projected contact (other) both count.
    assert _new_contacts(world, inst, world.message(m1).sent_at + timedelta(days=29)) == 2
    assert _rows(world, camp)["new_contact"].status == "consumed"


def test_auto_reply_during_hold_keeps_slot(world) -> None:
    """Negative case: only a genuine reply or a closure ends the hold early."""
    inst = world.institution()
    camp, _, m4 = _complete_with_hold(world, inst)
    lab = _lab_of(world, camp)
    world.clock.advance(timedelta(days=2))
    _inbound(world, camp, InboundClassification.AUTO_REPLY, "m04-ooo")
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    assert _counts(world, inst, lab, world.clock.now()) == (1, 1)
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "active"
    assert slot.hold_until == world.message(m4).sent_at + HOLD


def test_reply_after_expiry_leaves_no_active_slot_row(world) -> None:
    """Invariant: a conversation/closed campaign never keeps an active pending_slot row."""
    inst = world.institution()
    camp, m1, m4 = _complete_with_hold(world, inst)
    hold_until = world.message(m4).sent_at + HOLD
    world.clock.set(hold_until + timedelta(days=6))
    _inbound(world, camp, InboundClassification.GENUINE_REPLY, "m04-late")
    assert world.campaign_row(camp).state == CampaignState.CONVERSATION.value
    slot = _rows(world, camp)["pending_slot"]
    assert slot.status == "released"
    # The hold lapsed on its own; the later reply does not get recorded as ending it.
    assert (slot.released_at, slot.release_reason) == (hold_until, "hold_lapsed")
    _assert_history_preserved(world, inst, camp, m1)


def test_hold_off_by_default_email4_releases_immediately(world) -> None:
    """Control: with the setting off, Email 4 releases the slot (existing behaviour)."""
    inst = world.institution()
    world.settings(pending_per_institution=1)
    assert load_settings_hold_off(world)
    camp = world.campaign(world.pi(inst))
    m1 = world.draft_and_approve(camp)
    world.dispatch(m1)
    for step in (2, 3, 4):
        _run_step(world, camp, step)
    slot = _rows(world, camp)["pending_slot"]
    assert (slot.status, slot.hold_until) == ("released", None)
    _assert_history_preserved(world, inst, camp, m1)


def load_settings_hold_off(world) -> bool:
    with transaction(world.factory) as s:
        return not load_settings(s).hold_slot_14d_after_email4
