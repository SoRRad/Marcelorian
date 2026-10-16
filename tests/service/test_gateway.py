"""Gateway behaviour with the fake Gmail transport (acceptance 1, 13, 15, 16, 23, 24)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger, PacingState, Reservation
from marcelorian.domain.enums import CampaignState, MessageState, Mode
from marcelorian.domain.subjects import expected_subject
from marcelorian.domain.timing import resolve_local
from marcelorian.services import runtime
from marcelorian.services.gateway import Outcome
from tests.factories import APPLICANT_EMAIL, SPECIALTY

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def live_campaign(world, inst=None):
    return world.campaign(world.pi(inst or world.institution()), Mode.LIVE)


def parsed(raw: bytes):
    return message_from_bytes(raw, policy=default_policy)


def test_live_email1_end_to_end(live) -> None:
    camp = live_campaign(live)
    mid = live.draft_and_approve(camp)
    result = live.dispatch(mid)
    assert result.outcome is Outcome.SENT
    assert len(live.gmail.send_calls) == 1
    mime = parsed(live.gmail.send_calls[0])
    attempt = live.attempts(mid)[0]
    assert mime["From"] == APPLICANT_EMAIL
    assert mime["Message-ID"] == attempt.app_message_id
    assert attempt.app_message_id.endswith("@example.test>")
    assert mime["X-Marcelorian-Id"] == attempt.x_marcelorian_id
    assert mime["Subject"] == expected_subject(1, SPECIALTY)
    message = live.message(mid)
    assert message.state == MessageState.SENT.value
    assert message.sent_at == live.clock.now() and message.sent_at_provenance == "gateway_response"
    campaign = live.campaign_row(camp)
    assert campaign.next_step == 2 and campaign.next_due_at == message.sent_at + WEEK
    with transaction(live.factory) as s:
        ledger = s.scalars(select(BudgetLedger)).one()
        pacing = s.get(PacingState, "real")
    assert (ledger.ledger, ledger.status, ledger.used_at) == ("real", "used", message.sent_at)
    gap = pacing.next_allowed_at - pacing.last_submission_at
    assert timedelta(minutes=2) <= gap <= timedelta(minutes=6)


def test_followup_threads_with_actual_message_ids(live) -> None:
    camp = live_campaign(live)
    m1 = live.draft_and_approve(camp)
    live.dispatch(m1)
    first = live.message(m1)
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    assert live.dispatch(m2).outcome is Outcome.SENT
    mime = parsed(live.gmail.send_calls[-1])
    assert mime["In-Reply-To"] == first.rfc_message_id
    assert mime["References"] == first.rfc_message_id
    assert mime["Subject"] == "Re: " + expected_subject(1, SPECIALTY)
    assert live.message(m2).gmail_thread_id == first.gmail_thread_id
    assert list(parsed(live.gmail.send_calls[-1]).iter_attachments()) == []


def test_due_tuesday_approved_thursday_sent_thursday_next_interval_from_thursday(live) -> None:
    camp = live_campaign(live)
    live.dispatch(live.draft_and_approve(camp))  # Tue 09:00 NY
    due = live.campaign_row(camp).next_due_at
    assert due.astimezone(UTC).weekday() == 1
    thursday = resolve_local(datetime(2026, 10, 29, 9, 30), live_tz())
    live.clock.set(thursday)
    m2 = live.draft_and_approve(camp, 2)
    assert live.dispatch(m2).outcome is Outcome.SENT  # acceptance 13
    assert live.message(m2).sent_at == thursday
    assert live.campaign_row(camp).next_due_at == thursday + WEEK


def live_tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("America/New_York")


def test_no_send_starts_at_or_after_1100_local(live) -> None:
    camp = live_campaign(live)
    live.clock.set(resolve_local(datetime(2026, 10, 20, 10, 59, 59), live_tz()))
    mid = live.draft_and_approve(camp)
    live.clock.set(resolve_local(datetime(2026, 10, 20, 11, 0), live_tz()))
    result = live.dispatch(mid)
    assert result.outcome is Outcome.DEFERRED and result.reason == "window_passed"  # acceptance 15
    assert live.gmail.send_calls == []
    assert live.message_state(mid) is MessageState.APPROVED_QUEUED


def test_widened_window_sends_until_1600_monday_to_friday(live) -> None:
    """Owner decision W1: the window may be widened up to Mon-Fri 08:00-16:00."""
    live.settings(window_weekdays=[0, 1, 2, 3, 4], window_end="16:00")
    monday = resolve_local(datetime(2026, 10, 26, 15, 30), live_tz())
    live.clock.set(monday)
    m1 = live.draft_and_approve(live_campaign(live))
    assert live.dispatch(m1).outcome is Outcome.SENT
    assert live.message(m1).sent_at == monday
    live.clock.set(resolve_local(datetime(2026, 10, 26, 15, 59, 59), live_tz()))
    m2 = live.draft_and_approve(live_campaign(live, live.institution()))
    live.clock.set(resolve_local(datetime(2026, 10, 26, 16, 0), live_tz()))
    result = live.dispatch(m2)
    assert result.outcome is Outcome.DEFERRED and result.reason == "window_passed"
    assert len(live.gmail.send_calls) == 1
    live.clock.set(resolve_local(datetime(2026, 10, 31, 12, 0), live_tz()))  # Saturday
    m3 = live.draft_and_approve(live_campaign(live, live.institution()))
    window = live.message(m3)
    # The next Monday is after US DST ends; the window is still 08:00-16:00 local.
    assert window.eligible_window_start == resolve_local(datetime(2026, 11, 2, 8, 0), live_tz())
    assert window.eligible_window_end == resolve_local(datetime(2026, 11, 2, 16, 0), live_tz())


def test_default_window_is_unchanged_without_widening(live) -> None:
    live.clock.set(resolve_local(datetime(2026, 10, 26, 9, 0), live_tz()))  # Monday
    mid = live.draft_and_approve(live_campaign(live))
    message = live.message(mid)
    assert message.eligible_window_start == resolve_local(datetime(2026, 10, 27, 8, 0), live_tz())
    assert message.eligible_window_end == resolve_local(datetime(2026, 10, 27, 11, 0), live_tz())
    assert live.dispatch(mid).reason == "outside_window"


def test_pacing_spaces_sends_and_survives_restart(live) -> None:
    inst_a, inst_b = live.institution(), live.institution()
    m1 = live.draft_and_approve(live_campaign(live, inst_a))
    m2 = live.draft_and_approve(live_campaign(live, inst_b))
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.rebuild_gateway(seed=99)  # a restart must not reset spacing (B8)
    assert live.dispatch(m2).reason == "pacing"
    with transaction(live.factory) as s:
        allowed = s.get(PacingState, "real").next_allowed_at
    live.clock.set(allowed - timedelta(seconds=1))
    assert live.dispatch(m2).reason == "pacing"
    live.clock.set(allowed)
    assert live.dispatch(m2).outcome is Outcome.SENT  # acceptance 16


def test_budget_deferral_has_no_burst(live) -> None:
    live.settings(real_submissions_per_24h=1)
    m1 = live.draft_and_approve(live_campaign(live))
    m2 = live.draft_and_approve(live_campaign(live))
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.clock.advance(timedelta(minutes=10))
    result = live.dispatch(m2)
    assert (result.outcome, result.reason) == (Outcome.DEFERRED, "budget")
    assert live.message(m2).cap_block_reason == "budget_24h"


def test_definite_rejection_releases_budget_keeps_reservation(live) -> None:
    camp = live_campaign(live)
    mid = live.draft_and_approve(camp)
    live.gmail.send_script = ["definite:400"]
    result = live.dispatch(mid)
    assert result.outcome is Outcome.FAILED_DEFINITE  # acceptance 23
    assert live.message_state(mid) is MessageState.FAILED_DEFINITE
    with transaction(live.factory) as s:
        assert s.scalars(select(BudgetLedger)).one().status == "released"
        statuses = {r.kind: r.status for r in s.scalars(select(Reservation))}
    assert statuses == {"pending_slot": "active", "new_contact": "active"}  # B4
    assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE  # re-approval required
    live.clock.advance(timedelta(minutes=7))
    live.approve(mid)
    assert live.dispatch(mid).outcome is Outcome.SENT
    first, second = live.attempts(mid)
    assert first.app_message_id != second.app_message_id  # distinct per authorized attempt


def test_auth_failure_halts_all_dispatch(live) -> None:
    m1 = live.draft_and_approve(live_campaign(live))
    m2 = live.draft_and_approve(live_campaign(live))
    live.gmail.send_script = ["definite:401"]
    assert live.dispatch(m1).outcome is Outcome.FAILED_DEFINITE
    with transaction(live.factory) as s:
        assert runtime.get_runtime(s, live.clock.now()).auth_blocked
    live.clock.advance(timedelta(minutes=10))
    assert live.dispatch(m2).reason == "auth_required"
    assert len(live.gmail.send_calls) == 1


@pytest.mark.parametrize("problem", ["account", "scopes"])
def test_account_or_scope_mismatch_blocks(live, problem) -> None:
    mid = live.draft_and_approve(live_campaign(live))
    if problem == "account":
        live.gmail.account_email = "someone-else@example.test"
    else:
        live.gmail.scopes = frozenset({"https://www.googleapis.com/auth/gmail.send"})
    assert live.dispatch(mid).reason == "auth_required"
    assert live.gmail.send_calls == []


@pytest.mark.parametrize(("script", "reason"), [("fail", "sync_unavailable"), ("gap", "sync_gap")])
def test_sync_problems_block_real_sends(live, script, reason) -> None:
    mid = live.draft_and_approve(live_campaign(live))
    live.real_sync.script = [script]
    assert live.dispatch(mid).reason == reason
    assert live.gmail.send_calls == []
    if script == "gap":
        live.clock.advance(timedelta(minutes=10))
        assert live.dispatch(mid).reason == "sync_gap"  # stays blocked until gap reconciliation


def test_transport_unavailable_defers(live) -> None:
    from marcelorian.transport.base import TransportUnavailable

    mid = live.draft_and_approve(live_campaign(live))
    live.gmail.account_error = TransportUnavailable("offline")
    assert live.dispatch(mid).reason == "transport_unavailable"


def test_sent_message_is_never_dispatched_again(live) -> None:
    mid = live.draft_and_approve(live_campaign(live))
    assert live.dispatch(mid).outcome is Outcome.SENT
    for _ in range(3):
        live.clock.advance(timedelta(minutes=10))
        assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE  # acceptance 24 (no resend)
    assert len(live.gmail.send_calls) == 1


def test_campaign_under_review_defers(live) -> None:
    from marcelorian.db.models import Campaign
    from marcelorian.domain.enums import Actor, PauseReason
    from marcelorian.services import campaigns

    camp = live_campaign(live)
    mid = live.draft_and_approve(camp)
    with transaction(live.factory) as s:
        campaigns.open_pause(
            s,
            s.get(Campaign, camp),
            PauseReason.AUTO_REPLY,
            actor=Actor.SYSTEM,
            now=live.clock.now(),
        )
    assert live.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    assert live.dispatch(mid).reason == "campaign_not_sendable"
