"""M01: additional regression cases. Fake transport, synthetic data, injected clock.

Focus: the claim reads ``now`` BEFORE ``BEGIN IMMEDIATE`` acquires the write lock
(SQLAlchemy begins lazily on the first statement; ``BUSY_TIMEOUT_MS`` is 15 s). A lock wait
therefore sits between the claim's window/pacing evaluation and its commit, and the
proposed fix measures its hand-off limit from that pre-lock instant.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import event

from marcelorian.domain.enums import Mode
from marcelorian.domain.timing import resolve_local
from marcelorian.services.gateway import DispatchResult, Outcome
from tests.factories import World

NY = ZoneInfo("America/New_York")


def ny(*args: int) -> datetime:
    return resolve_local(datetime(*args), NY)  # type: ignore[arg-type]


@pytest.fixture
def live(world: World) -> World:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def email1(world: World) -> int:
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    return world.draft_and_approve(camp)


def lock_wait_in_claim(world: World, monkeypatch: pytest.MonkeyPatch, wait: timedelta) -> None:
    """The claim's BEGIN IMMEDIATE takes ``wait`` to acquire the write lock (another writer)."""
    engine = world.factory.kw["bind"]
    armed: list[bool] = []

    def on_begin(_conn: object) -> None:  # runs after the BEGIN IMMEDIATE listener
        if armed:
            armed.clear()
            world.clock.advance(wait)

    event.listen(engine, "begin", on_begin)
    original = world.gateway._claim

    def wrapped(*args: object, **kwargs: object) -> object:
        armed.append(True)
        try:
            return original(*args, **kwargs)
        finally:
            armed.clear()

    monkeypatch.setattr(world.gateway, "_claim", wrapped)


def send_instants(world: World) -> list[datetime]:
    instants: list[datetime] = []
    world.gmail.on_send = lambda _raw: instants.append(world.clock.now())
    return instants


# 1. Safety: a lock wait that crosses the window end must not lead to a transport call.
def test_lock_wait_across_window_end_never_submits(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.clock.set(ny(2026, 10, 20, 10, 59, 58))
    mid = email1(live)
    instants = send_instants(live)
    lock_wait_in_claim(live, monkeypatch, timedelta(seconds=5))
    result = live.dispatch(mid)
    print("result:", result, "send instants:", instants, "attempts:", live.attempts(mid))
    assert instants == [], f"transport called at {instants} (window end 15:00:00Z)"


# 2. Quality: a lock wait BEFORE the claim commits is an ordinary deferral (window passed,
#    rolls to the next window per B7), not a consumed approval that needs re-approval.
def test_lock_wait_across_window_end_is_a_plain_deferral(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.clock.set(ny(2026, 10, 20, 10, 59, 58))
    mid = email1(live)
    lock_wait_in_claim(live, monkeypatch, timedelta(seconds=5))
    result = live.dispatch(mid)
    observed = {
        "outcome": result.outcome,
        "attempts": [a.status for a in live.attempts(mid)],
        "message_state": live.message_state(mid).value,
        "sends": len(live.gmail.send_calls),
    }
    print("observed:", observed, result)
    assert observed == {
        "outcome": Outcome.DEFERRED,
        "attempts": [],
        "message_state": "approved_queued",
        "sends": 0,
    }


# 3. Regression guard for the proposed fix: a 6 s lock wait at 09:00 (mid-window, hand-off
#    itself instantaneous) must still send. The fix measures its 5 s limit from the pre-lock
#    clock read, so lock contention would burn the approval (needs_reapproval).
def test_lock_wait_mid_window_with_instant_handoff_still_sends(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    mid = email1(live)
    lock_wait_in_claim(live, monkeypatch, timedelta(seconds=6))
    result = live.dispatch(mid)
    print("result:", result, "message:", live.message_state(mid))
    assert result.outcome is Outcome.SENT
    assert len(live.gmail.send_calls) == 1


# 4. Same as 1 with a genuine SQLite write lock held by another connection (realism check).
def test_genuine_write_lock_across_window_end_never_submits(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.clock.set(ny(2026, 10, 20, 10, 59, 58))
    mid = email1(live)
    instants = send_instants(live)
    db_file = live.factory.kw["bind"].url.database
    ready, locked = threading.Event(), threading.Event()
    original = live.gateway._claim

    def wrapped(*args: object, **kwargs: object) -> object:
        ready.set()
        locked.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(live.gateway, "_claim", wrapped)
    out: list[DispatchResult] = []
    worker = threading.Thread(target=lambda: out.append(live.dispatch(mid)))
    worker.start()
    assert ready.wait(10)
    other = sqlite3.connect(db_file, isolation_level=None, timeout=30)
    other.execute("BEGIN IMMEDIATE")  # e.g. a sync writer
    locked.set()
    time.sleep(0.5)  # claim has read its clock and is now blocked on the lock
    live.clock.advance(timedelta(seconds=5))  # lock held across 11:00:00 New York
    other.execute("COMMIT")
    other.close()
    worker.join(30)
    print("result:", out, "send instants:", instants)
    assert instants == [], f"transport called at {instants} (window end 15:00:00Z)"


# 5. Residual, documented rather than tested: a stall INSIDE a future real adapter's send()
#    before its request (token refresh, DNS, TLS) is outside any gateway check. The P0.3
#    adapter contract (transport/base.py) requires send() to do no network I/O before its
#    single request; credentials are refreshed during the preflight account() call.
