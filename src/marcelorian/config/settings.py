"""Adjustable settings with validation (spec §11, §16; decision B16).

* Defaults: 25 real submissions per rolling 24 h, 2 pending per institution,
  1 per lab, 3 new contacts per institution per rolling 30 days, pacing 2-6 min.
* No hardcoded ceilings. Values must be finite, sensible integers.
* Any change that is *less conservative than its default* (a higher cap, a
  shorter pacing gap, a wider window) needs explicit confirmation. Every
  change is audited.
* There are no debug settings here: schedule overrides exist only inside an
  approved transport_test snapshot (B2), so nothing in settings can leak into
  live mode or disable approval, recipient validation, uncertainty handling or
  reply checks.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Setting
from marcelorian.domain.enums import Actor
from marcelorian.domain.timing import LastInterval, LastIntervalMode, MonthDay, WindowPolicy
from marcelorian.services.audit import audit


class SettingsError(ValueError):
    pass


class ConfirmationRequired(SettingsError):
    """A change is less conservative than its default and was not confirmed."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__("explicit confirmation required for: " + ", ".join(sorted(keys)))
        self.keys = sorted(keys)


@dataclass(frozen=True)
class AppSettings:
    pending_per_institution: int = 2
    pending_per_lab: int = 1
    new_contacts_per_institution_30d: int = 3
    real_submissions_per_24h: int = 25
    sandbox_submissions_per_24h: int = 25
    pacing_min_seconds: int = 120
    pacing_max_seconds: int = 360
    window_weekdays: tuple[int, ...] = (1, 2, 3)  # Tue, Wed, Thu (Mon=0)
    window_start: str = "08:00"
    window_end: str = "11:00"
    last_interval_mode: str = LastIntervalMode.CALENDAR_MONTH.value
    last_interval_days: int = 30
    blackout_federal_holidays: bool = False
    blackout_year_end: bool = False
    year_end_start: str = "12-20"
    year_end_end: str = "01-05"
    hold_slot_14d_after_email4: bool = False

    def window_policy(self) -> WindowPolicy:
        return WindowPolicy(
            weekdays=frozenset(self.window_weekdays),
            start=_parse_time(self.window_start),
            end=_parse_time(self.window_end),
            blackout_federal_holidays=self.blackout_federal_holidays,
            blackout_year_end=self.blackout_year_end,
            year_end_start=MonthDay.parse(self.year_end_start),
            year_end_end=MonthDay.parse(self.year_end_end),
        )

    def last_interval(self) -> LastInterval:
        return LastInterval(LastIntervalMode(self.last_interval_mode), self.last_interval_days)


DEFAULTS = AppSettings()
_CAP_KEYS = (
    "pending_per_institution",
    "pending_per_lab",
    "new_contacts_per_institution_30d",
    "real_submissions_per_24h",
    "sandbox_submissions_per_24h",
)
_FIELD_NAMES = {f.name for f in fields(AppSettings)}


def _parse_time(value: str) -> time:
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError) as exc:
        raise SettingsError(f"invalid time {value!r}; use HH:MM") from exc


def _strict_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise SettingsError(f"{name} must be a number")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise SettingsError(f"{name} must be a finite whole number")
        value = int(value)
    return int(value)


def validate(settings: AppSettings) -> AppSettings:
    """Return a normalized copy or raise ``SettingsError``."""
    values: dict[str, Any] = {}
    for key in _CAP_KEYS:
        number = _strict_int(key, getattr(settings, key))
        if number < 0:
            raise SettingsError(f"{key} must not be negative")
        values[key] = number
    pmin = _strict_int("pacing_min_seconds", settings.pacing_min_seconds)
    pmax = _strict_int("pacing_max_seconds", settings.pacing_max_seconds)
    if pmin <= 0 or pmax <= 0:
        raise SettingsError("pacing must stay positive")
    if pmin > pmax:
        raise SettingsError("pacing minimum must not exceed pacing maximum")
    values["pacing_min_seconds"], values["pacing_max_seconds"] = pmin, pmax
    days = _strict_int("last_interval_days", settings.last_interval_days)
    if days < 1:
        raise SettingsError("last_interval_days must be at least 1")
    values["last_interval_days"] = days
    try:
        LastIntervalMode(settings.last_interval_mode)
    except ValueError as exc:
        raise SettingsError("last_interval_mode must be calendar_month or n_days") from exc
    weekdays = tuple(sorted({_strict_int("window_weekdays", d) for d in settings.window_weekdays}))
    values["window_weekdays"] = weekdays
    for key in ("blackout_federal_holidays", "blackout_year_end", "hold_slot_14d_after_email4"):
        if not isinstance(getattr(settings, key), bool):
            raise SettingsError(f"{key} must be true or false")
    try:
        MonthDay.parse(settings.year_end_start)
        MonthDay.parse(settings.year_end_end)
    except (ValueError, TypeError) as exc:
        raise SettingsError("year-end blackout dates must be MM-DD") from exc
    normalized = replace(settings, **values)
    try:
        normalized.window_policy()
    except ValueError as exc:
        raise SettingsError(str(exc)) from exc
    return normalized


def less_conservative_keys(new: AppSettings, old: AppSettings) -> list[str]:
    """Keys whose new value is less conservative than both the default and the old value."""
    keys: list[str] = []
    for key in _CAP_KEYS:
        value = getattr(new, key)
        if value > getattr(DEFAULTS, key) and value > getattr(old, key):
            keys.append(key)
    for key in ("pacing_min_seconds", "pacing_max_seconds"):
        value = getattr(new, key)
        if value < getattr(DEFAULTS, key) and value < getattr(old, key):
            keys.append(key)
    new_policy, default_policy, old_policy = (
        new.window_policy(),
        DEFAULTS.window_policy(),
        old.window_policy(),
    )
    if not new_policy.weekdays <= default_policy.weekdays | old_policy.weekdays:
        keys.append("window_weekdays")
    if new_policy.start < min(default_policy.start, old_policy.start):
        keys.append("window_start")
    if new_policy.end > max(default_policy.end, old_policy.end):
        keys.append("window_end")
    return keys


def load_settings(session: Session) -> AppSettings:
    rows = session.scalars(select(Setting)).all()
    stored = {row.key: json.loads(row.value_json) for row in rows if row.key in _FIELD_NAMES}
    if "window_weekdays" in stored:
        stored["window_weekdays"] = tuple(stored["window_weekdays"])
    return validate(replace(DEFAULTS, **stored))


def update_settings(
    session: Session,
    changes: dict[str, Any],
    *,
    actor: Actor,
    now: datetime,
    confirm_less_conservative: bool = False,
) -> AppSettings:
    unknown = set(changes) - _FIELD_NAMES
    if unknown:
        raise SettingsError("unknown settings: " + ", ".join(sorted(unknown)))
    old = load_settings(session)
    if "window_weekdays" in changes:
        changes = {**changes, "window_weekdays": tuple(changes["window_weekdays"])}
    new = validate(replace(old, **changes))
    risky = less_conservative_keys(new, old)
    if risky and not confirm_less_conservative:
        raise ConfirmationRequired(risky)
    old_values, new_values = asdict(old), asdict(new)
    changed = {k: new_values[k] for k in changes if new_values[k] != old_values[k]}
    for key, value in changed.items():
        row = session.get(Setting, key)
        encoded = json.dumps(value)
        if row is None:
            session.add(Setting(key=key, value_json=encoded, updated_at=now))
        else:
            row.value_json, row.updated_at = encoded, now
    if changed:
        audit(
            session,
            at=now,
            actor=actor,
            entity_type="settings",
            entity_id="app",
            action="settings_changed",
            detail={
                "changes": {k: [old_values[k], new_values[k]] for k in changed},
                "confirmed_less_conservative": risky,
            },
        )
    return new
