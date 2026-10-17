"""Send-path probes from the repair review (M01, M06, round-3 #19).

These probe paths the repair tests do not exercise directly. Each asserts the safe
outcome. Fake transport, synthetic data, injected clock only.
"""

from __future__ import annotations

from datetime import timedelta
from email import message_from_bytes
from email.policy import default as default_policy

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger, Reservation
from marcelorian.domain.enums import Mode, PauseReason
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import reconciliation, send_results
from marcelorian.services.gateway import DispatchResult, Outcome
from tests.factories import World


@pytest.fixture
def live(world: World) -> World:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def stall_after_claim(world: World, monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    original = world.gateway._claim
    fired: list[bool] = []

    def wrapped(*args, **kwargs):
        claimed = original(*args, **kwargs)
        if not isinstance(claimed, DispatchResult) and not fired:
            fired.append(True)
            world.clock.advance(timedelta(seconds=seconds))
        return claimed

    monkeypatch.setattr(world.gateway, "_claim", wrapped)


def recover(world: World) -> dict[str, object]:
    return reconciliation.startup_recovery(
        world.factory,
        {Mode.LIVE: world.gmail, Mode.TRANSPORT_TEST: world.gmail, Mode.SANDBOX: world.sandbox},
        {"real": world.real_sync},
        world.clock,
    )


def budget(world: World) -> list[str]:
    with transaction(world.factory) as s:
        return [r.status for r in s.scalars(select(BudgetLedger).order_by(BudgetLedger.id))]


def test_not_invoked_record_failure_stays_ambiguous_and_never_sends(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    stall_after_claim(live, monkeypatch, 6)

    def boom(*_a, **_k):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(send_results, "record_not_invoked", boom)
    result = live.dispatch(mid)
    assert result.outcome is Outcome.NOT_SUBMITTED
    assert live.message(mid).state == "sending"
    again = live.dispatch(mid)
    recover(live)
    with transaction(live.factory) as s:
        pauses = [p.reason for p in campaign_svc.open_reasons(s, camp)]
    assert again.outcome is Outcome.NOT_ELIGIBLE
    assert len(live.gmail.send_calls) == 0
    assert [a.status for a in live.attempts(mid)] == ["uncertain"]
    assert budget(live) == ["held"]
    assert PauseReason.SEND_UNCERTAIN.value in pauses


def test_followup_not_invoked_then_reapproved_sends_once_on_verified_headers(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    live.gmail.rewrite_message_id = True
    m1 = live.draft_and_approve(camp)
    assert live.dispatch(m1).outcome is Outcome.SENT
    observed_id = live.message(m1).rfc_message_id
    assert observed_id and "rewritten" in observed_id
    due = live.campaign_row(camp).next_due_at
    assert due is not None
    live.clock.set(due + timedelta(minutes=1))
    m2 = live.draft_and_approve(camp, 2)
    live.real_sync.sync_now()
    stall_after_claim(live, monkeypatch, 6)
    first = live.dispatch(m2)
    assert first.outcome is Outcome.NOT_SUBMITTED, first
    assert live.message(m2).state == "needs_reapproval"
    monkeypatch.undo()
    live.clock.advance(timedelta(minutes=7))  # past pacing anchored at not_after
    live.approve(m2)
    second = live.dispatch(m2)
    assert second.outcome is Outcome.SENT, second
    assert len(live.gmail.send_calls) == 2
    raw = message_from_bytes(live.gmail.send_calls[-1], policy=default_policy)
    assert raw["In-Reply-To"] == observed_id
    attempts = live.attempts(m2)
    assert [a.status for a in attempts] == ["not_submitted", "sent"]
    assert attempts[0].app_message_id != attempts[1].app_message_id
    assert budget(live) == ["used", "released", "used"]


def test_email1_not_invoked_then_reapproved_consumes_one_contact(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp = live.campaign(live.pi(live.institution()), Mode.LIVE)
    mid = live.draft_and_approve(camp)
    stall_after_claim(live, monkeypatch, 6)
    assert live.dispatch(mid).outcome is Outcome.NOT_SUBMITTED
    monkeypatch.undo()
    live.clock.advance(timedelta(minutes=7))
    live.approve(mid)
    assert live.dispatch(mid).outcome is Outcome.SENT
    with transaction(live.factory) as s:
        rows = [
            (r.kind, r.status)
            for r in s.scalars(select(Reservation).where(Reservation.campaign_id == camp))
        ]
    print("reservations:", rows)
    assert len(live.gmail.send_calls) == 1
    assert sum(1 for kind, status in rows if kind == "new_contact" and status == "consumed") == 1
    assert budget(live) == ["released", "used"]
