"""Injected clocks.

All safety logic takes the current time from a ``Clock`` so tests (and the
sandbox simulated clock, decision C6) can control it. Every datetime that
crosses a module boundary is timezone-aware UTC.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current instant as an aware UTC datetime."""
        ...


def ensure_utc(value: datetime) -> datetime:
    """Return ``value`` converted to UTC; reject naive datetimes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetimes are not accepted; use aware UTC datetimes")
    return value.astimezone(UTC)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """A clock that only moves when told to (tests and sandbox simulated time)."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, delta: timedelta) -> datetime:
        if delta < timedelta(0):
            raise ValueError("simulated time never moves backwards")
        with self._lock:
            self._now = self._now + delta
            return self._now

    def set(self, when: datetime) -> datetime:
        target = ensure_utc(when)
        with self._lock:
            if target < self._now:
                raise ValueError("simulated time never moves backwards")
            self._now = target
            return self._now
