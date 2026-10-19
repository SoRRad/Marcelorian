"""Dashboard numbers: outreach status, places in use and verification progress."""

from __future__ import annotations

import io
from datetime import timedelta

from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import ResearchRecord
from marcelorian.domain.enums import CloseOutcome, Mode
from marcelorian.domain.seed import read_seed
from marcelorian.services import research_seed, status
from marcelorian.services.research_seed import import_seed
from tests.seed_fixture import csv_text, standard_rows
from tests.service.repair.test_m04_hold_release import _run_step
from tests.service.test_reservations import reply
from tests.service.test_same_place import close
from tests.service.test_same_place import started as _started


def started(world, pi_id: int) -> int:
    world.clock.advance(timedelta(minutes=10))  # past the pacing gap between sends
    return _started(world, pi_id)


def snapshot(world) -> status.OutreachStatus:
    with transaction(world.factory) as s:
        return status.outreach_status(s, Mode.SANDBOX, "institution", world.clock.now())


def test_empty_dashboard(world) -> None:
    result = snapshot(world)
    assert result.contacted == 0 and result.response_rate is None and result.places == []
    assert set(result.counts.values()) == {0}


def test_each_application_is_counted_where_it_stands(world) -> None:
    world.campaign(world.pi(world.institution()))  # not started
    world.draft_and_approve(world.campaign(world.pi(world.institution())))  # approved only
    started(world, world.pi(world.institution()))  # waiting
    talking = started(world, world.pi(world.institution()))
    reply(world, talking)  # replied
    rejected = started(world, world.pi(world.institution()))
    reply(world, rejected)
    close(world, rejected, CloseOutcome.NO_OPENING)  # rejected
    quiet = started(world, world.pi(world.institution()))
    for step in (2, 3, 4):
        _run_step(world, quiet, step)  # no reply after all follow-ups
    bounced = started(world, world.pi(world.institution()))
    close(world, bounced, CloseOutcome.BOUNCE_CONFIRMED)  # closed other
    result = snapshot(world)
    assert result.counts == {
        "waiting": 1,
        "needs_attention": 0,
        "replied": 1,
        "rejected": 1,
        "no_reply": 1,
        "approved_not_sent": 1,
        "not_started": 1,
        "closed_other": 1,
    }
    assert result.contacted == 5 and result.responded == 2 and result.response_rate == 40
    assert result.pending == 1
    states = sorted(p.state for p in result.places)
    assert states == ["approved_not_sent", "no_reply", "replied", "waiting"]
    no_reply = next(p for p in result.places if p.state == "no_reply")
    assert no_reply.free_from is not None
    world.clock.set(no_reply.free_from)
    assert "no_reply" not in [p.state for p in snapshot(world).places]


def test_verification_progress(world) -> None:
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(standard_rows()), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )
        ids = {r.external_record_id: r.id for r in s.scalars(select(ResearchRecord))}

    def find(external_id: str, verification: str) -> None:
        with transaction(world.factory) as s:
            research_seed.quick_finding(
                s,
                ids[external_id],
                verification=verification,
                source="https://synthetic-eye.test/faculty",
                note="",
                now=world.clock.now(),
            )

    find("C-FAC1", "confirmed_current")
    world.clock.advance(timedelta(days=80))
    find("C-PROG", "confirmed_current")
    find("C-AUTH1", "fellow_or_trainee")
    with transaction(world.factory) as s:
        first = research_seed.progress(s, world.clock.now().date())
    assert (first.confirmed, first.confirmed_this_week, first.expiring_soon) == (2, 1, 1)
    assert (first.negative, first.expired, first.total) == (1, 0, 6)
    world.clock.advance(timedelta(days=11))
    with transaction(world.factory) as s:
        later = research_seed.progress(s, world.clock.now().date())
    assert (later.confirmed, later.expired, later.confirmed_this_week) == (1, 1, 0)
