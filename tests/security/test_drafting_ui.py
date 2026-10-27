"""Drafting and approval pages: start, save, checklist, approve, revoke, campaign page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_drafting import PERSONAL, confirm_facts


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def post(client, url, csrf, **form):
    return client.post(
        url, data={**form, "csrf_token": csrf}, headers={"Origin": ORIGIN}, follow_redirects=False
    )


def start(client, csrf, world) -> tuple[int, int]:
    pi = world.pi(world.institution(), name="Pat Example")
    response = post(client, f"/candidates/{pi}/draft", csrf)
    campaign_id = int(response.headers["location"].split("/")[2])
    return pi, campaign_id


def test_draft_review_approve_flow(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    _pi, campaign_id = start(client, csrf, world)
    page = client.get(f"/campaigns/{campaign_id}/draft").text
    assert "Email 1 to Pat Example" in page and "Approve ✓" in page
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **PERSONAL)
    message_url = response.headers["location"]
    review = client.get(message_url).text
    assert (
        "Checklist" in review
        and "✗" not in review
        and "Your review is the final factual check" in review
    )
    response = post(client, f"{message_url}/approve", csrf)
    assert response.headers["location"].endswith("?notice=approved")
    page = client.get(response.headers["location"]).text
    assert "Approval never sends" in page and "Revoke approval" in page
    assert "Pat Example" in client.get("/approvals").text
    post(client, f"{message_url}/revoke", csrf)
    with transaction(world.factory) as s:
        assert s.get(Message, int(message_url.rsplit("/", 1)[1])).state == "awaiting_approval"
    assert "Campaign with Pat Example" in client.get(f"/campaigns/{campaign_id}").text


def test_practice_text_and_checklist_block(client, security, world) -> None:
    csrf = sign_in(client, security)
    _pi, campaign_id = start(client, csrf, world)  # no confirmed facts yet
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="practice")
    message_url = response.headers["location"]
    review = client.get(message_url).text
    assert "practice" in review and "✗" in review
    response = post(client, f"{message_url}/approve", csrf)
    assert "Fix+the+checklist+first" in response.headers["location"]
    with transaction(world.factory) as s:
        assert s.get(Campaign, campaign_id).state == "candidate"


def test_drafting_routes_need_csrf_and_real_ids(client, security, world) -> None:
    csrf = sign_in(client, security)
    assert client.post("/candidates/1/draft", headers={"Origin": ORIGIN}).status_code == 403
    assert post(client, "/candidates/999/draft", csrf).status_code == 404
    assert client.get("/messages/999").status_code == 404
    assert client.get("/campaigns/999/draft").status_code == 404


def test_lines_are_prefilled_and_approve_works_from_the_draft_page(client, security, world) -> None:
    from tests.service.test_drafting import add_evidence

    csrf = sign_in(client, security)
    confirm_facts(world)
    world.settings(strict_send_rules=False)
    _pi, campaign_id = start(client, csrf, world)
    add_evidence(world, campaign_id, "Synthetic choroid imaging in glaucoma progression")
    page = client.get(f"/campaigns/{campaign_id}/draft").text
    assert "I read your recent paper, “Synthetic choroid imaging in glaucoma progression”" in page
    assert "Other suggestions" in page or "data-fill" not in page
    assert 'name="body"' in page and "data-edited" in page
    form = {
        "personalized_sentence": "Your glaucoma progression work fits my training.",
        "skills_line": "OCT image analysis",
        "contribution_line": "your imaging projects",
        "edit_body": "no",
    }
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="approve", **form)
    assert response.headers["location"].endswith("?notice=approved#send"), response.headers
    message_id = int(response.headers["location"].split("/")[2].split("?")[0])
    assert world.message(message_id).state == "approved_queued"


def test_an_edited_email_box_is_kept_word_for_word(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    _pi, campaign_id = start(client, csrf, world)
    text = "Dear Dr. Example,\n\nMy own words, written in the preview box.\n\nBest regards,\nAlex"
    response = post(
        client,
        f"/campaigns/{campaign_id}/draft",
        csrf,
        action="save_here",
        body=text,
        edit_body="yes",
        **PERSONAL,
    )
    assert "Draft+saved" in response.headers["location"]
    with transaction(world.factory) as s:
        message = s.query(Message).filter_by(campaign_id=campaign_id).one()
        assert message.draft_body == text + "\n"
    assert (
        "My own words, written in the preview box."
        in client.get(f"/campaigns/{campaign_id}/draft").text
    )
