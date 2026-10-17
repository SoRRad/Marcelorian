"""Races around the atomic claim (acceptance 19, 25, 54-57; decisions B5, B10)."""

from __future__ import annotations

import contextlib
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message, SendAttempt
from marcelorian.domain.enums import (
    Actor,
    AttemptStatus,
    CampaignState,
    InboundClassification,
    MessageState,
    Mode,
)
from marcelorian.domain.timing import resolve_local
from marcelorian.services import approvals, dnc, runtime, scheduling
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound

NY = ZoneInfo("America/New_York")
WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def email1(world):
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    return camp, world.draft_and_approve(camp)


def in_other_transaction(world, fn):
    with transaction(world.factory) as s:
        fn(s, world.clock.now())


# -- concurrent claims (acceptance 19, 57) ---------------------------------------------------


def test_double_click_and_scheduler_tick_submit_exactly_once(live) -> None:
    _camp, mid = email1(live)
    barrier = threading.Barrier(3)

    def hold_in_preflight() -> None:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=5)  # all callers finish preflight before anyone claims

    live.gmail.on_account = hold_in_preflight
    results = []

    def run() -> None:
        results.append(live.gateway.dispatch(mid))

    threads = [threading.Thread(target=run) for _ in range(3)]  # UI x2 + scheduler tick
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    outcomes = sorted(r.outcome.value for r in results)
    assert outcomes == ["not_eligible", "not_eligible", "sent"]
    assert len(live.gmail.send_calls) == 1
    assert len(live.attempts(mid)) == 1


def test_unique_index_rejects_second_unresolved_attempt(live) -> None:
    _camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok", "fail"]  # prior-contact ok; reconciliation search fails
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN
    first = live.attempts(mid)[0]
    with transaction(live.factory) as s, pytest.raises(IntegrityError):
        s.add(
            SendAttempt(
                attempt_uid="f" * 32,
                message_id=mid,
                snapshot_id=first.snapshot_id,
                snapshot_sha256=first.snapshot_sha256,
                mode="live",
                sender=first.sender,
                recipient=first.recipient,
                intended_recipient=first.intended_recipient,
                app_message_id="<x@example.test>",
                x_marcelorian_id="e" * 32,
                status=AttemptStatus.IN_FLIGHT.value,
                claimed_at=live.clock.now(),
            )
        )
        s.flush()


# -- changes during preflight (acceptance 54, 56) ---------------------------------------------


def test_cancellation_during_preflight_prevents_submission(live) -> None:
    camp, mid = email1(live)

    def cancel(_query: str) -> None:
        in_other_transaction(
            live,
            lambda s, now: approvals.cancel_initial(
                s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=now
            ),
        )

    live.gmail.on_search = cancel  # runs during the prior-contact search (network preflight)
    result = live.dispatch(mid)
    assert result.outcome is Outcome.NOT_ELIGIBLE and result.reason == "changed_during_preflight"
    assert live.gmail.send_calls == []
    assert live.campaign_row(camp).state == CampaignState.CANDIDATE.value
    assert live.attempts(mid) == []


def test_revocation_during_preflight_prevents_submission(live) -> None:
    _camp, mid = email1(live)
    live.gmail.on_search = lambda _q: in_other_transaction(
        live,
        lambda s, now: approvals.revoke(s, s.get(Message, mid), actor=Actor.APPLICANT, now=now),
    )
    assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE
    assert live.gmail.send_calls == []
    assert live.message_state(mid) is MessageState.AWAITING_APPROVAL


def test_persistent_pause_during_preflight_prevents_submission(live) -> None:
    _camp, mid = email1(live)
    live.gmail.on_search = lambda _q: in_other_transaction(
        live, lambda s, now: runtime.pause_dispatch(s, actor=Actor.APPLICANT, now=now)
    )
    result = live.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.DEFERRED, "paused")
    assert live.gmail.send_calls == []
    assert live.message_state(mid) is MessageState.APPROVED_QUEUED


def test_mode_switch_during_preflight_prevents_submission(live) -> None:
    _camp, mid = email1(live)
    live.gmail.on_search = lambda _q: in_other_transaction(
        live,
        lambda s, now: runtime.switch_mode(
            s, Mode.SANDBOX, confirmed=True, actor=Actor.APPLICANT, now=now
        ),
    )
    assert live.dispatch(mid).reason == "mode_inactive"
    assert live.gmail.send_calls == []
    assert live.message(mid).mode == Mode.LIVE.value


def test_do_not_contact_during_preflight_prevents_submission(live) -> None:
    camp, mid = email1(live)
    pi_id = live.campaign_row(camp).pi_id
    live.gmail.on_search = lambda _q: in_other_transaction(
        live,
        lambda s, now: dnc.add_entry(
            s, scope_mode=Mode.LIVE, pi_id=pi_id, actor=Actor.APPLICANT, now=now
        ),
    )
    result = live.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.BLOCKED, "do_not_contact")
    assert live.gmail.send_calls == []
    assert live.message_state(mid) is MessageState.NEEDS_REAPPROVAL


def test_reply_recorded_during_preflight_prevents_followup(live) -> None:
    camp, m1 = email1(live)
    live.dispatch(m1)
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)

    def reply_arrives() -> None:
        in_other_transaction(
            live,
            lambda s, now: record_inbound(
                s,
                s.get(Campaign, camp),
                gmail_message_id="r-1",
                thread_id=None,
                classification=InboundClassification.GENUINE_REPLY,
                from_address=None,
                received_at=now,
                actor=Actor.SYSTEM,
                now=now,
            ),
        )

    live.real_sync.before = reply_arrives  # the incremental sync records the reply
    result = live.dispatch(m2)
    assert result.outcome is Outcome.NOT_ELIGIBLE  # acceptance 25 / 56
    assert len(live.gmail.send_calls) == 1
    assert live.message_state(m2) is MessageState.CANCELLED
    assert live.campaign_row(camp).state == CampaignState.CONVERSATION.value


def test_reply_found_in_thread_by_preflight_stops_followup(live) -> None:
    camp, m1 = email1(live)
    live.dispatch(m1)
    first = live.message(m1)
    live.clock.advance(timedelta(days=2))
    pi_address = live.attempts(m1)[0].recipient
    live.gmail.inject(
        sender=pi_address, to="applicant@example.test", thread_id=first.gmail_thread_id
    )
    live.clock.advance(timedelta(days=5))
    m2 = live.draft_and_approve(camp, 2)

    def classify(_campaign, meta):
        return InboundClassification.GENUINE_REPLY

    live.gateway._inbound_handler = classify  # P3 classifier stand-in
    result = live.dispatch(m2)
    assert (result.outcome, result.reason) == (Outcome.PAUSED, "inbound_activity")
    assert len(live.gmail.send_calls) == 1
    assert live.campaign_row(camp).state == CampaignState.CONVERSATION.value
    assert live.message_state(m2) is MessageState.CANCELLED


# -- the 11:00 boundary (acceptance 55) --------------------------------------------------------


def test_preflight_crossing_1100_does_not_submit_and_is_not_missed(live) -> None:
    live.clock.set(resolve_local(datetime(2026, 10, 20, 10, 59, 30), NY))
    _camp, mid = email1(live)
    live.real_sync.before = lambda: live.clock.set(
        resolve_local(datetime(2026, 10, 20, 11, 0, 5), NY)
    )
    result = live.dispatch(mid)
    assert result.outcome is Outcome.DEFERRED and result.reason == "window_passed"
    assert live.gmail.send_calls == []
    assert live.message_state(mid) is MessageState.APPROVED_QUEUED
    live.real_sync.before = None
    with transaction(live.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, live.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]  # considered inside the window, so not a miss
    message = live.message(mid)
    assert message.eligible_window_start == resolve_local(datetime(2026, 10, 21, 8, 0), NY)


def test_preflight_crossing_a_widened_window_end_does_not_submit(live) -> None:
    live.settings(window_weekdays=[0, 1, 2, 3, 4], window_end="16:00")
    live.clock.set(resolve_local(datetime(2026, 10, 26, 15, 59, 30), NY))  # Monday
    _camp, mid = email1(live)
    live.real_sync.before = lambda: live.clock.set(
        resolve_local(datetime(2026, 10, 26, 16, 0, 5), NY)
    )
    result = live.dispatch(mid)
    assert result.outcome is Outcome.DEFERRED and result.reason == "window_passed"
    assert live.gmail.send_calls == []
    live.real_sync.before = None
    with transaction(live.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, live.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]
    assert live.message(mid).eligible_window_start == resolve_local(
        datetime(2026, 10, 27, 8, 0), NY
    )


# -- cancellation after the claim (acceptance 54, B5) -----------------------------------------


def test_cancel_after_claim_is_not_a_recall(live) -> None:
    camp, mid = email1(live)
    outcome: list[str] = []
    live.gmail.on_send = lambda _raw: in_other_transaction(
        live,
        lambda s, now: outcome.append(
            approvals.cancel_initial(s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=now)
        ),
    )
    result = live.dispatch(mid)
    assert outcome == ["not_recalled"]
    assert result.outcome is Outcome.SENT  # the claim committed: recall is not promised
    campaign = live.campaign_row(camp)
    # Future steps stop (no automatic generation or dispatch), but the schedule is kept so
    # the applicant can deliberately re-draft the next step later (B5).
    assert campaign.sequence_stopped and campaign.next_step == 2
    assert live.message(mid).cancel_requested_at is not None


def test_cancel_after_claim_with_ambiguous_result_is_reconciled_not_retried(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.on_send = lambda _raw: in_other_transaction(
        live,
        lambda s, now: approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=now
        ),
    )
    result = live.dispatch(mid)
    assert result.outcome is Outcome.UNCERTAIN
    assert live.message_state(mid) is MessageState.UNCERTAIN
    with transaction(live.factory) as s, pytest.raises(approvals.CancelRefused):
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
        )
    assert len(live.gmail.send_calls) == 1


def test_claim_rechecks_message_snapshot_identity(live) -> None:
    _camp, mid = email1(live)

    def reapprove(_q: str) -> None:
        def work(s, now):
            message = s.get(Message, mid)
            approvals.revoke(s, message, actor=Actor.APPLICANT, now=now)
            approvals.approve(
                s,
                message,
                actor=Actor.APPLICANT,
                now=now,
                settings=load_settings(s),
                specialty_display_name="Ophthalmology",
            )

        in_other_transaction(live, work)

    live.gmail.on_search = reapprove
    assert live.dispatch(mid).reason == "changed_during_preflight"
    assert live.gmail.send_calls == []
    with transaction(live.factory) as s:
        assert s.scalars(select(Message)).one().state == MessageState.APPROVED_QUEUED.value
