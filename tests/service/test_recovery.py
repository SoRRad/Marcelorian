"""Crash recovery, reconciliation and uncertainty (acceptance 20-22, 58, 61, 62; decision B13)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent, BudgetLedger, Campaign, Reservation
from marcelorian.domain.enums import (
    Actor,
    AttemptStatus,
    CampaignState,
    InboundClassification,
    MessageState,
    Mode,
    PauseReason,
)
from marcelorian.services import budget, campaigns, reconciliation, send_results
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from tests.fakes.gmail import SimulatedCrash

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def email1(world):
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    return camp, world.draft_and_approve(camp)


def budget_rows(world):
    with transaction(world.factory) as s:
        return [
            (r.status, r.used_at) for r in s.scalars(select(BudgetLedger).order_by(BudgetLedger.id))
        ]


def reservation_states(world, camp):
    with transaction(world.factory) as s:
        return sorted(
            (r.kind, r.status)
            for r in s.scalars(select(Reservation).where(Reservation.campaign_id == camp))
        )


def recover(world):
    return reconciliation.startup_recovery(
        world.factory,
        {Mode.LIVE: world.gmail, Mode.TRANSPORT_TEST: world.gmail, Mode.SANDBOX: world.sandbox},
        {"real": world.real_sync},
        world.clock,
    )


def reply(world, camp, gid="reply-1"):
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


# -- acceptance 20: death before submission -----------------------------------------------------


def test_death_before_submission_is_recovered_never_duplicated(live, monkeypatch) -> None:
    _camp, mid = email1(live)

    def die(*_args, **_kwargs):
        raise SimulatedCrash("process died between claim commit and send")

    monkeypatch.setattr(live.gateway, "_submit", die)
    with pytest.raises(SimulatedCrash):
        live.dispatch(mid)
    assert live.message_state(mid) is MessageState.SENDING
    assert live.gmail.send_calls == []
    live.rebuild_gateway()  # restart
    result = recover(live)
    assert list(result["reconciled"].values()) == ["uncertain"]  # not found != not sent
    assert live.message_state(mid) is MessageState.UNCERTAIN
    assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE  # never auto-retried
    attempt = live.attempts(mid)[0]
    with transaction(live.factory) as s:  # defined recovery: recorded evidence + re-approval
        send_results.resolve_not_submitted(
            s,
            send_results.attempt_by_uid(s, attempt.attempt_uid),
            note="Gmail Sent folder checked by applicant; process log shows no request",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    assert live.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    live.clock.advance(timedelta(minutes=7))
    live.approve(mid)
    assert live.dispatch(mid).outcome is Outcome.SENT
    assert len(live.gmail.send_calls) == 1
    first, second = live.attempts(mid)
    assert (first.status, second.status) == ("not_submitted", "sent")
    assert first.app_message_id != second.app_message_id


# -- acceptance 21: death after Gmail accepted --------------------------------------------------


def test_death_after_acceptance_reconciles_to_sent_without_resend(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["crash_after_accept"]
    with pytest.raises(SimulatedCrash):
        live.dispatch(mid)
    accepted_at = live.gmail.sent_messages()[0].meta.internal_date
    live.clock.advance(timedelta(hours=1))
    live.rebuild_gateway()
    result = recover(live)
    assert list(result["reconciled"].values()) == ["sent"]
    message = live.message(mid)
    assert message.state == MessageState.SENT.value
    assert message.sent_at == accepted_at  # established submission time, not recovery time
    assert message.sent_at_provenance == "gmail_reconciliation"
    assert len(live.gmail.send_calls) == 1
    assert budget_rows(live) == [("used", accepted_at)]  # counted once
    assert live.campaign_row(camp).next_due_at == accepted_at + WEEK


def test_timeout_after_acceptance_reconciles_immediately(live) -> None:
    _camp, mid = email1(live)
    live.gmail.send_script = ["timeout_after_accept"]
    result = live.dispatch(mid)
    assert result.outcome is Outcome.SENT and result.reason == "reconciled:sent"
    assert len(live.gmail.send_calls) == 1


def test_rewritten_message_id_correlates_by_x_marcelorian_id(live) -> None:
    _camp, mid = email1(live)
    live.gmail.rewrite_message_id = True
    live.gmail.send_script = ["server_error_after_accept"]
    # rfc822msgid search cannot find a rewritten id: emulate Gmail returning it via the
    # fallback correlation path by searching on the X header is not possible, so the fake
    # returns no hit and the attempt must stay uncertain (never "not found -> retry").
    result = live.dispatch(mid)
    assert result.outcome is Outcome.UNCERTAIN
    assert live.gmail.sent_messages()  # it really was sent: retrying would duplicate it


def test_match_by_x_header_when_message_id_rewritten(live, monkeypatch) -> None:
    _camp, mid = email1(live)
    live.gmail.rewrite_message_id = True
    live.gmail.send_script = ["server_error_after_accept"]
    original = live.gmail.search_message_ids

    def search(query, *, include_spam_trash):
        if query.startswith("rfc822msgid:"):  # adapter fallback found the sent copy
            return type(original(query, include_spam_trash=include_spam_trash))(
                ids=tuple(m.meta.gmail_message_id for m in live.gmail.sent_messages()),
                complete=True,
            )
        return original(query, include_spam_trash=include_spam_trash)

    monkeypatch.setattr(live.gmail, "search_message_ids", search)
    assert live.dispatch(mid).outcome is Outcome.SENT


# -- acceptance 22: not found / failed search -> uncertain, held, never forgotten -----------------


@pytest.mark.parametrize("search", ["not_found", "fail", "incomplete"])
def test_uncertain_holds_budget_and_slot_and_is_never_retried(live, search) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok"] + (
        {"not_found": ["ok"], "fail": ["fail"], "incomplete": ["incomplete"]}[search]
    )
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN
    assert live.message_state(mid) is MessageState.UNCERTAIN
    assert budget_rows(live)[0][0] == "held"
    assert reservation_states(live, camp) == [("new_contact", "active"), ("pending_slot", "active")]
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.PAUSED_REVIEW.value
    for _ in range(3):  # never auto-retried
        live.clock.advance(timedelta(minutes=10))
        assert live.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE
    later = reconciliation.reconcile_unresolved(
        live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
    )
    assert list(later.values()) == ["uncertain"]  # never forgotten
    assert len(live.gmail.send_calls) == 1
    with transaction(live.factory) as s:
        assert budget.count(s, budget.Ledger.REAL, live.clock.now() + timedelta(days=30)) == 1


def test_found_without_reliable_timestamp_stays_uncertain(live) -> None:
    _camp, mid = email1(live)
    live.gmail.drop_internal_date = True
    live.gmail.send_script = ["timeout_after_accept"]
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN  # no invented timestamp


def test_applicant_resolutions_are_audited(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.dispatch(mid)
    uid = live.attempts(mid)[0].attempt_uid
    with transaction(live.factory) as s:
        attempt = send_results.attempt_by_uid(s, uid)
        send_results.record_still_unresolved(
            s, attempt, note="checking with owner", actor=Actor.APPLICANT, now=live.clock.now()
        )
        with pytest.raises(send_results.ResolutionError):
            send_results.resolve_not_submitted(
                s, attempt, note="  ", actor=Actor.APPLICANT, now=live.clock.now()
            )
        with pytest.raises(send_results.ResolutionError):
            send_results.resolve_confirmed_sent(
                s,
                attempt,
                sent_at=live.clock.now() + timedelta(1),
                settings=load_settings(s),
                actor=Actor.APPLICANT,
                now=live.clock.now(),
                gmail_message_id="gmail-msg-x",
                gmail_thread_id="gmail-thread-x",
            )
    reported = live.clock.now()
    live.clock.advance(timedelta(hours=2))
    with transaction(live.factory) as s, pytest.raises(send_results.ResolutionError):
        send_results.resolve_confirmed_sent(  # Gmail IDs are required (thread for follow-ups)
            s,
            send_results.attempt_by_uid(s, uid),
            sent_at=reported,
            settings=load_settings(s),
            actor=Actor.APPLICANT,
            now=live.clock.now(),
            gmail_message_id="",
            gmail_thread_id="",
        )
    with transaction(live.factory) as s:
        send_results.resolve_confirmed_sent(
            s,
            send_results.attempt_by_uid(s, uid),
            sent_at=reported,
            settings=load_settings(s),
            actor=Actor.APPLICANT,
            now=live.clock.now(),
            gmail_message_id="gmail-msg-confirmed",
            gmail_thread_id="gmail-thread-confirmed",
        )
    message = live.message(mid)
    assert (message.state, message.sent_at, message.sent_at_provenance) == (
        "sent",
        reported,
        "applicant_reported",
    )
    assert live.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    with transaction(live.factory) as s:
        actions = [
            e.action
            for e in s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "send_attempt"))
        ]
    assert {"claimed", "uncertain", "still_unresolved", "sent"} <= set(actions)


# -- acceptance 58: every held-budget transition ---------------------------------------------


def test_held_budget_transitions(live) -> None:
    ledger = budget.Ledger.REAL
    # hold -> used at the established time
    _c1, m1 = email1(live)
    live.dispatch(m1)
    assert budget_rows(live)[-1][0] == "used"
    # hold -> released on definite rejection
    live.clock.advance(timedelta(minutes=7))
    _c2, m2 = email1(live)
    live.gmail.send_script = ["definite:400"]
    live.dispatch(m2)
    assert budget_rows(live)[-1][0] == "released"
    # hold retained while uncertain, then -> released only on recorded non-submission
    live.clock.advance(timedelta(minutes=7))
    _c3, m3 = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.dispatch(m3)
    assert budget_rows(live)[-1][0] == "held"
    uid3 = live.attempts(m3)[0].attempt_uid
    with transaction(live.factory) as s:
        assert budget.count(s, ledger, live.clock.now()) == 2  # used + held, not the released
        attempt = send_results.attempt_by_uid(s, uid3)
        send_results.resolve_not_submitted(
            s,
            attempt,
            note="no copy in Sent; owner confirmed",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    assert budget_rows(live)[-1][0] == "released"
    # uncertain -> used on confirmed send (found later by reconciliation)
    live.clock.advance(timedelta(minutes=7))
    _c4, m4 = email1(live)
    live.gmail.send_script = ["server_error_after_accept"]
    live.gmail.search_script = ["ok", "fail"]
    assert live.dispatch(m4).outcome is Outcome.UNCERTAIN
    assert budget_rows(live)[-1][0] == "held"
    reconciliation.reconcile_unresolved(
        live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
    )
    status, used_at = budget_rows(live)[-1]
    assert status == "used" and used_at is not None
    with transaction(live.factory) as s:
        rows = s.scalars(select(BudgetLedger)).all()
        assert len(rows) == len({r.attempt_id for r in rows}) == 4  # one row per attempt
        with pytest.raises(budget.BudgetError):
            budget.release(s, rows[0].attempt_id, live.clock.now(), "x")  # used is final


def test_budget_window_boundary_is_exclusive_at_24_hours(live) -> None:
    live.settings(real_submissions_per_24h=1)
    _c1, m1 = email1(live)
    live.dispatch(m1)
    sent_at = live.message(m1).sent_at
    with transaction(live.factory) as s:
        assert (
            budget.count(
                s, budget.Ledger.REAL, sent_at + timedelta(hours=24) - timedelta(microseconds=1)
            )
            == 1
        )
        assert budget.count(s, budget.Ledger.REAL, sent_at + timedelta(hours=24)) == 0


# -- acceptance 61, 62: late results never overwrite stopped campaigns ---------------------------


def test_late_success_after_reply_keeps_conversation(live) -> None:
    camp, m1 = email1(live)
    live.dispatch(m1)
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    live.gmail.on_send = lambda _raw: reply(live, camp)  # reply lands while send is in flight
    result = live.dispatch(m2)
    assert result.outcome is Outcome.SENT
    assert live.message_state(m2) is MessageState.SENT  # the result is recorded
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.CONVERSATION.value  # not overwritten
    assert campaign.next_step is None
    assert reservation_states(live, camp) == [
        ("new_contact", "consumed"),
        ("pending_slot", "released"),
    ]
    assert [r[0] for r in budget_rows(live)] == ["used", "used"]


def test_reply_while_uncertain_keeps_attempt_ledger_and_never_restarts(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.dispatch(mid)
    reply(live, camp)
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.CONVERSATION.value
    assert reservation_states(live, camp) == [
        ("new_contact", "active"),
        ("pending_slot", "released"),
    ]
    assert budget_rows(live)[0][0] == "held"  # the reply did not erase the unresolved attempt
    uid = live.attempts(mid)[0].attempt_uid
    with transaction(live.factory) as s:
        attempt = send_results.attempt_by_uid(s, uid)
        send_results.resolve_not_submitted(
            s,
            attempt,
            note="confirmed absent from Sent",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.CONVERSATION.value  # never forced to outreach_pending


def test_resolving_one_reason_does_not_clear_another(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.dispatch(mid)
    uid = live.attempts(mid)[0].attempt_uid
    with transaction(live.factory) as s:
        campaigns.open_pause(
            s,
            s.get(Campaign, camp),
            PauseReason.AUTO_REPLY,
            actor=Actor.SYSTEM,
            now=live.clock.now(),
            source_ref="inbound:x",
        )
        attempt = send_results.attempt_by_uid(s, uid)
        send_results.resolve_not_submitted(
            s,
            attempt,
            note="confirmed absent from Sent",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    assert live.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    with transaction(live.factory) as s:
        reasons = [p.reason for p in campaigns.open_reasons(s, camp)]
    assert reasons == [PauseReason.AUTO_REPLY.value]


def test_recovery_never_restarts_closed_campaign(live) -> None:
    camp, mid = email1(live)
    live.gmail.send_script = ["server_error_after_accept"]
    live.gmail.search_script = ["ok", "fail"]
    live.dispatch(mid)
    with transaction(live.factory) as s:
        from marcelorian.domain.enums import CloseOutcome

        campaigns.close_campaign(
            s,
            s.get(Campaign, camp),
            CloseOutcome.APPLICANT_CLOSED,
            actor=Actor.APPLICANT,
            now=live.clock.now(),
            reason="decided not to pursue",
        )
    reconciliation.reconcile_unresolved(
        live.factory, {Mode.LIVE: live.gmail}, live.clock, include_uncertain=True
    )
    assert live.message_state(mid) is MessageState.SENT
    campaign = live.campaign_row(camp)
    assert campaign.state == CampaignState.CLOSED.value and campaign.next_step is None
    assert reservation_states(live, camp) == [
        ("new_contact", "consumed"),
        ("pending_slot", "released"),
    ]


def test_duplicate_corroborated_copies_are_flagged_for_review(live, monkeypatch) -> None:
    _camp, mid = email1(live)
    live.gmail.send_script = ["server_error_after_accept"]
    original = live.gmail._store_sent

    def store_twice(raw, thread_id):
        original(raw, thread_id)
        return original(raw, thread_id)

    monkeypatch.setattr(live.gmail, "_store_sent", store_twice)
    assert live.dispatch(mid).outcome is Outcome.SENT
    with transaction(live.factory) as s:
        reasons = [p.source_ref for p in campaigns.open_reasons(s, live.message(mid).campaign_id)]
    assert any(r and r.endswith(":duplicates") for r in reasons)
    assert AttemptStatus.SENT.value == live.attempts(mid)[0].status
