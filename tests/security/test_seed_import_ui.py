"""Import a lead list from the browser: upload, preview, import or cancel (synthetic seed)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Pi, ResearchRecord
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.seed_fixture import contact, csv_text, standard_rows


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher())
    return TestClient(app, base_url=ORIGIN)


def upload(client, csrf: str, text: str, name: str = "leads.csv"):
    return client.post(
        "/research/import",
        files={"file": (name, text.encode("utf-8"), "text/csv")},
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        follow_redirects=False,
    )


def count(world, model) -> int:
    with transaction(world.factory) as s:
        return int(s.scalar(select(func.count()).select_from(model)) or 0)


def test_upload_preview_then_import(client, security, world) -> None:
    csrf = sign_in(client, security)
    assert "Add a lead list" in client.get("/research").text
    response = upload(client, csrf, csv_text(standard_rows()))
    preview_url = response.headers["location"]
    page = client.get(preview_url).text
    assert "Import these leads" in page and count(world, ResearchRecord) == 0
    response = post(client, f"{preview_url}/commit", csrf)
    assert "Imported" in response.headers["location"]
    assert count(world, ResearchRecord) == len(standard_rows())
    assert count(world, Pi) == 0 and count(world, Campaign) == 0  # leads only
    response = post(client, f"{preview_url}/commit", csrf)  # a token is used once
    assert "expired" in response.headers["location"]


def test_invalid_file_is_refused_whole(client, security, world) -> None:
    csrf = sign_in(client, security)
    bad = contact("SYN-BAD-1", send_authorized="true")
    response = upload(client, csrf, csv_text([*standard_rows(), bad]))
    page = client.get(response.headers["location"]).text
    assert "cannot be imported" in page and "Import these leads" not in page
    assert count(world, ResearchRecord) == 0


def test_wrong_files_and_cancel(client, security, world) -> None:
    csrf = sign_in(client, security)
    assert "lead+list+.csv" in upload(client, csrf, "x", name="cv.pdf").headers["location"]
    assert "not+a+Marcelorian" in upload(client, csrf, "a,b\n1,2\n").headers["location"]
    response = upload(client, csrf, csv_text(standard_rows()))
    preview_url = response.headers["location"]
    assert "cancelled" in post(client, f"{preview_url}/cancel", csrf).headers["location"]
    assert client.get(preview_url).status_code == 404
    no_csrf = client.post(
        "/research/import",
        files={"file": ("leads.csv", b"x", "text/csv")},
        headers={"Origin": ORIGIN},
    )
    assert no_csrf.status_code == 403
