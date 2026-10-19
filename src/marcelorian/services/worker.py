"""Background worker and in-app notices (spec §9.4, §9.5; Outlook-only pilot U3).

The worker never sends: in the Outlook hand-off channel only the applicant's own click in
Outlook sends (owner decision S1), and practice sends need the practice button. It keeps
the state current so the screens and notices are right:

1. **Startup recovery** (once, before anything else): reconcile in-flight attempts, sync
   the practice mailbox, then the missed-window rule (spec §9.4 order).
2. **Each cycle** (spec §9.5 order, minus dispatch): heartbeat; the missed-window rule
   (an approved email whose window passed rolls to the next window, S7); due follow-up
   generation: a due follow-up whose template has no personal lines is drafted for the
   applicant's review (never approved), others become a notice to write the lines.

Notices are computed on request from the current state (nothing is stored), so they
never go stale: follow-ups to write, drafts to review, emails ready to open, emails left
open in Outlook, reviews, due tasks, and a stalled worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.config.settings import load_settings
from marcelorian.config.specialty import load_specialty
from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, Message, Task, WorkerHeartbeat
from marcelorian.domain.enums import Actor, CampaignState, MessageState, Mode
from marcelorian.services import (
    approvals,
    drafting,
    exports,
    handoff,
    reconciliation,
    runtime,
    scheduling,
)
from marcelorian.services import sync as sync_svc
from marcelorian.transport.base import MailTransport

log = logging.getLogger(__name__)

COMPONENT = "worker"
INTERVAL = timedelta(seconds=60)
STALLED_AFTER = timedelta(minutes=5)
_REVIEW_STATES = (
    MessageState.AWAITING_APPROVAL.value,
    MessageState.NEEDS_REAPPROVAL.value,
    MessageState.FAILED_DEFINITE.value,
)


# --------------------------------------------------------------------------- #
# One cycle
# --------------------------------------------------------------------------- #


@dataclass
class CycleResult:
    rolled: list[int] = field(default_factory=list)
    needs_reapproval: list[int] = field(default_factory=list)
    drafted: list[int] = field(default_factory=list)  # campaign ids with a new draft
    to_write: list[int] = field(default_factory=list)  # campaign ids waiting for lines


def heartbeat(session: Session, now: datetime) -> None:
    row = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.component == COMPONENT))
    if row is None:
        session.add(WorkerHeartbeat(at=now, component=COMPONENT))
    else:
        row.at = now


def last_heartbeat(session: Session) -> datetime | None:
    return session.scalar(select(WorkerHeartbeat.at).where(WorkerHeartbeat.component == COMPONENT))


def due_followups(session: Session, mode: Mode, now: datetime) -> list[Campaign]:
    """Active-mode campaigns whose next follow-up is due and not drafted yet."""
    campaigns = session.scalars(
        select(Campaign)
        .where(
            Campaign.mode == mode.value,
            Campaign.next_step.is_not(None),
            Campaign.next_due_at.is_not(None),
            Campaign.next_due_at <= now,
            Campaign.sequence_stopped.is_(False),
            Campaign.state != CampaignState.CLOSED.value,
        )
        .order_by(Campaign.next_due_at, Campaign.id)
    ).all()
    due = []
    for campaign in campaigns:
        step = int(campaign.next_step or 0)
        exists = session.scalar(
            select(Message.id).where(Message.campaign_id == campaign.id, Message.step == step)
        )
        if exists is None and drafting.draftable_step(session, campaign, now) == step:
            due.append(campaign)
    return due


def generate_followups(session: Session, mode: Mode, now: datetime, result: CycleResult) -> None:
    specialty = load_specialty().display_name
    for campaign in due_followups(session, mode, now):
        step = int(campaign.next_step or 0)
        if drafting.personal_fields_for(session, step):
            result.to_write.append(campaign.id)
            continue
        try:
            with session.begin_nested():  # one campaign's problem never stops the others
                drafting.save(
                    session,
                    campaign,
                    step,
                    {},
                    {},
                    body_override=None,
                    specialty_display_name=specialty,
                    now=now,
                    actor=Actor.SYSTEM,
                )
        except approvals.ApprovalError as exc:
            log.info("follow-up for campaign %s not drafted: %s", campaign.id, exc.reasons)
            continue
        result.drafted.append(campaign.id)


def run_cycle(
    session_factory: sessionmaker[Session], clock: Clock, campaign_clock: Clock
) -> CycleResult:
    """One worker cycle. ``campaign_clock`` is the practice clock (real time outside it)."""
    result = CycleResult()
    with transaction(session_factory) as session:
        real_now, now = clock.now(), campaign_clock.now()
        heartbeat(session, real_now)
        settings = load_settings(session)
        real_modes = [m for m in Mode if m is not Mode.SANDBOX]
        outcomes = scheduling.apply_missed_window_rule(
            session, real_now, settings, modes=real_modes
        ) + scheduling.apply_missed_window_rule(session, now, settings, modes=[Mode.SANDBOX])
        for message_id, outcome in outcomes:
            (result.rolled if outcome == "rolled" else result.needs_reapproval).append(message_id)
        mode = runtime.get_runtime(session, real_now).active_mode
        generate_followups(session, mode, now, result)
    return result


# --------------------------------------------------------------------------- #
# The thread
# --------------------------------------------------------------------------- #


class BackgroundWorker:
    """Runs startup recovery once, then a cycle every ``interval`` until stopped."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        clock: Clock,
        campaign_clock: Clock,
        transports: Mapping[Mode, MailTransport],
        syncs: Mapping[str, sync_svc.MailboxSync],
        *,
        interval: timedelta = INTERVAL,
        on_cycle: Callable[[CycleResult], None] | None = None,
        jobs: Sequence[Callable[[], object]] = (),
    ) -> None:
        self._factory = session_factory
        self._clock = clock
        self._campaign_clock = campaign_clock
        self._transports = transports
        self._syncs = syncs
        self._interval = interval.total_seconds()
        self._on_cycle = on_cycle
        self._jobs = tuple(jobs)  # after the safety work: exports, daily backup (§9.5)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def recover(self) -> dict[str, object]:
        # Real time here, so practice time can never roll a live approval early; the first
        # cycle then applies the rule to practice messages on practice time.
        return reconciliation.startup_recovery(
            self._factory, self._transports, self._syncs, self._clock
        )

    def cycle(self) -> CycleResult | None:
        try:
            result = run_cycle(self._factory, self._clock, self._campaign_clock)
        except Exception:  # keep the worker alive; the stalled notice shows persistent trouble
            log.exception("background cycle failed")
            return None
        for job in self._jobs:
            try:
                job()
            except Exception:  # a failed export or backup never stops the worker
                log.exception("background job failed")
        if self._on_cycle is not None:
            self._on_cycle(result)
        return result

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="marcelorian-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Run a cycle soon (after a mode switch, practice time change or a send)."""
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def _run(self) -> None:
        try:
            self.recover()
        except Exception:
            log.exception("startup recovery failed")
        while not self._stop.is_set():
            self.cycle()
            self._wake.wait(self._interval)
            self._wake.clear()


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Notice:
    key: str
    level: str  # action | warn | info
    text: str
    url: str
    count: int


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def notices(session: Session, *, now: datetime, campaign_now: datetime) -> list[Notice]:
    mode = runtime.get_runtime(session, now).active_mode
    found: list[Notice] = []

    to_write = due_followups(session, mode, campaign_now)
    if to_write:
        first = to_write[0].id
        found.append(
            Notice(
                "followups_to_write",
                "action",
                _plural(len(to_write), "follow-up is", "follow-ups are")
                + " due: write the personal line and approve.",
                f"/campaigns/{first}/draft" if len(to_write) == 1 else "/approvals#followups",
                len(to_write),
            )
        )
    to_review = int(
        session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.mode == mode.value, Message.state.in_(_REVIEW_STATES))
        )
        or 0
    )
    if to_review:
        found.append(
            Notice(
                "drafts_to_review",
                "action",
                _plural(to_review, "draft is", "drafts are") + " waiting for your review.",
                "/approvals",
                to_review,
            )
        )
    ready = scheduling.dispatchable_message_ids(session, mode, campaign_now)
    if ready:
        verb = "practice-send" if mode is Mode.SANDBOX else "open in Outlook"
        found.append(
            Notice(
                "ready_to_send",
                "action",
                _plural(len(ready), "approved email", "approved emails")
                + f" can be sent now: {verb}.",
                f"/messages/{ready[0]}#send" if len(ready) == 1 else "/approvals",
                len(ready),
            )
        )
    stale = [h for h in handoff.open_handoffs(session, now) if h.needs_reminder]
    if stale:
        found.append(
            Notice(
                "open_in_outlook",
                "warn",
                _plural(len(stale), "email has", "emails have")
                + " been open in Outlook for over a day: record Sent or Not sent.",
                f"/handoffs/{stale[0].attempt_uid}" if len(stale) == 1 else "/#handoffs",
                len(stale),
            )
        )
    reviews = session.execute(
        select(CampaignPause.campaign_id)
        .join(Campaign, Campaign.id == CampaignPause.campaign_id)
        .where(CampaignPause.resolved_at.is_(None), Campaign.mode == mode.value)
        .order_by(CampaignPause.id)
    ).all()
    if reviews:
        found.append(
            Notice(
                "reviews",
                "warn",
                _plural(len(reviews), "campaign needs", "campaigns need") + " a review.",
                f"/campaigns/{reviews[0][0]}",
                len(reviews),
            )
        )
    today = campaign_now.date().isoformat()
    due_tasks = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.done_at.is_(None), Task.due_on.is_not(None), Task.due_on <= today)
        )
        or 0
    )
    if due_tasks:
        found.append(
            Notice(
                "tasks_due",
                "action",
                _plural(due_tasks, "task is", "tasks are") + " due.",
                "/tasks",
                due_tasks,
            )
        )
    export_state = exports.load_state(session)
    if export_state.stale:
        found.append(Notice("exports_stale", "warn", export_state.error, "/data", 1))
    beat = last_heartbeat(session)
    if beat is not None and now - beat > STALLED_AFTER:
        minutes = int((now - beat).total_seconds() // 60)
        found.append(
            Notice(
                "worker_stalled",
                "warn",
                f"Background checks have not run for {minutes} minutes. Restart Marcelorian "
                "if this stays.",
                "/",
                1,
            )
        )
    return found
