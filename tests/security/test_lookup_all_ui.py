"""'Look up all' on the Candidates page: starts the background run, shows progress."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post
from tests.security.test_ui_security import ORIGIN, PORT, sign_in


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher(), lookup_pause=0)
    return TestClient(app, base_url=ORIGIN)


def test_look_up_all_runs_in_the_background(client, security, world) -> None:
    csrf = sign_in(client, security)
    inst = world.institution()
    for i in range(2):
        world.pi(inst, name=f"Pat Example{i}")
    page = client.get("/candidates").text
    assert "Look up all (2)" in page
    assert client.post("/candidates/lookup-all", headers={"Origin": ORIGIN}).status_code == 403
    response = post(client, "/candidates/lookup-all", csrf)
    assert "Looking+up+2+contacts" in response.headers["location"]
    deadline = time.monotonic() + 10
    while "Every contact has recent publications" not in client.get("/candidates").text:
        assert time.monotonic() < deadline, "the background lookup did not finish"
        time.sleep(0.05)
    assert "Last run: 2 of 2 looked up" in client.get("/candidates").text
    response = post(client, "/candidates/lookup-all", csrf)
    assert "Nothing+to+do" in response.headers["location"]
    assert post(client, "/candidates/lookup-all/stop", csrf).status_code == 303
