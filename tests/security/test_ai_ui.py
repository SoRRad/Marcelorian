"""Claude suggestion pages: key settings, suggest button, evidence beside claims (fakes only)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.db.models import Message
from marcelorian.platform.secrets import SecretStoreUnavailable
from marcelorian.services import ai, drafting
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.ai import FakeAIClient, FakeAIFactory
from tests.fakes.lookups import FakeFetcher
from tests.fakes.secrets import InMemorySecretStore
from tests.security.test_drafting_ui import post, start
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_ai import add_evidence, answer, fact_id
from tests.service.test_drafting import FACTS, confirm_facts

KEY = "sk-ant-synthetic-key-0000"


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def store():
    return InMemorySecretStore()


@pytest.fixture
def fake():
    return FakeAIFactory(FakeAIClient())


@pytest.fixture
def client(world, security, store, fake):
    app = create_app(
        security,
        world.factory,
        world.clock,
        fetcher=FakeFetcher(),
        secrets=lambda: store,
        ai_client=fake,
    )
    return TestClient(app, base_url=ORIGIN)


def test_key_save_remove_and_csrf(client, security, store, fake) -> None:
    csrf = sign_in(client, security)
    assert "Claude suggestions (optional)" in client.get("/settings").text
    assert client.post("/settings/ai-key", data={"api_key": KEY}).status_code == 403
    response = post(client, "/settings/ai-key", csrf, api_key="nope")
    assert "does+not+look+like" in response.headers["location"]
    response = post(client, "/settings/ai-key", csrf, api_key=KEY)
    assert "Key+checked+and+saved" in response.headers["location"]
    assert store.get(ai.KEY_NAME) == KEY and fake.client.key_checks == 1
    page = client.get("/settings").text
    assert "Remove key" in page and KEY not in page
    post(client, "/settings/ai-key/remove", csrf)
    assert store.get(ai.KEY_NAME) is None
    assert "Check and save key" in client.get("/settings").text


def test_rejected_key_is_not_stored(client, security, store, fake) -> None:
    csrf = sign_in(client, security)
    fake.client.error = "The API key was not accepted. Check it."
    response = post(client, "/settings/ai-key", csrf, api_key=KEY)
    assert "not+accepted" in response.headers["location"]
    assert store.get(ai.KEY_NAME) is None


def test_suggest_flow_shows_evidence_and_keeps_written_lines(
    client, security, world, store, fake
) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world, FACTS)
    _pi, campaign_id = start(client, csrf, world)
    draft_url = f"/campaigns/{campaign_id}/draft"
    assert "Suggest empty lines with Claude" not in client.get(draft_url).text  # no key yet
    response = post(client, f"{draft_url}/suggest", csrf)
    assert "Add+an+Anthropic+API+key" in response.headers["location"]
    post(client, "/settings/ai-key", csrf, api_key=KEY)
    evidence = add_evidence(world, campaign_id, "Synthetic choroid imaging study")
    fake.client.answer = answer(evidence, fact_id(world, "skills"))
    response = post(
        client,
        f"{draft_url}/suggest",
        csrf,
        skills_line="my own skills line",
        personalized_sentence="",
        contribution_line="",
    )
    assert "Claude+suggested" in response.headers["location"]
    assert fake.keys == [KEY, KEY]  # key check at save, then the suggestion
    _system, _prompt, schema = fake.client.calls[0]
    assert "skills_line" not in schema["properties"]  # written lines are never replaced
    page = client.get(draft_url).text
    assert "suggested by Claude" in page and "Synthetic choroid imaging study" in page
    with transaction(world.factory) as s:
        message = s.query(Message).filter_by(campaign_id=campaign_id).one()
        fields = drafting.draft_fields(message)
    assert fields["personal"]["skills_line"] == "my own skills line"
    assert fields["sources"]["personalized_sentence"] == "ai"
    assert fields["sources"]["contribution_line"] == "ai"
    assert fields["sources"]["skills_line"] == "applicant"
    review = client.get(f"/messages/{message.id}").text
    assert "lines and their evidence" in review
    assert "Synthetic choroid imaging study" in review
    assert "Used 1 time" in client.get("/settings").text


def test_suggest_errors_are_plain(client, security, world, store, fake) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world, FACTS)
    _pi, campaign_id = start(client, csrf, world)
    draft_url = f"/campaigns/{campaign_id}/draft"
    post(client, "/settings/ai-key", csrf, api_key=KEY)
    response = post(client, f"{draft_url}/suggest", csrf)
    assert "no+public+evidence" in response.headers["location"]
    add_evidence(world, campaign_id)
    fake.client.error = "Could not reach Anthropic. Check the internet connection."
    response = post(client, f"{draft_url}/suggest", csrf)
    assert "Could+not+reach+Anthropic" in response.headers["location"]
    fake.client.error = ""
    fake.client.answer = "not json"
    response = post(client, f"{draft_url}/suggest", csrf)
    assert "could+not+be+read" in response.headers["location"]
    with transaction(world.factory) as s:
        assert s.query(Message).filter_by(campaign_id=campaign_id).count() == 0


def test_unavailable_credential_store(world, security, fake) -> None:
    def broken():
        raise SecretStoreUnavailable("no store")

    app = create_app(
        security, world.factory, world.clock, fetcher=FakeFetcher(), secrets=broken, ai_client=fake
    )
    client = TestClient(app, base_url=ORIGIN)
    csrf = sign_in(client, security)
    response = post(client, "/settings/ai-key", csrf, api_key=KEY)
    assert "Credential+Manager+is+unavailable" in response.headers["location"]
