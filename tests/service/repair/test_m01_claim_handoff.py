"""M01 regression tests: a delay between the durable claim and ``transport.send``.

Each test asserts the desired safe outcome, so it
fails on code that lets a stale claim reach the transport. Fake transport, synthetic data
and the injected clock only.

Seam: ``SendGateway._claim`` returns only after its ``BEGIN IMMEDIATE`` transaction has
committed. Wrapping it on the instance lets a test act at the exact point between the
claim commit and the ``transport.send`` call (laptop sleep, a stalled dispatcher thread,
a wall-clock correction), without touching production code.

Safe outcome for a claim whose hand-off went stale: ``transport.send`` is never invoked;
the attempt is durably recorded as *known not submitted* (not ``in_flight``/``uncertain``);
the budget hold is released; the message leaves ``sending`` for a state that cannot be
blindly retried; no ``send_uncertain`` review is opened (nothing is ambiguous).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger, PacingState
from marcelorian.domain.enums import Actor, Mode, PauseReason
from marcelorian.domain.timing import resolve_local
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import runtime
from marcelorian.services.gateway import DispatchResult, Outcome
from tests.factories import APPLICANT_EMAIL, World

pytestmark = pytest.mark.narrow_window  # window-edge mechanics (11:00 end)

NY = ZoneInfo("America/New_York")
WEEK = timedelta(hours=168)

#: States from which a message cannot be submitted again without a deliberate decision
#: (needs_reapproval is the recommended one; approved_queued would be an owner choice).
SAFE_MESSAGE_STATES = {"needs_reapproval", "approved_queued"}

EXPECTED_NOT_SUBMITTED = {
    "new_transport_send_calls": 0,
    "outcome_sent_or_uncertain": False,
    "attempt_statuses": ["not_submitted"],
    "budget_statuses": ["released"],
    "message_state_safe": True,
    "send_uncertain_pause_open": False,
}


@pytest.fixture
def live(world: World) -> World:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def ny(*args: int) -> datetime:
    return resolve_local(datetime(*args), NY)  # type: ignore[arg-type]


def email1(world: World) -> tuple[int, int]:
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    return camp, world.draft_and_approve(camp)


def stall_after_claim(world: World, monkeypatch: pytest.MonkeyPatch, during: Callable[[], None]):
    """Run ``during`` once, right after the first claim commits and before ``send``."""
    original = world.gateway._claim
    fired: list[bool] = []

    def wrapped(*args, **kwargs):
        claimed = original(*args, **kwargs)
        if not isinstance(claimed, DispatchResult) and not fired:
            fired.append(True)
            during()
        return claimed

    monkeypatch.setattr(world.gateway, "_claim", wrapped)


def rewind(world: World, when: datetime) -> None:
    """A wall-clock correction backwards (ManualClock forbids it; reach in deliberately)."""
    with world.clock._lock:
        world.clock._now = when


def observe(
    world: World, mid: int, camp: int, result: DispatchResult, baseline_sends: int = 0
) -> dict[str, object]:
    attempts = world.attempts(mid)
    with transaction(world.factory) as s:
        budget = [
            r.status
            for r in s.scalars(
                select(BudgetLedger)
                .where(BudgetLedger.attempt_id.in_([a.id for a in attempts]))
                .order_by(BudgetLedger.id)
            )
        ]
        pauses = [p.reason for p in campaign_svc.open_reasons(s, camp)]
    state = world.message_state(mid).value
    return {
        "new_transport_send_calls": len(world.gmail.send_calls) - baseline_sends,
        "outcome_sent_or_uncertain": result.outcome in (Outcome.SENT, Outcome.UNCERTAIN),
        "attempt_statuses": [a.status for a in attempts],
        "budget_statuses": budget,
        "message_state_safe": state in SAFE_MESSAGE_STATES,
        "send_uncertain_pause_open": PauseReason.SEND_UNCERTAIN.value in pauses,
        # informational only (not compared): what actually happened
        "_message_state": state,
        "_outcome": f"{result.outcome.value}:{result.reason}",
    }


def compared(observed: dict[str, object]) -> dict[str, object]:
    return {k: v for k, v in observed.items() if not k.startswith("_")}


# --------------------------------------------------------------------------------------------
# 1. Claim inside the window, suspended across the window end (exact boundary and +5 s).
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resume_at", [ny(2026, 10, 20, 11, 0, 0), ny(2026, 10, 20, 11, 0, 5)], ids=["at_end", "end+5s"]
)
def test_claim_at_105959_then_suspend_past_window_end_is_not_submitted(
    live: World, monkeypatch: pytest.MonkeyPatch, resume_at: datetime
) -> None:
    live.clock.set(ny(2026, 10, 20, 10, 59, 59))
    camp, mid = email1(live)
    send_instants: list[datetime] = []
    live.gmail.on_send = lambda _raw: send_instants.append(live.clock.now())
    stall_after_claim(live, monkeypatch, lambda: live.clock.set(resume_at))
    result = live.dispatch(mid)
    observed = observe(live, mid, camp, result)
    print("observed:", observed, "send instants:", send_instants)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED


# --------------------------------------------------------------------------------------------
# 2. Overnight sleep: claim Tue 09:00, resume Wed 09:00 - inside *a* permitted window again,
#    so a recheck of in_window(now) alone would wrongly pass; the assigned window is over.
# --------------------------------------------------------------------------------------------


def test_overnight_suspend_after_claim_is_not_submitted_next_morning(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, mid = email1(live)
    stall_after_claim(live, monkeypatch, lambda: live.clock.advance(timedelta(hours=24)))
    result = live.dispatch(mid)
    observed = observe(live, mid, camp, result)
    print("observed:", observed)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED


# --------------------------------------------------------------------------------------------
# 3. Follow-up: a 30-minute stall INSIDE the window, during which the PI replies in the
#    campaign thread. The preflight sync and reply checks are 30 minutes stale at the call.
# --------------------------------------------------------------------------------------------


def test_followup_long_stall_inside_window_with_reply_in_thread_is_not_submitted(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    thread = live.message(m1).gmail_thread_id
    pi_address = live.attempts(m1)[0].recipient
    live.clock.advance(WEEK)  # Tue 2026-10-27 09:00 New York, inside the window
    m2 = live.draft_and_approve(camp, 2)
    baseline = len(live.gmail.send_calls)

    def stall() -> None:
        live.clock.advance(timedelta(minutes=10))
        live.gmail.inject(sender=pi_address, to=APPLICANT_EMAIL, thread_id=thread)  # PI replies
        live.clock.advance(timedelta(minutes=20))  # resume at 09:30, still inside the window

    stall_after_claim(live, monkeypatch, stall)
    result = live.dispatch(m2)
    observed = observe(live, m2, camp, result, baseline_sends=baseline)
    print("observed:", observed)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED


# --------------------------------------------------------------------------------------------
# 4. Persistent Pause pressed (UI thread) while the dispatcher's hand-off is stalled 10 min.
# --------------------------------------------------------------------------------------------


def test_pause_during_stalled_handoff_prevents_late_submission(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, mid = email1(live)

    def stall() -> None:
        live.clock.advance(timedelta(minutes=2))
        with transaction(live.factory) as s:
            runtime.pause_dispatch(s, actor=Actor.APPLICANT, now=live.clock.now())
        live.clock.advance(timedelta(minutes=8))

    stall_after_claim(live, monkeypatch, stall)
    result = live.dispatch(mid)
    observed = observe(live, mid, camp, result)
    print("observed:", observed)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED


# --------------------------------------------------------------------------------------------
# 5. Pacing at the actual submission instant (B8): A claims, its hand-off stalls; B (UI
#    "Approve and send") claims once pacing allows and is submitted; A resumes 1 s later.
# --------------------------------------------------------------------------------------------


def test_actual_submissions_stay_paced_when_a_claimed_handoff_stalls(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _camp_a, ma = email1(live)
    _camp_b, mb = email1(live)
    send_instants: list[datetime] = []
    live.gmail.on_send = lambda _raw: send_instants.append(live.clock.now())
    results: dict[str, DispatchResult] = {}

    def stall() -> None:
        with transaction(live.factory) as s:
            allowed = s.get(PacingState, "real").next_allowed_at
        live.clock.set(allowed)
        results["b"] = live.gateway.dispatch(mb)
        live.clock.advance(timedelta(seconds=1))

    stall_after_claim(live, monkeypatch, stall)
    results["a"] = live.dispatch(ma)
    gaps = [b - a for a, b in zip(sorted(send_instants), sorted(send_instants)[1:], strict=False)]
    print("results:", results, "send instants:", send_instants, "gaps:", gaps)
    pacing_min = timedelta(seconds=120)  # default pacing_min_seconds
    assert all(gap >= pacing_min for gap in gaps), f"actual submissions only {gaps} apart"


# --------------------------------------------------------------------------------------------
# 6. Wall clock corrected backwards after the claim (e.g. NTP after resume) to before the
#    window start: the claim-time checks can no longer be trusted -> fail closed.
# --------------------------------------------------------------------------------------------


def test_clock_regression_after_claim_is_not_submitted(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, mid = email1(live)
    stall_after_claim(live, monkeypatch, lambda: rewind(live, ny(2026, 10, 20, 7, 59, 0)))
    result = live.dispatch(mid)
    observed = observe(live, mid, camp, result)
    print("observed:", observed)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED


# --------------------------------------------------------------------------------------------
# 7. Control (must pass before AND after a fix): an ordinary hand-off of 1 s just before
#    the window end still submits exactly once.
# --------------------------------------------------------------------------------------------


def test_control_ordinary_handoff_just_before_window_end_still_sends(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.clock.set(ny(2026, 10, 20, 10, 59, 58))
    _camp, mid = email1(live)
    stall_after_claim(live, monkeypatch, lambda: live.clock.advance(timedelta(seconds=1)))
    result = live.dispatch(mid)
    assert result.outcome is Outcome.SENT
    assert len(live.gmail.send_calls) == 1
    assert [a.status for a in live.attempts(mid)] == ["sent"]


# --------------------------------------------------------------------------------------------
# 8. Email 1 Cancel pressed during a stalled hand-off: recorded as a stop request (not a
#    recall, B5). Because the transport is then never invoked, non-submission is definite
#    and note 11 applies: the B3 path (campaign back to candidate, reservations released).
# --------------------------------------------------------------------------------------------


def test_cancel_during_stalled_handoff_takes_effect_without_submission(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcelorian.db.models import Campaign, Reservation
    from marcelorian.services import approvals

    camp, mid = email1(live)
    answers: list[str] = []

    def stall() -> None:
        live.clock.advance(timedelta(minutes=2))
        with transaction(live.factory) as s:
            answers.append(
                approvals.cancel_initial(
                    s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=live.clock.now()
                )
            )
        live.clock.advance(timedelta(minutes=8))

    stall_after_claim(live, monkeypatch, stall)
    result = live.dispatch(mid)
    with transaction(live.factory) as s:
        reservations = sorted(
            r.status for r in s.scalars(select(Reservation).where(Reservation.campaign_id == camp))
        )
    observed = {
        "cancel_answer": answers,
        "transport_send_calls": len(live.gmail.send_calls),
        "attempt_statuses": [a.status for a in live.attempts(mid)],
        "message_state": live.message_state(mid).value,
        "campaign_state": live.campaign_row(camp).state,
        "active_reservations": reservations.count("active"),
        "_outcome": f"{result.outcome.value}:{result.reason}",
    }
    print("observed:", observed)
    assert compared(observed) == {
        "cancel_answer": ["not_recalled"],  # pressed after the claim: a stop request
        "transport_send_calls": 0,
        "attempt_statuses": ["not_submitted"],
        "message_state": "cancelled",
        "campaign_state": "candidate",
        "active_reservations": 0,
    }


# --------------------------------------------------------------------------------------------
# 9. transport_test with an approved "window always open" override (B2): no window end can
#    bound the hand-off, so only a hand-off limit protects. A 2-hour stall must not submit.
# --------------------------------------------------------------------------------------------


def test_transport_test_always_open_override_still_bounds_the_handoff(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcelorian.domain.snapshot import TestOverride

    world.connect_mailbox()
    tester = world.allowlist("tester@example.test")
    world.set_mode(Mode.TRANSPORT_TEST)
    world.clock.set(ny(2026, 10, 25, 3, 0))  # Sunday night: outside every normal window
    camp = world.campaign(
        world.pi(world.institution(), synthetic=True), Mode.TRANSPORT_TEST, test_recipient_id=tester
    )
    mid = world.draft_and_approve(camp, override=TestOverride(window_always_open=True))
    stall_after_claim(world, monkeypatch, lambda: world.clock.advance(timedelta(hours=2)))
    result = world.dispatch(mid)
    observed = observe(world, mid, camp, result)
    print("observed:", observed)
    assert compared(observed) == EXPECTED_NOT_SUBMITTED
