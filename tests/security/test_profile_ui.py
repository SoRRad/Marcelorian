"""Profile page: facts, CV upload (CSRF header), signature, Outlook address."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.services import approvals, profile
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.security.test_ui_security import ORIGIN, PORT, sign_in


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    return TestClient(create_app(security, world.factory, world.clock), base_url=ORIGIN)


def post(client, url, csrf, **form):
    return client.post(
        url, data={**form, "csrf_token": csrf}, headers={"Origin": ORIGIN}, follow_redirects=False
    )


def test_profile_page_and_fact_saving(client, security, world) -> None:
    csrf = sign_in(client, security)
    page = client.get("/profile").text
    assert "Save and confirm" in page and "Still needed before drafting" in page
    response = post(
        client, "/profile/fact", csrf, key="full_name", value="Alex Synthetic", action="confirm"
    )
    assert response.headers["location"] == "/profile#f-full_name"
    response = post(
        client, "/profile/fact", csrf, key="earliest_start", value="soon", action="confirm"
    )
    assert "error=" in response.headers["location"]
    assert "must look like 2027-07" in client.get(response.headers["location"]).text
    assert post(client, "/profile/fact", csrf, key="nope", value="x").status_code == 400
    with transaction(world.factory) as s:
        facts = profile.facts(s, world.applicant_id)
    assert facts["full_name"].status == "confirmed" and "earliest_start" not in facts


def test_cv_upload_needs_the_csrf_header(client, security, world) -> None:
    csrf = sign_in(client, security)
    files = {"file": ("my cv.pdf", b"%PDF-1.4 synthetic", "application/pdf")}
    assert client.post("/profile/cv", files=files, headers={"Origin": ORIGIN}).status_code == 403
    response = client.post(
        "/profile/cv",
        files=files,
        follow_redirects=False,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert response.status_code == 303 and response.headers["location"] == "/profile#cv"
    bad = {"file": ("cv.docx", b"PK\x03\x04", "application/octet-stream")}
    response = client.post(
        "/profile/cv",
        files=bad,
        follow_redirects=False,
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )
    assert "PDF" in client.get(response.headers["location"]).text
    assert "my cv.pdf" in client.get("/profile").text


def test_signature_and_outlook_address(client, security, world) -> None:
    csrf = sign_in(client, security)
    post(client, "/profile/signature", csrf, signature="Alex Synthetic, MD")
    post(client, "/profile/outlook", csrf, email="applicant@synthetic-hospital.test")
    response = post(client, "/profile/outlook", csrf, email="not an address")
    assert "error=" in response.headers["location"]
    with transaction(world.factory) as s:
        assert approvals._active_mailbox(s).email == "applicant@synthetic-hospital.test"
    page = client.get("/profile").text
    assert "Alex Synthetic, MD" in page and "outlook_handoff" in page
    dash = client.get("/").text
    assert "Set up your signature ✓" in dash and "Add your Outlook address ✓" in dash


def test_navigation_is_on_every_signed_in_page(client, security) -> None:
    sign_in(client, security)
    for path in ("/", "/profile", "/research", "/research/queue"):
        assert 'class="mainnav"' in client.get(path).text
