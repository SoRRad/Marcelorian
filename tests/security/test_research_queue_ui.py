"""Verification queue page: one record at a time, quick findings, reopen, CSRF."""

from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import ResearchRecord
from marcelorian.domain.seed import read_seed
from marcelorian.services.research_seed import import_seed
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.seed_fixture import contact, csv_text, standard_rows

SOURCE = "https://synthetic-eye.test/faculty"


def stage(world, rows) -> None:
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(rows), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    stage(world, standard_rows())
    return TestClient(create_app(security, world.factory, world.clock), base_url=ORIGIN)


def rid(world, external_id: str) -> int:
    with transaction(world.factory) as s:
        return s.scalars(
            select(ResearchRecord.id).where(ResearchRecord.external_record_id == external_id)
        ).one()


def post(client, url, csrf, **form):
    return client.post(
        url, data={**form, "csrf_token": csrf}, headers={"Origin": ORIGIN}, follow_redirects=False
    )


def test_queue_shows_the_most_sendable_record_first(client, security) -> None:
    sign_in(client, security)
    page = client.get("/research/queue").text
    assert "1 of 5" in page and "C-FAC1</a></h2>" in page
    assert 'href="https://synthetic-eye.test/people"' in page
    assert "Confirmed current" in page and "Skip for now" in page
    assert "Dr Synthetic C-FAC1" in client.get("/research").text
    assert "verification queue (5 to check)" in client.get("/research").text


def test_confirm_needs_a_source_then_moves_on(client, security, world) -> None:
    csrf = sign_in(client, security)
    fac = rid(world, "C-FAC1")
    response = post(
        client,
        f"/research/{fac}/quick",
        csrf,
        verification="confirmed_current",
        source="",
        pos="0",
        tier="",
    )
    assert response.status_code == 303 and "error=source" in response.headers["location"]
    page = client.get(response.headers["location"]).text
    assert "Say where you checked" in page and "Dr Synthetic C-FAC1" in page
    response = post(
        client,
        f"/research/{fac}/quick",
        csrf,
        verification="confirmed_current",
        source=SOURCE,
        pos="0",
        tier="",
    )
    assert response.headers["location"] == "/research/queue?pos=0"
    page = client.get("/research/queue").text
    assert "1 of 4" in page and "C-FAC1</a></h2>" not in page  # heading is the next record


def test_reopen_from_the_queue_stays_on_the_record(client, security, world) -> None:
    csrf = sign_in(client, security)
    fac = rid(world, "C-FAC1")
    post(
        client,
        f"/research/{fac}/quick",
        csrf,
        verification="fellow_or_trainee",
        source="",
        pos="0",
        tier="",
    )
    changed = standard_rows()
    changed[0] = contact(
        "C-FAC1", role="Professor", related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"])
    )
    stage(world, changed)
    page = client.get("/research/queue").text
    assert "New evidence since your finding" in page and "Keep: " in page and "Reopen" in page
    response = post(client, f"/research/{fac}/reopen", csrf, back="queue", pos="0", tier="")
    assert response.headers["location"] == f"/research/queue?pos=0&id={fac}"
    page = client.get(response.headers["location"]).text
    assert "Dr Synthetic C-FAC1" in page and "Not checked yet" in page
    detail = client.get(f"/research/{fac}").text
    assert "History of your checks" in detail and "Reopened" in detail


def test_detail_reopen_and_source_error(client, security, world) -> None:
    csrf = sign_in(client, security)
    auth = rid(world, "C-AUTH1")
    response = post(
        client,
        f"/research/{auth}/review",
        csrf,
        local_status="unreviewed",
        local_verification="confirmed_current",
        source="",
        note="",
    )
    assert response.headers["location"] == f"/research/{auth}?error=source"
    assert "Say where you checked" in client.get(response.headers["location"]).text
    post(
        client,
        f"/research/{auth}/review",
        csrf,
        local_status="unreviewed",
        local_verification="wrong_person",
        source="",
        note="",
    )
    assert "Reopen (back to not checked)" in client.get(f"/research/{auth}").text
    response = post(client, f"/research/{auth}/reopen", csrf, back="detail")
    assert response.headers["location"] == f"/research/{auth}"
    assert post(client, f"/research/{auth}/reopen", csrf, back="detail").status_code == 400


def test_queue_actions_require_csrf_and_valid_findings(client, security, world) -> None:
    csrf = sign_in(client, security)
    fac = rid(world, "C-FAC1")
    for url in (f"/research/{fac}/quick", f"/research/{fac}/reopen"):
        bad = client.post(url, data={"verification": "not_faculty"}, headers={"Origin": ORIGIN})
        assert bad.status_code == 403
    for value in ("unchecked", "approved"):
        response = post(client, f"/research/{fac}/quick", csrf, verification=value, pos="0")
        assert response.status_code == 400
    assert (
        post(client, "/research/99999/quick", csrf, verification="not_faculty", pos="0").status_code
        == 404
    )


def test_tier_filter_and_end_of_queue(client, security) -> None:
    sign_in(client, security)
    page = client.get("/research/queue", params={"tier": "3"}).text
    assert "1 of 3" in page and "Synthetic Retina Research Fellowship" in page
    end = client.get("/research/queue", params={"tier": "3", "pos": "9"}).text
    assert "Nothing left to check in this tier" in end and "Back to the start" in end
    assert client.get("/research/queue", params={"pos": "x", "tier": "y"}).status_code == 200
