"""Notice bar API and the follow-ups section (P3)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.ui.app import create_app, worker_of
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_worker import due_campaign


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def test_notices_need_a_session(client) -> None:
    assert client.get("/api/notices").status_code in (401, 403)


def test_notices_list_due_followups(client, security, world) -> None:
    sign_in(client, security)
    empty = client.get("/api/notices")
    assert empty.json() == {"notices": [], "actions": 0}
    assert empty.headers["cache-control"] == "no-store"
    campaign_id = due_campaign(world)
    data = client.get("/api/notices").json()
    assert data["actions"] == 1
    assert data["notices"][0]["key"] == "followups_to_write"
    assert data["notices"][0]["url"] == f"/campaigns/{campaign_id}/draft"
    page = client.get("/approvals").text
    assert "Follow-ups due" in page and f"/campaigns/{campaign_id}/draft" in page
    assert 'id="notices"' in client.get("/").text


def test_worker_is_built_but_not_started_by_create_app(client) -> None:
    worker = worker_of(client.app)
    assert worker._thread is None  # the launcher starts it; tests stay single-threaded
