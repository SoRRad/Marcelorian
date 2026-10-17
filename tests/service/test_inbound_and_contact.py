"""Inbound effects, prior contact and do-not-contact (acceptance 25-30, 64; B11, B14, C7)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, InboundEvent, Pi, Reservation
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    InboundClassification,
    MessageState,
    Mode,
    PauseReason,
)
from marcelorian.services import campaigns, dnc, prior_contact
from marcelorian.services.gateway import Outcome
from marcelorian.services.inbound import record_inbound
from tests.factories import APPLICANT_EMAIL

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def sent_email1(world, mode=Mode.LIVE):
    camp = world.campaign(world.pi(world.institution()), mode)
    mid = world.draft_and_approve(camp)
    assert world.dispatch(mid).outcome is Outcome.SENT
    return camp, mid


def inbound(world, camp, classification, gid):
    with transaction(world.factory) as s:
        return record_inbound(
            s,
            s.get(Campaign, camp),
            gmail_message_id=gid,
            thread_id=None,
            classification=classification,
            from_address="x@y.test",
            received_at=world.clock.now(),
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )[1]


def open_reasons(world, camp):
    with transaction(world.factory) as s:
        return [p.reason for p in campaigns.open_reasons(s, camp)]


# -- inbound effects ------------------------------------------------------------------------


def test_acknowledged_auto_reply_is_not_reprocessed(world) -> None:
    camp, _ = sent_email1(world, Mode.SANDBOX)
    assert inbound(world, camp, InboundClassification.AUTO_REPLY, "auto-1") is True
    with transaction(world.factory) as s:
        pause = s.scalars(select(CampaignPause)).one()
        campaigns.resolve_pause(
            s, pause, resolution="resume", actor=Actor.APPLICANT, now=world.clock.now()
        )
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    for _ in range(3):  # re-sync of the same message
        assert inbound(world, camp, InboundClassification.AUTO_REPLY, "auto-1") is False
    assert world.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value  # test 26


@pytest.mark.parametrize(
    ("classification", "reason"),
    [
        (InboundClassification.UNKNOWN_PARTICIPANT, PauseReason.UNKNOWN_PARTICIPANT),
        (InboundClassification.MANUAL_APPLICANT_MESSAGE, PauseReason.MANUAL_APPLICANT_MESSAGE),
        (InboundClassification.DELIVERY_NOTICE, PauseReason.DELIVERY_NOTICE),
        (InboundClassification.AUTO_REPLY, PauseReason.AUTO_REPLY),
        (InboundClassification.AMBIGUOUS_THREAD, PauseReason.AMBIGUOUS_THREAD),
    ],
)
def test_doubtful_events_pause_and_hold_slot(world, classification, reason) -> None:
    camp, _ = sent_email1(world, Mode.SANDBOX)
    inbound(world, camp, classification, "e-1")  # acceptance 27, 29, 32 (slot held)
    assert world.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    assert open_reasons(world, camp) == [reason.value]
    with transaction(world.factory) as s:
        slot = s.scalars(
            select(Reservation).where(
                Reservation.campaign_id == camp, Reservation.kind == "pending_slot"
            )
        ).one()
    assert slot.status == "active"


def test_genuine_reply_stops_sequence_and_frees_slot(world) -> None:
    camp, _ = sent_email1(world, Mode.SANDBOX)
    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(world_campaign := camp, 2)
    inbound(world, world_campaign, InboundClassification.GENUINE_REPLY, "r-1")
    assert world.message_state(m2) is MessageState.CANCELLED
    campaign = world.campaign_row(camp)
    assert campaign.state == CampaignState.CONVERSATION.value and campaign.next_step is None


def test_late_reply_after_email4_moves_to_conversation(world) -> None:
    camp, _ = sent_email1(world, Mode.SANDBOX)
    for step in (2, 3, 4):
        world.clock.set(world.campaign_row(camp).next_due_at)
        world.clock.set(_next_window_start(world))
        assert world.dispatch(world.draft_and_approve(camp, step)).outcome is Outcome.SENT
    assert world.campaign_row(camp).state == CampaignState.SEQUENCE_COMPLETE.value
    world.clock.advance(timedelta(days=20))
    inbound(world, camp, InboundClassification.GENUINE_REPLY, "late")
    assert world.campaign_row(camp).state == CampaignState.CONVERSATION.value  # test 30


def _next_window_start(world):
    from zoneinfo import ZoneInfo

    from marcelorian.domain.timing import WindowPolicy, next_window

    window = next_window(world.clock.now(), ZoneInfo("America/New_York"), WindowPolicy())
    return max(world.clock.now(), window.start)


def test_new_thread_reply_from_known_pi_pauses_without_freeing(live) -> None:
    camp, m1 = sent_email1(live)
    pi_address = live.attempts(m1)[0].recipient
    live.clock.advance(timedelta(days=3))
    live.gmail.inject(sender=pi_address, to=APPLICANT_EMAIL)  # different thread
    live.clock.advance(timedelta(days=4))
    m2 = live.draft_and_approve(camp, 2)
    result = live.dispatch(m2)
    assert (result.outcome, result.reason) == (Outcome.PAUSED, "inbound_activity")
    assert live.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value  # B11
    assert open_reasons(live, camp) == [PauseReason.AMBIGUOUS_THREAD.value]
    assert len(live.gmail.send_calls) == 1


def test_same_domain_stranger_is_ignored(live) -> None:
    camp, _m1 = sent_email1(live)
    live.clock.advance(timedelta(days=3))
    live.gmail.inject(sender="colleague@synthetic-univ.test", to=APPLICANT_EMAIL)
    live.clock.advance(timedelta(days=4))
    m2 = live.draft_and_approve(camp, 2)
    assert live.dispatch(m2).outcome is Outcome.SENT  # acceptance 28: never by domain alone


def test_same_subject_across_pis_is_never_merged(live) -> None:
    camp_a, ma = sent_email1(live)
    live.clock.advance(timedelta(minutes=7))
    camp_b, _mb = sent_email1(live)
    thread_a = live.message(ma).gmail_thread_id
    pi_a = live.attempts(ma)[0].recipient
    live.gmail.inject(
        sender=pi_a,
        to=APPLICANT_EMAIL,
        thread_id=thread_a,
        headers={"subject": "Re: Research Position Application – Ophthalmology"},
    )
    live.clock.advance(WEEK)
    live.gateway._inbound_handler = lambda c, m: InboundClassification.GENUINE_REPLY
    live.dispatch(live.draft_and_approve(camp_a, 2))
    assert live.campaign_row(camp_a).state == CampaignState.CONVERSATION.value
    assert live.campaign_row(camp_b).state == CampaignState.OUTREACH_PENDING.value


# -- prior contact (B14, acceptance 64) -----------------------------------------------------------


def test_prior_contact_pauses_and_ack_is_scoped_to_reviewed_ids(live) -> None:
    pi = live.pi(live.institution(), email="known-pi@synthetic-univ.test")
    old = live.gmail.inject(
        sender="known-pi@synthetic-univ.test",
        to=APPLICANT_EMAIL,
        when=live.clock.now() - timedelta(days=400),
    )
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    result = live.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.PAUSED, "prior_contact_found")
    assert live.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    with transaction(live.factory) as s:
        pause = s.scalars(select(CampaignPause)).one()
        acked = prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=live.clock.now()
        )
    assert acked == [old.gmail_message_id]
    newer = live.gmail.inject(sender=APPLICANT_EMAIL, to="known-pi@synthetic-univ.test")
    result = live.dispatch(mid)
    assert result.reason == "prior_contact_found"  # a new unacknowledged hit pauses again
    with transaction(live.factory) as s:
        pause = s.scalars(select(CampaignPause).where(CampaignPause.resolved_at.is_(None))).one()
        prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=live.clock.now()
        )
    assert newer.gmail_message_id
    assert live.dispatch(mid).outcome is Outcome.SENT


def test_drafts_excluded_spam_and_trash_included(live) -> None:
    pi = live.pi(live.institution(), email="draft-pi@synthetic-univ.test")
    live.gmail.inject(
        sender=APPLICANT_EMAIL, to="draft-pi@synthetic-univ.test", labels=frozenset({"DRAFT"})
    )
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    assert live.dispatch(mid).outcome is Outcome.SENT  # a draft is not prior contact
    pi2 = live.pi(live.institution(), email="spam-pi@synthetic-univ.test")
    live.gmail.inject(
        sender="spam-pi@synthetic-univ.test", to=APPLICANT_EMAIL, labels=frozenset({"SPAM"})
    )
    live.clock.advance(timedelta(minutes=7))
    m2 = live.draft_and_approve(live.campaign(pi2, Mode.LIVE))
    assert live.dispatch(m2).reason == "prior_contact_found"


def test_incomplete_prior_contact_search_is_not_clean(live) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    live.gmail.search_script = ["incomplete"]
    assert live.dispatch(mid).reason == "prior_contact_check_incomplete"
    live.gmail.search_script = ["fail"]
    assert live.dispatch(mid).reason == "prior_contact_check_incomplete"
    assert live.gmail.send_calls == []


def test_ack_does_not_bypass_do_not_contact(live) -> None:
    pi = live.pi(live.institution(), email="dnc-pi@synthetic-univ.test")
    live.gmail.inject(sender="dnc-pi@synthetic-univ.test", to=APPLICANT_EMAIL)
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    live.dispatch(mid)
    with transaction(live.factory) as s:
        pause = s.scalars(select(CampaignPause)).one()
        prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=live.clock.now()
        )
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            email="dnc-pi@synthetic-univ.test",
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    assert live.dispatch(mid).reason == "do_not_contact"
    assert live.gmail.send_calls == []


def test_transport_test_prior_contact_uses_test_address(world) -> None:
    world.connect_mailbox()
    tester = world.allowlist("tester@example.test")
    world.set_mode(Mode.TRANSPORT_TEST)
    world.gmail.inject(sender="tester@example.test", to=APPLICANT_EMAIL)  # earlier personal mail
    camp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    mid = world.draft_and_approve(camp)
    assert world.dispatch(mid).reason == "prior_contact_found"
    assert any('"tester@example.test"' in q for q in world.gmail.search_queries)


# -- do-not-contact identity (C7) ------------------------------------------------------------------


def test_dnc_follows_confirmed_merges_and_identifiers(world) -> None:
    inst = world.institution()
    a, b = world.pi(inst), world.pi(inst)
    with transaction(world.factory) as s:
        s.get(Pi, a).merged_into_id = b
        s.get(Pi, b).orcid = "0000-0000-0000-0001"
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            orcid="0000-0000-0000-0001",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
        assert dnc.matches(s, a, Mode.LIVE)  # via the merged identity's ORCID


def test_name_and_institution_alone_is_only_a_warning(world) -> None:
    inst = world.institution()
    listed = world.pi(inst, name="Alex Homonym")
    other = world.pi(inst, name="Alex Homonym")
    with transaction(world.factory) as s:
        dnc.add_entry(
            s, scope_mode=Mode.LIVE, pi_id=listed, actor=Actor.APPLICANT, now=world.clock.now()
        )
        assert not dnc.matches(s, other, Mode.LIVE)
        assert dnc.name_institution_warnings(s, other) == [listed]
    world.approve(world.draft(world.campaign(other)))  # not auto-merged, not auto-blocked


def test_inbound_event_rows_are_per_campaign(world) -> None:
    camp, _ = sent_email1(world, Mode.SANDBOX)
    inbound(world, camp, InboundClassification.AUTO_REPLY, "x-1")
    with transaction(world.factory) as s:
        assert len(s.scalars(select(InboundEvent)).all()) == 1
