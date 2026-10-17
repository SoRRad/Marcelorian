"""Pure timing rules: windows, blackouts, and follow-up due dates (spec §9.3-§9.4).

Conventions
-----------
* All inputs/outputs that are instants are aware UTC datetimes.
* Wall-clock times are interpreted in the PI's verified IANA zone.
* DST: a local time that does not exist (spring-forward gap) is moved forward
  by the length of the gap; an ambiguous local time (fall-back) uses its first
  occurrence (decision C8). Python's ``zoneinfo`` with ``fold=0`` implements
  exactly this, and ``resolve_local`` documents/centralises it.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from marcelorian.clock import ensure_utc

FIXED_FOLLOWUP_INTERVAL = timedelta(hours=7 * 24)
#: Horizon used when searching for the next window; a year covers any blackout.
_WINDOW_SEARCH_DAYS = 400
#: Length of the synthetic window used by the transport-test "always open" override.
_ALWAYS_OPEN_SPAN = timedelta(days=3650)


class SchedulingError(ValueError):
    """Raised when scheduling is impossible (unknown zone, no window found)."""


class LastIntervalMode(StrEnum):
    CALENDAR_MONTH = "calendar_month"
    N_DAYS = "n_days"


def load_zone(name: str | None) -> ZoneInfo:
    """Load an IANA zone; unknown or missing zones block scheduling (spec §6)."""
    if not name:
        raise SchedulingError("time zone is not set; scheduling is blocked until verified")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise SchedulingError(f"unknown IANA time zone {name!r}") from exc


def resolve_local(naive_local: datetime, tz: ZoneInfo) -> datetime:
    """Return the UTC instant for a wall-clock time in ``tz``.

    Nonexistent times move forward by the gap length; ambiguous times resolve to
    the first occurrence (fold=0).
    """
    if naive_local.tzinfo is not None:
        raise ValueError("resolve_local expects a naive wall-clock datetime")
    return naive_local.replace(tzinfo=tz, fold=0).astimezone(UTC)


# --------------------------------------------------------------------------- #
# Blackouts (optional, off by default; spec §9.3)
# --------------------------------------------------------------------------- #


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:  # Saturday -> Friday
        return day - timedelta(days=1)
    if day.weekday() == 6:  # Sunday -> Monday
        return day + timedelta(days=1)
    return day


def federal_holidays_observed(year: int) -> frozenset[date]:
    """US federal holidays (5 U.S.C. 6103) as observed, for holidays dated in ``year``.

    Observed dates may fall in the adjacent year (e.g. Jan 1 on a Saturday is
    observed on Dec 31 of the previous year).
    """
    fixed = [
        date(year, 1, 1),  # New Year's Day
        date(year, 6, 19),  # Juneteenth National Independence Day
        date(year, 7, 4),  # Independence Day
        date(year, 11, 11),  # Veterans Day
        date(year, 12, 25),  # Christmas Day
    ]
    floating = [
        _nth_weekday(year, 1, 0, 3),  # Birthday of Martin Luther King, Jr.
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving Day
    ]
    return frozenset([_observed(d) for d in fixed] + floating)


def is_federal_holiday(day: date) -> bool:
    return any(day in federal_holidays_observed(y) for y in (day.year - 1, day.year, day.year + 1))


@dataclass(frozen=True)
class MonthDay:
    month: int
    day: int

    def __post_init__(self) -> None:
        # Validate against a leap year so Feb 29 is accepted.
        date(2024, self.month, self.day)

    @classmethod
    def parse(cls, text: str) -> MonthDay:
        month_s, day_s = text.split("-")
        return cls(int(month_s), int(day_s))

    def key(self) -> tuple[int, int]:
        return (self.month, self.day)


def in_month_day_range(day: date, start: MonthDay, end: MonthDay) -> bool:
    """Inclusive month-day range that may wrap the year end (e.g. 12-20..01-05)."""
    key = (day.month, day.day)
    if start.key() <= end.key():
        return start.key() <= key <= end.key()
    return key >= start.key() or key <= end.key()


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WindowPolicy:
    """When sends may *start*, in the PI's local time.

    Spec §1: default Mon-Fri 08:00-16:00, the widest allowed (owner W1); it may be
    narrowed, never widened beyond that.
    """

    weekdays: frozenset[int] = field(default_factory=lambda: frozenset(range(5)))  # Mon=0
    start: time = time(8, 0)
    end: time = time(16, 0)
    blackout_federal_holidays: bool = False
    blackout_year_end: bool = False
    year_end_start: MonthDay = field(default_factory=lambda: MonthDay(12, 20))
    year_end_end: MonthDay = field(default_factory=lambda: MonthDay(1, 5))
    #: Only ever set from an approved transport_test snapshot override (B2).
    always_open: bool = False

    def __post_init__(self) -> None:
        if not self.weekdays or not self.weekdays <= frozenset(range(7)):
            raise ValueError("window weekdays must be a non-empty subset of 0..6")
        if not self.start < self.end:
            raise ValueError("window start must be before window end")

    def is_blackout(self, day: date) -> bool:
        if self.blackout_federal_holidays and is_federal_holiday(day):
            return True
        return self.blackout_year_end and in_month_day_range(
            day, self.year_end_start, self.year_end_end
        )


@dataclass(frozen=True)
class Window:
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end


def window_on_local_date(day: date, tz: ZoneInfo, policy: WindowPolicy) -> Window | None:
    if day.weekday() not in policy.weekdays or policy.is_blackout(day):
        return None
    start = resolve_local(datetime.combine(day, policy.start), tz)
    end = resolve_local(datetime.combine(day, policy.end), tz)
    if not start < end:  # pathological DST edge; treat as no window that day
        return None
    return Window(start, end)


def next_window(after: datetime, tz: ZoneInfo, policy: WindowPolicy) -> Window:
    """The first window whose end is strictly after ``after``.

    If ``after`` lies inside a window, that window is returned (its start may be
    earlier than ``after``).
    """
    after = ensure_utc(after)
    if policy.always_open:
        return Window(after, after + _ALWAYS_OPEN_SPAN)
    local_day = after.astimezone(tz).date()
    for offset in range(-1, _WINDOW_SEARCH_DAYS):
        window = window_on_local_date(local_day + timedelta(days=offset), tz, policy)
        if window is not None and window.end > after:
            return window
    raise SchedulingError("no permitted send window found within the search horizon")


def in_window(instant: datetime, tz: ZoneInfo, policy: WindowPolicy) -> bool:
    instant = ensure_utc(instant)
    if policy.always_open:
        return True
    return next_window(instant, tz, policy).contains(instant)


def eligible_window(due_at: datetime, now: datetime, tz: ZoneInfo, policy: WindowPolicy) -> Window:
    """The window a message approved at ``now`` with ``due_at`` is assigned to."""
    return next_window(max(ensure_utc(due_at), ensure_utc(now)), tz, policy)


# --------------------------------------------------------------------------- #
# Due dates (spec §9.3; decision C8)
# --------------------------------------------------------------------------- #


def add_calendar_month(local: datetime) -> datetime:
    """Same wall-clock time one calendar month later, clamped to month end."""
    if local.tzinfo is not None:
        raise ValueError("add_calendar_month expects a naive wall-clock datetime")
    year = local.year + (1 if local.month == 12 else 0)
    month = 1 if local.month == 12 else local.month + 1
    day = min(local.day, calendar.monthrange(year, month)[1])
    return local.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class LastInterval:
    mode: LastIntervalMode = LastIntervalMode.CALENDAR_MONTH
    days: int = 30

    def __post_init__(self) -> None:
        if self.days < 1:
            raise ValueError("the N-day last interval must be at least 1 day")


def due_for_step(
    step: int,
    previous_sent_at: datetime,
    tz: ZoneInfo,
    last_interval: LastInterval,
    override_intervals: dict[int, int] | None = None,
) -> datetime:
    """Due instant (UTC) for ``step`` (2-4), computed from the previous step's
    established actual ``sent_at``.

    ``override_intervals`` maps step -> seconds and exists only for an approved
    transport_test override (decision B2); the caller enforces the mode.
    """
    previous = ensure_utc(previous_sent_at)
    if step not in (2, 3, 4):
        raise ValueError("due dates exist only for steps 2-4")
    if override_intervals and step in override_intervals:
        seconds = override_intervals[step]
        if seconds <= 0:
            raise ValueError("override intervals must be positive")
        return previous + timedelta(seconds=seconds)
    if step in (2, 3):
        return previous + FIXED_FOLLOWUP_INTERVAL
    local = previous.astimezone(tz).replace(tzinfo=None)
    if last_interval.mode is LastIntervalMode.CALENDAR_MONTH:
        return resolve_local(add_calendar_month(local), tz)
    return resolve_local(local + timedelta(days=last_interval.days), tz)


def merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    ordered = sorted((s, e) for s, e in intervals if s < e)
    merged: list[tuple[datetime, datetime]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
