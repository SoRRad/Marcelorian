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


class PracticeClock:
    """Campaign time: real time, plus a forward-only offset while practice mode is active.

    Practice mode (sandbox) runs the real timing rules on simulated time (decision C6).
    Only campaign, message, send and reply operations use this clock; profile, research
    and verification data always use real time. The offset only grows, so simulated time
    never moves backwards; in a real mode the offset is ignored.
    """

    def __init__(self, base: Clock, offset: timedelta = timedelta(0), active: bool = False) -> None:
        if offset < timedelta(0):
            raise ValueError("the practice offset never goes negative")
        self._base = base
        self._offset = offset
        self._active = active
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            offset = self._offset if self._active else timedelta(0)
        return self._base.now() + offset

    @property
    def offset(self) -> timedelta:
        with self._lock:
            return self._offset

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def set_active(self, active: bool) -> None:
        with self._lock:
            self._active = active

    def advance(self, delta: timedelta) -> timedelta:
        if delta <= timedelta(0):
            raise ValueError("practice time only moves forward")
        with self._lock:
            if not self._active:
                raise ValueError("practice time moves only in practice mode")
            self._offset += delta
            return self._offset
