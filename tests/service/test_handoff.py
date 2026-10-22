"""Outlook hand-off channel (spec §9.8; owner decisions S1-S6; acceptance 81-86).

The app has no Outlook access: these tests use no transport for the hand-off itself
and assert that the fake Gmail transport is never called. Synthetic data, injected
clock only.
"""

from __future__ import annotations

import email
from datetime import datetime, timedelta
from email.policy import default as default_policy
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    BudgetLedger,
    Campaign,
    CampaignPause,
    Message,
    PacingState,
    Pi,
    SendAttempt,
)
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    Channel,
    MessageState,
    Mode,
)
from marcelorian.domain.timing import resolve_local
from marcelorian.services import approvals, dnc, handoff, prior_contact, reconciliation
from marcelorian.services.gateway import Outcome
from marcelorian.services.send_results import ResolutionError
from tests.factories import APPLICANT_EMAIL, World

ORCID = "0000-0002-0000-0778"
NY = ZoneInfo("America/New_York")


def ny(*args: int) -> datetime:
    return resolve_local(datetime(*args), NY)


@pytest.fixture
def outlook(world: World) -> World:
    world.connect_handoff_mailbox()
    world.set_mode(Mode.LIVE)
    world.signature_step()  # first use: no signature (S8)
    return world


def _email1(world: World, institution: int | None = None, **pi_kwargs: object) -> tuple[int, int]:
    inst = institution if institution is not None else world.institution()
    camp = world.campaign(world.pi(inst, **pi_kwargs), Mode.LIVE)  # type: ignore[arg-type]
    return camp, world.draft_and_approve(camp)


def _attempt(world: World, uid: str) -> SendAttempt:
    with transaction(world.factory) as s:
        row = s.scalar(select(SendAttempt).where(SendAttempt.attempt_uid == uid))
        assert row is not None
        return row


def _budget(world: World) -> list[str]:
    with transaction(world.factory) as s:
        return [row.status for row in s.scalars(select(BudgetLedger).order_by(BudgetLedger.id))]


def _readiness(world: World, mid: int) -> handoff.Readiness:
    with transaction(world.factory) as s:
        message = s.get(Message, mid)
        assert message is not None
        return handoff.readiness(s, message, load_settings(s), world.clock.now())


def _sent_email1(world: World) -> tuple[int, int]:
    camp, mid = _email1(world)
    result = world.hand_off(mid)
    assert result.attempt_uid is not None
    world.handoff_sent(result.attempt_uid)
    return camp, mid


# -- 81: final checks at the hand-off ------------------------------------------------


def test_handoff_records_attempt_and_returns_the_email(outlook: World) -> None:
    camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.outcome == "handed_off" and result.attempt_uid and result.package
    package = result.package
    assert package.subject == "Research Position Application – Ophthalmology"
    assert package.to.endswith("@synthetic-univ.test")
    assert package.compose_url and package.compose_url.startswith(
        "https://outlook.office.com/mail/deeplink/compose?"
    )
    assert package.link_omits_attachments  # the approved CV is in the file only
    assert outlook.message_state(mid) is MessageState.HANDED_OFF
    attempt = _attempt(outlook, result.attempt_uid)
    assert (attempt.channel, attempt.status, attempt.sender) == (
        "outlook_handoff",
        "handed_off",
        APPLICANT_EMAIL,
    )
    assert _budget(outlook) == ["held"]
    with transaction(outlook.factory) as s:
        assert s.get(PacingState, "real").next_allowed_at > outlook.clock.now()
    assert outlook.gmail.send_calls == []
    assert outlook.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value


def test_missing_confirmation_records_nothing(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.hand_off(mid, prior_contact_checked=False)
    assert (result.outcome, result.reason) == (
        "blocked",
        "confirmation_required:prior_contact_checked",
    )
    assert outlook.message_state(mid) is MessageState.APPROVED_QUEUED
    assert outlook.attempts(mid) == [] and _budget(outlook) == []


def test_failed_check_creates_no_handoff(outlook: World) -> None:
    camp, mid = _email1(outlook)
    with transaction(outlook.factory) as s:
        pi_id = s.get(Campaign, camp).pi_id
        dnc.add_entry(
            s, scope_mode=Mode.LIVE, actor=Actor.APPLICANT, now=outlook.clock.now(), pi_id=pi_id
        )
    result = outlook.hand_off(mid)
    assert (result.outcome, result.reason, result.package) == ("blocked", "do_not_contact", None)
    assert outlook.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert outlook.attempts(mid) == [] and _budget(outlook) == []


def test_outside_window_defers_without_handoff(outlook: World) -> None:
    outlook.clock.set(ny(2026, 10, 24, 10, 0))  # Saturday
    _camp, mid = _email1(outlook)  # assigned Monday 08:00-16:00
    result = outlook.hand_off(mid)
    assert (result.outcome, result.reason) == ("deferred", "outside_window")
    assert outlook.attempts(mid) == []


def test_reopening_returns_the_same_email_without_a_second_handoff(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    first = outlook.hand_off(mid)
    assert first.attempt_uid and first.package
    with transaction(outlook.factory) as s:
        again = handoff.package_for(s, first.attempt_uid)
        attempt = s.scalar(select(SendAttempt).where(SendAttempt.attempt_uid == first.attempt_uid))
        message_id = attempt.app_message_id
    assert again == first.package
    parsed = email.message_from_bytes(first.package.eml, policy=default_policy)
    assert parsed["Message-ID"] == message_id  # new Outlook needs one to save the draft
    assert outlook.hand_off(mid).outcome == Outcome.NOT_ELIGIBLE.value
    assert len(outlook.attempts(mid)) == 1


# -- 82: Sent / Not sent -------------------------------------------------------------------


def test_sent_records_the_confirmation_time_and_schedules_email2(outlook: World) -> None:
    camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.attempt_uid
    outlook.clock.advance(timedelta(minutes=9))  # sent from Outlook, then confirmed
    outlook.handoff_sent(result.attempt_uid, edited=True)
    message = outlook.message(mid)
    assert message.state == MessageState.SENT.value
    assert (message.sent_at, message.sent_at_provenance) == (
        outlook.clock.now(),
        "applicant_reported",
    )
    assert message.gmail_message_id is None
    assert outlook.campaign_row(camp).next_due_at == outlook.clock.now() + timedelta(days=7)
    assert _budget(outlook) == ["used"]
    attempt = _attempt(outlook, result.attempt_uid)
    assert (attempt.status, attempt.resolution) == ("sent", "applicant_sent")
    assert attempt.resolution_note == "edited in Outlook before sending"
    with pytest.raises(ResolutionError):
        outlook.handoff_sent(result.attempt_uid)
    with pytest.raises(ResolutionError):
        outlook.handoff_not_sent(result.attempt_uid)


def test_not_sent_releases_budget_and_requires_reapproval(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.attempt_uid
    outlook.handoff_not_sent(result.attempt_uid, note="closed the window by mistake")
    assert outlook.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert _attempt(outlook, result.attempt_uid).status == "not_submitted"
    assert _budget(outlook) == ["released"]
    with pytest.raises(ResolutionError):
        outlook.handoff_sent(result.attempt_uid)  # never resurrected
    outlook.clock.advance(timedelta(minutes=7))  # pacing from the first hand-off
    outlook.approve(mid)
    second = outlook.hand_off(mid)
    assert second.outcome == "handed_off" and second.attempt_uid != result.attempt_uid


def test_followup_needs_no_reply_confirmation_and_no_verified_headers(outlook: World) -> None:
    camp, _mid = _sent_email1(outlook)
    outlook.clock.set(outlook.campaign_row(camp).next_due_at + timedelta(minutes=1))
    mid2 = outlook.draft_and_approve(camp, 2)  # no Message-ID to verify in this channel
    refused = outlook.hand_off(mid2, no_reply=False)
    assert refused.reason == "confirmation_required:no_reply"
    result = outlook.hand_off(mid2)
    assert result.outcome == "handed_off" and result.package
    assert result.package.subject.startswith("Re: Research Position Application")
    assert result.package.reply_in_outlook
    assert outlook.gmail.send_calls == []


# -- 83: unresolved hand-offs count as possibly sent ----------------------------------------


def test_cancel_during_handoff_takes_effect_on_not_sent(outlook: World) -> None:
    camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.attempt_uid
    with transaction(outlook.factory) as s:
        outcome = approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=outlook.clock.now()
        )
    assert outcome == "not_recalled"
    assert outlook.message(mid).cancel_requested_at is not None
    outlook.handoff_not_sent(result.attempt_uid)
    assert outlook.message_state(mid) is MessageState.CANCELLED
    assert outlook.campaign_row(camp).state == CampaignState.CANDIDATE.value


def test_open_handoff_blocks_a_duplicate_sequence(outlook: World) -> None:
    pi_a = outlook.pi(outlook.institution(), email="dr.twin@synthetic-hospital.test")
    pi_b = outlook.pi(outlook.institution(), email="dr.twin@synthetic-univ.test")
    camp_a, camp_b = outlook.campaign(pi_a, Mode.LIVE), outlook.campaign(pi_b, Mode.LIVE)
    mid_a, mid_b = outlook.draft_and_approve(camp_a), outlook.draft_and_approve(camp_b)
    assert outlook.hand_off(mid_a).outcome == "handed_off"
    with transaction(outlook.factory) as s:  # enrichment links the two records
        s.get(Pi, pi_a).orcid = ORCID
        s.get(Pi, pi_b).orcid = ORCID
    outlook.clock.advance(timedelta(minutes=7))
    result = outlook.hand_off(mid_b)
    assert (result.outcome, result.reason) == ("blocked", "duplicate_outreach")


def test_at_most_three_open_handoffs(outlook: World) -> None:
    mids = [_email1(outlook)[1] for _ in range(4)]
    uids = []
    for mid in mids[:3]:
        result = outlook.hand_off(mid)
        assert result.outcome == "handed_off" and result.attempt_uid
        uids.append(result.attempt_uid)
        outlook.clock.advance(timedelta(minutes=7))
    blocked = outlook.hand_off(mids[3])
    assert (blocked.outcome, blocked.reason) == ("deferred", "too_many_open_handoffs")
    assert _readiness(outlook, mids[3]).reason == "too_many_open_handoffs"
    outlook.handoff_sent(uids[0])
    assert outlook.hand_off(mids[3]).outcome == "handed_off"
    with transaction(outlook.factory) as s:
        assert len(handoff.open_handoffs(s, outlook.clock.now())) == 3


def test_reminder_after_a_day_unresolved(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    outlook.clock.advance(timedelta(hours=23, minutes=59))
    with transaction(outlook.factory) as s:
        assert not handoff.open_handoffs(s, outlook.clock.now())[0].needs_reminder
    outlook.clock.advance(timedelta(minutes=1))
    with transaction(outlook.factory) as s:
        (row,) = handoff.open_handoffs(s, outlook.clock.now())
    assert row.needs_reminder and row.attempt_uid == result.attempt_uid


def test_startup_recovery_never_touches_a_handoff(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.attempt_uid
    reconciliation.startup_recovery(outlook.factory, {Mode.LIVE: outlook.gmail}, {}, outlook.clock)
    assert _attempt(outlook, result.attempt_uid).status == "handed_off"
    assert outlook.message_state(mid) is MessageState.HANDED_OFF
    outlook.handoff_sent(result.attempt_uid)
    reconciliation.startup_recovery(outlook.factory, {Mode.LIVE: outlook.gmail}, {}, outlook.clock)
    assert outlook.gmail.metadata_requests == []  # no Gmail copy is ever looked for
    assert outlook.message_state(mid) is MessageState.SENT


# -- 84: confirmations and reports -------------------------------------------------------


def test_reported_prior_contact_opens_a_review(outlook: World) -> None:
    camp, mid = _email1(outlook)
    with transaction(outlook.factory) as s:
        handoff.report_prior_contact(
            s,
            s.get(Campaign, camp),
            note="2024 thread found",
            actor=Actor.APPLICANT,
            now=outlook.clock.now(),
        )
    assert outlook.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    assert outlook.hand_off(mid).reason == "campaign_not_sendable"
    with transaction(outlook.factory) as s:
        pause = s.scalar(select(CampaignPause).where(CampaignPause.campaign_id == camp))
        assert pause is not None
        prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=outlook.clock.now()
        )
    assert outlook.hand_off(mid).outcome == "handed_off"


def test_reported_reply_stops_the_sequence(outlook: World) -> None:
    camp, _mid = _sent_email1(outlook)
    outlook.clock.set(outlook.campaign_row(camp).next_due_at + timedelta(minutes=1))
    mid2 = outlook.draft_and_approve(camp, 2)
    with transaction(outlook.factory) as s:
        handoff.report_reply(
            s, s.get(Campaign, camp), note=None, actor=Actor.APPLICANT, now=outlook.clock.now()
        )
    assert outlook.campaign_row(camp).state == CampaignState.CONVERSATION.value
    assert outlook.message_state(mid2) is MessageState.CANCELLED


def test_reply_reported_while_followup_is_handed_off(outlook: World) -> None:
    camp, _mid = _sent_email1(outlook)
    outlook.clock.set(outlook.campaign_row(camp).next_due_at + timedelta(minutes=1))
    mid2 = outlook.draft_and_approve(camp, 2)
    result = outlook.hand_off(mid2)
    assert result.attempt_uid
    with transaction(outlook.factory) as s:
        handoff.report_reply(
            s, s.get(Campaign, camp), note=None, actor=Actor.APPLICANT, now=outlook.clock.now()
        )
    assert outlook.message(mid2).cancel_requested_at is not None
    outlook.handoff_sent(result.attempt_uid)  # it had gone out already: recorded (B13)
    assert outlook.message_state(mid2) is MessageState.SENT
    assert outlook.campaign_row(camp).state == CampaignState.CONVERSATION.value


# -- 86: channels and the countdown --------------------------------------------------------


def test_gmail_gateway_refuses_while_outlook_is_the_mailbox(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.BLOCKED, "mailbox_channel_mismatch")
    assert outlook.gmail.send_calls == [] and outlook.attempts(mid) == []


def test_handoff_refuses_while_gmail_is_the_mailbox(world: World) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    _camp, mid = _email1(world)
    result = world.hand_off(mid)
    assert (result.outcome, result.reason) == ("blocked", "mailbox_channel_mismatch")
    assert world.attempts(mid) == []


def test_sandbox_has_no_handoff(world: World) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    assert world.hand_off(mid).reason == "handoff_real_modes_only"


def test_channel_switch_is_setup_only(world: World) -> None:
    world.connect_mailbox()  # Gmail first
    world.connect_handoff_mailbox()  # same address, other channel: a setup switch
    with transaction(world.factory) as s:
        assert approvals.active_channel(s) is Channel.OUTLOOK_HANDOFF
    world.connect_handoff_mailbox()  # the same identity reconnects
    world.set_mode(Mode.LIVE)
    _camp, mid = _email1(world)
    world.hand_off(mid)
    with pytest.raises(approvals.MailboxIdentityRefused), transaction(world.factory) as s:
        approvals.connect_mailbox(
            s, APPLICANT_EMAIL, frozenset(), actor=Actor.APPLICANT, now=world.clock.now()
        )
    with transaction(world.factory) as s:
        assert approvals.active_channel(s) is Channel.OUTLOOK_HANDOFF


def test_countdown_matches_the_window_check(outlook: World) -> None:
    friday_1600 = ny(2026, 10, 23, 16, 0)
    outlook.clock.set(friday_1600 - timedelta(minutes=1))
    _camp, mid = _email1(outlook)
    outlook.clock.set(friday_1600)
    ready = _readiness(outlook, mid)
    assert ready.reason == "window_passed" and ready.unlock_at is None  # assigned window over
    _camp2, mid2 = _email1(outlook)  # approved Friday 16:00: assigned Monday 08:00
    monday_0800 = ny(2026, 10, 26, 8, 0)
    ready2 = _readiness(outlook, mid2)
    assert (ready2.ready, ready2.reason, ready2.unlock_at) == (False, "outside_window", monday_0800)
    assert ready2.confirmation == "prior_contact_checked"
    outlook.clock.set(monday_0800 - timedelta(microseconds=1))
    assert not _readiness(outlook, mid2).ready
    outlook.clock.set(monday_0800)
    assert _readiness(outlook, mid2).ready
    assert outlook.hand_off(mid2).outcome == "handed_off"


def test_countdown_matches_the_pacing_check(outlook: World) -> None:
    _c1, mid1 = _email1(outlook)
    _c2, mid2 = _email1(outlook)
    assert outlook.hand_off(mid1).outcome == "handed_off"
    ready = _readiness(outlook, mid2)
    assert ready.reason == "pacing" and ready.unlock_at is not None
    outlook.clock.set(ready.unlock_at - timedelta(microseconds=1))
    assert outlook.hand_off(mid2).reason == "pacing"
    outlook.clock.set(ready.unlock_at)
    assert outlook.hand_off(mid2).outcome == "handed_off"


def test_countdown_matches_the_budget_check(outlook: World) -> None:
    outlook.settings(real_submissions_per_24h=1)
    _c1, mid1 = _email1(outlook)
    first = outlook.hand_off(mid1)
    assert first.attempt_uid
    outlook.handoff_sent(first.attempt_uid)
    sent_at = outlook.message(mid1).sent_at
    assert sent_at is not None
    outlook.clock.advance(timedelta(minutes=10))
    _c2, mid2 = _email1(outlook)
    ready = _readiness(outlook, mid2)
    # Tuesday 09:00 + 24 h is Wednesday 09:00, inside the window but after the assigned
    # Tuesday window ends: the countdown says so instead of inventing a time.
    assert ready.reason == "budget" and ready.unlock_at is None
    outlook.clock.set(sent_at + timedelta(hours=24) - timedelta(minutes=30))
    _c3, mid3 = _email1(outlook)  # approved Wednesday 08:30: today's window
    ready3 = _readiness(outlook, mid3)
    assert (ready3.reason, ready3.unlock_at) == ("budget", sent_at + timedelta(hours=24))
    outlook.clock.set(sent_at + timedelta(hours=24))
    assert outlook.hand_off(mid3).outcome == "handed_off"


def test_gmail_channel_readiness_needs_no_confirmation(world: World) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    _camp, mid = _email1(world)
    ready = _readiness(world, mid)
    assert (ready.ready, ready.confirmation, ready.unlock_at) == (True, None, world.clock.now())


def test_eml_from_a_real_handoff_parses(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    result = outlook.hand_off(mid)
    assert result.package
    parsed = email.message_from_bytes(result.package.eml, policy=default_policy)
    assert parsed["X-Unsent"] == "1" and parsed["From"] is None
    assert parsed["Subject"] == "Research Position Application – Ophthalmology"
    attachments = list(parsed.iter_attachments())
    assert [a.get_filename() for a in attachments] == ["cv-synthetic-v1.pdf"]


# -- review r5 regressions ----------------------------------------------------------------


def test_countdown_never_promises_an_unlock_the_open_handoff_cap_blocks(outlook: World) -> None:
    mids = [_email1(outlook)[1] for _ in range(4)]
    for i, mid in enumerate(mids[:3]):
        assert outlook.hand_off(mid).outcome == "handed_off"
        if i < 2:
            outlook.clock.advance(timedelta(minutes=7))
    outlook.clock.advance(timedelta(seconds=1))  # inside the third hand-off's pacing gap
    ready = _readiness(outlook, mids[3])
    assert (ready.reason, ready.unlock_at) == ("too_many_open_handoffs", None)


def test_a_click_held_by_the_app_counts_for_the_missed_window_rule(outlook: World) -> None:
    mids = [_email1(outlook)[1] for _ in range(4)]
    for mid in mids[:3]:
        assert outlook.hand_off(mid).outcome == "handed_off"
        outlook.clock.advance(timedelta(minutes=7))
    assert outlook.hand_off(mids[3]).reason == "too_many_open_handoffs"
    outlook.clock.set(ny(2026, 10, 20, 16, 30))  # Tuesday's window is over
    reconciliation.startup_recovery(outlook.factory, {Mode.LIVE: outlook.gmail}, {}, outlook.clock)
    # The applicant clicked and the app itself held it (B7): it rolls, no re-approval.
    assert outlook.message_state(mids[3]) is MessageState.APPROVED_QUEUED


def test_window_passing_without_a_click_rolls(outlook: World) -> None:
    _camp, mid = _email1(outlook)
    outlook.clock.set(ny(2026, 10, 20, 16, 30))
    reconciliation.startup_recovery(outlook.factory, {Mode.LIVE: outlook.gmail}, {}, outlook.clock)
    assert outlook.message_state(mid) is MessageState.APPROVED_QUEUED  # S7: no re-approval
    outlook.clock.set(ny(2026, 10, 21, 9, 0))
    assert outlook.hand_off(mid).outcome == "handed_off"


def test_reopened_handoff_keeps_the_signature_it_was_handed_off_with(outlook: World) -> None:
    outlook.settings(email_signature="A. Applicant")
    _camp, mid = _email1(outlook)
    first = outlook.hand_off(mid)
    assert first.attempt_uid and first.package
    outlook.settings(email_signature="Dr. Synthetic Applicant\nSynthetic University")
    with transaction(outlook.factory) as s:
        assert handoff.package_for(s, first.attempt_uid) == first.package
    assert first.package.body.endswith("A. Applicant\n")


def test_unbuildable_email_records_no_handoff(outlook: World, monkeypatch) -> None:
    _camp, mid = _email1(outlook)

    def broken(*_args: object, **_kwargs: object) -> None:
        raise ValueError("Header values may not contain linefeed or carriage return characters")

    monkeypatch.setattr(handoff, "build_package", broken)
    result = outlook.hand_off(mid)
    assert (result.outcome, result.reason) == ("blocked", "email_not_buildable")
    assert outlook.attempts(mid) == [] and _budget(outlook) == []
    assert outlook.message_state(mid) is MessageState.NEEDS_REAPPROVAL


# -- owner decision S8: signature at first use ---------------------------------------------


def test_first_handoff_waits_for_the_signature_step(world: World) -> None:
    world.connect_handoff_mailbox()
    world.set_mode(Mode.LIVE)
    _camp, mid = _email1(world)
    assert _readiness(world, mid).reason == "signature_step_required"
    result = world.hand_off(mid)
    assert (result.outcome, result.reason) == ("blocked", "signature_step_required")
    assert world.attempts(mid) == [] and world.message_state(mid) is MessageState.APPROVED_QUEUED
    world.signature_step("A. Applicant, MD\nResearch Fellow")
    assert _readiness(world, mid).ready
    result = world.hand_off(mid)
    assert result.package and result.package.body.endswith("A. Applicant, MD\nResearch Fellow\n")


def test_skipping_the_signature_step_is_allowed(world: World) -> None:
    world.connect_handoff_mailbox()
    world.set_mode(Mode.LIVE)
    world.signature_step("")  # Outlook on the web still adds its own to the link
    _camp, mid = _email1(world)
    result = world.hand_off(mid)
    assert result.outcome == "handed_off" and result.package
    assert result.package.body == world.message(mid).draft_body
