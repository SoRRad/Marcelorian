"""Regressions for defects found by the P0 adversarial review (every one was reproduced
first). Each test names the decision or spec rule it protects."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from marcelorian.clock import ManualClock
from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message, Pi, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    CloseOutcome,
    InboundClassification,
    Ledger,
    MessageState,
    Mode,
)
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.domain.timing import resolve_local
from marcelorian.services import (
    approvals,
    budget,
    campaigns,
    dnc,
    reconciliation,
    reservations,
    runtime,
    scheduling,
    send_results,
)
from marcelorian.services import sync as sync_svc
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from tests.factories import APPLICANT_EMAIL
from tests.fakes.gmail import SimulatedCrash

NY = ZoneInfo("America/New_York")
WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def active_kinds(world, camp):
    with transaction(world.factory) as s:
        return sorted(
            r.kind
            for r in s.scalars(
                select(Reservation).where(
                    Reservation.campaign_id == camp, Reservation.status == "active"
                )
            )
        )


def reply(world, camp, gid):
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


def close(world, camp, outcome=CloseOutcome.NO_OPENING):
    with transaction(world.factory) as s:
        campaigns.close_campaign(
            s, s.get(Campaign, camp), outcome, actor=Actor.APPLICANT, now=world.clock.now()
        )


# -- duplicate outreach (spec §3; B14) -----------------------------------------------------


def test_rediscovered_record_with_contacted_address_gets_no_second_email1(live) -> None:
    inst = live.institution()
    first = live.campaign(live.pi(inst, email="same.person@synthetic-univ.test"), Mode.LIVE)
    live.dispatch(live.draft_and_approve(first))
    close(live, first)
    rediscovered = live.pi(inst, email="same.person@synthetic-univ.test")  # not merged
    second = live.campaign(rediscovered, Mode.LIVE)
    with pytest.raises(ApprovalError) as info:
        live.approve(live.draft(second))
    assert "duplicate_outreach" in info.value.reasons
    assert len(live.gmail.send_calls) == 1


def test_merge_after_approval_blocks_both_initials_until_resolved(live) -> None:
    inst = live.institution()
    pi_a = live.pi(inst, email="a.example@synthetic-univ.test")
    pi_b = live.pi(inst, email="alex.example@synthetic-univ.test")
    camp_a, camp_b = live.campaign(pi_a, Mode.LIVE), live.campaign(pi_b, Mode.LIVE)
    m_a, m_b = live.draft_and_approve(camp_a), live.draft_and_approve(camp_b)
    with transaction(live.factory) as s:
        s.get(Pi, pi_b).merged_into_id = pi_a  # applicant confirms: the same person
    assert live.dispatch(m_a).reason == "duplicate_outreach"
    assert live.dispatch(m_b).reason == "duplicate_outreach"
    assert live.gmail.send_calls == []
    with transaction(live.factory) as s:  # the applicant keeps one sequence
        approvals.cancel_initial(
            s, s.get(Campaign, camp_b), actor=Actor.APPLICANT, now=live.clock.now()
        )
    live.approve(m_a)
    assert live.dispatch(m_a).outcome is Outcome.SENT
    assert len(live.gmail.send_calls) == 1


def test_messages_from_other_campaigns_count_as_prior_contact(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist("tester@example.test")
    world.set_mode(Mode.TRANSPORT_TEST)
    first = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    world.dispatch(world.draft_and_approve(first))
    close(world, first)
    world.clock.advance(timedelta(minutes=10))
    second = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    m2 = world.draft_and_approve(second)
    assert world.dispatch(m2).reason == "prior_contact_found"  # B14: no silent exclusion


# -- do-not-contact through exact addresses (C7) ----------------------------------------------


def test_dnc_on_a_pi_record_blocks_another_record_with_the_same_address(world) -> None:
    inst = world.institution()
    listed = world.pi(inst, email="same.person@synthetic-univ.test")
    with transaction(world.factory) as s:
        dnc.add_entry(
            s, scope_mode=Mode.LIVE, pi_id=listed, actor=Actor.APPLICANT, now=world.clock.now()
        )
    rediscovered = world.pi(inst, email="Same.Person@synthetic-univ.test")
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(rediscovered)))
    assert "do_not_contact" in info.value.reasons


# -- mode isolation (C9) ----------------------------------------------------------------------


def test_uncertain_sandbox_attempt_never_blocks_live(world, monkeypatch) -> None:
    pi_id = world.pi(world.institution())
    sbx = world.draft_and_approve(world.campaign(pi_id))

    def die(*_a, **_k):
        raise SimulatedCrash("process died during sandbox practice")

    monkeypatch.setattr(world.gateway, "_submit", die)
    with pytest.raises(SimulatedCrash):
        world.dispatch(sbx)
    monkeypatch.undo()
    world.rebuild_gateway()
    reconciliation.reconcile_unresolved(world.factory, {Mode.SANDBOX: world.sandbox}, world.clock)
    assert world.message_state(sbx) is MessageState.UNCERTAIN
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    live_msg = world.draft_and_approve(world.campaign(pi_id, Mode.LIVE))
    assert world.dispatch(live_msg).outcome is Outcome.SENT


# -- future-dated established send time (spec §11; B17) ---------------------------------------


def _send_with_gmail_clock_ahead(world, mid, ahead=timedelta(minutes=9)):
    original = world.gmail.get_message_metadata

    def shifted(gid):
        meta = original(gid)
        if "SENT" in meta.label_ids and meta.internal_date is not None:
            meta = dataclasses.replace(meta, internal_date=meta.internal_date + ahead)
        return meta

    world.gmail.get_message_metadata = shifted
    world.gmail.send_script = ["timeout_after_accept"]
    try:
        assert world.dispatch(mid).outcome is Outcome.SENT
    finally:
        world.gmail.get_message_metadata = original


def test_future_dated_usage_counts_in_budget_and_new_contacts(live) -> None:
    live.settings(real_submissions_per_24h=1, new_contacts_per_institution_30d=1)
    inst = live.institution()
    first = live.campaign(live.pi(inst), Mode.LIVE)
    _send_with_gmail_clock_ahead(live, live.draft_and_approve(first))
    assert live.message(live.message_for(first, 1).id).sent_at > live.clock.now()
    live.clock.advance(timedelta(minutes=6))
    with transaction(live.factory) as s:
        assert budget.count(s, Ledger.REAL, live.clock.now()) == 1
    second = live.campaign(live.pi(inst), Mode.LIVE)
    with pytest.raises(reservations.CapacityError):
        live.approve(live.draft(second))
    third = live.campaign(live.pi(live.institution()), Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(third)).reason == "budget"


# -- transport_test self-send (spec §12; runbook T1) ------------------------------------------


def test_self_send_is_connectivity_only(world) -> None:
    world.connect_mailbox()
    me = world.allowlist(APPLICANT_EMAIL)
    world.set_mode(Mode.TRANSPORT_TEST)
    for _ in range(3):  # a busy personal mailbox
        world.gmail.inject(sender="friend@example.org", to=APPLICANT_EMAIL)
        world.gmail.inject(sender=APPLICANT_EMAIL, to="friend@example.org")
    camp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=me
    )
    assert world.dispatch(world.draft_and_approve(camp)).outcome is Outcome.SENT


# -- auth block (spec §15) --------------------------------------------------------------------


def test_verified_reconnect_clears_auth_block(live) -> None:
    m1 = live.draft_and_approve(live.campaign(live.pi(live.institution()), Mode.LIVE))
    live.gmail.send_script = ["definite:401"]
    live.dispatch(m1)
    with transaction(live.factory) as s:
        assert runtime.get_runtime(s, live.clock.now()).auth_blocked
        approvals.connect_mailbox(
            s, APPLICANT_EMAIL, frozenset(), actor=Actor.APPLICANT, now=live.clock.now()
        )  # missing scopes: stays blocked
        assert runtime.get_runtime(s, live.clock.now()).auth_blocked
        approvals.connect_mailbox(
            s, APPLICANT_EMAIL, REQUIRED_SCOPES, actor=Actor.APPLICANT, now=live.clock.now()
        )
        assert not runtime.get_runtime(s, live.clock.now()).auth_blocked
    live.clock.advance(timedelta(minutes=10))
    m2 = live.draft_and_approve(live.campaign(live.pi(live.institution()), Mode.LIVE))
    assert live.dispatch(m2).outcome is Outcome.SENT


# -- cancellation during preflight with prior-contact hits (B10) ------------------------------


def test_cancel_during_prior_contact_search_with_hits_does_not_crash(live) -> None:
    pi = live.pi(live.institution(), email="known@synthetic-univ.test")
    live.gmail.inject(sender="known@synthetic-univ.test", to=APPLICANT_EMAIL)
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)

    def cancel(_q):
        with transaction(live.factory) as s:
            approvals.cancel_initial(
                s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
            )

    live.gmail.on_search = cancel
    result = live.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.NOT_ELIGIBLE, "changed_during_preflight")
    assert live.campaign_row(camp).state == CampaignState.CANDIDATE.value


# -- a cancel recorded during submission completes on definite non-submission (B5) ----------


def test_cancel_during_flight_then_definite_rejection_completes_cancellation(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    live.gmail.send_script = ["definite:400"]

    def cancel(_raw):
        with transaction(live.factory) as s:
            approvals.cancel_initial(
                s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
            )

    live.gmail.on_send = cancel
    assert live.dispatch(mid).outcome is Outcome.FAILED_DEFINITE
    assert live.message_state(mid) is MessageState.CANCELLED
    assert live.campaign_row(camp).state == CampaignState.CANDIDATE.value
    assert active_kinds(live, camp) == []  # no hidden lock (B6)
    live.gmail.on_send = None
    live.clock.advance(timedelta(minutes=7))
    live.draft_and_approve(camp)  # a deliberate new revision clears the old request
    assert live.dispatch(live.message_for(camp, 1).id).outcome is Outcome.SENT


def test_cancel_during_flight_then_uncertain_resolved_not_submitted(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    live.gmail.send_script = ["timeout_before_accept"]

    def cancel(_raw):
        with transaction(live.factory) as s:
            approvals.cancel_initial(
                s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
            )

    live.gmail.on_send = cancel
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN
    uid = live.attempts(mid)[0].attempt_uid
    with transaction(live.factory) as s:
        send_results.resolve_not_submitted(
            s,
            send_results.attempt_by_uid(s, uid),
            note="absent from Sent; owner confirmed",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    assert live.message_state(mid) is MessageState.CANCELLED
    assert live.campaign_row(camp).state == CampaignState.CANDIDATE.value


def test_pending_cancel_blocks_reapproval(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    with transaction(live.factory) as s:  # legacy/odd state: flag set on a failed message
        message = s.get(Message, mid)
        message.state = MessageState.FAILED_DEFINITE.value
        message.cancel_requested_at = live.clock.now()
    with pytest.raises(ApprovalError) as info:
        live.approve(mid)
    assert "cancel_pending" in info.value.reasons


# -- projected new-contact reservations never leak (spec §11; B6, B13) -------------------------


def _blocked_later(world, inst) -> bool:
    world.clock.advance(timedelta(days=60))
    other = world.campaign(world.pi(inst), Mode.LIVE)
    world.draft(other)
    with transaction(world.factory) as s:
        result = reservations.evaluate_initial(
            s, s.get(Campaign, other), load_settings(s), world.clock.now()
        )
    return "new_contacts_30d_full" in result.reasons


@pytest.mark.parametrize(
    "scenario",
    [
        "closed_then_not_submitted",
        "closed_then_rejected",
        "reply_then_not_submitted",
        "reply_before_dispatch",
    ],
)
def test_projected_contact_released_when_no_longer_needed(live, scenario) -> None:
    live.settings(new_contacts_per_institution_30d=1)
    inst = live.institution()
    camp = live.campaign(live.pi(inst), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    if scenario == "reply_before_dispatch":
        reply(live, camp, "early")
    else:
        live.gmail.send_script = (
            ["definite:400"] if scenario == "closed_then_rejected" else ["timeout_before_accept"]
        )
        if scenario == "closed_then_rejected":
            live.gmail.on_send = lambda _raw: close(live, camp)
        live.dispatch(mid)
        live.gmail.on_send = None
        if scenario == "closed_then_not_submitted":
            close(live, camp)
        if scenario == "reply_then_not_submitted":
            reply(live, camp, "r1")
        if scenario != "closed_then_rejected":
            assert "new_contact" in active_kinds(live, camp)  # held while unresolved
            uid = live.attempts(mid)[0].attempt_uid
            with transaction(live.factory) as s:
                send_results.resolve_not_submitted(
                    s,
                    send_results.attempt_by_uid(s, uid),
                    note="absent from Sent",
                    actor=Actor.APPLICANT,
                    now=live.clock.now(),
                )
    assert active_kinds(live, camp) == []
    assert not _blocked_later(live, inst)


# -- missed window + edited re-approval (S7: the window rolls) ------------------------------


def test_edit_after_a_rolled_window_reapproves_and_sends(factory) -> None:
    from tests.factories import World

    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))
    with transaction(world.factory) as s:
        rolled = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert rolled == [(mid, "rolled")]
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    world.clock.set(resolve_local(datetime(2026, 10, 21, 9, 0), NY))
    world.draft(camp, body="Dear Dr. Example,\n\nRevised wording.\n")
    world.approve(mid)
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    assert world.dispatch(mid).outcome is Outcome.SENT


def test_missed_window_rule_uses_current_window_policy(factory) -> None:
    from tests.factories import World

    world = World(factory, ManualClock(resolve_local(datetime(2026, 11, 10, 11, 30), NY)))
    world.narrow_window()
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)  # assigned Wed 2026-11-11 (Veterans Day)
    world.settings(blackout_federal_holidays=True)  # holiday now blacked out
    world.clock.set(resolve_local(datetime(2026, 11, 11, 11, 5), NY))  # laptop off all day
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]  # nothing could have been considered that day


def test_window_narrowed_after_approval_still_rolls(factory) -> None:
    from tests.factories import World

    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    world.narrow_window()
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)  # assigned Tue 08:00-11:00
    world.settings(window_end="09:00")  # narrowed after approval
    world.clock.set(resolve_local(datetime(2026, 10, 20, 9, 30), NY))
    assert world.dispatch(mid).reason == "outside_window"
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 0), NY))
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]
    message = world.message(mid)
    assert message.eligible_window_end == resolve_local(datetime(2026, 10, 21, 9, 0), NY)


def test_unloadable_zone_is_contained_per_message(factory) -> None:
    from tests.factories import World

    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    bad = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    good = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    with transaction(world.factory) as s:
        s.get(Message, bad).pi_timezone = "Mars/Olympus_Mons"  # corrupted record
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))
    with transaction(world.factory) as s:
        actions = dict(scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s)))
    assert actions == {bad: "needs_reapproval", good: "rolled"}  # contained, no abort


# -- a sync gap blocks every send; the approval waits (S7) -------------------------------------


def test_sync_gap_blocks_sends_and_the_approval_waits(factory) -> None:
    from tests.factories import World

    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        sync_svc.record_sync_gap(s, "real", "history cursor expired", world.clock.now())
    world.settings(pending_per_institution=0)
    for hour in (8, 9, 10):
        world.clock.set(resolve_local(datetime(2026, 10, 20, hour, 30), NY))
        assert world.dispatch(mid).reason == "sync_gap"
    world.clock.set(resolve_local(datetime(2026, 10, 20, 16, 0), NY))
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]
    world.clock.set(resolve_local(datetime(2026, 10, 21, 9, 0), NY))
    assert world.dispatch(mid).reason == "sync_gap"  # still nothing sent until the gap clears


# -- B5 exit stays reachable after Cancel/Stop on a claimed message ----------------------------


def test_next_step_can_be_redrafted_after_cancel_during_claim(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    live.gmail.on_send = lambda _raw: _cancel(live, camp)
    assert live.dispatch(mid).outcome is Outcome.SENT
    live.gmail.on_send = None
    assert live.campaign_row(camp).sequence_stopped
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)  # deliberate re-draft of the next unsent step
    assert not live.campaign_row(camp).sequence_stopped
    assert live.dispatch(m2).outcome is Outcome.SENT


def _cancel(world, camp):
    with transaction(world.factory) as s:
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )


# -- drafted dependency versions (B18) ---------------------------------------------------------


def test_fact_changed_between_draft_and_approval_is_refused(world) -> None:
    fact = world.fact("current_role_phrase", "a research fellow at Alpha Eye Institute")
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft(camp, facts=(fact,))
    with transaction(world.factory) as s:
        from marcelorian.db.models import ProfileFact

        approvals.update_fact(
            s,
            s.get(ProfileFact, fact),
            "a fellow at Beta",
            confirm=True,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "dependency_changed_since_draft" in info.value.reasons
    world.draft(camp, facts=(fact,))  # re-drafted from the current version
    world.approve(mid)


def test_salutation_changed_between_draft_and_approval_is_refused(world) -> None:
    pi = world.pi(world.institution())
    camp = world.campaign(pi)
    mid = world.draft(camp)
    with transaction(world.factory) as s:
        approvals.set_salutation(
            s, s.get(Pi, pi), "Professor Example", actor=Actor.APPLICANT, now=world.clock.now()
        )
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "dependency_changed_since_draft" in info.value.reasons
