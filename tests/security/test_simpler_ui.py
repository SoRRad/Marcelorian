"""The simpler screens (owner request 2026-10-27): one-form profile, strict-rules switch,
sortable Leads with PhD postings hidden, address editing, and practice Outlook to self."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.db.engine import transaction
from marcelorian.db.models import ResearchRecord
from marcelorian.services import profile
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_drafting_ui import post
from tests.security.test_ui_security import ORIGIN, PORT, sign_in
from tests.service.test_drafting import confirm_facts
from tests.service.test_tailoring import link_seed


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


@pytest.fixture
def client(world, security):
    app = create_app(security, world.factory, world.clock, fetcher=FakeFetcher(), lookup_pause=0)
    return TestClient(app, base_url=ORIGIN)


def test_navigation_has_four_places(client, security) -> None:
    sign_in(client, security)
    page = client.get("/").text
    for label in ("Home", "Leads", "Emails", "Settings"):
        assert f">{label}</a>" in page
    assert "Your pipeline" in page and "Ready to send" in page
    assert "Claude" not in client.get("/settings").text
    assert (
        "Your profile" in client.get("/profile").text
        and 'class="subnav"' in client.get("/profile").text
    )


def test_profile_saves_and_confirms_everything_at_once(client, security, world) -> None:
    csrf = sign_in(client, security)
    response = post(
        client,
        "/profile/facts",
        csrf,
        v_full_name="Alex Synthetic",
        v_current_role_phrase="a research fellow at a synthetic institute",
        v_skills="OCT image analysis",
        v_funding_preference="paid_preferred",
        v_recent_news="",
    )
    assert "Saved+and+confirmed+%284+changed%29" in response.headers["location"]
    with transaction(world.factory) as s:
        facts = profile.facts(s, world.applicant_id)
        assert facts["skills"].status == "confirmed" and "recent_news" not in facts
        version = facts["skills"].version
    post(client, "/profile/facts", csrf, v_skills="OCT image analysis")  # unchanged
    with transaction(world.factory) as s:
        assert profile.facts(s, world.applicant_id)["skills"].version == version


def test_strict_rules_switch(client, security, world) -> None:
    csrf = sign_in(client, security)
    world.settings(strict_send_rules=False)
    assert "Off (recommended)" in client.get("/settings").text
    post(client, "/settings/strict", csrf, strict="yes")
    assert "Strict rules are on." in client.get("/settings").text
    post(client, "/settings/strict", csrf)
    assert "Off (recommended)" in client.get("/settings").text


def test_leads_sort_and_hide_phd_postings(client, security, world) -> None:
    sign_in(client, security)
    camp = world.campaign(world.pi(world.institution()))
    with transaction(world.factory) as s:
        from marcelorian.db.models import Campaign

        pi_id = s.get(Campaign, camp).pi_id
    link_seed(world, pi_id, posting="PhD student position in ocular genetics")
    page = client.get("/research").text
    assert "PhD student position" not in page and "1 PhD-student position(s) hidden" in page
    assert "PhD student position" in client.get("/research?phd=show").text
    sorted_page = client.get("/research?sort=name&dir=desc").text
    assert "sort=name&amp;dir=asc" in sorted_page and "▼" in sorted_page
    with transaction(world.factory) as s:
        assert s.query(ResearchRecord).count() == 2


def test_address_can_be_edited_on_the_contact_page(client, security, world) -> None:
    csrf = sign_in(client, security)
    pi = world.pi(world.institution())
    response = post(
        client,
        f"/candidates/{pi}/emails",
        csrf,
        address="fresh@synthetic-univ.test",
        source="institutional_page",
        source_url="https://synthetic-univ.test/people",
        replace_current="yes",
    )
    assert "Address+saved" in response.headers["location"]
    page = client.get(f"/candidates/{pi}").text
    assert "fresh@synthetic-univ.test" in page and "used for emails" in page
    response = post(client, f"/candidates/{pi}/emails", csrf, address="x", source="nope")
    assert "error=" in response.headers["location"]


def test_practice_email_can_be_tried_in_outlook(client, security, world) -> None:
    csrf = sign_in(client, security)
    confirm_facts(world)
    world.connect_handoff_mailbox("me@synthetic-hospital.test")
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    page = client.get(f"/messages/{mid}/try-outlook").text
    assert "addressed to <strong>you</strong> (me@synthetic-hospital.test)" in page
    eml = client.get(f"/messages/{mid}/try-outlook/email.eml")
    assert eml.status_code == 200 and b"To: me@synthetic-hospital.test" in eml.content
    assert world.message(mid).state == "approved_queued"
    assert csrf
