"""Local UI access control (acceptance 52; spec §2)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, CampaignPause, Pi
from marcelorian.domain.enums import Actor, PauseReason
from marcelorian.services import campaigns, runtime
from marcelorian.ui.app import create_app
from marcelorian.ui.security import SESSION_COOKIE, UISecurity

PORT = 48123
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock)
    return TestClient(app, base_url=ORIGIN)


def sign_in(client, security) -> str:
    token = security.issue_bootstrap_token()
    response = client.post("/bootstrap/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert response.status_code == 200
    csrf = security.lookup(client.cookies.get(SESSION_COOKIE)).csrf_token
    return csrf


def test_requests_without_session_are_rejected(client) -> None:
    assert client.get("/").status_code == 401
    assert client.post("/actions/pause", headers={"Origin": ORIGIN}).status_code == 401


def test_bad_host_rejected(client, security) -> None:
    sign_in(client, security)
    for host in ("localhost:48123", "127.0.0.1:1", "evil.example", "127.0.0.1"):
        assert client.get("/", headers={"Host": host}).status_code == 400


def test_bad_or_missing_origin_rejected_for_posts(client, security) -> None:
    csrf = sign_in(client, security)
    for headers in (
        {},
        {"Origin": "http://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://localhost:48123"},
    ):
        response = client.post("/actions/pause", headers={**headers, "X-CSRF-Token": csrf})
        assert response.status_code == 403


def test_missing_or_wrong_csrf_rejected(client, security) -> None:
    csrf = sign_in(client, security)
    assert client.post("/actions/pause", headers={"Origin": ORIGIN}).status_code == 403
    assert (
        client.post(
            "/actions/pause", headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    response = client.post(
        "/actions/pause", data={"csrf_token": "wrong"}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 403
    response = client.post(
        "/actions/pause",
        data={"csrf_token": csrf},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_valid_post_changes_state(client, security, world) -> None:
    csrf = sign_in(client, security)
    response = client.post(
        "/actions/pause", headers={"Origin": ORIGIN, "X-CSRF-Token": csrf}, follow_redirects=False
    )
    assert response.status_code == 303
    with transaction(world.factory) as s:
        assert runtime.get_runtime(s, world.clock.now()).dispatch_paused


def test_no_state_changing_get(client, security, world) -> None:
    sign_in(client, security)
    for path in ("/actions/pause", "/actions/resume", "/bootstrap/exchange"):
        assert client.get(path).status_code == 405
    with transaction(world.factory) as s:
        assert not runtime.get_runtime(s, world.clock.now()).dispatch_paused


def test_every_state_changing_route_is_post_and_csrf_protected(client, security, world) -> None:
    app = create_app(security, world.factory, world.clock).app  # unwrap middleware
    csrf = sign_in(client, security)
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        assert not (methods & {"PUT", "PATCH", "DELETE"})
        if "POST" in methods and route.path != "/bootstrap/exchange":
            no_csrf = client.post(route.path, headers={"Origin": ORIGIN})
            assert no_csrf.status_code == 403, route.path
    assert csrf


def test_bootstrap_token_is_one_time_and_expires(client, security, clock) -> None:
    token = security.issue_bootstrap_token()
    assert client.get("/bootstrap").status_code == 200  # the GET never consumes the token
    ok = client.post("/bootstrap/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert ok.status_code == 200
    again = client.post("/bootstrap/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert again.status_code == 403
    stale = security.issue_bootstrap_token()
    clock.advance(timedelta(minutes=3))
    expired = client.post("/bootstrap/exchange", json={"token": stale}, headers={"Origin": ORIGIN})
    assert expired.status_code == 403
    assert client.post("/bootstrap/exchange", json={"token": token}).status_code == 403  # no Origin


def test_session_cookie_flags(client, security) -> None:
    token = security.issue_bootstrap_token()
    response = client.post("/bootstrap/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie and "path=/" in cookie
    assert token not in response.text


def test_security_headers_present(client, security) -> None:
    sign_in(client, security)
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"


def test_untrusted_text_is_escaped(client, security, world) -> None:
    pi = world.pi(world.institution(), name="Eve <script>alert(1)</script>")
    camp = world.campaign(pi)
    world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        campaigns.open_pause(
            s,
            s.get(Campaign, camp),
            PauseReason.AUTO_REPLY,
            actor=Actor.SYSTEM,
            now=world.clock.now(),
        )
        assert s.scalars(select(Pi)).one().full_name.startswith("Eve")
        assert s.scalars(select(CampaignPause)).one()
    sign_in(client, security)
    body = client.get("/").text
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_ui_binds_loopback_only(clock) -> None:
    with pytest.raises(ValueError):
        UISecurity("0.0.0.0", PORT, clock)  # noqa: S104


def test_oversized_form_rejected(client, security) -> None:
    sign_in(client, security)
    response = client.post(
        "/actions/pause",
        content=b"a=" + b"x" * 70_000,
        headers={"Origin": ORIGIN, "Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 413
