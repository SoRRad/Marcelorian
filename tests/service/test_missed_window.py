"""Missed-window rule (acceptance 17; decision B7)."""

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


def test_worker_asleep_through_window_requires_reapproval(queued) -> None:
    world, camp, mid = queued
    world.clock.set(at(20, 11, 5))
    assert apply_rule(world) == [(mid, "needs_reapproval")]
    assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert world.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    assert world.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE


def test_missing_evidence_is_not_fabricated_as_success(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 9))
    with transaction(world.factory) as s:  # a heartbeat is not dispatch evidence
        from marcelorian.db.models import WorkerHeartbeat

        s.add(WorkerHeartbeat(at=world.clock.now(), component="tray"))
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "needs_reapproval")]


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


def test_only_non_functioning_evaluations_mean_unavailable(queued) -> None:
    world, _camp, mid = queued
    world.connect_mailbox()
    # Sandbox sync cannot fail, so model connectivity loss through the sandbox transport.
    from marcelorian.transport.base import TransportUnavailable

    def offline():
        raise TransportUnavailable("offline")

    world.sandbox.account = offline  # type: ignore[method-assign]
    world.clock.set(at(20, 9))
    assert world.dispatch(mid).reason == "transport_unavailable"
    world.clock.set(at(20, 10))
    assert world.dispatch(mid).reason == "transport_unavailable"
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "needs_reapproval")]


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


def test_partial_pause_then_worker_off_is_a_miss(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 7))
    with transaction(world.factory) as s:
        runtime.pause_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
    world.clock.set(at(20, 9))
    assert world.dispatch(mid).reason == "paused"  # evaluated, but while deliberately paused
    with transaction(world.factory) as s:
        runtime.resume_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
    world.clock.set(at(20, 11))  # worker gone for the whole unpaused remainder
    assert apply_rule(world) == [(mid, "needs_reapproval")]


def test_mode_switch_for_whole_window_is_deliberate(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(20, 7))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.clock.set(at(20, 11, 1))
    assert apply_rule(world) == [(mid, "rolled")]
    assert world.message(mid).mode == Mode.SANDBOX.value


def test_approval_near_window_end_only_counts_remaining_portion(world) -> None:
    world.clock.set(at(20, 10, 58))
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    assert world.message(mid).eligible_window_start == at(20, 8)
    world.clock.set(at(20, 11))
    assert apply_rule(world) == [(mid, "needs_reapproval")]  # nobody evaluated 10:58-11:00

    world.clock.set(at(21, 10, 58))
    camp2 = world.campaign(world.pi(world.institution()))
    mid2 = world.draft_and_approve(camp2)
    world.settings(pending_per_institution=0)
    world.worker_cycle(mid2)  # evaluated within the remaining two minutes
    world.clock.set(at(21, 11))
    assert apply_rule(world) == [(mid2, "rolled")]


def test_reapproval_resolves_missed_window_and_sends(queued) -> None:
    world, camp, mid = queued
    world.clock.set(at(20, 11, 5))
    apply_rule(world)
    world.clock.set(at(21, 9))
    world.approve(mid)
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    assert world.dispatch(mid).outcome is Outcome.SENT


def test_startup_recovery_applies_rule_after_reconcile_and_sync(queued) -> None:
    world, _camp, mid = queued
    world.clock.set(at(21, 9))
    result = reconciliation.startup_recovery(
        world.factory, {Mode.SANDBOX: world.sandbox}, {"real": world.real_sync}, world.clock
    )
    assert result["missed_window"] == [(mid, "needs_reapproval")]
    assert world.real_sync.calls == 1


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


def test_windows_passed_during_a_long_outage_require_reapproval(queued) -> None:
    world, camp, mid = queued
    world.settings(pending_per_institution=0)
    world.clock.set(at(20, 9))
    assert world.worker_cycle(mid).reason == "over_capacity"  # Tuesday was considered
    world.clock.set(resolve_local(datetime(2026, 11, 3, 9, 0), NY))  # two weeks later
    assert apply_rule(world) == [(mid, "needs_reapproval")]
    assert world.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    with transaction(world.factory) as s:
        from marcelorian.db.models import CampaignPause

        pause = s.query(CampaignPause).filter_by(campaign_id=camp).one()
    assert pause.source_ref.endswith(at(21, 11).isoformat())  # Wednesday was the missed one


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


def test_widened_policy_does_not_count_an_evaluation_twice(queued) -> None:
    """A same-day wider window after the assigned one is judged only from its end."""
    world, _camp, mid = queued
    world.settings(pending_per_institution=0)
    world.clock.set(at(20, 9))
    world.worker_cycle(mid)  # considered inside the assigned Tue 08:00-11:00
    world.settings(window_end="16:00")  # widened afterwards
    world.clock.set(at(20, 16, 5))  # nobody evaluated Tue 11:00-16:00
    assert apply_rule(world) == [(mid, "needs_reapproval")]
