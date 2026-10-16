"""Direct coverage for named sub-cases of the required P0 handoff tests.

Spec §19 tests 57 (recovery during an in-flight send), 59 (a lowered *real* 24 h budget
defers a live follow-up), 61 (a reconciled late success after a reply), 62 (recovery
never restarts a sequence stopped while its attempt is uncertain) and 65 (the
Message-ID is reused, never regenerated, across reconciliations), plus the decision D4
property tests for the rolling 24-hour and 30-day boundaries (spec §11, note 1).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger, Campaign, Message, Pi
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    InboundClassification,
    Ledger,
    MessageState,
    Mode,
)
from marcelorian.services import approvals, budget, reconciliation, reservations, send_results
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def _email1(world):
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    return camp, world.draft_and_approve(camp)


def _reply(world, camp, gid="reply-1"):
    with transaction(world.factory) as s:
        record_inbound(
            s,
            s.get(Campaign, camp),
            gmail_message_id=gid,
            thread_id=None,
            classification=InboundClassification.GENUINE_REPLY,
            from_address=None,
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )


def _budget_rows(world):
    with transaction(world.factory) as s:
        return [
            (r.status, r.used_at) for r in s.scalars(select(BudgetLedger).order_by(BudgetLedger.id))
        ]


# -- spec §19 test 57: concurrent claims incl. *recovery* ---------------------------------------


def test_recovery_running_during_an_in_flight_send_never_duplicates(live) -> None:
    _camp, mid = _email1(live)
    recovered: list[dict[str, str]] = []

    def recovery_tick(_raw: bytes) -> None:  # recovery path runs while Gmail call is open
        recovered.append(
            reconciliation.reconcile_unresolved(
                live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
            )
        )

    live.gmail.on_send = recovery_tick
    result = live.dispatch(mid)
    assert len(live.gmail.send_calls) == 1
    assert list(recovered[0].values()) == ["uncertain"]  # not yet visible: held, not resent
    assert result.outcome is Outcome.SENT
    assert live.message_state(mid) is MessageState.SENT
    assert [r[0] for r in _budget_rows(live)] == ["used"]  # counted once
    live.gmail.on_send = None
    for _ in range(2):
        live.clock.advance(timedelta(minutes=10))
        assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE
    assert len(live.gmail.send_calls) == 1


# -- handoff item 4 / spec §19 test 62: recovery never restarts a *stopped sequence* ------------


@pytest.mark.parametrize("resolution", ["confirmed_sent", "not_submitted"])
def test_uncertain_followup_recovery_after_stop_sequence_never_restarts(live, resolution) -> None:
    camp, m1 = _email1(live)
    live.dispatch(m1)
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok", "fail"]  # reply search ok; reconciliation fails
    assert live.dispatch(m2).outcome is Outcome.UNCERTAIN
    with transaction(live.factory) as s:  # "Stop this follow-up sequence" while uncertain
        approvals.stop_sequence(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
        )
    assert live.message_state(m2) is MessageState.UNCERTAIN  # Cancel/Stop never clears it
    uid = live.attempts(m2)[0].attempt_uid
    thread_id = live.message(m1).gmail_thread_id
    live.clock.advance(timedelta(hours=1))
    with transaction(live.factory) as s:
        attempt = send_results.attempt_by_uid(s, uid)
        if resolution == "confirmed_sent":
            send_results.resolve_confirmed_sent(
                s,
                attempt,
                sent_at=attempt.claimed_at,
                settings=load_settings(s),
                actor=Actor.APPLICANT,
                now=live.clock.now(),
                gmail_message_id="gm-2",
                gmail_thread_id=thread_id,
            )
        else:
            send_results.resolve_not_submitted(
                s, attempt, note="absent from Sent", actor=Actor.APPLICANT, now=live.clock.now()
            )
    campaign = live.campaign_row(camp)
    assert campaign.sequence_stopped  # recovery did not restart the stopped sequence
    assert campaign.state == CampaignState.OUTREACH_PENDING.value  # slot kept (B5)
    if resolution == "not_submitted":
        with pytest.raises(ApprovalError) as info:  # no re-approval without deliberate re-draft
            live.approve(m2)
        assert "sequence_stopped" in info.value.reasons
    assert len(live.gmail.send_calls) == 2


# -- handoff item 4 / spec §19 test 61: a *reconciled* late success after a reply ---------------


def test_reconciled_late_success_after_reply_keeps_conversation(live) -> None:
    camp, m1 = _email1(live)
    live.dispatch(m1)
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    live.gmail.send_script = ["server_error_after_accept"]
    live.gmail.search_script = ["ok", "fail"]  # new-thread reply search ok; reconciliation fails
    assert live.dispatch(m2).outcome is Outcome.UNCERTAIN
    _reply(live, camp)
    assert live.campaign_row(camp).state == CampaignState.CONVERSATION.value
    assert _budget_rows(live)[-1][0] == "held"
    live.clock.advance(timedelta(hours=2))
    reconciliation.reconcile_unresolved(
        live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
    )
    assert live.message_state(m2) is MessageState.SENT
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.CONVERSATION.value and campaign.next_step is None
    status, used_at = _budget_rows(live)[-1]
    assert status == "used" and used_at == live.gmail.sent_messages()[-1].meta.internal_date


# -- spec §19 test 65: Message-ID unchanged through reconciliation ------------------------------


def test_message_id_is_never_regenerated_across_reconciliations(live) -> None:
    _camp, mid = _email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok"]  # prior-contact search; reconciliation finds nothing
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN
    attempt = live.attempts(mid)[0]
    for _ in range(3):
        live.clock.advance(timedelta(hours=1))
        reconciliation.reconcile_unresolved(
            live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
        )
    queries = [q for q in live.gmail.search_queries if q.startswith("rfc822msgid:")]
    assert len(queries) == 4  # the immediate reconciliation + 3 later ones
    assert set(queries) == {"rfc822msgid:" + attempt.app_message_id.strip("<>")}
    assert live.attempts(mid)[0].app_message_id == attempt.app_message_id


# -- spec §19 test 59 / B17: lowered *real* 24 h budget defers a live follow-up ------------------


def test_lowered_real_budget_defers_live_followup(live) -> None:
    camp, m1 = _email1(live)
    live.dispatch(m1)
    live.clock.advance(WEEK - timedelta(hours=1))
    _other, mo = _email1(live)
    assert live.dispatch(mo).outcome is Outcome.SENT  # one real submission in the last 24 h
    live.clock.advance(timedelta(hours=1))
    m2 = live.draft_and_approve(camp, 2)
    live.settings(real_submissions_per_24h=1)
    result = live.dispatch(m2)
    assert (result.outcome, result.reason) == (Outcome.DEFERRED, "budget")
    assert live.message(m2).cap_block_reason == "budget_24h"


# -- D4: property-based tests for the rolling boundaries ----------------------------------------


@pytest.fixture
def one_send(live):
    """One real submission (budget) and one consumed new contact, both at ``sent_at``."""
    _camp, mid = _email1(live)
    assert live.dispatch(mid).outcome is Outcome.SENT
    with transaction(live.factory) as s:
        message = s.get(Message, mid)
        inst_id = s.get(Pi, s.get(Campaign, message.campaign_id).pi_id).institution_id
        return live, message.sent_at, inst_id


@settings(
    max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(offset=st.timedeltas(min_value=timedelta(hours=-1), max_value=timedelta(days=40)))
def test_property_rolling_boundaries_match_the_documented_rule(one_send, offset) -> None:
    world, sent_at, inst_id = one_send  # examples only read, so sharing the fixture is safe
    now = sent_at + offset
    with transaction(world.factory) as s:
        used = budget.count(s, Ledger.REAL, now)
        contacts = reservations.count_new_contacts(s, Mode.LIVE, [inst_id], now)
    # Rule (spec §11, note 1): counts iff t > now - W; no upper bound.
    assert used == (1 if sent_at > now - timedelta(hours=24) else 0)
    assert contacts == (1 if sent_at > now - timedelta(days=30) else 0)


@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(offset=st.integers(min_value=-3, max_value=3))
def test_property_exact_boundary_instants(one_send, offset) -> None:
    world, sent_at, inst_id = one_send
    tick = timedelta(microseconds=offset)
    with transaction(world.factory) as s:
        at_day = budget.count(s, Ledger.REAL, sent_at + timedelta(hours=24) + tick)
        at_month = reservations.count_new_contacts(
            s, Mode.LIVE, [inst_id], sent_at + timedelta(days=30) + tick
        )
    assert at_day == (1 if offset < 0 else 0)
    assert at_month == (1 if offset < 0 else 0)
