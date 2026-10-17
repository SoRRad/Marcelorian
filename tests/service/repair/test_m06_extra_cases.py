"""M06: additional regression cases. Fake transports, synthetic data, injected clock.

Each test asserts the DESIRED SAFE OUTCOME. Nothing here says anything about live Gmail.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from email import message_from_bytes
from email.policy import default as default_policy

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger, Message
from marcelorian.domain.enums import Mode
from marcelorian.services import reconciliation
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.transport.base import (
    AmbiguousOutcome,
    DefiniteRejection,
    MessageMeta,
    TransportUnavailable,
)
from marcelorian.transport.sandbox import SandboxTransport
from tests.factories import World
from tests.fakes.gmail import FakeGmailTransport, SimulatedCrash

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world: World) -> World:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def email1(world: World, mode: Mode = Mode.LIVE) -> tuple[int, int]:
    camp = world.campaign(world.pi(world.institution()), mode)
    return camp, world.draft_and_approve(camp)


def recover(world: World, sandbox: object | None = None) -> dict[str, object]:
    return reconciliation.startup_recovery(
        world.factory,
        {
            Mode.LIVE: world.gmail,
            Mode.TRANSPORT_TEST: world.gmail,
            Mode.SANDBOX: sandbox or world.sandbox,
        },
        {"real": world.real_sync},
        world.clock,
    )


def stored_id(world: World, mid: int) -> str | None:
    gid = world.message(mid).gmail_message_id
    assert gid is not None
    return world.gmail.store[gid].meta.header("message-id")


def try_followup(world: World, camp: int, step: int) -> dict[str, object]:
    before = len(world.gmail.send_calls)
    mid = world.draft(camp, step)
    approved, reasons = True, None
    try:
        world.approve(mid)
    except ApprovalError as exc:
        approved, reasons = False, exc.reasons
    outcome = world.dispatch(mid).outcome.value if approved else None
    new = world.gmail.send_calls[before:]
    mime = message_from_bytes(new[-1], policy=default_policy) if new else None
    return {
        "mid": mid,
        "approved": approved,
        "reasons": reasons,
        "outcome": outcome,
        "sends": len(new),
        "in_reply_to": mime["In-Reply-To"] if mime else None,
        "bcc": mime["Bcc"] if mime else None,
        "raw": new[-1] if new else None,
    }


# 1. Crash (process death) during the post-send metadata read, then restart.
def test_crash_during_post_send_read_then_restart_verifies_without_resend(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.gmail.rewrite_message_id = True
    original = FakeGmailTransport.get_message_metadata
    state = {"crash": True}

    def crashing(gid: str) -> MessageMeta:
        if state["crash"]:
            state["crash"] = False
            raise SimulatedCrash("process died during the post-send metadata read")
        return original(live.gmail, gid)

    monkeypatch.setattr(live.gmail, "get_message_metadata", crashing)
    camp, m1 = email1(live)
    with contextlib.suppress(SimulatedCrash):
        live.dispatch(m1)
    crashed_during_read = not state["crash"]
    assert live.message(m1).state == "sent"
    recover(live)
    recover(live)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("critic-1:", {"crashed_during_read": crashed_during_read, **follow})
    assert crashed_during_read, "no post-send metadata read happened at all"
    assert [a.status for a in live.attempts(m1)] == ["sent"]
    assert len(live.gmail.send_calls) == 2
    assert follow["in_reply_to"] == stored_id(live, m1)


# 2. A provider-observed value is now copied into outgoing headers: CRLF must not inject.
def test_observed_message_id_with_crlf_is_never_threaded(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeGmailTransport.get_message_metadata

    def hostile(gid: str) -> MessageMeta:
        meta = original(live.gmail, gid)
        headers = dict(meta.headers)
        headers["message-id"] = "<x@mail.gmail.test>\r\nBcc: evil@example.test"
        return MessageMeta(
            meta.gmail_message_id, meta.thread_id, meta.internal_date, meta.label_ids, headers
        )

    monkeypatch.setattr(live.gmail, "get_message_metadata", hostile)
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    recover(live)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("critic-2:", {k: v for k, v in follow.items() if k != "raw"})
    assert follow["sends"] == 0, "follow-up sent although the observed Message-ID is malformed"
    assert follow["bcc"] is None


# 3. Any exception class from the metadata read (incl. ones the send path treats specially)
#    after a successful send: outcome stays SENT, budget used once, follow-up held.
@pytest.mark.parametrize(
    "exc",
    [
        TransportUnavailable("read failed"),
        AmbiguousOutcome("read timed out"),
        DefiniteRejection(404, "notFound"),
        DefiniteRejection(401, "authError", auth_failure=True),
        KeyError("s00001"),
    ],
    ids=["unavailable", "ambiguous", "definite404", "definite401", "keyerror"],
)
def test_read_failure_class_never_changes_send_outcome_and_holds_followup(
    live: World, monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    reads: list[str] = []

    def broken(gid: str) -> MessageMeta:
        reads.append(gid)
        raise exc

    monkeypatch.setattr(live.gmail, "get_message_metadata", broken)
    camp, m1 = email1(live)
    result = live.dispatch(m1)
    with transaction(live.factory) as s:
        ledger = [r.status for r in s.scalars(select(BudgetLedger))]
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print(
        "critic-3:",
        type(exc).__name__,
        result,
        ledger,
        len(reads),
        {k: v for k, v in follow.items() if k != "raw"},
    )
    assert result.outcome is Outcome.SENT
    assert ledger == ["used"]
    assert [a.status for a in live.attempts(m1)] == ["sent"]
    assert len(reads) >= 1
    assert follow["sends"] == 0 and len(live.gmail.send_calls) == 1


# 4. The read happens strictly AFTER the success commit (DB already says sent + used).
def test_post_send_read_happens_after_success_commit(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeGmailTransport.get_message_metadata
    seen: list[tuple[str, list[str]]] = []

    def observing(gid: str) -> MessageMeta:
        with transaction(live.factory) as s:
            msg = s.scalar(select(Message).where(Message.gmail_message_id == gid))
            ledger = [r.status for r in s.scalars(select(BudgetLedger))]
            seen.append((msg.state if msg else "missing", ledger))
        return original(live.gmail, gid)

    monkeypatch.setattr(live.gmail, "get_message_metadata", observing)
    _camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    print("critic-4:", seen)
    assert seen == [("sent", ["used"])]


# 5. Claim-time recheck: earlier step loses verification while the follow-up's network
#    preflight runs (between the local verdict and the atomic claim).
def test_claim_recheck_blocks_followup_when_refs_become_unverified_mid_preflight(
    live: World,
) -> None:
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    fired = {"n": 0}

    def unverify() -> None:
        if fired["n"] == 0:
            fired["n"] += 1
            with transaction(live.factory) as s:
                s.get(Message, m1).rfc_message_id = None

    live.gmail.on_account = unverify
    before = len(live.gmail.send_calls)
    result = live.dispatch(m2)
    print("critic-5:", result, live.message(m2).state, fired)
    assert fired["n"] == 1, "preflight hook did not run"
    assert len(live.gmail.send_calls) == before
    assert result.outcome is Outcome.BLOCKED


# 6. Sandbox: the in-memory mailbox is lost on restart. A crash between the success commit
#    and the in-process verification leaves that practice sequence held, fail-closed, until
#    P2 persists the sandbox mailbox (accepted P0 limitation, DECISIONS note 31); sandbox
#    is never special-cased (spec §12). The campaign can still be closed.
def test_sandbox_restart_between_commit_and_verification_holds_followups_fail_closed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def crash(gid: str) -> MessageMeta:
        raise SimulatedCrash("died after commit, before verification")

    monkeypatch.setattr(world.sandbox, "get_message_metadata", crash)
    camp, m1 = email1(world, Mode.SANDBOX)
    with contextlib.suppress(SimulatedCrash):
        world.dispatch(m1)
    assert world.message(m1).state == "sent"
    assert len(world.attempts(m1)) == 1
    fresh = SandboxTransport(world.clock)  # restart: the in-memory mailbox is gone
    recover(world, sandbox=fresh)
    world.clock.advance(WEEK)
    mid = world.draft(camp, 2)
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "thread_headers_unverified" in info.value.reasons


# 7. Spec §19 test 65 control: the attempt's app Message-ID is never changed by verification.
def test_app_message_id_unchanged_by_verification(live: World) -> None:
    live.gmail.rewrite_message_id = True
    _camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    before = message_from_bytes(live.gmail.send_calls[0], policy=default_policy)["Message-ID"]
    assert live.attempts(m1)[0].app_message_id == before
    recover(live)
    after = live.attempts(m1)[0].app_message_id
    print("critic-7:", before == after, live.message(m1).rfc_message_id)
    assert before == after


# 8. Applicant-typed id that is the PI's inbound reply (not SENT) must not verify.
def test_confirmed_with_inbound_message_id_stays_unverified(live: World) -> None:
    from marcelorian.config.settings import load_settings
    from marcelorian.domain.enums import Actor
    from marcelorian.services import send_results

    camp, m1 = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok", "fail"]
    assert live.dispatch(m1).outcome is Outcome.UNCERTAIN
    inbound = live.gmail.inject(sender="pi@example.test", to="applicant@example.test")
    live.clock.advance(timedelta(hours=1))
    with transaction(live.factory) as s:
        attempt = send_results.attempt_by_uid(s, live.attempts(m1)[0].attempt_uid)
        send_results.resolve_confirmed_sent(
            s,
            attempt,
            sent_at=attempt.claimed_at,
            settings=load_settings(s),
            actor=Actor.APPLICANT,
            now=live.clock.now(),
            gmail_message_id=inbound.gmail_message_id,
            gmail_thread_id=inbound.thread_id,
        )
    recover(live)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("critic-8:", {k: v for k, v in follow.items() if k != "raw"})
    assert follow["sends"] == 0
