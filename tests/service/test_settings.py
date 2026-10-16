"""Adjustable settings (decision B16, acceptance 67)."""

from __future__ import annotations

import json
import math

import pytest
from sqlalchemy import select

from marcelorian.config.settings import (
    DEFAULTS,
    ConfirmationRequired,
    SettingsError,
    load_settings,
    update_settings,
)
from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent
from marcelorian.domain.enums import Actor


def test_defaults_match_spec() -> None:
    assert DEFAULTS.real_submissions_per_24h == 25
    assert DEFAULTS.pending_per_institution == 2
    assert DEFAULTS.pending_per_lab == 1
    assert DEFAULTS.new_contacts_per_institution_30d == 3
    assert (DEFAULTS.pacing_min_seconds, DEFAULTS.pacing_max_seconds) == (120, 360)
    assert DEFAULTS.window_weekdays == (1, 2, 3)
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
        {"pacing_min_seconds": 60},
        {"window_end": "12:00"},
        {"window_start": "07:00"},
        {"window_weekdays": [0, 1, 2, 3]},
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
