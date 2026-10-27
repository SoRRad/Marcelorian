"""Without strict send rules (the default since 2026-10-27) only the double-email guard,
the do-not-contact list and a missing address stop a send; the rest advises."""

from __future__ import annotations

import pytest

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Message
from marcelorian.domain.enums import Mode
from marcelorian.services import drafting, handoff
from marcelorian.services.reservations import CapacityError
from tests.factories import World
from tests.service.test_handoff import _email1, ny

SPECIALTY = "Ophthalmology"


@pytest.fixture
def relaxed(world: World) -> World:
    world.settings(strict_send_rules=False)
    return world


@pytest.fixture
def outlook(relaxed: World) -> World:
    relaxed.connect_handoff_mailbox()
    relaxed.set_mode(Mode.LIVE)
    relaxed.signature_step()
    return relaxed


def test_a_saturday_hand_off_is_allowed_with_a_suggested_monday_time(outlook: World) -> None:
    outlook.clock.set(ny(2026, 10, 24, 22, 0))  # Saturday night in New York
    _camp, mid = _email1(outlook)
    with transaction(outlook.factory) as s:
        message = s.get(Message, mid)
        settings = load_settings(s)
        status = handoff.readiness(s, message, settings, outlook.clock.now())
        suggested = handoff.suggested_send_time(s, message, settings, outlook.clock.now())
    assert status.ready
    assert suggested == ny(2026, 10, 26, 8, 0)  # Monday 08:00 at the PI's location
    result = outlook.hand_off(mid)
    assert result.outcome == handoff.HANDED_OFF and result.package is not None


def test_strict_rules_lock_the_button_again(outlook: World) -> None:
    outlook.settings(strict_send_rules=True)
    outlook.clock.set(ny(2026, 10, 24, 22, 0))
    _camp, mid = _email1(outlook)
    assert outlook.hand_off(mid).outcome == "deferred"


def test_institution_limits_warn_but_the_same_lab_blocks(relaxed: World) -> None:
    inst = relaxed.institution()
    first = relaxed.pi(inst)
    relaxed.draft_and_approve(relaxed.campaign(first))
    other_lab = relaxed.campaign(relaxed.pi(inst))  # same institution, another lab
    relaxed.draft_and_approve(other_lab)  # institution-wide: only a warning now
    same_lab = relaxed.campaign(relaxed.pi(inst, lab_group_id=relaxed.lab_group_of(first)))
    with pytest.raises(CapacityError) as info:
        relaxed.draft_and_approve(same_lab)
    assert "lab_pending_full" in info.value.result.reasons


def test_only_address_and_do_not_contact_block_the_checklist(relaxed: World) -> None:
    camp = relaxed.campaign(relaxed.pi(relaxed.institution()))
    mid = relaxed.draft(camp)
    with transaction(relaxed.factory) as s:
        checks = drafting.checklist(
            s,
            s.get(Message, mid),
            specialty_display_name=SPECIALTY,
            today=relaxed.clock.now().date(),
        )
    blocking = {c.key for c in checks if not c.warn_only}
    assert blocking == {"recipient", "dnc"}


def test_practice_try_in_outlook_goes_to_your_own_address(relaxed: World) -> None:
    relaxed.connect_handoff_mailbox("me@synthetic-hospital.test")
    camp = relaxed.campaign(relaxed.pi(relaxed.institution()))
    mid = relaxed.draft_and_approve(camp)
    with transaction(relaxed.factory) as s:
        package = handoff.practice_package(s, s.get(Message, mid), load_settings(s))
    assert package.to == "me@synthetic-hospital.test"
    assert package.compose_url and "me%40synthetic-hospital.test" in package.compose_url
    assert relaxed.message(mid).state == "approved_queued"  # nothing recorded
