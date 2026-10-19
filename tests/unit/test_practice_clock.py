"""The practice clock: forward-only offset, applied only in practice mode."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from marcelorian.clock import ManualClock, PracticeClock

START = datetime(2026, 10, 20, 13, 0, tzinfo=UTC)


def test_offset_applies_only_while_active() -> None:
    base = ManualClock(START)
    clock = PracticeClock(base, timedelta(days=2), active=False)
    assert clock.now() == START
    clock.set_active(True)
    assert clock.now() == START + timedelta(days=2)
    assert clock.advance(timedelta(hours=1)) == timedelta(days=2, hours=1)
    base.advance(timedelta(minutes=5))
    assert clock.now() == START + timedelta(days=2, hours=1, minutes=5)


def test_never_backwards_and_only_in_practice() -> None:
    clock = PracticeClock(ManualClock(START), active=True)
    for bad in (timedelta(0), timedelta(seconds=-1)):
        with pytest.raises(ValueError):
            clock.advance(bad)
    clock.set_active(False)
    with pytest.raises(ValueError):
        clock.advance(timedelta(days=1))
    with pytest.raises(ValueError):
        PracticeClock(ManualClock(START), timedelta(seconds=-1))
