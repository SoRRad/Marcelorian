"""Send pages: practice send in sandbox, Outlook hand-off in live, replies, tasks."""

from __future__ import annotations

import email
from email.policy import default as default_policy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message
from marcelorian.domain.enums import Mode
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post, start
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_drafting import PERSONAL, add_evidence, confirm_facts


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def approved_message(client, csrf, world) -> tuple[int, int]:
    confirm_facts(world)
    _pi, campaign_id = start(client, csrf, world)
    add_evidence(world, campaign_id)  # the PI's work, so the personal line is tailored
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **PERSONAL)
    message_url = response.headers["location"]
    assert post(client, f"{message_url}/approve", csrf).status_code == 303
    return campaign_id, int(message_url.rsplit("/", 1)[1])


def test_practice_send_simulated_reply_and_rejection(client, security, world) -> None:
    csrf = sign_in(client, security)
    campaign_id, message_id = approved_message(client, csrf, world)
    page = client.get(f"/messages/{message_id}").text
    assert "Send (practice)" in page and "practice mailbox" in page
    response = post(client, f"/messages/{message_id}/practice-send", csrf)
    assert response.headers["location"].endswith("notice=sent")
    assert world.message(message_id).state == "sent"
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Simulate a PI reply" in page
    post(client, f"/campaigns/{campaign_id}/simulate", csrf, kind="reply")
    assert world.campaign_row(campaign_id).state == "conversation"
    post(client, f"/campaigns/{campaign_id}/reply", csrf, tag="no_opening")
    row = world.campaign_row(campaign_id)
    assert (row.state, row.close_outcome) == ("closed", "no_opening")


def test_outlook_handoff_in_live_mode(client, security, world) -> None:
    csrf = sign_in(client, security)
    world.set_mode(Mode.LIVE)
    world.connect_handoff_mailbox()
    world.signature_step("Alex Synthetic, MD")
    campaign_id, message_id = approved_message(client, csrf, world)
    page = client.get(f"/messages/{message_id}").text
    assert "Open in Outlook on the web" in page and "nothing is sent until you press Send" in page
    response = post(client, f"/messages/{message_id}/handoff", csrf, method="file")
    assert "error=" in response.headers["location"]  # the Outlook check was not confirmed
    response = post(client, f"/messages/{message_id}/handoff", csrf, method="file", confirm="yes")
    handoff_url = response.headers["location"].split("?")[0]
    assert handoff_url.startswith("/handoffs/")
    page = client.get(response.headers["location"]).text
    assert "Download the email file" in page and "I sent it" in page
    eml = client.get(f"{handoff_url}/email.eml")
    assert eml.headers["content-type"].startswith("message/rfc822")
    parsed = email.message_from_bytes(eml.content, policy=default_policy)
    assert parsed["X-Unsent"] == "1"
    assert "Alex Synthetic, MD" in parsed.get_body(("plain",)).get_content()
    response = post(client, f"{handoff_url}/sent", csrf, edited="yes")
    assert response.headers["location"] == f"/messages/{message_id}?notice=sent"
    assert world.message(message_id).state == "sent"
    post(client, f"/campaigns/{campaign_id}/reply", csrf, tag="referral", note="Dr Colleague")
    assert world.campaign_row(campaign_id).state == "conversation"
    assert "Dr Colleague" in client.get("/tasks").text


def test_not_sent_and_prior_contact(client, security, world) -> None:
    csrf = sign_in(client, security)
    world.set_mode(Mode.LIVE)
    world.connect_handoff_mailbox()
    world.signature_step("")
    campaign_id, message_id = approved_message(client, csrf, world)
    response = post(client, f"/messages/{message_id}/handoff", csrf, method="link", confirm="yes")
    handoff_url = response.headers["location"].split("?")[0]
    assert "outlook.office.com" in client.get(response.headers["location"]).text
    post(client, f"{handoff_url}/not-sent", csrf, note="changed my mind")
    assert world.message(message_id).state != "sent"
    response = post(client, f"/campaigns/{campaign_id}/prior-contact", csrf, note="met at ARVO")
    assert response.headers["location"].endswith("notice=review_opened")
    page = client.get(f"/campaigns/{campaign_id}").text
    assert "Needs your review" in page and "prior contact found" in page


def test_send_routes_need_csrf_and_real_ids(client, security, world) -> None:
    csrf = sign_in(client, security)
    for url in (
        "/messages/1/handoff",
        "/messages/1/practice-send",
        "/campaigns/1/reply",
        "/campaigns/1/simulate",
        "/tasks/1/done",
    ):
        assert client.post(url, headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/handoffs/nope").status_code == 404
    assert post(client, "/campaigns/999/reply", csrf, tag="positive").status_code == 404
    assert post(client, "/messages/999/practice-send", csrf).status_code == 404
    with transaction(world.factory) as s:
        assert s.scalars(select(Campaign)).first() is None
        assert s.scalars(select(Message)).first() is None


def test_practice_skip_ahead_to_send_time(client, security, world) -> None:
    from datetime import timedelta

    csrf = sign_in(client, security)
    world.clock.advance(timedelta(hours=10))  # 19:00 in New York: outside the send window
    _campaign_id, message_id = approved_message(client, csrf, world)
    page = client.get(f"/messages/{message_id}").text
    assert "Not yet" in page and "skip ahead to the send time" in page
    real_now = world.clock.now()
    response = post(client, "/settings/practice-time/until", csrf, message_id=str(message_id))
    assert response.headers["location"] == f"/messages/{message_id}#send"
    assert "Send (practice)" in client.get(f"/messages/{message_id}").text
    assert world.clock.now() == real_now  # only practice time moved
    assert post(client, "/settings/practice-time/until", csrf, message_id="999").status_code == 404
