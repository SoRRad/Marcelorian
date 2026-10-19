"""Missed-window rule (acceptance 17; owner decision S7: an unsent window rolls, no re-approval)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from marcelorian.clock import ManualClock
from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Actor, CampaignState, MessageState, Mode
from marcelorian.domain.timing import resolve_local
from marcelorian.services import reconciliation, runtime, scheduling
from marcelorian.services.gateway import Outcome

pytestmark = pytest.mark.narrow_window  # window-edge mechanics (11:00 end)

NY = ZoneInfo("America/New_York")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return resolve_local(datetime(2026, 10, day, hour, minute), NY)


@pytest.fixture
def clock():
    """This module starts on Monday 2026-10-19 12:00 New York."""
    return ManualClock(at(19, 12))


@pytest.fixture
def queued(world):
    """Email 1 approved on Monday 2026-10-19 12:00 NY -> assigned Tuesday 08:00-11:00."""
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    message = world.message(mid)
    assert (message.eligible_window_start, message.eligible_window_end) == (at(20, 8), at(20, 11))
    return world, camp, mid


def apply_rule(world):
    with transaction(world.factory) as s:
        return scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))


def test_functioning_deferral_in_window_rolls_to_next_window(queued) -> None:
    world, _camp, mid = queued
    world.settings(real_submissions_per_24h=25)
    world.clock.set(at(20, 9))
    with transaction(world.factory) as s:
        runtime.pause_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
        runtime.resume_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
    # A cap-only deferral during the window: the message was considered.
    world.settings(pending_per_institution=0)
    assert world.worker_cycle(mid).reason == "over_capacity"
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "rolled")]  # B6: cap deferral is not a miss
    message = world.message(mid)
    assert message.state == MessageState.APPROVED_QUEUED.value
    assert (message.eligible_window_start, message.eligible_window_end) == (at(21, 8), at(21, 11))


def test_brief_restart_within_window_preserves_queue(queued) -> None:
    world, _camp, mid = queued
    world.settings(pending_per_institution=0)  # keep it deferred so it is not sent
    world.clock.set(at(20, 8, 1))
    world.worker_cycle(mid)
    world.rebuild_gateway()  # restart gap 08:01 - 10:30
    world.clock.set(at(20, 10, 30))
    world.worker_cycle(mid)
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "rolled")]


def test_persistent_pause_for_whole_window_is_deliberate(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 7))
    with transaction(world.factory) as s:
        runtime.pause_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
    world.clock.set(at(20, 11, 30))
    assert apply_rule(world) == [(mid, "rolled")]
    with transaction(world.factory) as s:  # persistent: still paused until explicitly resumed
        assert runtime.get_runtime(s, world.clock.now()).dispatch_paused


def test_mode_switch_for_whole_window_is_deliberate(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 7))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.clock.set(at(20, 11, 1))
    assert apply_rule(world) == [(mid, "rolled")]
    assert world.message(mid).mode == Mode.SANDBOX.value


def test_window_not_passed_stays_queued(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 10, 59))
    assert apply_rule(world) == []
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED


def test_blackout_defers_eligible_window_not_due(world) -> None:
    world.settings(blackout_federal_holidays=True)
    world.clock.set(resolve_local(datetime(2026, 11, 25, 12, 0), NY))  # day before Thanksgiving
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    message = world.message(mid)
    assert message.due_at == world.clock.now()  # due date untouched (acceptance 18)
    assert message.eligible_window_start == resolve_local(datetime(2026, 12, 1, 8, 0), NY)


# -- every passed window is judged, not just the assigned one (review round 3) ----------------


def test_each_considered_window_rolls_to_the_first_one_after_now(queued) -> None:
    world, _camp, mid = queued
    world.settings(pending_per_institution=0)
    for day in (20, 21):  # Tuesday and Wednesday both considered by a healthy worker
        world.clock.set(at(day, 9))
        world.worker_cycle(mid)
    world.clock.set(at(22, 7))  # Thursday before the window
    assert apply_rule(world) == [(mid, "rolled")]
    message = world.message(mid)
    assert (message.eligible_window_start, message.eligible_window_end) == (at(22, 8), at(22, 11))


# -- owner decision S7: nothing is sent without a click, so an unsent window just rolls ----


def test_unclicked_window_rolls_without_reapproval(queued) -> None:
    world, camp, mid = queued
    world.clock.set(at(20, 11, 5))  # nobody clicked Send on Tuesday
    assert apply_rule(world) == [(mid, "rolled")]
    message = world.message(mid)
    assert message.state == MessageState.APPROVED_QUEUED.value
    assert (message.eligible_window_start, message.eligible_window_end) == (at(21, 8), at(21, 11))
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    world.clock.set(at(21, 9))
    assert world.dispatch(mid).outcome is Outcome.SENT  # the click re-runs every check


def test_app_unavailable_through_window_still_rolls(queued) -> None:
    world, _camp, mid = queued
    from marcelorian.transport.base import TransportUnavailable

    def offline():
        raise TransportUnavailable("offline")

    world.sandbox.account = offline  # type: ignore[method-assign]
    world.clock.set(at(20, 9))
    assert world.dispatch(mid).reason == "transport_unavailable"
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "rolled")]


def test_long_absence_rolls_to_the_first_window_after_now(queued) -> None:
    world, camp, mid = queued
    world.clock.set(resolve_local(datetime(2026, 11, 3, 9, 0), NY))  # two weeks later, Tue 09:00
    assert apply_rule(world) == [(mid, "rolled")]
    message = world.message(mid)
    assert message.eligible_window_start == resolve_local(datetime(2026, 11, 3, 8, 0), NY)
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value


def test_rolling_uses_the_current_policy(queued) -> None:
    world, _camp, mid = queued
    world.settings(window_end="16:00")  # widened after approval
    world.clock.set(at(20, 16, 5))
    assert apply_rule(world) == [(mid, "rolled")]
    message = world.message(mid)
    assert (message.eligible_window_start, message.eligible_window_end) == (at(21, 8), at(21, 16))


def test_startup_recovery_rolls_after_reconcile_and_sync(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(21, 9))
    result = reconciliation.startup_recovery(
        world.factory, {Mode.SANDBOX: world.sandbox}, {"real": world.real_sync}, world.clock
    )
    assert result["missed_window"] == [(mid, "rolled")]
    assert world.real_sync.calls == 1
    assert world.message(mid).eligible_window_end == at(21, 11)
