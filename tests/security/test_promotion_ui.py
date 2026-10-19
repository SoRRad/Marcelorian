"""The "Turn into a contact" form on a research record."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.security.test_research_queue_ui import post, rid, stage
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.seed_fixture import standard_rows


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    stage(world, standard_rows())
    return TestClient(create_app(security, world.factory, world.clock), base_url=ORIGIN)


FORM = {
    "last_name": "C-FAC1",
    "salutation": "Dr. C-FAC1",
    "email_source": "institutional_page",
    "email_source_url": "https://synthetic-eye.test/people/fac1",
    "timezone": "America/New_York",
    "timezone_confirmed": "yes",
}


def test_blockers_then_form_then_contact(client, security, world) -> None:
    csrf = sign_in(client, security)
    fac = rid(world, "C-FAC1")
    page = client.get(f"/research/{fac}").text
    assert "Not yet possible" in page and "Confirm the person yourself first" in page
    post(
        client,
        f"/research/{fac}/quick",
        csrf,
        verification="confirmed_current",
        source="https://synthetic-eye.test/faculty",
        pos="0",
        tier="",
    )
    page = client.get(f"/research/{fac}").text
    assert f'action="/research/{fac}/promote"' in page and "America/New_York" in page
    response = post(client, f"/research/{fac}/promote", csrf, **{**FORM, "timezone_confirmed": ""})
    assert "promote_error=" in response.headers["location"]
    assert "Confirm the institution" in client.get(response.headers["location"]).text
    response = post(client, f"/research/{fac}/promote", csrf, **FORM)
    assert response.headers["location"] == f"/research/{fac}#promote"
    assert "Turned into a contact on" in client.get(f"/research/{fac}").text


def test_promote_requires_csrf_and_a_real_record(client, security, world) -> None:
    csrf = sign_in(client, security)
    fac = rid(world, "C-FAC1")
    assert (
        client.post(f"/research/{fac}/promote", data=FORM, headers={"Origin": ORIGIN}).status_code
        == 403
    )
    assert post(client, "/research/99999/promote", csrf, **FORM).status_code == 404
