"""Settings: readiness checklist, mode switch, practice time, templates; practice follow-up."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.domain.enums import Mode
from marcelorian.services import practice, runtime
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post, start
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_drafting import PERSONAL, confirm_facts


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def mode(world) -> str:
    with transaction(world.factory) as s:
        return runtime.get_runtime(s, world.clock.now()).active_mode.value


def test_live_needs_the_checklist(client, security, world) -> None:
    csrf = sign_in(client, security)
    page = client.get("/settings").text
    assert "Before live mode" in page and "The real Outlook test (P0.3b) passed" in page
    response = post(client, "/settings/mode", csrf, mode="live", confirm="yes")
    assert "Complete+the+checklist" in response.headers["location"]
    assert mode(world) == "sandbox"
    confirm_facts(world)
    world.signature_step("")
    world.connect_handoff_mailbox()
    for key in practice.ATTESTATIONS:
        post(client, "/settings/readiness", csrf, key=key, value="yes")
    response = post(client, "/settings/mode", csrf, mode="live", confirm="yes", resume="yes")
    assert "Switched+to+live" in response.headers["location"]
    assert mode(world) == "live"
    post(client, "/settings/readiness", csrf, key="limits_reviewed", value="no")
    assert mode(world) == "sandbox"  # withdrawing readiness leaves live mode
    assert post(client, "/settings/mode", csrf, mode="live").headers["location"].count("error") == 1


def test_practice_time_and_a_practice_follow_up(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    _pi, campaign_id = start(client, csrf, world)
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **PERSONAL)
    message_url = response.headers["location"]
    post(client, f"{message_url}/approve", csrf)
    post(client, f"{message_url}/practice-send", csrf)
    assert "Draft Email 2" not in client.get(f"/campaigns/{campaign_id}").text
    for _ in range(2):
        post(client, "/settings/practice-time", csrf, step="week")
    assert "days ahead of real time" in client.get("/settings").text
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Draft Email 2" in page
    draft = client.get(f"/campaigns/{campaign_id}/draft").text
    assert "Email 2 to Pat Example" in draft
    with transaction(world.factory) as s:
        assert practice.load_offset(s).days == 14


def test_practice_time_only_in_practice_and_templates(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    world.set_mode(Mode.LIVE)
    client2 = TestClient(create_app(security, world.factory, world.clock), base_url=ORIGIN)
    csrf2 = sign_in(client2, security)
    response = post(client2, "/settings/practice-time", csrf2, step="day")
    assert "only+in+practice+mode" in response.headers["location"]
    response = post(client, "/settings/template/2", csrf, text="Hello {nickname}")
    assert "unknown+placeholders" in response.headers["location"]
    post(
        client,
        "/settings/template/2",
        csrf,
        text="Dear {salutation},\n\nA short note on {pi_topic}.\n\n{signature_name}",
    )
    assert "A short note on" in client.get("/settings").text
    assert post(client, "/settings/mode", csrf, mode="moon", confirm="yes").status_code == 400
