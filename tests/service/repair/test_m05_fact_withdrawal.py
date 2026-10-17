"""M05 regression tests: withdrawing a fact's confirmation without changing its text.

Each test asserts the DESIRED SAFE OUTCOME from the owner's brief:

* ``update_fact(fact, <same text>, confirm=False)`` on a confirmed fact withdraws the
  confirmation: status -> proposed, version bumped (spec §4: "its own version
  (incremented on every change)"), text unchanged, audited;
* it invalidates ONLY the unsent approved messages that depend on that fact
  (B18 / §9.1): dependent ``approved_queued`` -> ``needs_reapproval``; unrelated approvals
  and already-sent messages are untouched;
* a withdrawn fact can never be sent: not by a later dispatch, not by a withdrawal that
  lands during the network preflight, and not by approving a draft that cites it;
* genuine no-ops (same text and same status) stay no-ops: no version bump, no audit, no
  invalidation.

Synthetic data, sandbox / fake Gmail transports, injected clock only.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import ApprovalSnapshot, AuditEvent, Message, ProfileFact
from marcelorian.domain.enums import (
    Actor,
    FactStatus,
    MessageState,
    Mode,
    SnapshotState,
)
from marcelorian.services import approvals
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome

TEXT = "a research fellow at Synthetic Eye Institute"


# --------------------------------------------------------------------------- helpers
def _update(world, fact_id: int, value: str | None = None, *, confirm: bool) -> list[int]:
    """Call the existing API; ``value=None`` means "keep the current text"."""
    with transaction(world.factory) as s:
        fact = s.get(ProfileFact, fact_id)
        assert fact is not None
        return approvals.update_fact(
            s,
            fact,
            fact.value if value is None else value,
            confirm=confirm,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )


def _fact(world, fact_id: int) -> ProfileFact:
    with transaction(world.factory) as s:
        fact = s.get(ProfileFact, fact_id)
        assert fact is not None
        return fact


def _fact_audits(world, fact_id: int) -> list[AuditEvent]:
    with transaction(world.factory) as s:
        return list(
            s.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == "profile_fact",
                    AuditEvent.entity_id == str(fact_id),
                )
                .order_by(AuditEvent.id)
            )
        )


def _snapshot_of(world, message_id: int) -> ApprovalSnapshot:
    with transaction(world.factory) as s:
        message = s.get(Message, message_id)
        assert message is not None and message.current_snapshot_id is not None
        snap = s.get(ApprovalSnapshot, message.current_snapshot_id)
        assert snap is not None
        return snap


def _camp(world, mode: Mode = Mode.SANDBOX) -> int:
    return world.campaign(world.pi(world.institution()), mode)


# --------------------------------------------------------------------------- reproduction
def test_same_text_confirm_false_withdraws_confirmation(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    before = _fact(world, fid)
    assert before.status == FactStatus.CONFIRMED.value and before.version == 1

    _update(world, fid, confirm=False)  # same text, confirm=False

    after = _fact(world, fid)
    assert after.value == TEXT  # the text is not touched
    assert after.status == FactStatus.PROPOSED.value, "confirmation was not withdrawn"
    assert after.version == 2, "a status change is a change: version must increment (§4)"
    audits = _fact_audits(world, fid)
    assert audits, "withdrawal must leave an audit record"
    assert json.loads(audits[-1].detail_json or "{}").get("status") == FactStatus.PROPOSED.value


def test_withdrawal_invalidates_only_dependent_unsent_approvals(world) -> None:
    used = world.fact("current_role_phrase", TEXT)
    other = world.fact("recent_news", "nothing new")

    sent_camp = _camp(world)
    sent_mid = world.draft_and_approve(sent_camp, facts=(used,))
    assert world.dispatch(sent_mid).outcome is Outcome.SENT  # already sent, cites `used`

    dep_mid = world.draft_and_approve(_camp(world), facts=(used,))  # unsent, cites `used`
    unrelated_mid = world.draft_and_approve(_camp(world), facts=(other,))  # cites other fact
    plain_mid = world.draft_and_approve(_camp(world))  # cites no fact

    affected = _update(world, used, confirm=False)

    assert affected == [dep_mid]
    assert world.message_state(dep_mid) is MessageState.NEEDS_REAPPROVAL
    dep_snap = _snapshot_of(world, dep_mid)
    assert dep_snap.state == SnapshotState.INVALIDATED.value and dep_snap.end_reason
    # untouched: unrelated approvals and the already-sent message
    assert world.message_state(unrelated_mid) is MessageState.APPROVED_QUEUED
    assert world.message_state(plain_mid) is MessageState.APPROVED_QUEUED
    assert world.message_state(sent_mid) is MessageState.SENT
    assert _snapshot_of(world, sent_mid).state == SnapshotState.CONSUMED.value
    # the other fact is not affected by the withdrawal
    assert _fact(world, other).status == FactStatus.CONFIRMED.value
    assert _fact(world, other).version == 1
    # history kept: the invalidated message's transition is audited
    with transaction(world.factory) as s:
        rows = list(
            s.scalars(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "message",
                    AuditEvent.entity_id == str(dep_mid),
                    AuditEvent.to_state == MessageState.NEEDS_REAPPROVAL.value,
                )
            )
        )
    assert len(rows) == 1


def test_dispatch_after_withdrawal_never_sends(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    mid = world.draft_and_approve(_camp(world), facts=(fid,))

    _update(world, fid, confirm=False)
    result = world.dispatch(mid)

    assert result.outcome is not Outcome.SENT, "a message citing a withdrawn fact was sent"
    assert world.attempts(mid) == []
    assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def test_withdrawal_during_preflight_prevents_submission(live) -> None:
    fid = live.fact("current_role_phrase", TEXT)
    mid = live.draft_and_approve(_camp(live, Mode.LIVE), facts=(fid,))

    live.gmail.on_search = lambda _q: _update(live, fid, confirm=False)  # mid-preflight
    result = live.dispatch(mid)

    assert live.gmail.send_calls == [], "a withdrawn fact was submitted to Gmail"
    assert result.outcome in (Outcome.NOT_ELIGIBLE, Outcome.BLOCKED)
    assert live.attempts(mid) == []
    assert live.message_state(mid) is MessageState.NEEDS_REAPPROVAL


def test_approval_refused_for_draft_citing_withdrawn_fact(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    mid = world.draft(_camp(world), facts=(fid,))  # awaiting approval, no snapshot yet

    assert _update(world, fid, confirm=False) == []  # nothing approved depended on it
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "fact_not_confirmed" in info.value.reasons


def test_reconfirm_after_withdrawal_requires_redraft_then_approves(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    camp = _camp(world)
    mid = world.draft(camp, facts=(fid,))  # drafted from version 1

    _update(world, fid, confirm=False)  # v2 proposed
    _update(world, fid, confirm=True)  # v3 confirmed again, same text
    assert _fact(world, fid).status == FactStatus.CONFIRMED.value
    assert _fact(world, fid).version == 3

    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "dependency_changed_since_draft" in info.value.reasons  # note 13
    world.draft(camp, facts=(fid,))  # deliberate re-draft from v3
    world.approve(mid)
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED


# --------------------------------------------------------------------------- controls
# Behaviour that held before the fix and must keep holding.
def test_control_same_text_confirm_true_on_confirmed_is_noop(world) -> None:
    fid = world.fact("current_role_phrase", TEXT)
    mid = world.draft_and_approve(_camp(world), facts=(fid,))

    assert _update(world, fid, confirm=True) == []

    fact = _fact(world, fid)
    assert (fact.status, fact.version) == (FactStatus.CONFIRMED.value, 1)
    assert _fact_audits(world, fid) == []
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED


def test_control_same_text_confirm_false_on_proposed_is_noop(world) -> None:
    fid = world.fact("current_role_phrase", TEXT, confirmed=False)

    assert _update(world, fid, confirm=False) == []

    fact = _fact(world, fid)
    assert (fact.status, fact.version) == (FactStatus.PROPOSED.value, 1)
    assert _fact_audits(world, fid) == []


def test_control_unrelated_fact_edit_leaves_approvals(world) -> None:
    used = world.fact("current_role_phrase", TEXT)
    other = world.fact("recent_news", "nothing new")
    mid = world.draft_and_approve(_camp(world), facts=(used,))

    assert _update(world, other, "something new", confirm=False) == []
    assert _update(world, other, confirm=True) == []

    assert world.message_state(mid) is MessageState.APPROVED_QUEUED
    assert world.dispatch(mid).outcome is Outcome.SENT
