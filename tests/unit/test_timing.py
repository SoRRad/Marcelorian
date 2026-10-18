"""Timing rules: windows, DST, calendar months, N days, blackouts (acceptance 12, 14, 15, 18)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from marcelorian.domain.timing import (
    LastInterval,
    LastIntervalMode,
    MonthDay,
    SchedulingError,
    WindowPolicy,
    add_calendar_month,
    due_for_step,
    federal_holidays_observed,
    in_window,
    is_federal_holiday,
    load_zone,
    next_window,
    resolve_local,
)

NY = ZoneInfo("America/New_York")
LA = ZoneInfo("America/Los_Angeles")
DEFAULT_POLICY = WindowPolicy()
#: Narrowed window (the pre-W1 default); the edge tests below are written against it.
POLICY = WindowPolicy(weekdays=frozenset({1, 2, 3}), end=time(11))


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# -- windows -------------------------------------------------------------------


def test_default_window_is_mon_to_fri_0800_to_1600() -> None:
    # Owner W1 (2026-10-17): the default is the widest allowed window.
    assert DEFAULT_POLICY.weekdays == frozenset({0, 1, 2, 3, 4})
    assert (DEFAULT_POLICY.start, DEFAULT_POLICY.end) == (time(8), time(16))


def test_default_window_edges_and_weekend() -> None:
    friday_1559 = resolve_local(datetime(2026, 10, 23, 15, 59), NY)
    assert in_window(friday_1559, NY, DEFAULT_POLICY)
    assert not in_window(friday_1559 + timedelta(minutes=1), NY, DEFAULT_POLICY)
    window = next_window(friday_1559 + timedelta(minutes=1), NY, DEFAULT_POLICY)
    assert window.start == resolve_local(datetime(2026, 10, 26, 8, 0), NY)
    assert window.end == resolve_local(datetime(2026, 10, 26, 16, 0), NY)


def test_window_start_inclusive_end_exclusive_in_pi_zone() -> None:
    tuesday_8 = resolve_local(datetime(2026, 10, 20, 8, 0), NY)
    tuesday_11 = resolve_local(datetime(2026, 10, 20, 11, 0), NY)
    assert in_window(tuesday_8, NY, POLICY)
    assert in_window(tuesday_11 - timedelta(microseconds=1), NY, POLICY)
    assert not in_window(tuesday_11, NY, POLICY)  # acceptance 15: never at/after 11:00
    assert not in_window(tuesday_8 - timedelta(seconds=1), NY, POLICY)


def test_next_window_after_thursday_close_is_next_tuesday() -> None:
    thursday_1100 = resolve_local(datetime(2026, 10, 22, 11, 0), NY)
    window = next_window(thursday_1100, NY, POLICY)
    assert window.start == resolve_local(datetime(2026, 10, 27, 8, 0), NY)
    assert window.end == resolve_local(datetime(2026, 10, 27, 11, 0), NY)


def test_window_uses_pi_zone_not_applicant_zone() -> None:
    instant = utc(2026, 10, 20, 13, 30)  # 09:30 New York, 06:30 Los Angeles
    assert in_window(instant, NY, POLICY)
    assert not in_window(instant, LA, POLICY)


def test_narrowed_window_excludes_monday_friday_and_weekend() -> None:
    for day in (19, 23, 24, 25):  # Mon, Fri, Sat, Sun in Oct 2026
        assert not in_window(resolve_local(datetime(2026, 10, day, 9, 0), NY), NY, POLICY)


def test_window_offsets_follow_dst() -> None:
    before = next_window(utc(2026, 3, 2, 0, 0), NY, POLICY)  # Tue 3 Mar 2026, EST
    after = next_window(utc(2026, 3, 9, 0, 0), NY, POLICY)  # Tue 10 Mar 2026, EDT
    assert before.start == utc(2026, 3, 3, 13, 0)
    assert after.start == utc(2026, 3, 10, 12, 0)


def test_always_open_policy_only_widens_window() -> None:
    policy = WindowPolicy(always_open=True)
    sunday_night = utc(2026, 10, 25, 3, 0)
    assert in_window(sunday_night, NY, policy)
    assert next_window(sunday_night, NY, policy).start == sunday_night


def test_invalid_policy_rejected() -> None:
    with pytest.raises(ValueError):
        WindowPolicy(weekdays=frozenset())
    with pytest.raises(ValueError):
        WindowPolicy(start=time(11), end=time(8))


# -- time zones (acceptance 14) -----------------------------------------------------


@pytest.mark.parametrize("name", [None, "", "Mars/Olympus_Mons"])
def test_unknown_or_missing_zone_blocks_scheduling(name: str | None) -> None:
    with pytest.raises(SchedulingError):
        load_zone(name)


# -- DST resolution ---------------------------------------------------------------


def test_nonexistent_local_time_moves_forward_by_gap() -> None:
    resolved = resolve_local(datetime(2026, 3, 8, 2, 30), NY)
    assert resolved.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)


def test_ambiguous_local_time_uses_first_occurrence() -> None:
    resolved = resolve_local(datetime(2026, 11, 1, 1, 30), NY)
    assert resolved == utc(2026, 11, 1, 5, 30)  # 01:30 EDT, not 01:30 EST


# -- due dates (acceptance 12, decision C8) ---------------------------------------------


def test_steps_2_and_3_are_exactly_seven_times_24_hours_even_across_dst() -> None:
    sent = utc(2026, 3, 3, 14, 0)  # 09:00 EST
    due = due_for_step(2, sent, NY, LastInterval())
    assert due - sent == timedelta(hours=168)
    assert due.astimezone(NY).hour == 10  # wall clock shifts; the 7x24 h rule is exact
    assert due_for_step(3, due, NY, LastInterval()) - due == timedelta(hours=168)


@pytest.mark.parametrize(
    ("sent_local", "expected_local"),
    [
        (datetime(2027, 1, 31, 9, 15), datetime(2027, 2, 28, 9, 15)),
        (datetime(2028, 1, 31, 9, 15), datetime(2028, 2, 29, 9, 15)),
        (datetime(2026, 3, 31, 10, 0), datetime(2026, 4, 30, 10, 0)),
        (datetime(2026, 12, 31, 8, 30), datetime(2027, 1, 31, 8, 30)),
        (datetime(2026, 2, 10, 9, 0), datetime(2026, 3, 10, 9, 0)),  # crosses spring-forward
        (datetime(2026, 10, 1, 10, 0), datetime(2026, 11, 1, 10, 0)),  # crosses fall-back
    ],
)
def test_step4_calendar_month_in_pi_zone(sent_local: datetime, expected_local: datetime) -> None:
    sent = resolve_local(sent_local, NY)
    due = due_for_step(4, sent, NY, LastInterval())
    assert due.astimezone(NY).replace(tzinfo=None) == expected_local


def test_step4_month_computed_in_pi_zone_not_utc() -> None:
    # 2027-01-31 23:30 in Los Angeles is already Feb 1 in UTC.
    sent = resolve_local(datetime(2027, 1, 31, 23, 30), LA)
    assert sent.astimezone(UTC).month == 2
    due = due_for_step(4, sent, LA, LastInterval())
    assert due.astimezone(LA).replace(tzinfo=None) == datetime(2027, 2, 28, 23, 30)


def test_step4_n_calendar_days_in_pi_zone_with_dst() -> None:
    interval = LastInterval(LastIntervalMode.N_DAYS, 10)
    sent = resolve_local(datetime(2026, 3, 1, 9, 0), NY)  # EST
    due = due_for_step(4, sent, NY, interval)
    assert due.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 11, 9, 0)  # EDT
    assert due - sent == timedelta(days=10) - timedelta(hours=1)


def test_step4_n_days_nonexistent_target_moves_forward() -> None:
    interval = LastInterval(LastIntervalMode.N_DAYS, 7)
    sent = resolve_local(datetime(2026, 3, 1, 2, 30), NY)
    due = due_for_step(4, sent, NY, interval)
    assert due.astimezone(NY).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 30)


def test_override_intervals_apply_only_when_given() -> None:
    sent = utc(2026, 10, 20, 13, 0)
    assert due_for_step(2, sent, NY, LastInterval(), {2: 600}) == sent + timedelta(minutes=10)
    with pytest.raises(ValueError):
        due_for_step(2, sent, NY, LastInterval(), {2: 0})


def test_due_for_step_rejects_step_1_and_naive() -> None:
    with pytest.raises(ValueError):
        due_for_step(1, utc(2026, 1, 1), NY, LastInterval())
    with pytest.raises(ValueError):
        due_for_step(2, datetime(2026, 1, 1), NY, LastInterval())


def test_n_day_interval_must_be_positive() -> None:
    with pytest.raises(ValueError):
        LastInterval(LastIntervalMode.N_DAYS, 0)


# -- blackouts (acceptance 18) ------------------------------------------------------------


def test_blackouts_off_by_default() -> None:
    thanksgiving_morning = resolve_local(datetime(2026, 11, 26, 9, 0), NY)
    assert in_window(thanksgiving_morning, NY, POLICY)


def test_federal_holiday_blackout_defers_to_next_window() -> None:
    policy = replace(POLICY, blackout_federal_holidays=True)
    thanksgiving_morning = resolve_local(datetime(2026, 11, 26, 9, 0), NY)
    assert not in_window(thanksgiving_morning, NY, policy)
    assert next_window(thanksgiving_morning, NY, policy).start == resolve_local(
        datetime(2026, 12, 1, 8, 0), NY
    )


def test_year_end_blackout_wraps_the_new_year() -> None:
    policy = WindowPolicy(blackout_year_end=True)
    window = next_window(resolve_local(datetime(2026, 12, 21, 12, 0), NY), NY, policy)
    assert window.start.astimezone(NY).date() == date(2027, 1, 6)


def test_blackout_never_changes_due_dates() -> None:
    sent = resolve_local(datetime(2026, 11, 19, 9, 0), NY)
    due = due_for_step(2, sent, NY, LastInterval())
    assert due == sent + timedelta(hours=168)  # Thanksgiving: due is unchanged


def test_observed_federal_holidays() -> None:
    assert date(2026, 7, 3) in federal_holidays_observed(2026)  # July 4 2026 is a Saturday
    assert is_federal_holiday(date(2027, 12, 31))  # Jan 1 2028 is a Saturday
    assert is_federal_holiday(date(2026, 11, 26))  # Thanksgiving
    assert not is_federal_holiday(date(2026, 11, 25))


def test_month_day_parsing() -> None:
    assert MonthDay.parse("12-20") == MonthDay(12, 20)
    with pytest.raises(ValueError):
        MonthDay.parse("13-01")


# -- property tests (decision D4) -------------------------------------------------------

instants = st.datetimes(
    min_value=datetime(2025, 1, 1), max_value=datetime(2030, 12, 31), timezones=st.just(UTC)
)
zones = st.sampled_from(
    [
        NY,
        LA,
        ZoneInfo("America/Chicago"),
        ZoneInfo("America/Denver"),
        ZoneInfo("America/Phoenix"),
        ZoneInfo("Pacific/Honolulu"),
        ZoneInfo("America/Anchorage"),
    ]
)


@settings(max_examples=300, deadline=None)
@given(after=instants, tz=zones, policy=st.sampled_from([POLICY, DEFAULT_POLICY]))
def test_next_window_is_valid_and_earliest(
    after: datetime, tz: ZoneInfo, policy: WindowPolicy
) -> None:
    window = next_window(after, tz, policy)
    local_start = window.start.astimezone(tz)
    assert window.end > after
    assert local_start.weekday() in policy.weekdays
    assert local_start.time() == time(8, 0)
    assert window.end.astimezone(tz).time() == policy.end
    probe = after
    while probe < window.start:  # no permitted instant before the returned window
        assert not in_window(probe, tz, policy)
        probe += timedelta(hours=1)


@settings(max_examples=300, deadline=None)
@given(sent=instants, tz=zones)
def test_calendar_month_properties(sent: datetime, tz: ZoneInfo) -> None:
    due = due_for_step(4, sent, tz, LastInterval())
    local_sent, local_due = sent.astimezone(tz), due.astimezone(tz)
    assert timedelta(days=27) < due - sent < timedelta(days=32)
    assert local_due.day <= local_sent.day
    assert (local_due.year * 12 + local_due.month) - (local_sent.year * 12 + local_sent.month) == 1


@settings(max_examples=200, deadline=None)
@given(local=st.datetimes(min_value=datetime(2025, 1, 1), max_value=datetime(2030, 12, 1)))
def test_add_calendar_month_clamps(local: datetime) -> None:
    result = add_calendar_month(local)
    assert result.time() == local.time()
    assert result.day == local.day or result.day < local.day
