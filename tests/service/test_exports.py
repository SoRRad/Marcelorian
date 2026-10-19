"""Exports (spec §13; U4): formula safety, exact round trip, atomic writes, stale handling."""

from __future__ import annotations

import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Mode
from marcelorian.services import exports, worker
from tests.service.test_drafting import confirm_facts
from tests.service.test_outreach_status import started

TRICKY = [
    '=HYPERLINK("x")',
    "+1",
    "-2",
    "@SUM(A1)",
    "\tTab",
    "\rReturn",
    "'quoted",
    "''double",
    "plain",
    "",
    'multi\nline, with "quotes"',
]


@pytest.mark.parametrize("value", TRICKY)
def test_escape_is_reversible(value: str) -> None:
    escaped = exports.escape(value)
    assert not escaped.startswith(("=", "+", "-", "@", "\t", "\r"))
    assert exports.unescape(escaped) == value


@given(st.lists(st.text(max_size=40), min_size=1, max_size=6))
def test_csv_round_trip_is_exact(values: list[str]) -> None:
    columns = [f"c{i}" for i in range(len(values))]
    data = exports.render_csv(columns, [dict(zip(columns, values, strict=True))])
    assert data.startswith("﻿".encode())
    header, row = exports.parse(data)
    assert header == columns and row == values


def test_files_for_a_started_campaign(world) -> None:
    confirm_facts(world)
    campaign_id = started(world, world.pi(world.institution(), name="Pat Example"))
    with transaction(world.factory) as s:
        files = exports.build_files(s, Mode.SANDBOX, "institution", world.clock.now())
    outreach = exports.parse(files["outreach_contacts.csv"])
    row = dict(zip(outreach[0], outreach[1], strict=True))
    assert row["campaign_id"] == str(campaign_id) and row["pi_name"] == "Pat Example"
    assert row["email1_sent_at"] and row["next_action"] == "Email 2 when due"
    log = exports.parse(files["email_log.csv"])
    entry = dict(zip(log[0], log[1], strict=True))
    assert entry["step"] == "1" and entry["body"].startswith("Dear ") and entry["outcome"] == "sent"
    owner = exports.parse(files["owner_report.csv"])
    assert owner[0] == list(exports.OWNER_COLUMNS)
    assert "pi_email" not in owner[0] and "notes" not in owner[0]  # allowlist only
    summary = dict(exports.parse(files["status_summary.csv"])[1:])
    assert summary["Applications contacted"] == "1"


def test_refresh_writes_mode_folders_and_skips_when_unchanged(world, tmp_path) -> None:
    started(world, world.pi(world.institution()))
    state = exports.refresh(world.factory, tmp_path, world.clock)
    assert not state.stale and state.last_success_at
    assert (tmp_path / "practice" / "owner_report.csv").is_file()
    assert (tmp_path / "owner_report.csv").is_file()  # live files exist even when empty
    before = (tmp_path / "practice" / "email_log.csv").stat().st_mtime_ns
    exports.refresh(world.factory, tmp_path, world.clock)
    assert (tmp_path / "practice" / "email_log.csv").stat().st_mtime_ns == before


def test_locked_file_marks_stale_then_recovers(world, tmp_path, monkeypatch) -> None:
    started(world, world.pi(world.institution()))
    real_replace = os.replace

    def locked(src, dst):
        if str(dst).endswith("owner_report.csv"):
            raise PermissionError("open in Excel")
        return real_replace(src, dst)

    monkeypatch.setattr(exports.os, "replace", locked)
    state = exports.refresh(world.factory, tmp_path, world.clock)
    assert state.stale and "owner_report.csv" in state.error
    assert not list(tmp_path.rglob("*.tmp"))  # temp files cleaned up
    with transaction(world.factory) as s:
        now = world.clock.now()
        assert "exports_stale" in {n.key for n in worker.notices(s, now=now, campaign_now=now)}
    monkeypatch.setattr(exports.os, "replace", real_replace)
    assert not exports.refresh(world.factory, tmp_path, world.clock).stale  # retried
