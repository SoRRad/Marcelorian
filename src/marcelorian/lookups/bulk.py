"""Look up every contact in one click (spec §6 "Discovery"; owner idea, 2026-10-20).

The applicant starts it; it runs in its own background thread with progress and a Stop
button, and never touches sending (spec: discovery never delays safety work).

* Who: every contact (not merged into another) without a lookup in the last 30 days.
  Because each finished contact records its lookup runs, stopping and starting again
  simply continues with the ones not done yet (resumable).
* How: the same free, read-only PubMed/OpenAlex/NIH RePORTER calls as the single lookup,
  one contact at a time with a pause between contacts, so the services are not
  hammered. Network calls happen outside any database transaction.
* A contact whose sources all failed is counted as failed and is tried again next time.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.db.engine import transaction
from marcelorian.db.models import LookupRun, Pi
from marcelorian.lookups import enrich
from marcelorian.lookups.http import Fetcher

log = logging.getLogger(__name__)

REFRESH_AFTER = timedelta(days=30)
PAUSE_SECONDS = 1.0
#: enrich records a source it could not try (e.g. OpenAlex without an ORCID) as skipped.
SKIPPED = "skipped"


def needing_lookup(session: Session, now: datetime) -> list[int]:
    """Contacts with no successful lookup in the last 30 days, oldest contacts first."""
    latest = (
        select(LookupRun.pi_id, func.max(LookupRun.ran_at).label("ran_at"))
        .where(  # only a source that ran and answered counts; failures and skips retry
            LookupRun.pi_id.is_not(None),
            LookupRun.ok.is_(True),
            LookupRun.message.not_like(f"{SKIPPED}%"),
        )
        .group_by(LookupRun.pi_id)
        .subquery()
    )
    rows = session.execute(
        select(Pi.id, latest.c.ran_at)
        .outerjoin(latest, latest.c.pi_id == Pi.id)
        .where(Pi.merged_into_id.is_(None))
        .order_by(Pi.id)
    ).all()
    cutoff = now - REFRESH_AFTER
    return [pi_id for pi_id, ran_at in rows if ran_at is None or ran_at < cutoff]


@dataclass(frozen=True)
class Progress:
    running: bool = False
    total: int = 0
    done: int = 0
    failed: int = 0
    current: str = ""
    stopped: bool = False
    finished_at: datetime | None = None


class BulkLookup:
    """One background run at a time; progress is kept in memory for the screens."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        fetcher: Fetcher,
        clock: Clock,
        *,
        pause: float = PAUSE_SECONDS,
        on_contact_done: Callable[[], None] | None = None,
    ) -> None:
        self._factory = session_factory
        self._fetcher = fetcher
        self._clock = clock
        self._pause = pause
        self._on_contact_done = on_contact_done
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._progress = Progress()
        self._thread: threading.Thread | None = None

    def progress(self) -> Progress:
        with self._lock:
            return self._progress

    def start(self) -> int:
        """Start a run; returns how many contacts it will look up (0 if none or busy)."""
        with self._lock:
            if self._progress.running:
                return 0
            with transaction(self._factory) as session:
                ids = needing_lookup(session, self._clock.now())
            if not ids:
                return 0
            self._stop.clear()
            self._progress = Progress(running=True, total=len(ids))
            self._thread = threading.Thread(
                target=self._run, args=(ids,), name="marcelorian-lookups", daemon=True
            )
            self._thread.start()
            return len(ids)

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _update(self, **changes: object) -> None:
        with self._lock:
            self._progress = replace(self._progress, **changes)  # type: ignore[arg-type]

    def _run(self, ids: list[int]) -> None:
        try:
            for pi_id in ids:
                if self._stop.is_set():
                    self._update(stopped=True)
                    break
                self._one(pi_id)
                if self._stop.wait(self._pause):
                    self._update(stopped=True)
                    break
        except Exception:  # a broken run never takes the app down
            log.exception("bulk lookup stopped by an error")
        finally:
            self._update(running=False, current="", finished_at=self._clock.now())

    def _one(self, pi_id: int) -> None:
        with transaction(self._factory) as session:
            try:
                query = enrich.pi_query(session, pi_id)
            except LookupError:
                self._update(done=self.progress().done + 1)
                return
        self._update(current=f"{query.first_name} {query.last_name}".strip())
        results = enrich.gather_for_pi(query, self._fetcher, self._clock.now().date())
        with transaction(self._factory) as session:
            enrich.store_pi_results(session, pi_id, results, self._clock.now())
        progress = self.progress()
        answered = any(r.ok and not r.message.startswith(SKIPPED) for r in results)
        failed = progress.failed + (0 if answered else 1)
        self._update(done=progress.done + 1, failed=failed)
        if self._on_contact_done is not None:
            self._on_contact_done()
