"""Settings → Publication lookups: optional keys and the NCBI contact address."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.db.models import Pi
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.fakes.secrets import InMemorySecretStore
from tests.security.test_drafting_ui import post
from tests.security.test_ui_security import ORIGIN, PORT, sign_in

NCBI_KEY = "0123456789abcdef0123456789abcdef0123"


@pytest.fixture
def store():
    return InMemorySecretStore()


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def client(world, clock, store, fetcher):
    security = UISecurity("127.0.0.1", PORT, clock)
    app = create_app(security, world.factory, world.clock, fetcher=fetcher, secrets=lambda: store)
    return TestClient(app, base_url=ORIGIN), security


def test_keys_and_address_are_saved_used_and_removed(client, world, store, fetcher) -> None:
    client, security = client
    csrf = sign_in(client, security)
    page = client.get("/settings").text
    assert "Publication lookups (optional keys)" in page and "Save OpenAlex key" in page
    assert client.post("/settings/lookup-key/ncbi", data={"api_key": NCBI_KEY}).status_code == 403
    response = post(client, "/settings/lookup-key/ncbi", csrf, api_key="no")
    assert "does+not+look+like" in response.headers["location"]
    response = post(client, "/settings/lookup-key/ncbi", csrf, api_key=NCBI_KEY)
    assert "key+saved" in response.headers["location"]
    response = post(client, "/settings/ncbi-email", csrf, email="nope")
    assert "error=" in response.headers["location"]
    post(client, "/settings/ncbi-email", csrf, email="owner@example.org")
    page = client.get("/settings").text
    assert NCBI_KEY not in page and "PubMed (NCBI) key: <strong>saved</strong>" in page
    assert 'value="owner@example.org"' in page
    assert store.get("ncbi_api_key") == NCBI_KEY

    pi = world.pi(world.institution(), name="Alex Example")
    with transaction(world.factory) as s:
        assert s.get(Pi, pi) is not None
    post(client, f"/candidates/{pi}/lookup", csrf)
    pubmed = [p for u, p in fetcher.calls if "eutils" in u]
    assert pubmed and all(
        (p["api_key"], p["email"], p["tool"]) == (NCBI_KEY, "owner@example.org", "marcelorian")
        for p in pubmed
    )

    post(client, "/settings/lookup-key/ncbi/remove", csrf)
    assert store.get("ncbi_api_key") is None
    assert "Save PubMed (NCBI) key" in client.get("/settings").text
    assert post(client, "/settings/lookup-key/other", csrf, api_key=NCBI_KEY).status_code == 404
