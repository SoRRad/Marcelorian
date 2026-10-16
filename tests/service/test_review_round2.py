"""Regressions for defects found by the second P0 adversarial review (each reproduced
first). Each test names the decision or spec rule it protects."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from email import message_from_bytes
from email.policy import default as default_policy
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from marcelorian.clock import ManualClock
from marcelorian.config.settings import AppSettings, SettingsError, load_settings, validate
from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalSnapshot,
    Campaign,
    CampaignPause,
    DispatchEvaluation,
    Message,
    Pi,
    PiEmail,
)
from marcelorian.domain.enums import Actor, CloseOutcome, MessageState, Mode, PauseReason
from marcelorian.domain.recipients import RecipientError
from marcelorian.domain.timing import resolve_local
from marcelorian.services import campaigns, dnc, scheduling
from marcelorian.services import sync as sync_svc
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from tests.factories import World

NY = ZoneInfo("America/New_York")
REAL_PI = "real-pi@synthetic-univ.test"


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def close(world, camp, outcome=CloseOutcome.NO_OPENING):
    with transaction(world.factory) as s:
        campaigns.close_campaign(
            s,
            s.get(Campaign, camp),
            outcome,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            reason="not pursuing",
        )


def set_orcid(world, pi_id, orcid):
    with transaction(world.factory) as s:
        s.get(Pi, pi_id).orcid = orcid


def add_known_address(world, pi_id, address, confidence="C"):
    with transaction(world.factory) as s:
        s.add(
            PiEmail(
                pi_id=pi_id,
                address=address,
                confidence=confidence,
                ownership_verified=False,
                source="synthetic",
                created_at=world.clock.now(),
            )
        )


# -- strict addresses (§9.6, §12, §3; B11, C7; acceptance 69) ----------------------------------


def test_pasted_mailto_link_is_canonicalized_and_refused_as_a_real_pi_tester(world) -> None:
    world.pi(world.institution(), email=REAL_PI)  # a real (non-synthetic) PI
    world.connect_mailbox()
    with pytest.raises(RecipientError):
        world.allowlist(f"mailto:{REAL_PI}")
    with pytest.raises(RecipientError):  # group syntax is never an address
        world.allowlist(f"mailto:x:{REAL_PI}")
    assert world.gmail.send_calls == []


def test_dnc_entry_entered_as_mailto_link_still_blocks(world) -> None:
    pi = world.pi(world.institution(), email=REAL_PI)
    with transaction(world.factory) as s:
        entry = dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            email=f"MAILTO:{REAL_PI}",
            reason="asked not to be contacted",
        )
        assert entry.email == REAL_PI
    with transaction(world.factory) as s:
        assert dnc.matches(s, pi, Mode.LIVE)


def test_pi_email_rows_are_stored_canonical(world) -> None:
    pi = world.pi(world.institution())
    add_known_address(world, pi, "  MailTo:Dr.Other@Synthetic-Hospital.TEST ")
    with transaction(world.factory) as s:
        stored = set(s.scalars(select(PiEmail.address).where(PiEmail.pi_id == pi)))
    assert "dr.other@synthetic-hospital.test" in stored
    with pytest.raises(RecipientError):
        add_known_address(world, pi, "group: dr.other@synthetic-hospital.test;")


def test_sent_mime_is_addressed_to_exactly_the_recorded_recipient(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    assert live.dispatch(mid).outcome is Outcome.SENT
    parsed = message_from_bytes(live.gmail.send_calls[0], policy=default_policy)
    delivered = [a.addr_spec for a in parsed["To"].addresses]
    assert delivered == [live.attempts(mid)[0].recipient]


# -- duplicate outreach over the full identity (§3, §9.6; B14, C7) ------------------------------


def test_shared_known_address_does_not_permit_a_second_sequence(live) -> None:
    inst = live.institution()
    rec_r = live.pi(inst, email="dr.same@synthetic-hospital.test")
    first = live.campaign(rec_r, Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(first)).outcome is Outcome.SENT
    close(live, first)
    live.clock.advance(timedelta(minutes=10))
    # Re-discovered, unmerged record: another send-ready address, plus the address R was
    # emailed at as a known address.
    rec_p = live.pi(inst, email="dr.same@synthetic-univ.test")
    add_known_address(live, rec_p, "dr.same@synthetic-hospital.test")
    with pytest.raises(ApprovalError) as info:
        live.approve(live.draft(live.campaign(rec_p, Mode.LIVE)))
    assert "duplicate_outreach" in info.value.reasons
    assert len(live.gmail.send_calls) == 1


def test_shared_orcid_unmerged_record_gets_no_second_email1(live) -> None:
    rec_r = live.pi(live.institution(), email="moved.pi@synthetic-old.test")
    set_orcid(live, rec_r, "0000-0002-0000-0001")
    first = live.campaign(rec_r, Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(first)).outcome is Outcome.SENT
    close(live, first)
    live.clock.advance(timedelta(minutes=10))
    rec_p = live.pi(live.institution(), email="moved.pi@synthetic-new.test")
    set_orcid(live, rec_p, "0000-0002-0000-0001")
    with pytest.raises(ApprovalError) as info:
        live.approve(live.draft(live.campaign(rec_p, Mode.LIVE)))
    assert "duplicate_outreach" in info.value.reasons
    assert len(live.gmail.send_calls) == 1


def test_identity_linked_after_approval_is_blocked_at_dispatch(live) -> None:
    rec_r = live.pi(live.institution(), email="dr.late@synthetic-old.test")
    first = live.campaign(rec_r, Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(first)).outcome is Outcome.SENT
    close(live, first)
    live.clock.advance(timedelta(minutes=10))
    rec_p = live.pi(live.institution(), email="dr.late@synthetic-new.test")
    mid = live.draft_and_approve(live.campaign(rec_p, Mode.LIVE))
    set_orcid(live, rec_r, "0000-0002-0000-0042")  # enrichment links them after approval
    set_orcid(live, rec_p, "0000-0002-0000-0042")
    assert live.dispatch(mid).reason == "duplicate_outreach"
    assert len(live.gmail.send_calls) == 1


def test_prior_contact_hit_on_another_campaigns_app_message_is_not_acknowledgeable(live) -> None:
    first = live.campaign(live.pi(live.institution()), Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(first)).outcome is Outcome.SENT
    own_gid = live.gmail.sent_messages()[0].meta.gmail_message_id
    close(live, first)
    live.clock.advance(timedelta(minutes=10))
    # Unlinked record; the search nevertheless returns the app's earlier message (for
    # instance the PI forwarded it from a new address the app does not know yet).
    mid = live.draft_and_approve(live.campaign(live.pi(live.institution()), Mode.LIVE))
    original = live.gmail.search_message_ids

    def search(query, *, include_spam_trash):
        result = original(query, include_spam_trash=include_spam_trash)
        if query.startswith("("):
            return type(result)(ids=(*result.ids, own_gid), complete=result.complete)
        return result

    live.gmail.search_message_ids = search  # type: ignore[method-assign]
    assert live.dispatch(mid).reason == "duplicate_outreach"
    with transaction(live.factory) as s:
        pauses = s.scalars(
            select(CampaignPause).where(
                CampaignPause.reason == PauseReason.PRIOR_CONTACT_FOUND.value
            )
        ).all()
    assert pauses == []  # nothing offered for acknowledgment
    assert live.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert len(live.gmail.send_calls) == 1


# -- do-not-contact entries by identifier (§3; C7) ----------------------------------------------


def test_orcid_entry_covers_the_known_address_of_the_record_with_that_orcid(world) -> None:
    inst = world.institution()
    listed = world.pi(inst, email="dr.listed@synthetic-univ.test")
    set_orcid(world, listed, "0000-0003-0000-0042")
    with transaction(world.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            orcid="0000-0003-0000-0042",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    rediscovered = world.pi(inst, email="dr.listed@synthetic-univ.test")
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(rediscovered)))
    assert "do_not_contact" in info.value.reasons


def test_address_entry_covers_the_orcid_of_the_record_with_that_address(world) -> None:
    listed = world.pi(world.institution(), email="dr.moved@synthetic-old.test")
    set_orcid(world, listed, "0000-0003-0000-0077")
    with transaction(world.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            email="dr.moved@synthetic-old.test",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    rediscovered = world.pi(world.institution(), email="dr.moved@synthetic-new.test")
    set_orcid(world, rediscovered, "0000-0003-0000-0077")
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(rediscovered)))
    assert "do_not_contact" in info.value.reasons


def test_identity_closure_follows_chains_but_never_name_and_institution(world) -> None:
    inst = world.institution()
    a = world.pi(inst, email="chain.a@synthetic-univ.test")
    b = world.pi(inst, email="chain.b@synthetic-univ.test")
    add_known_address(world, b, "chain.a@synthetic-univ.test")  # A ~ B by address
    c = world.pi(world.institution(), email="chain.c@synthetic-other.test")
    set_orcid(world, b, "0000-0003-0000-0101")
    set_orcid(world, c, "0000-0003-0000-0101")  # B ~ C by ORCID
    namesake = world.pi(inst, email="namesake@synthetic-univ.test")
    with transaction(world.factory) as s:
        s.get(Pi, namesake).full_name = s.get(Pi, a).full_name
    with transaction(world.factory) as s:
        person = dnc.pi_identity(s, a)
    assert {a, b, c} <= person.pi_ids and namesake not in person.pi_ids
    assert "chain.c@synthetic-other.test" in person.addresses


# -- missed-window rule (§9.4; B7, note 3, note 16) ---------------------------------------------


def test_corrupted_snapshot_is_contained_per_message(factory) -> None:
    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    bad = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    good = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    with transaction(world.factory) as s:  # stored snapshot bytes damaged while laptop was off
        snap = s.get(ApprovalSnapshot, s.get(Message, bad).current_snapshot_id)
        snap.canonical_json = snap.canonical_json.replace("Synthetic", "Tampered")
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))  # whole window missed
    with transaction(world.factory) as s:
        actions = dict(scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s)))
    assert actions == {bad: "needs_reapproval", good: "needs_reapproval"}
    with transaction(world.factory) as s:
        assert s.get(ApprovalSnapshot, s.get(Message, bad).current_snapshot_id).end_reason == (
            "snapshot_invalid"
        )


def test_cap_deferral_without_a_current_sync_is_not_functioning(factory) -> None:
    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    with transaction(world.factory) as s:  # last good Gmail coverage: Monday noon
        sync_svc.record_sync_success(s, "real", world.clock.now(), None, world.clock.now())
    mid = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    world.settings(real_submissions_per_24h=0)
    for hour in (8, 9, 10):  # Tuesday: Gmail unreachable; local cap deferrals only
        world.clock.set(resolve_local(datetime(2026, 10, 20, hour, 30), NY))
        assert world.dispatch(mid).reason == "budget"
    with transaction(world.factory) as s:
        flags = list(
            s.scalars(
                select(DispatchEvaluation.functioning).where(DispatchEvaluation.message_id == mid)
            )
        )
    assert flags == [False, False, False]
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(mid, "needs_reapproval")]


def test_cap_deferral_with_a_current_sync_rolls(factory) -> None:
    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    mid = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    world.settings(real_submissions_per_24h=0)
    world.clock.set(resolve_local(datetime(2026, 10, 20, 9, 30), NY))
    assert world.worker_cycle(mid).reason == "budget"
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(mid, "rolled")]


def test_unresolved_own_attempt_is_a_functioning_safety_hold(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist("tester@example.test")
    world.set_mode(Mode.TRANSPORT_TEST)
    first = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    m1 = world.draft_and_approve(first)
    world.gmail.send_script = ["timeout_before_accept"]
    world.gmail.search_script = ["ok", "fail"]  # prior-contact ok; reconciliation fails
    assert world.dispatch(m1).outcome is Outcome.UNCERTAIN
    close(world, first, CloseOutcome.APPLICANT_CLOSED)
    second = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    m2 = world.draft_and_approve(second)
    for hour, minute in ((9, 30), (10, 0), (10, 30)):  # worker, auth and sync all healthy
        world.clock.set(resolve_local(datetime(2026, 10, 20, hour, minute), NY))
        assert world.worker_cycle(m2).reason == "own_attempt_unresolved"
    assert len(world.gmail.send_calls) == 1
    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))
    with transaction(world.factory) as s:
        actions = scheduling.apply_missed_window_rule(s, world.clock.now(), load_settings(s))
    assert actions == [(m2, "rolled")]  # B7: nothing was unavailable


# -- settings sanity (note 15) ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("weekdays", "ye_start", "ye_end"),
    [
        ((1,), "12-31", "12-29"),  # only Dec 30 on a Tuesday: 2025, then 2031
        ((1, 2, 3), "12-31", "12-28"),  # Dec 29/30 Tue-Thu: none in 2028-2030
        ((1,), "12-31", "12-30"),  # nothing at all
    ],
)
def test_multi_year_send_gap_is_rejected(weekdays, ye_start, ye_end) -> None:
    settings = AppSettings(
        window_weekdays=weekdays,
        blackout_year_end=True,
        year_end_start=ye_start,
        year_end_end=ye_end,
    )
    with pytest.raises(SettingsError):
        validate(settings)


def test_ordinary_blackouts_are_accepted() -> None:
    validate(AppSettings(blackout_federal_holidays=True, blackout_year_end=True))
    validate(AppSettings(window_weekdays=(1,), blackout_federal_holidays=True))
    assert date(2026, 12, 22).weekday() == 1  # sanity for the fixtures above
