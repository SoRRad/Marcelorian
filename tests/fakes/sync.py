"""Fake real-mailbox sync with scripted outcomes and a hook for race tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.db.engine import transaction
from marcelorian.services.sync import SyncOutcome, record_sync_gap, record_sync_success


@dataclass
class FakeRealSync:
    session_factory: sessionmaker[Session]
    clock: Clock
    mailbox_key: str = "real"
    script: list[str] = field(default_factory=list)
    before: Callable[[], None] | None = None
    calls: int = 0

    def sync_now(self) -> SyncOutcome:
        self.calls += 1
        if self.before:
            self.before()
        action = self.script.pop(0) if self.script else "ok"
        now = self.clock.now()
        if action == "fail":
            return SyncOutcome(ok=False, detail="network down")
        if action == "gap":
            with transaction(self.session_factory) as session:
                record_sync_gap(session, self.mailbox_key, "history cursor expired", now)
            return SyncOutcome(ok=False, gap=True, detail="history cursor expired")
        with transaction(self.session_factory) as session:
            record_sync_success(session, self.mailbox_key, now, None, now)
        return SyncOutcome(ok=True)
