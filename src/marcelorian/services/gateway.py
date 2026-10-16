"""The single send gateway (spec §9.6; decisions B2, B8, B10, B12-B14, B17, B20).

Every sending path (UI approval, scheduler tick, recovery) calls
``SendGateway.dispatch``. There is no other code path to a transport's
``send``.

Phases
------
0. Local checks in a short transaction (cheap early exit, no network).
1. Network preflight with **no** transaction held: account/scope match, an
   incremental sync, then prior-contact checks (Email 1) or thread and
   new-thread reply checks (follow-ups).
2. MIME built from the verified snapshot bytes, with a fresh Message-ID and
   ``X-Marcelorian-Id`` for this attempt.
3. Atomic claim (``BEGIN IMMEDIATE``): re-reads message, approval, campaign
   stop/review status, active mode, persistent pause, do-not-contact,
   dependencies, recipient, due time, window (11:00 limit), pacing, budget,
   reservations and sync coverage; inserts the ``in_flight`` attempt, holds
   the budget, records pacing, and commits. **This commit is the point beyond
   which recall is not promised.**
4. Exactly one ``send`` call, no retries.
5. Success -> committed before anything else. Definite rejection ->
   ``failed_definite``. Anything else -> reconciliation; "not found" makes the
   attempt ``uncertain`` and it is never retried automatically.

Known limit (spec §10): a genuinely new reply that arrives after the claim
cannot stop that submission.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.config.settings import AppSettings, load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalSnapshot,
    Blob,
    Campaign,
    InboundEvent,
    Institution,
    Message,
    Pi,
    SendAttempt,
)
from marcelorian.domain.enums import (
    STOPPED_STATES,
    Actor,
    AttemptStatus,
    CampaignState,
    DeferralReason,
    InboundClassification,
    Ledger,
    MessageState,
    Mode,
    SentAtProvenance,
    SnapshotState,
    ledger_for,
)
from marcelorian.domain.gmail_params import (
    REQUIRED_SCOPES,
    messages_from_since_query,
    prior_contact_query,
)
from marcelorian.domain.identifiers import new_x_marcelorian_id, opaque_token, rfc_message_id
from marcelorian.domain.mime import build_mime
from marcelorian.domain.recipients import RecipientError, validate_recipient
from marcelorian.domain.snapshot import SnapshotContent, SnapshotError, verify_canonical
from marcelorian.domain.timing import SchedulingError, in_window, load_zone
from marcelorian.services import (
    approvals,
    budget,
    dnc,
    pacing,
    prior_contact,
    recipients,
    reconciliation,
    reservations,
    runtime,
    scheduling,
    send_results,
)
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import sync as sync_svc
from marcelorian.services.audit import audit
from marcelorian.services.inbound import record_inbound
from marcelorian.transport.base import (
    DefiniteRejection,
    MailTransport,
    MessageMeta,
    SearchResult,
    TransportKind,
)

log = logging.getLogger(__name__)


class Outcome(StrEnum):
    SENT = "sent"
    FAILED_DEFINITE = "failed_definite"
    UNCERTAIN = "uncertain"
    DEFERRED = "deferred"
    PAUSED = "paused"
    BLOCKED = "blocked"
    NOT_ELIGIBLE = "not_eligible"


@dataclass(frozen=True)
class DispatchResult:
    outcome: Outcome
    reason: str | None = None
    attempt_uid: str | None = None


@dataclass(frozen=True)
class _Verdict:
    outcome: Outcome
    reason: str
    invalidate: bool = False
    cap_reason: str | None = None


@dataclass(frozen=True)
class _Plan:
    message_id: int
    campaign_id: int
    mode: Mode
    step: int
    snapshot_id: int
    content: SnapshotContent
    contact_addresses: tuple[str, ...]
    last_sent_at: datetime | None
    #: transport_test message addressed to the connected sender itself (connectivity test).
    self_send: bool
    #: When this evaluation began. Every evaluation record of one dispatch uses it, so a
    #: preflight that crosses the window end still counts as considered in the window (B7).
    started: datetime


#: Classifies a new message found in a campaign thread during preflight.
InboundHandler = Callable[[Campaign, MessageMeta], InboundClassification]


def conservative_inbound_handler(_campaign: Campaign, _meta: MessageMeta) -> InboundClassification:
    """P0 default: anything new in the thread is doubtful -> pause and hold (spec §10).

    P3 replaces this with the metadata classifier.
    """
    return InboundClassification.AMBIGUOUS_THREAD


def _expected_kind(mode: Mode) -> TransportKind:
    return TransportKind.GMAIL if mode.is_real else TransportKind.SANDBOX


class SendGateway:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        clock: Clock,
        transports: Mapping[Mode, MailTransport],
        syncs: Mapping[str, sync_svc.MailboxSync],
        specialty_display_name: str,
        rng: random.Random | None = None,
        inbound_handler: InboundHandler = conservative_inbound_handler,
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._transports = dict(transports)
        self._syncs = dict(syncs)
        self._specialty = specialty_display_name
        self._rng = rng or random.SystemRandom()
        self._inbound_handler = inbound_handler

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def dispatch(self, message_id: int, *, actor: Actor = Actor.DISPATCHER) -> DispatchResult:
        started = self._clock.now()
        with transaction(self._factory) as session:
            message = session.get(Message, message_id)
            if message is None or message.state != MessageState.APPROVED_QUEUED.value:
                return DispatchResult(Outcome.NOT_ELIGIBLE, "not_approved_queued")
            settings = load_settings(session)
            verdict = self._verdict(session, message, settings, started, preflight_started=None)
            if verdict is not None:
                return self._apply_verdict(session, message, verdict, started, started)
            plan = self._plan(session, message, started)

        transport = self._transports.get(plan.mode)
        if transport is None or transport.kind is not _expected_kind(plan.mode):
            return self._record(plan, _Verdict(Outcome.BLOCKED, "transport_mode_mismatch"))

        preflight = self._preflight(transport, plan)
        if preflight is not None:
            return preflight

        token, x_id = opaque_token(), new_x_marcelorian_id()
        app_message_id = rfc_message_id(plan.content.sender, token)
        with transaction(self._factory) as session:
            blobs = {ref.sha256: session.get(Blob, ref.sha256) for ref in plan.content.attachments}
            data = {sha: blob.data for sha, blob in blobs.items() if blob is not None}
        try:
            raw = build_mime(
                plan.content,
                data,
                rfc_message_id=app_message_id,
                x_marcelorian_id=x_id,
                date=self._clock.now(),
            )
        except SnapshotError:
            return self._record(
                plan, _Verdict(Outcome.BLOCKED, "attachment_mismatch", invalidate=True)
            )

        claimed = self._claim(plan, started, app_message_id, x_id, actor)
        if isinstance(claimed, DispatchResult):
            return claimed
        attempt_uid = claimed
        return self._submit(transport, plan, attempt_uid, raw, actor)

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def _verdict(
        self,
        session: Session,
        message: Message,
        settings: AppSettings,
        now: datetime,
        *,
        preflight_started: datetime | None,
    ) -> _Verdict | None:
        snapshot = (
            session.get(ApprovalSnapshot, message.current_snapshot_id)
            if message.current_snapshot_id
            else None
        )
        if snapshot is None or snapshot.state != SnapshotState.ACTIVE.value:
            return _Verdict(Outcome.BLOCKED, "snapshot_not_active", invalidate=True)
        try:
            content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
        except SnapshotError:
            return _Verdict(Outcome.BLOCKED, "snapshot_hash_mismatch", invalidate=True)
        mode = Mode(message.mode)
        if (content.message_id, content.mode, content.step) != (message.id, mode, message.step):
            return _Verdict(Outcome.BLOCKED, "snapshot_mismatch", invalidate=True)
        campaign = session.get(Campaign, message.campaign_id)
        assert campaign is not None
        view = runtime.get_runtime(session, now)
        if view.active_mode is not mode:
            return _Verdict(Outcome.DEFERRED, DeferralReason.MODE_INACTIVE)
        if view.dispatch_paused:
            return _Verdict(Outcome.DEFERRED, DeferralReason.PAUSED)
        if mode.is_real and view.auth_blocked:
            return _Verdict(Outcome.DEFERRED, DeferralReason.AUTH_REQUIRED)
        # A mailbox-level gap is checked before any cap or pacing deferral, so a cap
        # deferral can never be recorded as a functioning evaluation during a gap (B7).
        coverage = sync_svc.coverage(session, sync_svc.mailbox_key_for(mode), now)
        if coverage.gap_open:
            return _Verdict(Outcome.DEFERRED, DeferralReason.SYNC_GAP)
        if message.cancel_requested_at is not None:
            return _Verdict(Outcome.BLOCKED, "cancel_requested", invalidate=True)
        state = CampaignState(campaign.state)
        if state in STOPPED_STATES or state is CampaignState.CANDIDATE:
            return _Verdict(Outcome.BLOCKED, "campaign_not_sendable")
        if state is CampaignState.PAUSED_REVIEW or campaign_svc.open_reasons(session, campaign.id):
            return _Verdict(Outcome.DEFERRED, DeferralReason.CAMPAIGN_NOT_SENDABLE)
        if message.step > 1 and campaign.sequence_stopped:
            return _Verdict(Outcome.DEFERRED, DeferralReason.SEQUENCE_STOPPED)
        if content.test_override is not None and mode is not Mode.TRANSPORT_TEST:
            return _Verdict(Outcome.BLOCKED, "override_outside_transport_test", invalidate=True)
        if not approvals.dependencies_current(session, snapshot.id):
            return _Verdict(Outcome.BLOCKED, "dependency_changed", invalidate=True)
        pi = session.get(Pi, campaign.pi_id)
        assert pi is not None
        institution = session.get(Institution, pi.institution_id)
        assert institution is not None
        if not institution.tz_verified or institution.iana_tz != content.pi_timezone:
            return _Verdict(Outcome.BLOCKED, "timezone_unverified", invalidate=True)
        if not pi.salutation_reviewed:
            return _Verdict(Outcome.BLOCKED, "salutation_not_reviewed", invalidate=True)
        extra = (content.to,) if mode is Mode.TRANSPORT_TEST else ()
        if dnc.matches(session, pi.id, mode, extra):
            return _Verdict(Outcome.BLOCKED, "do_not_contact", invalidate=True)
        if message.step == 1 and approvals.duplicate_outreach(session, campaign, content.to):
            # Merges or re-discovery after approval must never permit a second sequence.
            return _Verdict(Outcome.BLOCKED, "duplicate_outreach", invalidate=True)
        try:
            validate_recipient(
                recipients.build_context(
                    session,
                    campaign=campaign,
                    pi=pi,
                    transport_kind=_expected_kind(mode).value,
                    snapshot_to=content.to,
                    snapshot_intended=content.intended_recipient,
                    has_test_override=content.test_override is not None,
                )
            )
        except RecipientError:
            return _Verdict(Outcome.BLOCKED, "recipient_invalid", invalidate=True)
        try:
            tz = load_zone(content.pi_timezone)
        except SchedulingError:
            return _Verdict(Outcome.BLOCKED, "timezone_invalid", invalidate=True)
        policy = scheduling.policy_for(snapshot, settings)
        if message.due_at is not None and now < message.due_at:
            return _Verdict(Outcome.DEFERRED, DeferralReason.NOT_DUE)
        if message.eligible_window_end is None or now >= message.eligible_window_end:
            return _Verdict(Outcome.DEFERRED, DeferralReason.WINDOW_PASSED)
        if message.eligible_window_start is None or now < message.eligible_window_start:
            return _Verdict(Outcome.DEFERRED, DeferralReason.OUTSIDE_WINDOW)
        try:
            permitted = in_window(now, tz, policy)
        except SchedulingError:
            return _Verdict(Outcome.BLOCKED, "no_permitted_window", invalidate=True)
        if not permitted:
            return _Verdict(Outcome.DEFERRED, DeferralReason.OUTSIDE_WINDOW)
        ledger = ledger_for(mode)
        if not pacing.allows(session, ledger, now):
            return _Verdict(Outcome.DEFERRED, DeferralReason.PACING)
        limit = (
            settings.real_submissions_per_24h
            if ledger is Ledger.REAL
            else settings.sandbox_submissions_per_24h
        )
        if not budget.has_capacity(session, ledger, limit, now):
            return _Verdict(Outcome.DEFERRED, DeferralReason.BUDGET, cap_reason="budget_24h")
        if message.step == 1:
            capacity = reservations.validate_for_dispatch(session, campaign, settings, now)
            if not capacity.ok:
                return _Verdict(
                    Outcome.DEFERRED,
                    DeferralReason.OVER_CAPACITY,
                    cap_reason="over_capacity:" + ",".join(capacity.reasons),
                )
        if preflight_started is not None and (
            coverage.last_gmail_coverage_at is None
            or coverage.last_gmail_coverage_at < preflight_started
        ):
            return _Verdict(Outcome.DEFERRED, DeferralReason.SYNC_UNAVAILABLE)
        return None

    def _apply_verdict(
        self,
        session: Session,
        message: Message,
        verdict: _Verdict,
        now: datetime,
        evaluated_at: datetime,
    ) -> DispatchResult:
        if verdict.invalidate and message.state == MessageState.APPROVED_QUEUED.value:
            snapshot = session.get(ApprovalSnapshot, message.current_snapshot_id)
            if snapshot is not None and snapshot.state == SnapshotState.ACTIVE.value:
                snapshot.state, snapshot.ended_at, snapshot.end_reason = (
                    SnapshotState.INVALIDATED.value,
                    now,
                    verdict.reason,
                )
            campaign_svc.set_message_state(
                session,
                message,
                MessageState.NEEDS_REAPPROVAL,
                actor=Actor.GATEWAY,
                now=now,
                reason=verdict.reason,
            )
        message.last_deferral_reason = verdict.reason
        if verdict.reason in (DeferralReason.BUDGET, DeferralReason.OVER_CAPACITY):
            message.cap_block_reason = verdict.cap_reason
        reason: DeferralReason | str
        try:
            reason = DeferralReason(verdict.reason)
        except ValueError:
            reason = verdict.reason
        scheduling.record_evaluation(
            session, message, evaluated_at, outcome=verdict.outcome.value, reason=reason
        )
        return DispatchResult(verdict.outcome, str(verdict.reason))

    @staticmethod
    def _changed(message: Message, plan: _Plan) -> bool:
        """The message was claimed, revoked or re-approved by someone else meanwhile."""
        return (
            message.state != MessageState.APPROVED_QUEUED.value
            or message.current_snapshot_id != plan.snapshot_id
        )

    def _record(self, plan: _Plan, verdict: _Verdict) -> DispatchResult:
        with transaction(self._factory) as session:
            message = session.get(Message, plan.message_id)
            assert message is not None
            if self._changed(message, plan):
                return DispatchResult(Outcome.NOT_ELIGIBLE, "changed_during_preflight")
            return self._apply_verdict(session, message, verdict, self._clock.now(), plan.started)

    def _plan(self, session: Session, message: Message, started: datetime) -> _Plan:
        snapshot = session.get(ApprovalSnapshot, message.current_snapshot_id)
        assert snapshot is not None
        content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
        campaign = session.get(Campaign, message.campaign_id)
        assert campaign is not None
        mode = Mode(message.mode)
        if mode is Mode.TRANSPORT_TEST:
            addresses: tuple[str, ...] = (content.to,)  # the test address stands in for the PI
        else:
            cluster = dnc.merge_cluster(session, campaign.pi_id)
            addresses = tuple(sorted(dnc.cluster_addresses(session, cluster) | {content.to}))
        last_sent_at = None
        if message.step > 1:
            last_sent_at = session.scalar(
                select(Message.sent_at).where(
                    Message.campaign_id == campaign.id, Message.step == message.step - 1
                )
            )
        return _Plan(
            message_id=message.id,
            campaign_id=campaign.id,
            mode=mode,
            step=message.step,
            snapshot_id=snapshot.id,
            content=content,
            contact_addresses=addresses,
            last_sent_at=last_sent_at,
            self_send=mode is Mode.TRANSPORT_TEST and content.to == content.sender.lower(),
            started=started,
        )

    # ------------------------------------------------------------------ #
    # Network preflight (no transaction held)
    # ------------------------------------------------------------------ #
    def _preflight(self, transport: MailTransport, plan: _Plan) -> DispatchResult | None:
        try:
            account = transport.account()
        except DefiniteRejection as exc:
            if exc.auth_failure:
                self._auth_block("gmail_auth_failure")
            return self._record(plan, _Verdict(Outcome.DEFERRED, DeferralReason.AUTH_REQUIRED))
        except Exception:
            return self._record(
                plan, _Verdict(Outcome.DEFERRED, DeferralReason.TRANSPORT_UNAVAILABLE)
            )
        if plan.mode.is_real and (
            account.email.strip().lower() != plan.content.sender.lower()
            or not account.scopes >= REQUIRED_SCOPES
        ):
            self._auth_block("account_or_scope_mismatch")
            return self._record(plan, _Verdict(Outcome.DEFERRED, DeferralReason.AUTH_REQUIRED))
        sync = self._syncs.get(sync_svc.mailbox_key_for(plan.mode))
        if sync is None:
            return self._record(plan, _Verdict(Outcome.DEFERRED, DeferralReason.SYNC_UNAVAILABLE))
        try:
            outcome = sync.sync_now()
        except Exception:
            outcome = sync_svc.SyncOutcome(ok=False, detail="sync raised")
        if not outcome.ok:
            reason = DeferralReason.SYNC_GAP if outcome.gap else DeferralReason.SYNC_UNAVAILABLE
            return self._record(plan, _Verdict(Outcome.DEFERRED, reason))
        if plan.step == 1:
            return self._prior_contact(transport, plan)
        return self._reply_checks(transport, plan)

    def _auth_block(self, reason: str) -> None:
        with transaction(self._factory) as session:
            runtime.set_auth_block(session, True, reason, now=self._clock.now())

    def _prior_contact(self, transport: MailTransport, plan: _Plan) -> DispatchResult | None:
        incomplete = _Verdict(Outcome.DEFERRED, DeferralReason.PRIOR_CONTACT_CHECK_INCOMPLETE)
        with transaction(self._factory) as session:
            message = session.get(Message, plan.message_id)
            assert message is not None
            if self._changed(message, plan):
                return DispatchResult(Outcome.NOT_ELIGIBLE, "changed_during_preflight")
            if prior_contact.unresolved_attempt_to(
                session, set(plan.contact_addresses), plan.mode
            ):
                return self._apply_verdict(
                    session, message, incomplete, self._clock.now(), plan.started
                )
        if plan.self_send:
            # A transport_test self-send checks connectivity only (spec §12): searching the
            # sender's own address would match the whole mailbox. Recorded in DECISIONS.md.
            return None
        try:
            search = transport.search_message_ids(
                prior_contact_query(plan.contact_addresses), include_spam_trash=True
            )
        except Exception:
            return self._record(plan, incomplete)
        with transaction(self._factory) as session:
            now = self._clock.now()
            message = session.get(Message, plan.message_id)
            campaign = session.get(Campaign, plan.campaign_id)
            assert message is not None and campaign is not None
            if self._changed(message, plan) or CampaignState(campaign.state) in (
                *STOPPED_STATES,
                CampaignState.CANDIDATE,
            ):
                return DispatchResult(Outcome.NOT_ELIGIBLE, "changed_during_preflight")
            check = prior_contact.evaluate(
                session, campaign_id=campaign.id, mailbox_email=plan.content.sender, search=search
            )
            if not check.complete:
                return self._apply_verdict(session, message, incomplete, now, plan.started)
            if check.unacknowledged:
                prior_contact.open_review(
                    session, campaign, check.unacknowledged, actor=Actor.GATEWAY, now=now
                )
                scheduling.record_evaluation(
                    session,
                    message,
                    plan.started,
                    outcome=Outcome.PAUSED.value,
                    reason="prior_contact_found",
                )
                return DispatchResult(Outcome.PAUSED, "prior_contact_found")
        return None

    def _reply_checks(self, transport: MailTransport, plan: _Plan) -> DispatchResult | None:
        incomplete = _Verdict(Outcome.DEFERRED, DeferralReason.REPLY_CHECK_INCOMPLETE)
        thread_id = plan.content.thread_id
        if thread_id is None or plan.last_sent_at is None:
            return self._record(plan, _Verdict(Outcome.BLOCKED, "thread_unknown"))
        try:
            in_thread = transport.get_thread_metadata(thread_id)
            since = int(plan.last_sent_at.timestamp())
            search = (
                SearchResult(ids=(), complete=True)
                if plan.self_send  # the sender's own mail is not a possible reply
                else transport.search_message_ids(
                    messages_from_since_query(plan.contact_addresses, since),
                    include_spam_trash=True,
                )
            )
        except Exception:
            return self._record(plan, incomplete)
        if not search.complete:
            return self._record(plan, incomplete)
        with transaction(self._factory) as session:
            now = self._clock.now()
            message = session.get(Message, plan.message_id)
            campaign = session.get(Campaign, plan.campaign_id)
            assert message is not None and campaign is not None
            known = prior_contact.campaign_gmail_ids(session, campaign.id) | set(
                session.scalars(
                    select(InboundEvent.gmail_message_id).where(
                        InboundEvent.campaign_id == campaign.id
                    )
                )
            )
            thread_ids = {m.gmail_message_id for m in in_thread}
            found = False
            for meta in in_thread:
                if meta.gmail_message_id in known:
                    continue
                classification = self._inbound_handler(campaign, meta)
                record_inbound(
                    session,
                    campaign,
                    gmail_message_id=meta.gmail_message_id,
                    thread_id=meta.thread_id,
                    classification=classification,
                    from_address=meta.header("from"),
                    received_at=meta.internal_date,
                    actor=Actor.GATEWAY,
                    now=now,
                )
                found = True
            for gmail_id in search.ids:
                if gmail_id in known or gmail_id in thread_ids:
                    continue
                # Possible reply from a known identity in a new thread: confirm, never free (B11).
                record_inbound(
                    session,
                    campaign,
                    gmail_message_id=gmail_id,
                    thread_id=None,
                    classification=InboundClassification.AMBIGUOUS_THREAD,
                    from_address=None,
                    received_at=None,
                    actor=Actor.GATEWAY,
                    now=now,
                )
                found = True
            if found:
                scheduling.record_evaluation(
                    session,
                    message,
                    plan.started,
                    outcome=Outcome.PAUSED.value,
                    reason="inbound_activity",
                )
                return DispatchResult(Outcome.PAUSED, "inbound_activity")
        return None

    # ------------------------------------------------------------------ #
    # Atomic claim and submission
    # ------------------------------------------------------------------ #
    def _claim(
        self, plan: _Plan, started: datetime, app_message_id: str, x_id: str, actor: Actor
    ) -> str | DispatchResult:
        with transaction(self._factory) as session:
            now = self._clock.now()
            message = session.get(Message, plan.message_id)
            assert message is not None
            if self._changed(message, plan):
                return DispatchResult(Outcome.NOT_ELIGIBLE, "changed_during_preflight")
            settings = load_settings(session)
            verdict = self._verdict(session, message, settings, now, preflight_started=started)
            if verdict is not None:
                return self._apply_verdict(session, message, verdict, now, plan.started)
            snapshot = session.get(ApprovalSnapshot, plan.snapshot_id)
            assert snapshot is not None
            attempt = SendAttempt(
                attempt_uid=opaque_token(),
                message_id=message.id,
                snapshot_id=snapshot.id,
                snapshot_sha256=snapshot.sha256,
                mode=message.mode,
                sender=plan.content.sender,
                recipient=plan.content.to,
                intended_recipient=plan.content.intended_recipient,
                app_message_id=app_message_id,
                x_marcelorian_id=x_id,
                status=AttemptStatus.IN_FLIGHT.value,
                claimed_at=now,
                submit_started_at=now,
            )
            session.add(attempt)
            session.flush()
            snapshot.state = SnapshotState.CONSUMED.value
            campaign_svc.set_message_state(
                session,
                message,
                MessageState.SENDING,
                actor=actor,
                now=now,
                reason=f"claimed {attempt.attempt_uid}",
            )
            message.cap_block_reason = None
            message.last_deferral_reason = None
            ledger = ledger_for(plan.mode)
            budget.hold(session, ledger, attempt.id, now)
            pacing.record_submission(
                session,
                ledger,
                now,
                self._rng,
                settings.pacing_min_seconds,
                settings.pacing_max_seconds,
            )
            scheduling.record_evaluation(
                session, message, plan.started, outcome="claimed", reason=None, functioning=True
            )
            audit(
                session,
                at=now,
                actor=actor,
                entity_type="send_attempt",
                entity_id=attempt.id,
                action="claimed",
                detail={"attempt": attempt.attempt_uid, "snapshot_sha256": snapshot.sha256},
                mode=message.mode,
            )
            return attempt.attempt_uid

    def _submit(
        self, transport: MailTransport, plan: _Plan, attempt_uid: str, raw: bytes, actor: Actor
    ) -> DispatchResult:
        try:
            result = transport.send(raw, plan.content.thread_id)
        except DefiniteRejection as exc:
            with transaction(self._factory) as session:
                attempt = send_results.attempt_by_uid(session, attempt_uid)
                send_results.finalize_rejected(
                    session,
                    attempt,
                    error_code=str(exc.status),
                    detail=exc.reason or "",
                    actor=actor,
                    now=self._clock.now(),
                )
                if exc.auth_failure:
                    runtime.set_auth_block(
                        session, True, "gmail_auth_failure", now=self._clock.now()
                    )
            return DispatchResult(Outcome.FAILED_DEFINITE, str(exc.status), attempt_uid)
        except Exception as exc:  # timeout, connection loss, 5xx, unknown: ambiguous
            log.warning(
                "ambiguous send outcome for attempt %s: %s", attempt_uid, type(exc).__name__
            )
            status = reconciliation.reconcile_attempt(
                self._factory,
                transport,
                self._clock,
                attempt_uid,
                actor=Actor.RECONCILER,
                detail=f"ambiguous:{type(exc).__name__}",
            )
            outcome = Outcome.SENT if status == "sent" else Outcome.UNCERTAIN
            return DispatchResult(outcome, f"reconciled:{status}", attempt_uid)
        try:
            with transaction(self._factory) as session:
                now = self._clock.now()
                attempt = send_results.attempt_by_uid(session, attempt_uid)
                send_results.finalize_sent(
                    session,
                    attempt,
                    sent_at=now,
                    provenance=SentAtProvenance.GATEWAY_RESPONSE,
                    gmail_message_id=result.gmail_message_id,
                    gmail_thread_id=result.gmail_thread_id,
                    settings=load_settings(session),
                    actor=actor,
                    now=now,
                )
        except Exception:
            # Accepted by the provider but not committed locally: the attempt stays in_flight
            # and startup recovery reconciles it; it is never sent again.
            log.exception("commit after accepted send failed for attempt %s", attempt_uid)
            return DispatchResult(Outcome.UNCERTAIN, "commit_failed_after_acceptance", attempt_uid)
        return DispatchResult(Outcome.SENT, None, attempt_uid)
