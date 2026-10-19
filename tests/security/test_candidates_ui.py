"""Candidates pages: list, detail, lookup (fake fetcher), fit, skip, do-not-contact."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_ui_security import ORIGIN, PORT, sign_in


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def client(world, security, fetcher):
    app = create_app(security, world.factory, world.clock, fetcher=fetcher)
    return TestClient(app, base_url=ORIGIN)


def post(client, url, csrf, **form):
    return client.post(
        url, data={**form, "csrf_token": csrf}, headers={"Origin": ORIGIN}, follow_redirects=False
    )


def test_list_and_detail(client, security, world) -> None:
    sign_in(client, security)
    assert "No candidates yet" in client.get("/candidates").text
    pi = world.pi(world.institution(name="Synthetic Eye Institute"), name="Alex Example")
    page = client.get("/candidates").text
    assert "Alex Example" in page and "Research fit not assessed" in page
    detail = client.get(f"/candidates/{pi}").text
    assert "send-ready" in detail and "Look up PubMed" in detail
    assert client.get("/candidates/999").status_code == 404


def test_lookup_fit_skip_and_dnc(client, security, world, fetcher) -> None:
    csrf = sign_in(client, security)
    pi = world.pi(world.institution(name="Synthetic Eye Institute"), name="Alex Example")
    response = post(client, f"/candidates/{pi}/lookup", csrf)
    assert response.headers["location"] == f"/candidates/{pi}#evidence"
    detail = client.get(f"/candidates/{pi}").text
    assert "Deep learning on synthetic OCT scans" in detail and "5R01EY000001-03" in detail
    assert "An ended synthetic project" not in detail
    post(client, f"/candidates/{pi}/assessment", csrf, fit="strong", note="OCT methods")
    assert "Strong research fit" in client.get("/candidates").text
    assert post(client, f"/candidates/{pi}/assessment", csrf, fit="great").status_code == 400
    post(client, f"/candidates/{pi}/skip", csrf, skipped="yes", reason="later")
    assert "Alex Example" not in client.get("/candidates").text
    post(client, f"/candidates/{pi}/skip", csrf, skipped="no")
    response = post(client, f"/candidates/{pi}/dnc", csrf, reason="x")
    assert "error=" in response.headers["location"]
    post(client, f"/candidates/{pi}/dnc", csrf, reason="asked", confirm="yes")
    assert "On your do-not-contact list" in client.get(f"/candidates/{pi}").text


def test_candidate_actions_need_csrf(client, security, world) -> None:
    sign_in(client, security)
    pi = world.pi(world.institution())
    for action in ("lookup", "assessment", "skip", "dnc"):
        assert (
            client.post(
                f"/candidates/{pi}/{action}", data={"skipped": "yes"}, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )
