"""The launcher: database preparation, sign-in link, single instance."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marcelorian.app_main import free_port, main, open_in_browser, prepare_database
from marcelorian.clock import ManualClock
from marcelorian.db.engine import transaction
from marcelorian.db.models import ApplicantProfile, RuntimeState
from marcelorian.platform.instance_lock import InstanceLock
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.conftest import TUESDAY_0900_NY


def test_prepare_database_migrates_and_seeds_first_run(tmp_path: Path) -> None:
    clock = ManualClock(TUESDAY_0900_NY)
    factory = prepare_database(tmp_path / "data" / "app.db", clock)
    prepare_database(tmp_path / "data" / "app.db", clock)  # idempotent on restart
    with transaction(factory) as s:
        assert s.scalar(select(func.count()).select_from(ApplicantProfile)) == 1
        assert s.get(RuntimeState, 1).active_mode == "sandbox"


def test_sign_in_link_opens_a_session(tmp_path: Path) -> None:
    clock = ManualClock(TUESDAY_0900_NY)
    factory = prepare_database(tmp_path / "app.db", clock)
    port = free_port()
    security = UISecurity("127.0.0.1", port, clock)
    opened: list[str] = []
    open_in_browser(security, opened.append)
    assert opened[0].startswith(f"http://127.0.0.1:{port}/bootstrap#t=")
    token = opened[0].split("#t=", 1)[1]
    origin = f"http://127.0.0.1:{port}"
    client = TestClient(create_app(security, factory, clock), base_url=origin)
    response = client.post("/bootstrap/exchange", json={"token": token}, headers={"Origin": origin})
    assert response.status_code == 200
    assert client.get("/").status_code == 200
    assert "Getting started" in client.get("/").text


def test_a_second_launch_is_refused(tmp_path: Path, capsys) -> None:
    lock = InstanceLock(tmp_path / "instance.lock").acquire()
    try:
        assert main(["--data-dir", str(tmp_path), "--no-browser"]) == 2
    finally:
        lock.release()
    assert "already running" in capsys.readouterr().out
