"""Look up every contact in one click: who is due, progress, stop/resume, failures."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from sqlalchemy import func, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Evidence, LookupRun
from marcelorian.lookups import bulk
from tests.fakes.lookups import FakeFetcher

ALL_SOURCES = {"esearch", "esummary", "openalex_author", "openalex_works", "reporter"}


def due(world) -> list[int]:
    with transaction(world.factory) as s:
        return bulk.needing_lookup(s, world.clock.now())


def run(world, fetcher, **kw) -> bulk.BulkLookup:
    runner = bulk.BulkLookup(world.factory, fetcher, world.clock, pause=0, **kw)
    runner.start()
    runner.join(10)
    return runner


def test_everyone_is_looked_up_once_then_skipped(world) -> None:
    inst = world.institution()
    pis = [world.pi(inst, name=f"Pat Example{i}") for i in range(3)]
    assert due(world) == pis
    runner = run(world, FakeFetcher())
    progress = runner.progress()
    assert (progress.running, progress.total, progress.done, progress.failed) == (False, 3, 3, 0)
    assert progress.finished_at is not None
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(Evidence)) > 0
    assert due(world) == [] and runner.start() == 0  # nothing left: a no-op
    world.clock.advance(bulk.REFRESH_AFTER + timedelta(days=1))
    assert due(world) == pis  # refreshed after 30 days


def test_failed_contacts_are_tried_again(world) -> None:
    pi = world.pi(world.institution())
    runner = run(world, FakeFetcher(fail=ALL_SOURCES))
    assert runner.progress().failed == 1
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(LookupRun)) > 0  # recorded...
    assert due(world) == [pi]  # ...but still due


def test_stop_then_resume_continues_with_the_rest(world) -> None:
    inst = world.institution()
    pis = [world.pi(inst, name=f"Pat Example{i}") for i in range(3)]
    done = threading.Event()
    runner = bulk.BulkLookup(
        world.factory, FakeFetcher(), world.clock, pause=30, on_contact_done=done.set
    )
    assert runner.start() == 3
    assert runner.start() == 0  # one run at a time
    assert done.wait(10)
    runner.stop()
    runner.join(10)
    progress = runner.progress()
    assert progress.stopped and progress.done == 1 and not progress.running
    assert due(world) == pis[1:]
    second = run(world, FakeFetcher())
    assert second.progress().done == 2 and due(world) == []


def test_merged_contacts_are_not_looked_up(world) -> None:
    from marcelorian.db.models import Pi

    inst = world.institution()
    keep, merged = world.pi(inst, name="Pat Example"), world.pi(inst, name="Pat Example2")
    with transaction(world.factory) as s:
        s.get(Pi, merged).merged_into_id = keep
    assert due(world) == [keep]


def test_progress_is_safe_to_read_while_running(world) -> None:
    world.pi(world.institution())
    runner = bulk.BulkLookup(world.factory, FakeFetcher(), world.clock, pause=0)
    runner.start()
    deadline = time.monotonic() + 10
    while runner.progress().running and time.monotonic() < deadline:
        assert runner.progress().done <= runner.progress().total
    runner.join(10)
    assert runner.progress().done == 1
