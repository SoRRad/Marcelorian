"""Background worker (P3): heartbeat, missed-window rule, due follow-ups, notices, thread."""

from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import select

from marcelorian.clock import PracticeClock
from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent, Campaign, Message, WorkerHeartbeat
from marcelorian.domain.enums import MessageState, Mode
from marcelorian.services import drafting, worker
from tests.service.test_drafting import confirm_facts
from tests.service.test_outreach_status import started

NO_LINES_TEMPLATE = (
    "Dear {salutation},\n\nI am following up on my earlier email.\n\n"
    "Best regards,\n{signature_name}"
)


def cycle(world) -> worker.CycleResult:
    return worker.run_cycle(world.factory, world.clock, world.clock)


def keys(world) -> dict[str, worker.Notice]:
    with transaction(world.factory) as s:
        now = world.clock.now()
        return {n.key: n for n in worker.notices(s, now=now, campaign_now=now)}


def due_campaign(world) -> int:
    confirm_facts(world)
    campaign_id = started(world, world.pi(world.institution()))
    with transaction(world.factory) as s:
        due = s.get(Campaign, campaign_id).next_due_at
    world.clock.advance(due - world.clock.now() + timedelta(minutes=1))
    return campaign_id


def test_due_followup_with_personal_line_becomes_a_notice(world) -> None:
    campaign_id = due_campaign(world)
    result = cycle(world)
    assert result.to_write == [campaign_id] and result.drafted == []
    notice = keys(world)["followups_to_write"]
    assert notice.url == f"/campaigns/{campaign_id}/draft" and notice.count == 1


def test_due_followup_without_personal_lines_is_drafted_once(world) -> None:
    campaign_id = due_campaign(world)
    with transaction(world.factory) as s:
        drafting.set_template(s, 2, NO_LINES_TEMPLATE, now=world.clock.now())
    assert cycle(world).drafted == [campaign_id]
    assert cycle(world).drafted == []  # never drafted twice
    with transaction(world.factory) as s:
        message = s.scalar(
            select(Message).where(Message.campaign_id == campaign_id, Message.step == 2)
        )
        assert message.state == MessageState.AWAITING_APPROVAL.value  # never approved for you
        creators = s.scalars(
            select(AuditEvent.actor).where(
                AuditEvent.entity_type == "message", AuditEvent.entity_id == str(message.id)
            )
        ).all()
        assert "system" in creators
    found = keys(world)
    assert "followups_to_write" not in found and found["drafts_to_review"].count == 1


def test_stopped_or_closed_campaigns_are_left_alone(world) -> None:
    campaign_id = due_campaign(world)
    with transaction(world.factory) as s:
        s.get(Campaign, campaign_id).sequence_stopped = True
    assert cycle(world).to_write == []


def test_missed_window_rolls_the_approval(world) -> None:
    campaign_id = world.campaign(world.pi(world.institution()))
    message_id = world.draft_and_approve(campaign_id)
    with transaction(world.factory) as s:
        end = s.get(Message, message_id).eligible_window_end
    world.clock.advance(end - world.clock.now() + timedelta(minutes=1))
    assert cycle(world).rolled == [message_id]
    with transaction(world.factory) as s:
        message = s.get(Message, message_id)
        assert message.state == MessageState.APPROVED_QUEUED.value
        assert message.eligible_window_end > world.clock.now()


def test_heartbeat_and_stalled_notice(world) -> None:
    assert "worker_stalled" not in keys(world)  # never started: no warning
    cycle(world)
    cycle(world)
    with transaction(world.factory) as s:
        assert len(s.scalars(select(WorkerHeartbeat)).all()) == 1  # one row, updated
    world.clock.advance(timedelta(minutes=6))
    assert "6 minutes" in keys(world)["worker_stalled"].text
    cycle(world)
    assert "worker_stalled" not in keys(world)


def test_thread_recovers_cycles_and_stops(world) -> None:
    seen: list[worker.CycleResult] = []
    bg = worker.BackgroundWorker(
        world.factory,
        world.clock,
        world.clock,
        {},
        {},
        interval=timedelta(seconds=30),
        on_cycle=seen.append,
    )
    bg.start()
    for _ in range(200):
        if seen:
            break
        time.sleep(0.01)
    bg.stop()
    assert seen, "the first cycle runs right after startup recovery"
    with transaction(world.factory) as s:
        assert worker.last_heartbeat(s) is not None


def test_cycle_errors_are_contained(world, monkeypatch) -> None:
    bg = worker.BackgroundWorker(world.factory, world.clock, world.clock, {}, {})

    def boom(*_a, **_k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(worker, "run_cycle", boom)
    assert bg.cycle() is None


def test_practice_time_never_rolls_a_live_approval(world) -> None:
    world.connect_handoff_mailbox()
    world.set_mode(Mode.LIVE)
    world.signature_step()
    message_id = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    with transaction(world.factory) as s:
        end = s.get(Message, message_id).eligible_window_end
    ahead = PracticeClock(world.clock, end - world.clock.now() + timedelta(days=1), active=True)
    result = worker.run_cycle(world.factory, world.clock, ahead)
    assert message_id not in result.rolled
    with transaction(world.factory) as s:
        assert s.get(Message, message_id).eligible_window_end == end
