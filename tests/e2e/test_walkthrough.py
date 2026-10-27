"""Real-browser walkthrough of the tester guide (Chromium via Playwright).

Unit tests set request headers by hand, so they cannot catch what only a browser does
(for example the ``Origin: null`` that ``Referrer-Policy: no-referrer`` caused on every
form). This test serves the real app (real security middleware, real HTTP) on a free
port with a throwaway database and fake lookup data, then follows the tester guide:
sign in, profile, CV, signature, Outlook address, lead import, verify, contact, look up
everyone, tailored draft, approve, skip ahead, practice send, reply, reports, backup.

Runs only with ``MARCELORIAN_E2E=1`` and Chromium installed
(``python -m playwright install chromium``); the CI "browser" job does both.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from marcelorian.app_main import HOST, free_port, prepare_database
from marcelorian.clock import SystemClock
from marcelorian.platform.paths import AppPaths
from marcelorian.ui.app import create_app
from marcelorian.ui.security import UISecurity
from tests.fakes.lookups import FakeFetcher
from tests.seed_fixture import standard_rows, write_csv

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("MARCELORIAN_E2E") != "1", reason="set MARCELORIAN_E2E=1 to run"
    ),
    pytest.mark.timeout(240),
]

PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PROFILE = {
    "full_name": "Alex Synthetic",
    "current_role_phrase": "a research fellow at a synthetic eye institute",
    "earliest_start": "2027-07",
    "skills": "OCT image analysis and clinical data analysis",
    "research_interests": "glaucoma and retinal imaging",
}


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, str, Path]]:
    import uvicorn

    clock = SystemClock()
    paths = AppPaths(tmp_path / "data", tmp_path / "exports", exports_synced_by_onedrive=False)
    factory = prepare_database(paths.db_path, clock, paths.backups_dir)
    port = free_port()
    security = UISecurity(HOST, port, clock)
    app = create_app(security, factory, clock, fetcher=FakeFetcher(), paths=paths, lookup_pause=0)
    server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    seed = write_csv(tmp_path / "leads.csv", standard_rows())
    cv = tmp_path / "cv.pdf"
    cv.write_bytes(PDF)
    try:
        yield security.origin, security.bootstrap_url(security.issue_bootstrap_token()), seed
    finally:
        server.should_exit = True
        thread.join(10)
        factory.kw["bind"].dispose()


def test_tester_guide_in_a_real_browser(served, tmp_path: Path) -> None:
    from playwright.sync_api import expect, sync_playwright

    base, sign_in_url, seed = served
    shots = os.environ.get("MARCELORIAN_SHOTS")  # optional folder for screenshots
    problems: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        page.on("console", lambda m: m.type == "error" and problems.append(m.text))
        page.on(
            "response",
            lambda r: r.status >= 400 and problems.append(f"{r.status} {r.request.method} {r.url}"),
        )

        def submit(locator) -> None:  # click a submit button and wait for the next page
            with page.expect_navigation():
                locator.click()

        def shot(name: str) -> None:
            if shots:
                page.screenshot(path=str(Path(shots) / f"{name}.png"), full_page=True)

        # Sign in with the one-time link.
        page.goto(sign_in_url)
        page.wait_for_url(re.compile(r"^(?!.*bootstrap).*$"))
        shot("01-home-empty")

        # Settings → Your profile: one form, one "Save and confirm all".
        page.goto(f"{base}/profile")
        for key, value in PROFILE.items():
            page.locator(f"#f-{key} [name=v_{key}]").fill(value)
        funding = page.locator("#f-funding_preference select")
        funding.select_option(funding.locator("option").nth(1).get_attribute("value"))
        submit(page.locator("button:has-text('Save and confirm all')"))
        expect(page.locator("main")).to_contain_text("All required answers are confirmed")
        page.set_input_files("#cv input[type=file]", str(tmp_path / "cv.pdf"))
        submit(page.locator("#cv button[type=submit]"))
        page.fill("#signature textarea", "Alex Synthetic, MD")
        submit(page.locator("#signature button[type=submit]"))
        page.fill("#outlook input[name=email]", "alex.synthetic@example.com")
        submit(page.locator("#outlook button[type=submit]"))
        shot("02-profile")

        # Leads: import the list (upload by script with the CSRF header).
        page.goto(f"{base}/research")
        page.set_input_files("input[name=file]", str(seed))
        with page.expect_navigation(url=re.compile(r"/research/import/")):
            page.click("text=Check the file")
        shot("03-import-preview")
        submit(page.locator("text=Import these leads"))
        expect(page.locator("main")).to_contain_text("Imported")
        shot("04-leads")

        # Check the first lead, then turn it into a contact.
        page.goto(f"{base}/research/queue")
        record_href = page.locator("main h2 a").first.get_attribute("href")
        page.fill("input[name=source]", "Department faculty page (synthetic)")
        submit(page.locator("button[value=confirmed_current]"))
        page.goto(f"{base}{record_href}")
        form = page.locator('form[action$="/promote"]')
        if not form.locator("input[name=email_source_url]").input_value():
            form.locator("input[name=email_source_url]").fill("https://example.org/faculty")
        form.locator("input[name=timezone_confirmed]").check()
        if form.locator("input[name=not_same_person]").count():
            form.locator("input[name=not_same_person]").check()
        submit(form.locator("button[type=submit]"))

        # Home: look up everyone in the background, then wait for it to finish.
        page.goto(f"{base}/")
        submit(page.locator("text=/Look up all publications/"))
        deadline = time.monotonic() + 30
        while "Look up all publications" in page.content() or "Looking up" in page.content():
            assert time.monotonic() < deadline, "bulk lookup did not finish"
            time.sleep(0.2)
            page.goto(f"{base}/")
        shot("05-home-board")

        # The contact's card leads to drafting; the lines come prefilled.
        page.locator('.column[data-stage="contact"] a.pcard').first.click()
        page.wait_for_load_state()
        shot("06-contact")
        submit(page.locator("text=Draft an email"))
        expect(page.locator("#interests")).to_contain_text("Glaucoma progression")
        sentence = page.locator("textarea[name=personalized_sentence]")
        assert sentence.input_value().strip(), "the line about the PI is prefilled"
        assert "July" not in page.locator("textarea[name=body]").input_value()  # no start date
        picker = page.locator('select[data-fill="personalized_sentence"]')
        if picker.count():
            picker.select_option(index=1)
            assert sentence.input_value() in page.locator("textarea[name=body]").input_value()
        shot("07-draft")
        submit(page.locator("button[value=approve]"))
        expect(page.locator("main")).to_contain_text("Approved")
        expect(page.locator("main")).to_contain_text("Suggested send time")
        shot("08-approved")

        # Try it in Outlook (addressed to me), then practice-send it.
        page.locator("text=Try it in Outlook (sent to you)").click()
        page.wait_for_load_state()
        expect(page.locator("main")).to_contain_text("alex.synthetic@example.com")
        shot("09-try-outlook")
        page.go_back()
        submit(page.locator("button:has-text('Send (practice)')"))
        expect(page.locator("main")).to_contain_text("Recorded as sent")

        # A simulated reply, then record what it said.
        page.locator("a:has-text('Campaign with')").first.click()
        submit(page.locator("button:has-text('Simulate a PI reply')"))
        page.select_option("select[name=tag]", "positive")
        submit(page.locator("#reply button.primary"))
        expect(page.locator("main")).to_contain_text("Reply recorded: Positive")

        # Emails, Settings, reports and a backup.
        page.goto(f"{base}/approvals")
        expect(page.locator("main")).to_contain_text("Ready to send")
        page.goto(f"{base}/settings")
        expect(page.locator("main")).to_contain_text("Sending rules")
        shot("10-settings")
        page.goto(f"{base}/")
        shot("11-home-after")
        page.goto(f"{base}/data")
        submit(page.locator("text=Update now"))
        expect(page.locator("main")).to_contain_text("Exports updated")
        submit(page.locator("text=Back up now"))
        expect(page.locator("main")).to_contain_text("Backup saved")

        browser.close()
    assert problems == [], problems
