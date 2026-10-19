"""Your data page: exports, downloads, backups, restore staging, delete everything (P4)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from marcelorian.platform.paths import AppPaths
from marcelorian.services import backup, deletion
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post
from tests.security.test_ui_security import ORIGIN, PORT, sign_in


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def paths(tmp_path: Path, world) -> AppPaths:
    # The world's database lives elsewhere; a real file here lets backups run.
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "marcelorian.db").close()
    return AppPaths(data, tmp_path / "exports", exports_synced_by_onedrive=True)


@pytest.fixture
def stops() -> list[int]:
    return []


@pytest.fixture
def client(world, security, paths, stops):
    app = create_app(
        security,
        world.factory,
        world.clock,
        fetcher=FakeFetcher(),
        paths=paths,
        request_shutdown=lambda: stops.append(1),
    )
    return TestClient(app, base_url=ORIGIN)


def test_data_page_exports_and_downloads(client, security, paths) -> None:
    csrf = sign_in(client, security)
    page = client.get("/data").text
    assert "Reports for the program owner" in page and "synced by OneDrive" in page
    response = post(client, "/data/export", csrf)
    assert "Exports+updated" in response.headers["location"]
    download = client.get("/data/exports/live/owner_report.csv")
    assert download.status_code == 200
    assert download.content.startswith("﻿".encode())
    assert "attachment" in download.headers["content-disposition"]
    assert client.get("/data/exports/live/..%2Fsecrets.csv").status_code == 404
    assert client.get("/data/exports/nope/owner_report.csv").status_code == 404
    assert client.post("/data/export", headers={"Origin": ORIGIN}).status_code == 403


def test_backup_and_restore_staging(client, security, paths) -> None:
    csrf = sign_in(client, security)
    response = post(client, "/data/backup", csrf)
    assert "Backup+saved" in response.headers["location"]
    name = backup.list_backups(paths.backups_dir)[0].name
    response = post(client, "/data/restore", csrf, name=name)
    assert "Tick+the+box" in response.headers["location"]
    response = post(client, "/data/restore", csrf, name=name, confirm="yes")
    assert "Restore+is+ready" in response.headers["location"]
    assert backup.restore_pending(paths.data_dir)
    assert "A restore is waiting" in client.get("/data").text
    post(client, "/data/restore/cancel", csrf)
    assert not backup.restore_pending(paths.data_dir)


def test_delete_needs_the_word_then_stops_the_app(client, security, paths, stops) -> None:
    csrf = sign_in(client, security)
    response = post(client, "/data/delete", csrf, word="delete", confirm="yes")
    assert response.status_code == 303 and not deletion.requested(paths.data_dir)
    response = post(client, "/data/delete", csrf, word="DELETE", confirm="yes")
    assert response.status_code == 200 and "What stays outside the app" in response.text
    assert deletion.requested(paths.data_dir) and stops == [1]


def test_without_paths_the_page_says_so(world, security) -> None:
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    client = TestClient(app, base_url=ORIGIN)
    csrf = sign_in(client, security)
    assert "not available" in client.get("/data").text
    assert post(client, "/data/backup", csrf).status_code == 404
