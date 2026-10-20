"""Every signed-in page carries the CSRF meta tag (uploads and the notice bar need it).

Found in a real-browser walkthrough: the Research page rendered without it, so the lead
list upload (sent by script with the token from the meta tag) was refused.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.security.test_ui_security import ORIGIN, PORT, sign_in

PAGES = (
    "/",
    "/profile",
    "/research",
    "/research/queue",
    "/candidates",
    "/approvals",
    "/tasks",
    "/settings",
    "/data",
)


@pytest.fixture
def security(clock):
    return UISecurity("127.0.0.1", PORT, clock)


def test_every_page_has_the_token_and_notice_bar(world, security) -> None:
    client = TestClient(
        create_app(security, world.factory, world.clock, fetcher=FakeFetcher()), base_url=ORIGIN
    )
    csrf = sign_in(client, security)
    for path in PAGES:
        page = client.get(path)
        assert page.status_code == 200, path
        assert f'<meta name="csrf-token" content="{csrf}">' in page.text, path
        assert 'id="notices"' in page.text, path
