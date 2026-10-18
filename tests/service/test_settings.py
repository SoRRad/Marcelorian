"""Adjustable settings (decision B16, acceptance 67)."""

from __future__ import annotations

import json
import math
from datetime import time

import pytest
from sqlalchemy import select

from marcelorian.config.settings import (
    DEFAULTS,
    ConfirmationRequired,
    SettingsError,
    _window_widened,
    load_settings,
    update_settings,
)
from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent
from marcelorian.domain.enums import Actor
from marcelorian.domain.timing import WindowPolicy


def test_defaults_match_spec() -> None:
    assert DEFAULTS.real_submissions_per_24h == 25
    assert DEFAULTS.pending_per_institution == 2
    assert DEFAULTS.pending_per_lab == 1
    assert DEFAULTS.new_contacts_per_institution_30d == 3
    assert (DEFAULTS.pacing_min_seconds, DEFAULTS.pacing_max_seconds) == (120, 360)
    # Owner W1 (2026-10-17): the default window is the widest allowed one.
    assert DEFAULTS.window_weekdays == (0, 1, 2, 3, 4)
    assert (DEFAULTS.window_start, DEFAULTS.window_end) == ("08:00", "16:00")
    assert not DEFAULTS.blackout_federal_holidays and not DEFAULTS.blackout_year_end
    assert not DEFAULTS.hold_slot_14d_after_email4


def test_increase_above_default_requires_confirmation_and_is_audited(factory, clock) -> None:
    with transaction(factory) as s:
        with pytest.raises(ConfirmationRequired) as info:
            update_settings(
                s, {"real_submissions_per_24h": 40}, actor=Actor.APPLICANT, now=clock.now()
            )
        assert info.value.keys == ["real_submissions_per_24h"]
    with transaction(factory) as s:
        update_settings(
            s,
            {"real_submissions_per_24h": 40},
            actor=Actor.APPLICANT,
            now=clock.now(),
            confirm_less_conservative=True,
        )
    with transaction(factory) as s:
        assert load_settings(s).real_submissions_per_24h == 40
        event = s.scalars(select(AuditEvent).where(AuditEvent.action == "settings_changed")).one()
        assert json.loads(event.detail_json)["changes"]["real_submissions_per_24h"] == [25, 40]


def test_lowering_needs_no_confirmation(factory, clock) -> None:
    with transaction(factory) as s:
        update_settings(
            s,
            {"pending_per_institution": 1, "pacing_min_seconds": 300},
            actor=Actor.APPLICANT,
            now=clock.now(),
        )
        assert load_settings(s).pending_per_institution == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"window_start": "07:00"},
        {"window_start": "07:59"},
        {"window_end": "16:01"},
        {"window_end": "17:00"},
        {"window_weekdays": [0, 1, 2, 3, 4, 5]},
        {"window_weekdays": [6]},
    ],
)
def test_window_cannot_leave_the_outer_bound(factory, clock, changes) -> None:
    # Spec §1 (W1): adjustable within Mon-Fri 08:00-16:00 PI local time, never beyond.
    with transaction(factory) as s, pytest.raises(SettingsError) as info:
        update_settings(
            s, changes, actor=Actor.APPLICANT, now=clock.now(), confirm_less_conservative=True
        )
    assert not isinstance(info.value, ConfirmationRequired)


NARROWED = {"window_weekdays": [1, 2, 3], "window_start": "08:00", "window_end": "11:00"}


@pytest.mark.parametrize(
    "changes",
    [
        {"window_end": "16:00"},
        {"window_weekdays": [0, 1, 2, 3, 4]},
        {"window_weekdays": [0, 1, 2, 3, 4], "window_end": "16:00"},
    ],
)
def test_widening_back_within_the_default_needs_no_confirmation_and_is_audited(
    factory, clock, changes
) -> None:
    # The default is the outer bound, so no window inside it is less conservative than
    # the default (as with caps, only values beyond the default need confirmation).
    with transaction(factory) as s:
        update_settings(s, NARROWED, actor=Actor.APPLICANT, now=clock.now())
    with transaction(factory) as s:
        update_settings(s, changes, actor=Actor.APPLICANT, now=clock.now())
    with transaction(factory) as s:
        settings = load_settings(s)
        for key, value in changes.items():
            expected = tuple(value) if isinstance(value, list) else value
            assert getattr(settings, key) == expected
        events = s.scalars(
            select(AuditEvent)
            .where(AuditEvent.action == "settings_changed")
            .order_by(AuditEvent.id)
        ).all()
        assert set(json.loads(events[-1].detail_json)["changes"]) == set(changes)


def test_narrowing_the_window_is_allowed(factory, clock) -> None:
    with transaction(factory) as s:
        update_settings(
            s,
            {"window_weekdays": [2], "window_start": "09:00", "window_end": "10:30"},
            actor=Actor.APPLICANT,
            now=clock.now(),
        )
        assert load_settings(s).window_weekdays == (2,)


@pytest.mark.parametrize(
    "changes",
    [
        {"pacing_min_seconds": 60},
        {"pending_per_lab": 2},
    ],
)
def test_less_conservative_changes_need_confirmation(factory, clock, changes) -> None:
    with transaction(factory) as s, pytest.raises(ConfirmationRequired):
        update_settings(s, changes, actor=Actor.APPLICANT, now=clock.now())


@pytest.mark.parametrize(
    "changes",
    [
        {"real_submissions_per_24h": -1},
        {"real_submissions_per_24h": math.inf},
        {"real_submissions_per_24h": math.nan},
        {"real_submissions_per_24h": 2.5},
        {"real_submissions_per_24h": "25"},
        {"real_submissions_per_24h": True},
        {"pacing_min_seconds": 0},
        {"pacing_min_seconds": 400, "pacing_max_seconds": 300},
        {"window_start": "11:00", "window_end": "08:00"},
        {"window_weekdays": []},
        {"window_weekdays": [7]},
        {"last_interval_mode": "weekly"},
        {"last_interval_days": 0},
        {"last_interval_days": 3_000_000},
        {"blackout_year_end": True, "year_end_start": "12-20", "year_end_end": "12-19"},
        {"year_end_start": "13-40"},
        {"debug_always_open": True},
    ],
)
def test_invalid_values_rejected(factory, clock, changes) -> None:
    with transaction(factory) as s, pytest.raises(SettingsError):
        update_settings(
            s, changes, actor=Actor.APPLICANT, now=clock.now(), confirm_less_conservative=True
        )


def test_no_setting_can_disable_safety_controls() -> None:
    names = set(DEFAULTS.__dataclass_fields__)
    forbidden = {"approval", "recipient", "uncertain", "reply", "debug", "override", "always_open"}
    assert not {n for n in names if any(f in n for f in forbidden)}


_NARROW_DEFAULT = WindowPolicy(weekdays=frozenset({1, 2, 3}), end=time(11))


@pytest.mark.parametrize(
    ("old", "new", "widened"),
    [
        # afternoon-only, then back to an 08:00 start: 11:00-13:00 is in neither window
        (
            WindowPolicy(weekdays=frozenset({1, 2, 3}), start=time(13), end=time(16)),
            WindowPolicy(weekdays=frozenset({1, 2, 3}), end=time(16)),
            True,
        ),
        # Tuesday-only 08:00-16:00, then Tue-Thu 08:00-16:00: Wed/Thu afternoons are new
        (
            WindowPolicy(weekdays=frozenset({1}), end=time(16)),
            WindowPolicy(weekdays=frozenset({1, 2, 3}), end=time(16)),
            True,
        ),
        # default 08:00-11:00 and old 11:00-16:00 together cover 08:00-16:00
        (
            WindowPolicy(weekdays=frozenset({1, 2, 3}), start=time(11), end=time(16)),
            WindowPolicy(weekdays=frozenset({1, 2, 3}), end=time(16)),
            False,
        ),
    ],
)
def test_window_widening_is_judged_by_permitted_slots(old, new, widened) -> None:
    """Slot-based check, exercised against a narrower default than the shipped one."""
    assert _window_widened(new, _NARROW_DEFAULT, old) is widened


@pytest.mark.parametrize(
    ("first", "then"),
    [
        # narrowed to afternoons, then back to an 08:00 start (within the default)
        ({"window_start": "11:00", "window_end": "16:00"}, {"window_start": "08:00"}),
        # narrowed, then back to the default end
        ({"window_end": "11:00"}, {"window_end": "16:00"}),
    ],
)
def test_returning_within_default_and_old_windows_needs_no_confirmation(
    factory, clock, first, then
) -> None:
    with transaction(factory) as s:
        update_settings(
            s, first, actor=Actor.APPLICANT, now=clock.now(), confirm_less_conservative=True
        )
    with transaction(factory) as s:
        update_settings(s, then, actor=Actor.APPLICANT, now=clock.now())
