"""The guided Outlook hand-off check (P0.3b) driven end to end with scripted answers.

No browser, no Outlook, no network: the opener and console are fakes and the clock
is injected. The tool has no transport, so nothing can be sent by it.
"""

from __future__ import annotations

import email
import re
from datetime import UTC, datetime, timedelta
from email.policy import default as default_policy
from pathlib import Path

import pytest
from sqlalchemy import select

from marcelorian.clock import ManualClock
from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.models import Campaign, Message, SendAttempt
from marcelorian.tools.outlook_check import CheckError, OutlookCheck, synthetic_pdf

S = "applicant.test@synthetic-hospital.test"
R2 = "applicant.personal@synthetic-mail.test"
SUNDAY_NIGHT = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)  # outside any real send window


class ScriptedConsole:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)

    def ask(self, prompt: str, choices: tuple[str, ...]) -> str:
        self.prompts.append(prompt)
        answer = self.answers.pop(0)
        assert answer in choices, (prompt, answer, choices)
        return answer

    def text(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answers.pop(0)


class FakeOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.files: list[Path] = []
        self.copies: list[str] = []

    def open_url(self, url: str) -> None:
        self.urls.append(url)

    def open_file(self, path: Path) -> None:
        self.files.append(path)

    def copy(self, text: str) -> bool:
        self.copies.append(text)
        return True


HAPPY = [
    S,
    R2,
    "Test Applicant, MD",  # setup: S, R2, signature step
    "y",
    "y",
    "s",
    "n",  # A: prior contact, B1 ok, sent, not edited
    "y",
    "s",
    "n",
    "y",  # A follow-up: no reply, sent, not edited, same conversation
    "y",  # B6: reply arrived
    "y",
    "y",
    "s",
    "n",  # B: prior contact, B2 ok, sent, not edited
    "y",
    "s",
    "n",
    "new",  # B follow-up: no reply, sent, not edited, new conversation
    "y",
    "y",
    "y",  # C: confirm, Not sent, nothing arrived
]


def _run(tmp_path: Path, answers: list[str]) -> tuple[OutlookCheck, ScriptedConsole, FakeOpener]:
    clock = ManualClock(SUNDAY_NIGHT)
    console, opener = ScriptedConsole(answers), FakeOpener()
    check = OutlookCheck(
        tmp_path / "check",
        console,
        opener,
        clock=clock,
        sleep=lambda seconds: clock.advance(timedelta(seconds=seconds)),
    )
    check.run()
    assert console.answers == []  # every scripted answer was asked for
    return check, console, opener


def test_full_check_records_every_result(tmp_path: Path) -> None:
    check, _console, opener = _run(tmp_path, HAPPY)
    results = {k: v.status for k, v in check.report.results.items()}
    assert results == {
        "B1": "pass",
        "B2": "pass",
        "B3": "pass",
        "B4": "new",
        "B5": "pass",
        "B6": "pass",
    }
    assert check.report.real_sends == 4
    assert len(opener.urls) == 2 and len(opener.files) == 2 and len(opener.copies) == 1
    assert all(
        u.startswith("https://outlook.office.com/mail/deeplink/compose?") for u in opener.urls
    )
    # The follow-up copy text and the file carry the signature; the link never does.
    assert opener.copies[0].endswith("Test Applicant, MD\n")
    assert (
        "Test+Applicant%2C+MD" not in opener.urls[0] and "Applicant%2C%20MD" not in opener.urls[0]
    )
    first_file = email.message_from_bytes(opener.files[0].read_bytes(), policy=default_policy)
    assert first_file["X-Unsent"] == "1" and first_file["From"] is None
    assert [a.get_filename() for a in first_file.iter_attachments()] == ["synthetic-test-cv.pdf"]
    follow_file = email.message_from_bytes(opener.files[1].read_bytes(), policy=default_policy)
    assert follow_file["Subject"].startswith("Re: Research Position Application")


def test_report_has_no_addresses(tmp_path: Path) -> None:
    check, _console, _opener = _run(tmp_path, HAPPY)
    text = (check.folder / "report.txt").read_text(encoding="utf-8")
    assert S not in text and R2 not in text and "@" not in text
    assert "| B4 | new | use Reply and paste |" in text


def test_database_ends_in_the_expected_states(tmp_path: Path) -> None:
    check, _console, _opener = _run(tmp_path, HAPPY)
    factory = make_session_factory(make_engine(check.folder / "check.db"))
    with transaction(factory) as s:
        states = [c.state for c in s.scalars(select(Campaign).order_by(Campaign.id))]
        assert states == ["conversation", "closed", "closed"]
        attempts = [(a.channel, a.status) for a in s.scalars(select(SendAttempt))]
        assert attempts.count(("outlook_handoff", "sent")) == 4
        assert attempts.count(("outlook_handoff", "not_submitted")) == 1
        assert all(channel == "outlook_handoff" for channel, _ in attempts)
        c1 = s.scalars(select(Message).order_by(Message.id)).all()[-1]
        assert c1.state == "cancelled"  # campaign C closed after its Not sent


def test_not_sending_email1_skips_its_followup_and_reply(tmp_path: Path) -> None:
    answers = [S, R2, "", "y", "y", "n", *HAPPY[12:]]  # A1 not sent; B and C as before
    check, _console, opener = _run(tmp_path, answers)
    results = check.report.results
    assert results["B1"].note == "not sent; B3 and B6 skipped"
    assert (results["B3"].status, results["B6"].status) == ("skipped", "skipped")
    assert results["B2"].status == "pass" and results["B5"].status == "pass"
    assert check.report.real_sends == 2
    assert opener.copies == []


def test_a_used_folder_is_refused(tmp_path: Path) -> None:
    _run(tmp_path, HAPPY)
    with pytest.raises(CheckError):
        OutlookCheck(tmp_path / "check", ScriptedConsole([]), FakeOpener()).run()


def test_same_address_twice_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CheckError):
        OutlookCheck(tmp_path / "x", ScriptedConsole([S, S]), FakeOpener()).run()


def test_synthetic_pdf_is_well_formed() -> None:
    data = synthetic_pdf("Synthetic (test) CV")
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref_at = int(re.search(rb"startxref\n(\d+)", data).group(1))  # type: ignore[union-attr]
    assert data[xref_at:].startswith(b"xref")
    offsets = re.findall(rb"(\d{10}) 00000 n", data)
    for number, offset in enumerate(offsets, start=1):
        assert data[int(offset) :].startswith(f"{number} 0 obj".encode())
