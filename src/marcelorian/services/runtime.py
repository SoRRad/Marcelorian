"""Active mode, persistent pause and the global auth block (spec §9.4, §12; B7, B9).

* Exactly one mode is active for dispatch and follow-up generation.
* Switching mode needs explicit confirmation, pauses dispatch (an intentional
  pause, not a missed-window event) and never changes any stored message's
  mode. Dispatch resumes only when explicitly resumed.
* The persistent pause survives restarts because it lives in the database.
* Every change is logged in ``runtime_state_log`` so the missed-window rule can
  tell deliberate unavailability from worker/connectivity failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from marcelorian.db.models import RuntimeState, RuntimeStateLog
from marcelorian.domain.enums import Actor, Mode, RuntimePauseReason
from marcelorian.services.audit import audit


class ModeSwitchError(RuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeView:
    active_mode: Mode
    dispatch_paused: bool
    pause_reason: str | None
    live_readiness_complete: bool
    auth_blocked: bool
    auth_block_reason: str | None


def _row(session: Session, now: datetime) -> RuntimeState:
    row = session.get(RuntimeState, 1)
    if row is None:
        # First run: sandbox is the default mode (spec §16, §21).
        row = RuntimeState(
            id=1,
            active_mode=Mode.SANDBOX.value,
            dispatch_paused=False,
            pause_reason=None,
            live_readiness_complete=False,
            auth_blocked=False,
            updated_at=now,
        )
        session.add(row)
        session.add(
            RuntimeStateLog(
                at=now, active_mode=row.active_mode, dispatch_paused=False, reason="initialized"
            )
        )
        session.flush()
    return row


def get_runtime(session: Session, now: datetime) -> RuntimeView:
    row = _row(session, now)
    return RuntimeView(
        active_mode=Mode(row.active_mode),
        dispatch_paused=row.dispatch_paused,
        pause_reason=row.pause_reason,
        live_readiness_complete=row.live_readiness_complete,
        auth_blocked=row.auth_blocked,
        auth_block_reason=row.auth_block_reason,
    )


def _log(session: Session, row: RuntimeState, now: datetime, reason: str) -> None:
    session.add(
        RuntimeStateLog(
            at=now, active_mode=row.active_mode, dispatch_paused=row.dispatch_paused, reason=reason
        )
    )


def switch_mode(
    session: Session, new_mode: Mode, *, confirmed: bool, actor: Actor, now: datetime
) -> RuntimeView:
    if not confirmed:
        raise ModeSwitchError("switching mode requires explicit confirmation")
    row = _row(session, now)
    if new_mode is Mode.LIVE and not row.live_readiness_complete:
        raise ModeSwitchError("live mode requires the completed live readiness checklist")
    old = row.active_mode
    row.active_mode = new_mode.value
    row.dispatch_paused = True
    row.pause_reason = RuntimePauseReason.MODE_SWITCH.value
    row.updated_at = now
    _log(session, row, now, f"mode_switch:{old}->{new_mode.value}")
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="runtime",
        entity_id=1,
        action="mode_switched",
        from_state=old,
        to_state=new_mode.value,
        reason="dispatch paused until explicitly resumed",
    )
    return get_runtime(session, now)


def pause_dispatch(
    session: Session,
    *,
    actor: Actor,
    now: datetime,
    reason: RuntimePauseReason = RuntimePauseReason.APPLICANT,
) -> RuntimeView:
    row = _row(session, now)
    row.dispatch_paused = True
    row.pause_reason = reason.value
    row.updated_at = now
    _log(session, row, now, f"pause:{reason.value}")
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="runtime",
        entity_id=1,
        action="paused",
        reason=reason.value,
    )
    return get_runtime(session, now)


def resume_dispatch(session: Session, *, actor: Actor, now: datetime) -> RuntimeView:
    row = _row(session, now)
    row.dispatch_paused = False
    row.pause_reason = None
    row.updated_at = now
    _log(session, row, now, "resume")
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="runtime",
        entity_id=1,
        action="resumed",
        detail={"mode": row.active_mode},
    )
    return get_runtime(session, now)


def set_live_readiness(session: Session, complete: bool, *, actor: Actor, now: datetime) -> None:
    """Recorded by the live readiness checklist (P5/P6 UI); P0 stores the flag only.

    Live mode never stays active without readiness: withdrawing it while live is
    active (e.g. a setup-only mailbox switch, M02) moves to sandbox with dispatch
    paused, exactly as an explicit mode switch would.
    """
    row = _row(session, now)
    row.live_readiness_complete = complete
    row.updated_at = now
    audit(
        session,
        at=now,
        actor=actor,
        entity_type="runtime",
        entity_id=1,
        action="live_readiness",
        to_state=str(complete),
    )
    if not complete and row.active_mode == Mode.LIVE.value:
        row.active_mode = Mode.SANDBOX.value
        row.dispatch_paused = True
        row.pause_reason = RuntimePauseReason.MODE_SWITCH.value
        _log(session, row, now, f"mode_switch:{Mode.LIVE.value}->{Mode.SANDBOX.value}")
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="runtime",
            entity_id=1,
            action="mode_switched",
            from_state=Mode.LIVE.value,
            to_state=Mode.SANDBOX.value,
            reason="live readiness withdrawn; dispatch paused until explicitly resumed",
        )


def set_auth_block(session: Session, blocked: bool, reason: str | None, *, now: datetime) -> None:
    """Gmail auth failure / scope or account mismatch halts all real dispatch (spec §15)."""
    row = _row(session, now)
    if row.auth_blocked == blocked and row.auth_block_reason == reason:
        return
    row.auth_blocked = blocked
    row.auth_block_reason = reason if blocked else None
    row.updated_at = now
    audit(
        session,
        at=now,
        actor=Actor.SYSTEM,
        entity_type="runtime",
        entity_id=1,
        action="auth_block" if blocked else "auth_unblock",
        reason=reason,
    )
