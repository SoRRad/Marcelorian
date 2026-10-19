"""Mode isolation (acceptance 11, 43, 44, 60, 63, 69; decisions B2, B8, B9, B11, C7, C9)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalSnapshot,
    BudgetLedger,
    Campaign,
    PacingState,
    RecipientAllowlist,
)
from marcelorian.domain.enums import Actor, CampaignState, MessageState, Mode
from marcelorian.domain.recipients import RecipientError
from marcelorian.domain.snapshot import TestOverride, canonical_json, parse_canonical, sha256_hex
from marcelorian.domain.timing import resolve_local
from marcelorian.services import approvals, dnc, recipients, runtime
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.services.runtime import ModeSwitchError

NY = ZoneInfo("America/New_York")
TESTER = "tester@example.test"


def test_mode_switch_requires_confirmation_and_pauses(world) -> None:
    with transaction(world.factory) as s, pytest.raises(ModeSwitchError):
        runtime.switch_mode(
            s, Mode.TRANSPORT_TEST, confirmed=False, actor=Actor.APPLICANT, now=world.clock.now()
        )
    world.set_mode(Mode.TRANSPORT_TEST, resume=False)
    with transaction(world.factory) as s:
        view = runtime.get_runtime(s, world.clock.now())
    assert view.active_mode is Mode.TRANSPORT_TEST and view.dispatch_paused


def test_live_requires_readiness_checklist(world) -> None:
    with transaction(world.factory) as s, pytest.raises(ModeSwitchError):
        runtime.switch_mode(
            s, Mode.LIVE, confirmed=True, actor=Actor.APPLICANT, now=world.clock.now()
        )


def test_dispatch_waits_for_explicit_resume_after_switch(world) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE, resume=False)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(camp)
    assert world.dispatch(mid).reason == "paused"
    with transaction(world.factory) as s:
        runtime.resume_dispatch(s, actor=Actor.APPLICANT, now=world.clock.now())
    assert world.dispatch(mid).outcome is Outcome.SENT


def test_test_message_never_becomes_live(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist(TESTER)
    world.set_mode(Mode.TRANSPORT_TEST)
    camp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    mid = world.draft_and_approve(camp)
    world.set_mode(Mode.LIVE)
    assert world.dispatch(mid).reason == "mode_inactive"  # acceptance 11
    assert world.message(mid).mode == Mode.TRANSPORT_TEST.value
    with transaction(world.factory) as s:
        assert s.scalars(select(ApprovalSnapshot)).one().mode == Mode.TRANSPORT_TEST.value
    assert world.gmail.send_calls == []


@pytest.mark.narrow_window
def test_inactive_mode_messages_are_not_missed(world) -> None:
    from marcelorian.config.settings import load_settings
    from marcelorian.services import scheduling

    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.dispatch(mid)
    world.clock.advance(timedelta(hours=3))
    with transaction(world.factory) as s:
        assert scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s)) == [
            (mid, "rolled")
        ]


def test_pacing_is_shared_by_real_modes_and_not_reset_by_switch(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist(TESTER)
    world.set_mode(Mode.TRANSPORT_TEST)
    tcamp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    assert world.dispatch(world.draft_and_approve(tcamp)).outcome is Outcome.SENT
    world.set_mode(Mode.LIVE)
    lcamp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(lcamp)
    assert world.dispatch(mid).reason == "pacing"  # B8
    with transaction(world.factory) as s:
        world.clock.set(s.get(PacingState, "real").next_allowed_at)
    assert world.dispatch(mid).outcome is Outcome.SENT
    with transaction(world.factory) as s:
        assert {r.ledger for r in s.scalars(select(BudgetLedger))} == {"real"}  # shared ledger


def test_sandbox_caps_enforced_and_isolated_from_live(world) -> None:
    world.settings(same_place_scope="lab")  # the pending cap of 2, not the same-place rule
    inst = world.institution()
    for _ in range(2):
        world.draft_and_approve(world.campaign(world.pi(inst)))
    from marcelorian.services.reservations import CapacityError

    with pytest.raises(CapacityError):
        world.approve(world.draft(world.campaign(world.pi(inst))))  # acceptance 43: enforced
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    live_camp = world.campaign(world.pi(inst), Mode.LIVE)
    mid = world.draft_and_approve(live_camp)  # sandbox occupancy does not count in live
    assert world.dispatch(mid).outcome is Outcome.SENT
    with transaction(world.factory) as s:
        ledgers = sorted(r.ledger for r in s.scalars(select(BudgetLedger)))
    assert ledgers == ["real"]


def test_sandbox_budget_is_separate(world) -> None:
    world.settings(real_submissions_per_24h=0)
    camp = world.campaign(world.pi(world.institution()))
    assert world.dispatch(world.draft_and_approve(camp)).outcome is Outcome.SENT


def test_allowlist_enforced_at_gateway(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist(TESTER)
    world.set_mode(Mode.TRANSPORT_TEST)
    camp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        recipients.deactivate_allowlist_address(
            s, s.get(RecipientAllowlist, tester), actor=Actor.APPLICANT, now=world.clock.now()
        )
    result = world.dispatch(mid)
    assert result.outcome is Outcome.BLOCKED  # acceptance 44
    assert world.gmail.send_calls == []
    assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL


def test_transport_test_records_intended_and_actual_recipient(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist(TESTER)
    world.set_mode(Mode.TRANSPORT_TEST)
    pi = world.pi(world.institution(), synthetic=True, email="fake-pi@synthetic-univ.test")
    camp = world.campaign(pi, Mode.TRANSPORT_TEST, test_recipient_id=tester)
    mid = world.draft_and_approve(camp)
    assert world.dispatch(mid).outcome is Outcome.SENT
    attempt = world.attempts(mid)[0]
    assert (attempt.recipient, attempt.intended_recipient) == (
        TESTER,
        "fake-pi@synthetic-univ.test",
    )


def test_override_window_but_pacing_and_shared_budget_still_apply(world) -> None:
    world.connect_mailbox()
    a, b = world.allowlist(TESTER), world.allowlist("tester2@example.test")
    world.set_mode(Mode.TRANSPORT_TEST)
    world.clock.set(resolve_local(datetime(2026, 10, 25, 3, 0), NY))  # Sunday night
    override = TestOverride(window_always_open=True, step_intervals_seconds=((2, 600),))
    ca = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=a
    )
    cb = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=b
    )
    ma = world.draft_and_approve(ca, override=override)
    mb = world.draft_and_approve(cb, override=override)
    assert world.dispatch(ma).outcome is Outcome.SENT  # window override (acceptance 63)
    assert world.dispatch(mb).reason == "pacing"  # pacing still applies
    assert world.campaign_row(ca).next_due_at == world.message(ma).sent_at + timedelta(seconds=600)
    world.settings(real_submissions_per_24h=1)
    world.clock.advance(timedelta(minutes=10))
    assert world.dispatch(mb).reason == "budget"  # shared real budget still applies
    with transaction(world.factory) as s:
        snap = s.scalars(select(ApprovalSnapshot).where(ApprovalSnapshot.message_id == ma)).one()
        content = parse_canonical(snap.canonical_json)
    assert content.test_override == override and content.to == TESTER  # visibly bound


def test_live_rejects_override_even_if_injected_into_snapshot(world) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:  # simulate a corrupted/tampered approval
        snap = s.scalars(select(ApprovalSnapshot)).one()
        from dataclasses import replace

        content = replace(
            parse_canonical(snap.canonical_json),
            test_override=TestOverride(window_always_open=True),
        )
        snap.canonical_json = canonical_json(content)
        snap.sha256 = sha256_hex(snap.canonical_json)
    result = world.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.BLOCKED, "override_outside_transport_test")
    assert world.gmail.send_calls == []


def test_allowlist_rejects_real_pi_address(world) -> None:
    world.pi(world.institution(), email="real-pi@synthetic-univ.test")
    with transaction(world.factory) as s, pytest.raises(RecipientError):
        recipients.add_allowlist_address(
            s, "Real-PI@synthetic-univ.test", "x", actor=Actor.APPLICANT, now=world.clock.now()
        )


def test_one_active_campaign_per_test_recipient(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist(TESTER)
    world.set_mode(Mode.TRANSPORT_TEST)
    c1 = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    c2 = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    world.draft_and_approve(c1)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(c2))
    assert "test_recipient_busy" in info.value.reasons
    with transaction(world.factory) as s, pytest.raises(IntegrityError):
        s.get(Campaign, c2).state = CampaignState.OUTREACH_PENDING.value
        s.flush()  # the partial unique index is the backstop (acceptance 69)


def test_test_address_is_never_promoted_to_live(world) -> None:
    world.connect_mailbox()
    world.allowlist(TESTER)
    world.set_mode(Mode.LIVE)
    pi = world.pi(world.institution(), email=TESTER)
    with pytest.raises(ApprovalError):
        world.approve(world.draft(world.campaign(pi, Mode.LIVE)))


def test_simulated_dnc_never_edits_live_list(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        dnc.add_entry(
            s, scope_mode=Mode.SANDBOX, pi_id=pi, actor=Actor.SYSTEM, now=world.clock.now()
        )
        assert dnc.matches(s, pi, Mode.SANDBOX)
        assert not dnc.matches(s, pi, Mode.LIVE)
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            email="other@synthetic-univ.test",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )


def test_live_dnc_applies_in_every_mode(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        dnc.add_entry(
            s, scope_mode=Mode.LIVE, pi_id=pi, actor=Actor.APPLICANT, now=world.clock.now()
        )
        assert dnc.matches(s, pi, Mode.SANDBOX) and dnc.matches(s, pi, Mode.TRANSPORT_TEST)


def test_campaign_requires_allowlisted_test_recipient(world) -> None:
    pi = world.pi(world.institution(), synthetic=True)
    with transaction(world.factory) as s, pytest.raises(ApprovalError):
        approvals.create_campaign(
            s,
            applicant_id=world.applicant_id,
            pi_id=pi,
            mode=Mode.TRANSPORT_TEST,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
