"""M02 additional regression cases. Each asserts the desired safe outcome."""

from __future__ import annotations

from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import InboundEvent, MailboxAccount, RuntimeState, SyncState
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.gmail_params import REQUIRED_SCOPES
from marcelorian.services import approvals, runtime
from marcelorian.services.sync import record_sync_success
from tests.factories import APPLICANT_EMAIL

OTHER = "different-applicant@example.test"


def _connect(world, email, scopes=REQUIRED_SCOPES):
    try:
        with transaction(world.factory) as s:
            approvals.connect_mailbox(
                s, email, scopes, actor=Actor.APPLICANT, now=world.clock.now()
            )
    except Exception as exc:  # refusal type not defined yet
        return exc
    return None


def _snap(world):
    with transaction(world.factory) as s:
        rt = s.get(RuntimeState, 1)
        sync = s.get(SyncState, "real")
        return (
            [(a.id, a.email, a.active, a.version) for a in s.scalars(select(MailboxAccount))],
            (rt.auth_blocked, rt.auth_block_reason, rt.live_readiness_complete),
            (sync.history_cursor, sync.last_gmail_coverage_at) if sync else None,
        )


def test_unmatched_real_inbound_event_alone_binds_identity(world) -> None:
    """A real-mailbox InboundEvent with no campaign (campaign_id NULL) is mailbox history."""
    world.connect_mailbox()
    with transaction(world.factory) as s:
        s.add(
            InboundEvent(
                mode=Mode.LIVE.value,
                campaign_id=None,
                gmail_message_id="gm-A-1",
                gmail_thread_id="th-A-1",
                from_address="someone@synthetic-univ.test",
                classification="unmatched",
                recorded_at=world.clock.now(),
            )
        )
    before = _snap(world)
    exc = _connect(world, OTHER)
    assert exc is not None, f"switch accepted despite real inbound history: {_snap(world)}"
    assert _snap(world) == before


def test_refused_switch_with_missing_scopes_keeps_original_block_reason(world) -> None:
    """Refusal must precede the note-17 scope branch (no 'required_scopes_missing' overwrite)."""
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    world.campaign(world.pi(world.institution()), Mode.LIVE)
    with transaction(world.factory) as s:
        runtime.set_auth_block(s, True, "gmail_auth_failure", now=world.clock.now())
    exc = _connect(world, OTHER, frozenset())
    accounts, auth, _ = _snap(world)
    assert exc is not None
    assert auth[:2] == (True, "gmail_auth_failure")
    assert [a[1] for a in accounts if a[2]] == [APPLICANT_EMAIL]


def test_setup_only_switch_clears_live_readiness_and_cursor(world) -> None:
    """Readiness evidence ('Gmail reconnect tested') belonged to the old account."""
    world.connect_mailbox()
    with transaction(world.factory) as s:
        runtime.set_live_readiness(s, True, actor=Actor.APPLICANT, now=world.clock.now())
        record_sync_success(s, "real", world.clock.now(), "hist-A-1", world.clock.now())
    exc = _connect(world, OTHER)
    _, auth, sync = _snap(world)
    assert exc is None
    assert sync in (None, (None, None)), f"inherited sync row {sync}"
    assert auth[2] is False, "live readiness carried over to a different Gmail account"


def test_setup_switch_round_trip_then_history_binds_latest(world) -> None:
    """A -> B (setup) -> history under B -> A refused; B reconnect keeps row/version."""
    world.connect_mailbox()
    assert _connect(world, OTHER) is None
    world.set_mode(Mode.LIVE)
    world.campaign(world.pi(world.institution()), Mode.LIVE)
    accounts_before, _, _ = _snap(world)
    assert _connect(world, APPLICANT_EMAIL) is not None, "switch back to A accepted"
    assert _connect(world, "  " + OTHER.upper()) is None
    accounts_after, _, _ = _snap(world)
    assert accounts_after == accounts_before
