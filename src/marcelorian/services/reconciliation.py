"""Reconciliation of ambiguous and crashed attempts (spec §9.6 steps 6-7; decisions B12, B13).

* Search Sent by the attempt's own ``rfc822msgid`` (never regenerated) and
  corroborate by SENT label, Message-ID or ``X-Marcelorian-Id``, recipient,
  time and thread.
* Exactly one corroborated match -> ``sent`` with Gmail's internal date as the
  established submission time (no invented timestamps: a match without a date
  stays uncertain for review).
* Not found, unmatched results, or a failed/incomplete search -> ``uncertain``
  with budget and reservations held. Nothing here ever submits again.
* Startup recovery order (spec §9.4): reconcile in-flight attempts, complete
  the sync, apply the missed-window rule, then dispatch may resume.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import ApprovalSnapshot, Campaign, Message, SendAttempt
from marcelorian.domain.enums import (
    UNRESOLVED_ATTEMPT_STATES,
    Actor,
    AttemptStatus,
    Mode,
    PauseReason,
    SentAtProvenance,
)
from marcelorian.domain.gmail_params import sent_search_query
from marcelorian.domain.identifiers import bare_message_id
from marcelorian.domain.snapshot import verify_canonical
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import scheduling, send_results
from marcelorian.services import sync as sync_svc
from marcelorian.transport.base import MailTransport, MessageMeta

log = logging.getLogger(__name__)
CLOCK_SKEW = timedelta(minutes=10)


def corroborates(
    meta: MessageMeta, attempt: SendAttempt, expected_thread: str | None, now: datetime
) -> bool:
    if "SENT" not in meta.label_ids:
        return False
    header_id = meta.header("message-id")
    header_x = meta.header("x-marcelorian-id")
    id_match = (
        header_id is not None
        and bare_message_id(header_id) == bare_message_id(attempt.app_message_id)
    ) or (header_x is not None and header_x.strip() == attempt.x_marcelorian_id)
    if not id_match:
        return False
    recipients = {addr.lower() for _name, addr in getaddresses([meta.header("to") or ""])}
    if attempt.recipient.lower() not in recipients:
        return False
    if meta.internal_date is None:
        return True  # identity matches; the timestamp question is handled by the caller
    if not attempt.claimed_at - CLOCK_SKEW <= meta.internal_date <= now + CLOCK_SKEW:
        return False
    return expected_thread is None or meta.thread_id == expected_thread


def reconcile_attempt(
    session_factory: sessionmaker[Session],
    transport: MailTransport,
    clock: Clock,
    attempt_uid: str,
    *,
    actor: Actor = Actor.RECONCILER,
    detail: str = "reconciliation",
) -> str:
    """Returns 'sent', 'uncertain' or 'noop'. Never submits."""
    with transaction(session_factory) as session:
        attempt = send_results.attempt_by_uid(session, attempt_uid)
        if AttemptStatus(attempt.status) not in UNRESOLVED_ATTEMPT_STATES:
            return "noop"
        snapshot = session.get(ApprovalSnapshot, attempt.snapshot_id)
        assert snapshot is not None
        expected_thread = verify_canonical(snapshot.canonical_json, snapshot.sha256).thread_id
        app_message_id = attempt.app_message_id
    metas: list[MessageMeta] = []
    search_ok = True
    try:
        search = transport.search_message_ids(
            sent_search_query(app_message_id), include_spam_trash=True
        )
        search_ok = search.complete
        metas = [transport.get_message_metadata(gid) for gid in search.ids]
    except Exception as exc:
        log.warning("reconciliation search failed for %s: %s", attempt_uid, type(exc).__name__)
        search_ok = False
    with transaction(session_factory) as session:
        now = clock.now()
        attempt = send_results.attempt_by_uid(session, attempt_uid)
        if AttemptStatus(attempt.status) not in UNRESOLVED_ATTEMPT_STATES:
            return "noop"
        attempt.reconcile_count += 1
        attempt.last_reconciled_at = now
        matches = [m for m in metas if corroborates(m, attempt, expected_thread, now)]
        if not search_ok and not matches:
            send_results.mark_uncertain(
                session, attempt, detail=f"{detail}; search failed", actor=actor, now=now
            )
            return "uncertain"
        if not matches:
            reason = "unmatched results" if metas else "not found"
            send_results.mark_uncertain(
                session, attempt, detail=f"{detail}; {reason}", actor=actor, now=now
            )
            return "uncertain"
        dated = sorted(
            (m for m in matches if m.internal_date is not None),
            key=lambda m: m.internal_date or now,
        )
        if not dated:
            send_results.mark_uncertain(
                session,
                attempt,
                detail=f"{detail}; found but no reliable send time",
                actor=actor,
                now=now,
            )
            return "uncertain"
        first = dated[0]
        assert first.internal_date is not None
        send_results.finalize_sent(
            session,
            attempt,
            sent_at=first.internal_date,
            provenance=SentAtProvenance.GMAIL_RECONCILIATION,
            gmail_message_id=first.gmail_message_id,
            gmail_thread_id=first.thread_id,
            settings=load_settings(session),
            actor=actor,
            now=now,
        )
        if len(matches) > 1:
            # More than one corroborated copy: recorded as sent, but held for applicant review.
            message = session.get(Message, attempt.message_id)
            assert message is not None
            owner = session.get(Campaign, message.campaign_id)
            assert owner is not None
            campaign_svc.open_pause(
                session,
                owner,
                PauseReason.SEND_UNCERTAIN,
                actor=actor,
                now=now,
                source_ref=f"attempt:{attempt.attempt_uid}:duplicates",
                detail={"matches": [m.gmail_message_id for m in matches]},
            )
        return "sent"


def reconcile_unresolved(
    session_factory: sessionmaker[Session],
    transports: Mapping[Mode, MailTransport],
    clock: Clock,
    *,
    include_uncertain: bool = False,
) -> dict[str, str]:
    """Startup step 1: reconcile every in-flight (optionally uncertain) attempt."""
    statuses = [AttemptStatus.IN_FLIGHT.value]
    if include_uncertain:
        statuses.append(AttemptStatus.UNCERTAIN.value)
    with transaction(session_factory) as session:
        pending = [
            (a.attempt_uid, Mode(a.mode))
            for a in session.scalars(select(SendAttempt).where(SendAttempt.status.in_(statuses)))
        ]
    results: dict[str, str] = {}
    for uid, mode in pending:
        transport = transports.get(mode)
        if transport is None:
            with transaction(session_factory) as session:
                attempt = send_results.attempt_by_uid(session, uid)
                if attempt.status == AttemptStatus.IN_FLIGHT.value:
                    send_results.mark_uncertain(
                        session,
                        attempt,
                        detail="startup: transport unavailable",
                        actor=Actor.RECONCILER,
                        now=clock.now(),
                    )
            results[uid] = "uncertain"
            continue
        results[uid] = reconcile_attempt(
            session_factory, transport, clock, uid, detail="startup recovery"
        )
    return results


def startup_recovery(
    session_factory: sessionmaker[Session],
    transports: Mapping[Mode, MailTransport],
    syncs: Mapping[str, sync_svc.MailboxSync],
    clock: Clock,
) -> dict[str, object]:
    """Spec §9.4 order: reconcile -> sync -> missed-window rule. Dispatch only afterwards."""
    reconciled = reconcile_unresolved(session_factory, transports, clock)
    sync_results = {key: sync.sync_now() for key, sync in syncs.items()}
    with transaction(session_factory) as session:
        missed = scheduling.apply_missed_window_rule(session, clock.now(), load_settings(session))
    return {"reconciled": reconciled, "sync": sync_results, "missed_window": missed}
