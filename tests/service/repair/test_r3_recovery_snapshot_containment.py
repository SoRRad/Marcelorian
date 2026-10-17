"""Round-2 fix #17 is incomplete: one hash-failing snapshot still aborts startup recovery.

Scenario (spec §9.4 startup order: reconcile in-flight attempts -> sync -> missed-window
rule -> dispatch may resume): two live Email 1s, A and B, are approved on Monday for the
Tuesday 08:00-11:00 New York window. On Tuesday at 08:30 A is claimed and Gmail accepts it,
then the process dies (attempt ``in_flight``). The laptop stays off until 11:05. While it
was off, A's stored snapshot bytes were damaged, exactly the corruption round-2 finding #17
used (``canonical_json`` no longer matches its SHA-256).

Commit 1a61224 contained that corruption inside ``apply_missed_window_rule`` and patched
spec §9.4: "A message whose approval snapshot fails its hash check goes to review, without
affecting other messages" (also note 24, acceptance 72). But ``startup_recovery`` runs
``reconcile_attempt`` first, and that calls ``verify_canonical`` on A's snapshot outside
any handler. ``SnapshotError`` propagates out of ``reconcile_unresolved`` and
``startup_recovery``: the sync and the missed-window rule never run, B (whose window passed
with the worker off) is left ``approved_queued`` with a past window and no review, and
dispatch can never resume. Every later restart fails the same way, so one bad snapshot stops
the whole sender, the outcome round-2 #17 was fixed to prevent.

Expected once fixed: recovery completes, A is held for review (never resent), and the
missed-window rule still handles B.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from marcelorian.clock import ManualClock
from marcelorian.db.engine import transaction
from marcelorian.db.models import ApprovalSnapshot, Message
from marcelorian.domain.enums import MessageState, Mode
from marcelorian.domain.timing import resolve_local
from marcelorian.services import reconciliation
from tests.factories import World
from tests.fakes.gmail import SimulatedCrash

NY = ZoneInfo("America/New_York")


def test_corrupted_in_flight_snapshot_does_not_abort_startup_recovery(factory) -> None:
    world = World(factory, ManualClock(resolve_local(datetime(2026, 10, 19, 12, 0), NY)))
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    a = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    b = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))

    world.clock.set(resolve_local(datetime(2026, 10, 20, 8, 30), NY))
    world.gmail.send_script = ["crash_after_accept"]
    with pytest.raises(SimulatedCrash):
        world.worker_cycle(a)  # Gmail accepted A, then the process died
    assert world.message_state(a) is MessageState.SENDING
    assert len(world.gmail.send_calls) == 1

    with transaction(world.factory) as s:  # stored snapshot bytes damaged while off
        snap = s.get(ApprovalSnapshot, s.get(Message, a).current_snapshot_id)
        snap.canonical_json = snap.canonical_json.replace("Synthetic", "Tampered")

    world.clock.set(resolve_local(datetime(2026, 10, 20, 11, 5), NY))  # restart after window
    world.rebuild_gateway()
    result = reconciliation.startup_recovery(
        world.factory,
        {Mode.LIVE: world.gmail, Mode.TRANSPORT_TEST: world.gmail, Mode.SANDBOX: world.sandbox},
        {"real": world.real_sync},
        world.clock,
    )

    # B's window passed with the worker off: the rule must still have run for it.
    assert dict(result["missed_window"]).get(b) == "needs_reapproval"
    assert world.message_state(b) is MessageState.NEEDS_REAPPROVAL
    # A is never resent; it stays held (uncertain or sent-with-review), never re-queued.
    assert len(world.gmail.send_calls) == 1
    assert world.message_state(a) is not MessageState.APPROVED_QUEUED
