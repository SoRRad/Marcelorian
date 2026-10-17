"""M05 additional regression cases: boundaries, crash points, defence in depth.

Desired safe outcomes only. Synthetic data, sandbox / fake Gmail, injected clock.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent, ProfileFact
from marcelorian.domain.enums import Actor, FactStatus, MessageState, Mode
from marcelorian.services import approvals
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from tests.fakes.gmail import SimulatedCrash

TEXT = "a research fellow at Synthetic Eye Institute"


def _withdraw(world, fid: int) -> list[int]:
    with transaction(world.factory) as s:
        fact = s.get(ProfileFact, fid)
        assert fact is not None
        return approvals.update_fact(
            s, fact, fact.value, confirm=False, actor=Actor.APPLICANT, now=world.clock.now()
        )


def _fact(world, fid: int) -> ProfileFact:
    with transaction(world.factory) as s:
        fact = s.get(ProfileFact, fid)
        assert fact is not None
        return fact


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def _camp(world, mode: Mode = Mode.SANDBOX) -> int:
    return world.campaign(world.pi(world.institution()), mode)


# ---- reproduced the finding before the fix -------------------------
def test_withdrawal_invalidates_approved_followup(world) -> None:
    from datetime import timedelta

    fid = world.fact("current_role_phrase", TEXT)
    camp = _camp(world)
    m1 = world.draft_and_approve(camp)
    assert world.dispatch(m1).outcome is Outcome.SENT
    world.clock.advance(timedelta(days=7))
    m2 = world.draft_and_approve(camp, 2, facts=(fid,))
    assert world.message_state(m2) is MessageState.APPROVED_QUEUED

    assert _withdraw(world, fid) == [m2]
    assert world.message_state(m2) is MessageState.NEEDS_REAPPROVAL
    assert world.message_state(m1) is MessageState.SENT


def test_reapproval_after_withdrawal_refused_until_reconfirmed(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    mid = world.draft_and_approve(_camp(world), facts=(fid,))
    _withdraw(world, fid)
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "fact_not_confirmed" in info.value.reasons
    assert world.attempts(mid) == []


# ---- defence in depth (the claim-time check in dependencies_current; DECISIONS note 29)
# --------------------------------------------------------------------------------------
def test_claim_rejects_unconfirmed_fact_even_without_version_bump(live) -> None:
    fid = live.fact("current_role_phrase", TEXT)
    mid = live.draft_and_approve(_camp(live, Mode.LIVE), facts=(fid,))
    with transaction(live.factory) as s:  # hypothetical future path: status-only write
        fact = s.get(ProfileFact, fid)
        assert fact is not None
        fact.status = FactStatus.PROPOSED.value
    result = live.dispatch(mid)
    assert live.gmail.send_calls == [], "unconfirmed fact submitted at claim"
    assert result.outcome is not Outcome.SENT


# ---- controls (pass now and must still pass with the fix) ---------------------------
def test_withdrawal_after_claim_is_not_recalled(live) -> None:
    fid = live.fact("current_role_phrase", TEXT)
    mid = live.draft_and_approve(_camp(live, Mode.LIVE), facts=(fid,))
    seen: list[list[int]] = []
    live.gmail.on_send = lambda _raw: seen.append(_withdraw(live, fid))
    assert live.dispatch(mid).outcome is Outcome.SENT
    assert seen == [[]]  # message was SENDING: not invalidated (spec: unsent only)
    assert len(live.gmail.send_calls) == 1
    assert live.message_state(mid) is MessageState.SENT


def test_withdrawal_leaves_uncertain_attempt_alone(live) -> None:
    fid = live.fact("current_role_phrase", TEXT)
    mid = live.draft_and_approve(_camp(live, Mode.LIVE), facts=(fid,))
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok", "fail"]
    assert live.dispatch(mid).outcome is Outcome.UNCERTAIN
    assert _withdraw(live, fid) == []
    assert live.message_state(mid) is MessageState.UNCERTAIN
    assert len(live.attempts(mid)) == 1
    assert len(live.gmail.send_calls) == 1


def test_withdrawal_is_atomic_on_crash(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    mid = world.draft_and_approve(_camp(world), facts=(fid,))
    with pytest.raises(SimulatedCrash), transaction(world.factory) as s:
        fact = s.get(ProfileFact, fid)
        assert fact is not None
        approvals.update_fact(
            s, fact, fact.value, confirm=False, actor=Actor.APPLICANT, now=world.clock.now()
        )
        s.flush()
        raise SimulatedCrash("process died before commit")
    fact = _fact(world, fid)
    assert (fact.status, fact.version) == (FactStatus.CONFIRMED.value, 1)
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED
    with transaction(world.factory) as s:
        assert not list(
            s.scalars(select(AuditEvent).where(AuditEvent.entity_type == "profile_fact"))
        )
    # after "restart" the applicant can withdraw again and it takes effect
    _withdraw(world, fid)
    assert world.dispatch(mid).outcome is not Outcome.SENT


def test_double_withdrawal_is_idempotent(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    world.draft_and_approve(_camp(world), facts=(fid,))
    _withdraw(world, fid)
    v = _fact(world, fid).version
    assert _withdraw(world, fid) == []
    assert _fact(world, fid).version == v
