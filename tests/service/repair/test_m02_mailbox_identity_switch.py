"""M02 regression tests: old Gmail thread/history context must not move to a new connected account.

Owner brief (M02): allow reconnection of the same identity; block switching identity after
real-mode campaign/attempt/mailbox history exists; validate before mutating the active
account, stored credentials, approvals or cursors; a clean setup-only switch inherits no
cursor.

Every test asserts the DESIRED SAFE OUTCOME. Tests marked "guard" describe behaviour
that existed before the fix and must keep holding.

Synthetic data, fake transports and the injected clock only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalSnapshot,
    MailboxAccount,
    Message,
    RuntimeState,
    SyncState,
)
from marcelorian.domain.enums import Actor, MessageState, Mode
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.services import approvals, runtime
from marcelorian.services.gateway import Outcome
from marcelorian.services.sync import record_sync_success
from tests.factories import APPLICANT_EMAIL
from tests.fakes.gmail import FakeGmailTransport

OTHER = "different-applicant@example.test"
WEEK = timedelta(hours=168)
FOREIGN_CURSOR = "hist-A-1000"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _fingerprint(session, message_ids: list[int]) -> dict:
    """Everything a refused identity switch must leave untouched."""
    accounts = session.scalars(select(MailboxAccount).order_by(MailboxAccount.id)).all()
    sync = session.get(SyncState, "real")
    rt = session.get(RuntimeState, 1)
    messages = {}
    for mid in message_ids:
        message = session.get(Message, mid)
        snapshot = (
            session.get(ApprovalSnapshot, message.current_snapshot_id)
            if message.current_snapshot_id
            else None
        )
        messages[mid] = (message.state, snapshot.state if snapshot else None)
    return {
        "accounts": [(a.id, a.email, a.active, a.version, a.scopes_json) for a in accounts],
        "sync": (
            (sync.history_cursor, sync.last_gmail_coverage_at, sync.gap_open) if sync else None
        ),
        "auth": (rt.auth_blocked, rt.auth_block_reason),
        "messages": messages,
    }


def _attempt_connect(world, email: str, message_ids: list[int], scopes=REQUIRED_SCOPES):
    """Run ``connect_mailbox`` in one transaction.

    On an exception the session is inspected BEFORE rollback, so the test sees whether
    anything had already been mutated (validation must precede every mutation).
    Returns (exception or None, in-session fingerprint at the moment of refusal or None).
    """
    session = world.factory()
    session.begin()
    try:
        approvals.connect_mailbox(
            session, email, scopes, actor=Actor.APPLICANT, now=world.clock.now()
        )
    except Exception as exc:  # the fix's refusal type is not defined yet
        during = _fingerprint(session, message_ids)
        session.rollback()
        session.close()
        return exc, during
    session.commit()
    session.close()
    return None, None


def _state(world, message_ids: list[int]) -> dict:
    with transaction(world.factory) as s:
        return _fingerprint(s, message_ids)


def _record_cursor(world, cursor: str = FOREIGN_CURSOR) -> None:
    with transaction(world.factory) as s:
        now = world.clock.now()
        record_sync_success(s, "real", now, cursor, now)


def _history(world, kind: str) -> list[int]:
    """Create real-mode history of ``kind`` for mailbox A; return message ids to watch."""
    world.connect_mailbox()  # identity A
    if kind == "transport_test_self_send":
        me = world.allowlist(APPLICANT_EMAIL)
        world.set_mode(Mode.TRANSPORT_TEST)
        camp = world.campaign(
            world.pi(world.institution(), synthetic=True),
            Mode.TRANSPORT_TEST,
            test_recipient_id=me,
        )
        mid = world.draft_and_approve(camp)
        assert world.dispatch(mid).outcome is Outcome.SENT
        return [mid]
    world.set_mode(Mode.LIVE)
    camp = world.campaign(world.pi(world.institution()), Mode.LIVE)
    if kind == "live_candidate_only":
        return []
    mid = world.draft_and_approve(camp)
    if kind == "live_sent":
        assert world.dispatch(mid).outcome is Outcome.SENT
    elif kind == "live_uncertain":
        world.gmail.send_script = ["timeout_before_accept"]
        assert world.dispatch(mid).outcome is Outcome.UNCERTAIN
    elif kind == "live_approved_queued":
        pass
    else:  # pragma: no cover
        raise AssertionError(kind)
    return [mid]


# --------------------------------------------------------------------------- #
# 1. Switching identity after real-mode history is refused, before any mutation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind",
    [
        "live_sent",
        "live_uncertain",
        "live_approved_queued",
        "transport_test_self_send",
        # Literal reading of the brief ("real-mode campaign ... history"): a real-mode
        # campaign row alone binds the identity. Drop this case if the owner picks the
        # narrower "mailbox-tied records only" definition.
        "live_candidate_only",
    ],
)
def test_identity_switch_refused_after_real_history_and_nothing_mutated(world, kind) -> None:
    watched = _history(world, kind)
    world.clock.advance(timedelta(minutes=10))
    _record_cursor(world)
    with transaction(world.factory) as s:
        # a revoked token: the applicant is at the "Gmail needs reconnecting" prompt
        runtime.set_auth_block(s, True, "gmail_auth_failure", now=world.clock.now())
    before = _state(world, watched)

    exc, during = _attempt_connect(world, OTHER, watched)
    after = _state(world, watched)

    assert exc is not None, (
        "connect_mailbox accepted a different identity after real-mode history.\n"
        f"before: {before}\nafter:  {after}"
    )
    assert during == before, f"mutated before refusing: {during} (was {before})"
    assert after == before


# --------------------------------------------------------------------------- #
# 2. Wrong Google account at the reconnect prompt (gateway account-mismatch block)
# --------------------------------------------------------------------------- #


def test_wrong_account_at_reconnect_prompt_does_not_switch_or_unblock(world) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    sent = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    assert world.dispatch(sent).outcome is Outcome.SENT
    world.clock.advance(timedelta(minutes=10))
    queued = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    # The stored token now belongs to another Google account: the gateway blocks.
    world.gmail.account_email = OTHER
    assert world.dispatch(queued).reason == "auth_required"
    before = _state(world, [sent, queued])
    assert before["auth"] == (True, "account_or_scope_mismatch")

    # The applicant presses "reconnect" and signs in with that other account again.
    exc, _during = _attempt_connect(world, OTHER, [sent, queued])
    after = _state(world, [sent, queued])

    assert exc is not None, f"identity silently switched at reconnect: {after}"
    assert after["auth"] == (True, "account_or_scope_mismatch")
    assert after["accounts"] == before["accounts"]
    assert after["messages"][queued] == (MessageState.APPROVED_QUEUED.value, "active")

    # Whatever happens next, nothing is submitted from the other account. (Before the fix the
    # approval was invalidated; re-approving then showed the downstream send.)
    if world.message_state(queued) is MessageState.NEEDS_REAPPROVAL:
        world.approve(queued)
    world.clock.advance(timedelta(minutes=10))
    result = world.dispatch(queued)
    assert result.outcome is not Outcome.SENT, "Email 1 submitted from the other account"


# --------------------------------------------------------------------------- #
# 3. End to end: a follow-up after a switch ignores the reply in the old mailbox
# --------------------------------------------------------------------------- #


def test_followup_after_switch_cannot_ignore_reply_in_old_mailbox(world) -> None:
    pi_address = "pi-m02@synthetic-univ.test"
    world.connect_mailbox()  # identity A
    world.set_mode(Mode.LIVE)
    camp = world.campaign(world.pi(world.institution(), email=pi_address), Mode.LIVE)
    m1 = world.draft_and_approve(camp)
    assert world.dispatch(m1).outcome is Outcome.SENT
    mailbox_a = world.gmail
    first = world.message(m1)
    # The PI replies in the campaign thread, in mailbox A (not synced yet).
    mailbox_a.inject(
        sender=pi_address,
        to=APPLICANT_EMAIL,
        thread_id=first.gmail_thread_id,
        when=world.clock.now() + timedelta(days=1),
        headers={"In-Reply-To": first.rfc_message_id},
    )

    # The applicant connects a different Google account B (new token, empty mailbox).
    mailbox_b = FakeGmailTransport(clock=world.clock, account_email=OTHER)
    world.gmail = mailbox_b
    world.rebuild_gateway()
    exc, _ = _attempt_connect(world, OTHER, [m1])

    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(camp, 2)
    snapshot_sender = None
    with transaction(world.factory) as s:
        snap = s.get(ApprovalSnapshot, s.get(Message, m2).current_snapshot_id)
        snapshot_sender = snap.sender
    result = world.worker_cycle(m2)

    assert mailbox_b.send_calls == [], (
        f"Email 2 submitted from {snapshot_sender} into thread {first.gmail_thread_id!r} "
        f"of the old mailbox while the PI's reply sits there (switch refused: {exc!r}; "
        f"dispatch: {result})"
    )
    assert len(mailbox_a.send_calls) == 1


# --------------------------------------------------------------------------- #
# 4. Setup-only switch (no real-mode history): allowed, inherits no cursor/coverage
# --------------------------------------------------------------------------- #


def test_setup_only_switch_starts_clean_without_inherited_cursor(world) -> None:
    world.connect_mailbox()  # identity A, used only for sandbox practice so far
    sandbox = world.draft_and_approve(world.campaign(world.pi(world.institution())))
    assert world.dispatch(sandbox).outcome is Outcome.SENT  # sandbox history never binds
    _record_cursor(world)
    with transaction(world.factory) as s:
        runtime.set_auth_block(s, True, "gmail_auth_failure", now=world.clock.now())

    exc, _ = _attempt_connect(world, OTHER, [])
    after = _state(world, [])

    assert exc is None, f"setup-only switch refused: {exc!r}"
    active = [a for a in after["accounts"] if a[2]]
    assert [a[1] for a in active] == [OTHER]
    assert after["auth"] == (False, None)
    cursor, coverage, gap = after["sync"] if after["sync"] else (None, None, False)
    assert cursor is None, f"new account inherited the old history cursor {cursor!r}"
    assert coverage is None, f"new account inherited the old coverage time {coverage!r}"
    assert gap is False


# --------------------------------------------------------------------------- #
# Guards (expected to pass now and after the fix)
# --------------------------------------------------------------------------- #


def test_guard_same_identity_reconnect_after_history_keeps_everything(world) -> None:
    watched = _history(world, "live_sent")
    world.clock.advance(timedelta(minutes=10))
    queued = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    watched.append(queued)
    _record_cursor(world)
    with transaction(world.factory) as s:
        runtime.set_auth_block(s, True, "gmail_auth_failure", now=world.clock.now())
    before = _state(world, watched)

    exc, _ = _attempt_connect(world, "  Applicant@Example.TEST ", watched)
    after = _state(world, watched)

    assert exc is None
    assert [(a[0], a[1], a[2], a[3]) for a in after["accounts"]] == [
        (a[0], a[1], a[2], a[3]) for a in before["accounts"]
    ]
    assert after["sync"] == before["sync"]
    assert after["messages"] == before["messages"]
    assert after["auth"] == (False, None)


def test_guard_first_connection_with_real_campaign_and_no_mailbox_is_allowed(world) -> None:
    world.campaign(world.pi(world.institution()), Mode.LIVE)  # created before connecting
    exc, _ = _attempt_connect(world, APPLICANT_EMAIL, [])
    assert exc is None
    assert [a[1] for a in _state(world, [])["accounts"] if a[2]] == [APPLICANT_EMAIL]
