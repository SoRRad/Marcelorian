"""Research catalog pages: filters, evidence vs claims, safe links, CSRF-protected review."""

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


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    rows = [
        *standard_rows(),
        contact(
            "C-XSS",
            name="<script>alert(1)</script>",
            evidence_json=json.dumps(
                [
                    {
                        "field": "role",
                        "value": "x",
                        "source_url": "javascript:alert(1)",
                        "source_type": "Bad link",
                        "checked_on": "2026-10-18",
                    }
                ]
            ),
        ),
    ]
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(rows), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )
    return TestClient(create_app(security, world.factory, world.clock), base_url=ORIGIN)


def record_id(world, external_id: str) -> int:
    with transaction(world.factory) as s:
        return s.scalars(
            select(ResearchRecord.id).where(ResearchRecord.external_record_id == external_id)
        ).one()


def test_research_requires_a_session(client) -> None:
    assert client.get("/research").status_code == 401


def test_list_and_filters(client, security) -> None:
    sign_in(client, security)
    page = client.get("/research")
    assert page.status_code == 200 and "Leads are only suggestions" in page.text
    assert "Dr Synthetic C-FAC1" in page.text and "shared route" in page.text
    filtered = client.get("/research", params={"record_type": "opportunity"})
    assert "Synthetic Retina Research Fellowship" in filtered.text
    assert "Dr Synthetic C-FAC1" not in filtered.text
    by_level = client.get("/research", params={"confidence": "none"})
    assert "Dr Synthetic C-AUTH1" in by_level.text and "C-FAC1<" not in by_level.text


def test_detail_separates_evidence_from_claims(client, security, world) -> None:
    sign_in(client, security)
    page = client.get(f"/research/{record_id(world, 'C-AUTH1')}").text
    assert "Observed evidence (0)" in page and "Unverified claims" in page
    assert "c-auth1@claimed-only.test" in page  # shown as a claim
    assert "never used as a recipient" in page
    assert "Prior contact: not checked" in page
    fac = client.get(f"/research/{record_id(world, 'C-FAC1')}").text
    assert 'href="https://synthetic-eye.test/people"' in fac
    assert 'rel="noopener noreferrer"' in fac
    assert "Official source seen" in fac


def test_untrusted_text_is_escaped_and_bad_urls_are_not_links(client, security, world) -> None:
    sign_in(client, security)
    page = client.get(f"/research/{record_id(world, 'C-XSS')}").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert 'href="javascript:' not in page


def test_review_post_requires_csrf_and_saves(client, security, world) -> None:
    csrf = sign_in(client, security)
    rid = record_id(world, "C-FAC1")
    form = {
        "local_status": "set_aside",
        "local_verification": "fellow_or_trainee",
        "source": "https://synthetic-eye.test/fellows",
        "note": "listed as a fellow",
    }
    assert (
        client.post(f"/research/{rid}/review", data=form, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    response = client.post(
        f"/research/{rid}/review",
        data={**form, "csrf_token": csrf},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/research/{rid}").text
    assert "Blocked" in page and "listed as a fellow" in page
    bad = client.post(
        f"/research/{rid}/review",
        data={**form, "local_verification": "approved", "csrf_token": csrf},
        headers={"Origin": ORIGIN},
    )
    assert bad.status_code == 400
    assert client.get("/research/999999").status_code == 404
