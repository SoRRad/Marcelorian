"""Tailoring on screen: interests box, posting confirmation, warn in practice, block live."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.domain.enums import Mode
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post, start
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_drafting import add_evidence, confirm_facts
from tests.service.test_tailoring import GENERIC_LINE, TOPIC_LINE, link_seed, pi_of


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def test_interests_box_and_practice_warning(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    _pi, campaign_id = start(client, csrf, world)
    draft = client.get(f"/campaigns/{campaign_id}/draft").text
    assert "This PI's interests" in draft and "Nothing on file yet" in draft
    add_evidence(world, campaign_id, "Adaptive optics imaging of cone photoreceptors")
    assert (
        "Adaptive optics imaging of cone photoreceptors"
        in client.get(f"/campaigns/{campaign_id}/draft").text
    )
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **GENERIC_LINE)
    message_url = response.headers["location"]
    review = client.get(message_url).text
    assert "⚠ Tailored to this PI" in review and "a real email must pass this" in review
    approved = post(client, f"{message_url}/approve", csrf)  # practice only warns
    assert approved.headers["location"].endswith("notice=approved")


def test_live_email_must_be_tailored(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    world.set_mode(Mode.LIVE)
    world.connect_handoff_mailbox()
    world.signature_step()
    _pi, campaign_id = start(client, csrf, world)
    add_evidence(world, campaign_id, "Adaptive optics imaging of cone photoreceptors")
    response = post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **GENERIC_LINE)
    message_url = response.headers["location"]
    assert "✗ Tailored to this PI" in client.get(message_url).text
    refused = post(client, f"{message_url}/approve", csrf)
    assert "Tailored+to+this+PI" in refused.headers["location"]
    post(client, f"/campaigns/{campaign_id}/draft", csrf, action="save", **TOPIC_LINE)
    approved = post(client, f"{message_url}/approve", csrf)
    assert approved.headers["location"].endswith("notice=approved")


def test_confirm_posting_on_the_contact_page(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    pi_id, campaign_id = start(client, csrf, world)
    record_id = link_seed(world, pi_of(world, campaign_id), posting="Synthetic Retina Fellowship")
    page = client.get(f"/candidates/{pi_id}").text
    assert "Synthetic Retina Fellowship" in page and "this posting is current" in page
    response = post(client, f"/candidates/{pi_id}/postings/{record_id}", csrf, action="confirm")
    assert response.headers["location"].endswith("#postings")
    assert "confirmed current" in client.get(f"/candidates/{pi_id}").text
    draft = client.get(f"/campaigns/{campaign_id}/draft").text
    assert "Posted position you confirmed" in draft and "Synthetic Retina Fellowship" in draft
    post(client, f"/candidates/{pi_id}/postings/{record_id}", csrf, action="withdraw")
    assert "this posting is current" in client.get(f"/candidates/{pi_id}").text
    bad = post(client, f"/candidates/{pi_id}/postings/99999", csrf, action="confirm")
    assert "not+linked" in bad.headers["location"]
    assert post(client, f"/candidates/{pi_id}/postings/{record_id}", csrf).status_code == 400
