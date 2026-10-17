"""M06 regression tests: provider-observed RFC Message-ID and follow-up readiness.

Fake transport (``FakeGmailTransport`` with
``rewrite_message_id``), synthetic data and the injected clock only. Nothing here
exercises live Gmail: whether Gmail keeps our Message-ID remains a P0.3 checklist item.

Each test asserts the DESIRED SAFE OUTCOME:

* A submission the provider accepted is recorded ``sent`` exactly once and is never
  retried, whatever happens to a later metadata read.
* The Message-ID kept for threading (``message.rfc_message_id``, which feeds the next
  snapshot's ``In-Reply-To``/``References``) is the one the provider actually holds,
  read back with metadata, not merely the one the app generated.
* Until the thread/header data of the sent step is verified, the next step is not
  submitted (follow-up readiness is held); no follow-up threads on unverified headers.

Tests ``c``, ``i`` and ``j`` are controls: behaviour that held before the fix and must
keep holding.
"""

from __future__ import annotations

import contextlib
from datetime import timedelta
from email import message_from_bytes
from email.policy import default as default_policy

import pytest
from sqlalchemy import select

from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import BudgetLedger
from marcelorian.domain.enums import Actor, Mode
from marcelorian.services import reconciliation, send_results
from marcelorian.services.approvals import ApprovalError
from marcelorian.services.gateway import Outcome
from marcelorian.transport.base import SearchResult, TransportUnavailable
from tests.factories import World
from tests.fakes.gmail import FakeGmailTransport

WEEK = timedelta(hours=168)


@pytest.fixture
def live(world: World) -> World:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


def email1(world: World, mode: Mode = Mode.LIVE) -> tuple[int, int]:
    camp = world.campaign(world.pi(world.institution()), mode)
    return camp, world.draft_and_approve(camp)


def parsed(raw: bytes):
    return message_from_bytes(raw, policy=default_policy)


def provider_id(world: World, gmail_message_id: str | None) -> str | None:
    """The Message-ID header as the (fake) provider stores it for that Gmail message."""
    assert gmail_message_id is not None
    return world.gmail.store[gmail_message_id].meta.header("message-id")


def recover(world: World) -> dict[str, object]:
    return reconciliation.startup_recovery(
        world.factory,
        {Mode.LIVE: world.gmail, Mode.TRANSPORT_TEST: world.gmail, Mode.SANDBOX: world.sandbox},
        {"real": world.real_sync},
        world.clock,
    )


def try_followup(world: World, camp: int, step: int) -> dict[str, object]:
    """Draft, approve and dispatch the next step; report what reached the transport."""
    before = len(world.gmail.send_calls)
    mid = world.draft(camp, step)
    approved, reasons, outcome = True, None, None
    try:
        world.approve(mid)
    except ApprovalError as exc:
        approved, reasons = False, exc.reasons
    if approved:
        outcome = world.dispatch(mid).outcome.value
    new_calls = world.gmail.send_calls[before:]
    mime = parsed(new_calls[-1]) if new_calls else None
    return {
        "mid": mid,
        "approved": approved,
        "approval_reasons": reasons,
        "dispatch_outcome": outcome,
        "followup_send_calls": len(new_calls),
        "in_reply_to": mime["In-Reply-To"] if mime else None,
        "references": mime["References"] if mime else None,
    }


def fail_metadata(world: World, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every metadata read fails (the send itself still succeeds)."""
    calls: list[str] = []

    def broken(gmail_message_id: str):
        calls.append(gmail_message_id)
        raise TransportUnavailable("metadata read failed")

    monkeypatch.setattr(world.gmail, "get_message_metadata", broken)
    return calls


# --------------------------------------------------------------------------------------
# a. Gateway success with a rewritten Message-ID
# --------------------------------------------------------------------------------------
def test_a_success_with_rewritten_id_stores_provider_observed_id(live: World) -> None:
    live.gmail.rewrite_message_id = True
    _camp, mid = email1(live)
    result = live.dispatch(mid)
    message = live.message(mid)
    attempt = live.attempts(mid)[0]
    stored = provider_id(live, message.gmail_message_id)
    observed = {
        "outcome": result.outcome.value,
        "send_calls": len(live.gmail.send_calls),
        "message_state": message.state,
        "fake_rewrote_id": stored != attempt.app_message_id,
        "metadata_read_back_after_send": message.gmail_message_id in live.gmail.metadata_requests,
        "threading_id_is_provider_observed": message.rfc_message_id == stored,
    }
    print(
        "M06-a observed:",
        observed,
        "| stored rfc_message_id is app id:",
        message.rfc_message_id == attempt.app_message_id,
    )
    assert observed == {
        "outcome": "sent",
        "send_calls": 1,
        "message_state": "sent",
        "fake_rewrote_id": True,
        "metadata_read_back_after_send": True,
        "threading_id_is_provider_observed": True,
    }


# --------------------------------------------------------------------------------------
# b. The follow-up must reference the Message-ID the provider actually holds
# --------------------------------------------------------------------------------------
def test_b_followup_references_provider_observed_id_after_rewrite(live: World) -> None:
    live.gmail.rewrite_message_id = True
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    first = live.message(m1)
    stored = provider_id(live, first.gmail_message_id)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print(
        "M06-b observed:",
        follow,
        "| provider id:",
        stored,
        "| app id:",
        live.attempts(m1)[0].app_message_id,
    )
    assert follow["followup_send_calls"] == 1
    assert follow["in_reply_to"] == stored
    assert follow["references"] == stored


# --------------------------------------------------------------------------------------
# c. CONTROL: a failed metadata read after success never causes a resend
# --------------------------------------------------------------------------------------
def test_c_metadata_failure_after_success_is_never_retried(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_metadata(live, monkeypatch)
    _camp, mid = email1(live)
    result = live.dispatch(mid)
    recover(live)
    again = live.dispatch(mid)
    live.clock.advance(timedelta(minutes=30))
    recover(live)
    with transaction(live.factory) as s:
        ledger = [(r.status) for r in s.scalars(select(BudgetLedger))]
    observed = {
        "outcome": result.outcome.value,
        "second_dispatch": again.outcome.value,
        "send_calls": len(live.gmail.send_calls),
        "attempt_statuses": [a.status for a in live.attempts(mid)],
        "message_state": live.message(mid).state,
        "budget": ledger,
    }
    print("M06-c observed:", observed)
    assert observed == {
        "outcome": "sent",
        "second_dispatch": "not_eligible",
        "send_calls": 1,
        "attempt_statuses": ["sent"],
        "message_state": "sent",
        "budget": ["used"],
    }


# --------------------------------------------------------------------------------------
# d. Failed metadata read after success: follow-up readiness is held
# --------------------------------------------------------------------------------------
def test_d_metadata_failure_holds_followup_readiness(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = fail_metadata(live, monkeypatch)
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print(
        "M06-d observed:",
        follow,
        "| metadata reads attempted:",
        len(reads),
        "| email1 rfc_message_id is app id:",
        live.message(m1).rfc_message_id == live.attempts(m1)[0].app_message_id,
    )
    assert len(live.gmail.send_calls) - follow["followup_send_calls"] == 1  # Email 1 once
    assert follow["followup_send_calls"] == 0, "follow-up threaded on unverified headers"
    assert live.message(follow["mid"]).state != "sent"


# --------------------------------------------------------------------------------------
# e. Once metadata becomes readable, a verification pass unblocks with the observed id
# --------------------------------------------------------------------------------------
def test_e_verification_after_metadata_recovers_uses_observed_id(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    live.gmail.rewrite_message_id = True
    original = FakeGmailTransport.get_message_metadata
    broken = {"on": True}

    def flaky(gmail_message_id: str):
        if broken["on"]:
            raise TransportUnavailable("metadata read failed")
        return original(live.gmail, gmail_message_id)

    monkeypatch.setattr(live.gmail, "get_message_metadata", flaky)
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    stored = provider_id(live, live.message(m1).gmail_message_id)
    broken["on"] = False
    recover(live)  # startup recovery (the designed home of the pending-verification pass)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("M06-e observed:", follow, "| provider id:", stored)
    assert len(live.gmail.send_calls) == 2  # Email 1 once, the follow-up once
    assert follow["in_reply_to"] == stored


# --------------------------------------------------------------------------------------
# f. Reconciled send corroborated by X-Marcelorian-Id while Message-ID was rewritten
# --------------------------------------------------------------------------------------
def test_f_reconciled_send_with_rewritten_id_keeps_provider_observed_id(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, m1 = email1(live)
    live.gmail.rewrite_message_id = True
    live.gmail.send_script = ["server_error_after_accept"]
    original = live.gmail.search_message_ids

    def search(query: str, *, include_spam_trash: bool) -> SearchResult:
        if query.startswith("rfc822msgid:"):  # same emulation as test_recovery.py
            return SearchResult(
                ids=tuple(m.meta.gmail_message_id for m in live.gmail.sent_messages()),
                complete=True,
            )
        return original(query, include_spam_trash=include_spam_trash)

    monkeypatch.setattr(live.gmail, "search_message_ids", search)
    result = live.dispatch(m1)
    first = live.message(m1)
    stored = provider_id(live, first.gmail_message_id)
    live.gmail.rewrite_message_id = False
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    observed = {
        "outcome": result.outcome.value,
        "reason": result.reason,
        "email1_send_calls": len(live.gmail.send_calls) - follow["followup_send_calls"],
        "threading_id_is_provider_observed": first.rfc_message_id == stored,
        "followup_in_reply_to_is_provider_observed": follow["in_reply_to"] == stored,
    }
    print("M06-f observed:", observed, follow)
    assert observed == {
        "outcome": "sent",
        "reason": "reconciled:sent",
        "email1_send_calls": 1,
        "threading_id_is_provider_observed": True,
        "followup_in_reply_to_is_provider_observed": True,
    }


# --------------------------------------------------------------------------------------
# g. Applicant-confirmed send: typed Gmail IDs are unverified thread data
# --------------------------------------------------------------------------------------
def test_g_applicant_confirmed_send_holds_followup_until_verified(live: World) -> None:
    live.gmail.rewrite_message_id = True
    camp, m1 = email1(live)
    live.gmail.send_script = ["timeout_after_accept"]
    live.gmail.search_script = ["ok", "fail"]  # prior-contact search ok, reconciliation fails
    assert live.dispatch(m1).outcome is Outcome.UNCERTAIN
    sent_copy = live.gmail.sent_messages()[0].meta
    stored = sent_copy.header("message-id")
    live.clock.advance(timedelta(hours=1))
    with transaction(live.factory) as s:
        attempt = send_results.attempt_by_uid(s, live.attempts(m1)[0].attempt_uid)
        send_results.resolve_confirmed_sent(
            s,
            attempt,
            sent_at=live.clock.now() - timedelta(minutes=50),
            settings=load_settings(s),
            actor=Actor.APPLICANT,
            now=live.clock.now(),
            gmail_message_id=sent_copy.gmail_message_id,
            gmail_thread_id=sent_copy.thread_id,
        )
    live.clock.advance(WEEK)
    held = try_followup(live, camp, 2)
    print("M06-g before any verification pass:", held, "| provider id:", stored)
    assert held["followup_send_calls"] == 0, "follow-up sent on applicant-typed, unverified IDs"
    recover(live)  # verification pass reads the metadata of the confirmed Gmail message
    mid = held["mid"]
    with contextlib.suppress(ApprovalError):
        live.approve(mid)
    before = len(live.gmail.send_calls)
    live.dispatch(mid)
    new = live.gmail.send_calls[before:]
    print("M06-g after verification pass: new sends", len(new))
    assert len(new) == 1 and parsed(new[0])["In-Reply-To"] == stored


# --------------------------------------------------------------------------------------
# h. Follow-up placed in a different thread than requested: the next step is held
# --------------------------------------------------------------------------------------
def test_h_followup_thread_mismatch_holds_next_step(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.clock.advance(WEEK)
    real_send = FakeGmailTransport.send

    def ignores_thread(raw: bytes, thread_id: str | None):
        return real_send(live.gmail, raw, None)  # provider started a new conversation

    monkeypatch.setattr(live.gmail, "send", ignores_thread)
    step2 = try_followup(live, camp, 2)
    assert step2["followup_send_calls"] == 1
    m2 = live.message(step2["mid"])
    first = live.message(m1)
    monkeypatch.setattr(live.gmail, "send", lambda raw, tid: real_send(live.gmail, raw, tid))
    live.clock.advance(WEEK)
    step3 = try_followup(live, camp, 3)
    observed = {
        "step2_state": m2.state,
        "step2_thread_differs": m2.gmail_thread_id != first.gmail_thread_id,
        "step3_send_calls": step3["followup_send_calls"],
    }
    print("M06-h observed:", observed, step3)
    assert observed == {
        "step2_state": "sent",
        "step2_thread_differs": True,
        "step3_send_calls": 0,
    }


# --------------------------------------------------------------------------------------
# i, j. CONTROLS: preserved ids (fake Gmail) and sandbox keep working
# --------------------------------------------------------------------------------------
def test_i_control_preserved_ids_followup_threads_normally(live: World) -> None:
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    stored = provider_id(live, live.message(m1).gmail_message_id)
    assert stored == live.attempts(m1)[0].app_message_id
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("M06-i observed:", follow)
    assert follow["followup_send_calls"] == 1 and follow["in_reply_to"] == stored


def test_j_control_sandbox_followup_unaffected(world: World) -> None:
    camp, m1 = email1(world, Mode.SANDBOX)
    assert world.dispatch(m1).outcome is Outcome.SENT
    first = world.message(m1)
    stored = world.sandbox.messages[first.gmail_message_id].header("message-id")
    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(camp, 2)
    assert world.dispatch(m2).outcome is Outcome.SENT
    second = world.sandbox.messages[world.message(m2).gmail_message_id]
    print("M06-j sandbox in-reply-to matches:", second.header("in-reply-to") == stored)
    assert second.header("in-reply-to") == stored


# --------------------------------------------------------------------------------------
# k-n. Boundary / negative cases for the fix design
# --------------------------------------------------------------------------------------
def test_k_metadata_without_message_id_header_holds_followup(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = FakeGmailTransport.get_message_metadata

    def headerless(gmail_message_id: str):
        meta = original(live.gmail, gmail_message_id)
        headers = {k: v for k, v in meta.headers.items() if k != "message-id"}
        return type(meta)(
            meta.gmail_message_id, meta.thread_id, meta.internal_date, meta.label_ids, headers
        )

    monkeypatch.setattr(live.gmail, "get_message_metadata", headerless)
    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    recover(live)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("M06-k observed:", follow)
    assert len(live.gmail.send_calls) == 1 and follow["followup_send_calls"] == 0


def test_l_applicant_typed_unknown_gmail_id_holds_followup(live: World) -> None:
    camp, m1 = email1(live)
    live.gmail.send_script = ["timeout_before_accept"]
    live.gmail.search_script = ["ok", "fail"]
    assert live.dispatch(m1).outcome is Outcome.UNCERTAIN
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
            gmail_message_id="typo-gmail-id",
            gmail_thread_id="typo-thread-id",
        )
    recover(live)  # the metadata read for the typed id fails (unknown id)
    live.clock.advance(WEEK)
    follow = try_followup(live, camp, 2)
    print("M06-l observed:", follow)
    assert follow["followup_send_calls"] == 0, "follow-up threaded on typed, unverified IDs"


def test_m_gateway_refuses_followup_snapshot_with_unverified_references(live: World) -> None:
    """Defense in depth: a queued follow-up whose earlier step lost verification never sends."""
    from marcelorian.db.models import Message

    camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    live.clock.advance(WEEK)
    m2 = live.draft_and_approve(camp, 2)
    with transaction(live.factory) as s:  # emulate a legacy/unverified row after approval
        s.get(Message, m1).rfc_message_id = None
    before = len(live.gmail.send_calls)
    result = live.dispatch(m2)
    print("M06-m observed:", result, live.message_state(m2))
    assert len(live.gmail.send_calls) == before
    assert result.outcome in (Outcome.BLOCKED, Outcome.DEFERRED)


def test_n_post_send_metadata_read_holds_no_db_transaction(
    live: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from marcelorian.config.settings import update_settings

    original = FakeGmailTransport.get_message_metadata
    seen: list[str] = []

    def writes_during_read(gmail_message_id: str):
        # A concurrent writer must be able to commit while the read is in progress.
        with transaction(live.factory) as s:
            update_settings(
                s,
                {"pacing_min_seconds": 120},
                actor=Actor.APPLICANT,
                now=live.clock.now(),
                confirm_less_conservative=True,
            )
        seen.append(gmail_message_id)
        return original(live.gmail, gmail_message_id)

    monkeypatch.setattr(live.gmail, "get_message_metadata", writes_during_read)
    _camp, m1 = email1(live)
    assert live.dispatch(m1).outcome is Outcome.SENT
    print("M06-n metadata reads after send:", seen)
    assert seen == [live.message(m1).gmail_message_id]
