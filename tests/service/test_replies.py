"""Replies, tags, reviews and tasks (spec §10; Outlook-only pilot)."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, DoNotContact, PiEmail, Task
from marcelorian.domain.enums import CampaignState, Mode
from marcelorian.services import replies
from tests.service.test_outreach_status import started


def campaign(world, campaign_id: int) -> Campaign:
    return world.campaign_row(campaign_id)


def tag(world, campaign_id: int, value: str, **kw) -> None:
    with transaction(world.factory) as s:
        replies.record_reply(s, s.get(Campaign, campaign_id), value, now=world.clock.now(), **kw)


def test_practice_reply_then_no_opening_closes(world) -> None:
    camp = started(world, world.pi(world.institution()))
    tag(world, camp, "no_opening", note="no funding this year")
    row = campaign(world, camp)
    assert (row.state, row.close_outcome, row.reply_tag) == ("closed", "no_opening", "no_opening")


def test_positive_reply_keeps_the_conversation(world) -> None:
    camp = started(world, world.pi(world.institution()))
    tag(world, camp, "positive")
    assert campaign(world, camp).state == CampaignState.CONVERSATION.value


def test_do_not_contact_tag_uses_the_mode_scope(world) -> None:
    camp = started(world, world.pi(world.institution()))
    tag(world, camp, "do_not_contact")
    with transaction(world.factory) as s:
        entry = s.scalars(select(DoNotContact)).one()
        assert entry.scope_mode == Mode.SANDBOX.value  # practice never edits the live list
    assert campaign(world, camp).close_outcome == "do_not_contact"


def test_referral_and_contact_later_create_tasks(world) -> None:
    camp = started(world, world.pi(world.institution()))
    tag(world, camp, "referral", note="Dr Synthetic Colleague")
    with pytest.raises(replies.ReplyError):
        tag(world, camp, "contact_later")
    tag(world, camp, "contact_later", contact_later_on=date(2027, 3, 1))
    with transaction(world.factory) as s:
        tasks = replies.open_tasks(s)
        assert [(t.kind, t.due_on) for t in tasks] == [
            ("contact_later", "2027-03-01"),
            ("referral", None),
        ]
        replies.complete_task(s, tasks[0].id, now=world.clock.now())
    with transaction(world.factory) as s:
        assert len(replies.open_tasks(s)) == 1
        assert (
            s.scalar(select(Task.details).where(Task.kind == "referral"))
            == "Dr Synthetic Colleague"
        )


def test_reply_errors(world) -> None:
    pi = world.pi(world.institution())
    candidate = world.campaign(pi)
    with pytest.raises(replies.ReplyError):
        tag(world, candidate, "positive")
    camp = started(world, world.pi(world.institution()))
    with pytest.raises(replies.ReplyError):
        tag(world, camp, "maybe")


def test_auto_reply_review_resume_and_delivery_bounce(world) -> None:
    camp = started(world, world.pi(world.institution()))
    with transaction(world.factory) as s:
        replies.simulate(s, s.get(Campaign, camp), "auto_reply", now=world.clock.now())
    assert campaign(world, camp).state == CampaignState.PAUSED_REVIEW.value
    with transaction(world.factory) as s:
        pause = s.scalars(select(CampaignPause)).one()
        assert "resume" in replies.actions_for(pause)
        replies.resolve_review(s, pause.id, "resume", now=world.clock.now(), mailbox_email=None)
    assert campaign(world, camp).state == CampaignState.OUTREACH_PENDING.value

    other = started(world, world.pi(world.institution()))
    with transaction(world.factory) as s:
        replies.simulate(s, s.get(Campaign, other), "delivery_notice", now=world.clock.now())
        pause = s.scalars(select(CampaignPause).where(CampaignPause.campaign_id == other)).one()
        replies.resolve_review(s, pause.id, "bounce", now=world.clock.now(), mailbox_email=None)
    row = campaign(world, other)
    assert (row.state, row.close_outcome) == ("closed", "bounce_confirmed")
    with transaction(world.factory) as s:
        assert s.scalar(select(PiEmail.invalid).where(PiEmail.pi_id == row.pi_id)) is True


def test_simulations_only_in_practice_and_close(world) -> None:
    world.set_mode(Mode.LIVE)
    live = world.campaign(world.pi(world.institution()), Mode.LIVE)
    with transaction(world.factory) as s, pytest.raises(replies.ReplyError):
        replies.simulate(s, s.get(Campaign, live), "reply", now=world.clock.now())
    with transaction(world.factory) as s:
        with pytest.raises(replies.ReplyError):
            replies.close(s, s.get(Campaign, live), " ", now=world.clock.now())
        replies.close(s, s.get(Campaign, live), "changed my mind", now=world.clock.now())
    assert campaign(world, live).state == "closed"
