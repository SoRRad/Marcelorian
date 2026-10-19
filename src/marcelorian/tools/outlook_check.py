"""Guided Outlook hand-off check (P0.3b; spec §9.8, §18; runbook "P0.3b").

Run on the applicant's Windows laptop::

    python -m marcelorian.tools.outlook_check --folder marcelorian-outlook-check

This tool **never sends anything**. It has no mail transport at all. It prepares
synthetic test emails through the production hand-off code, opens each one in
Outlook (a link or an ``.eml`` file) and asks you what happened. You press Send
in Outlook yourself. Recipients are only your own two consenting addresses:
**S** (the Outlook account you send from) and **R2** (a second address you
control, for example your personal Gmail). At most four test emails go out.

Everything is stored in a separate test database inside ``--folder``; your real
data is never touched. The report written at the end contains no addresses.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import subprocess
import sys
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock, SystemClock
from marcelorian.config.settings import load_settings, set_signature, update_settings
from marcelorian.config.specialty import load_specialty
from marcelorian.db.engine import make_engine, make_session_factory, transaction
from marcelorian.db.migrate import upgrade
from marcelorian.db.models import (
    ApplicantProfile,
    Campaign,
    Institution,
    LabGroup,
    Message,
    Pi,
    PiEmail,
)
from marcelorian.domain.enums import Actor, CampaignState, CloseOutcome, MessageState, Mode
from marcelorian.domain.handoff import HandoffPackage
from marcelorian.domain.recipients import RecipientError, normalize_address
from marcelorian.domain.snapshot import TestOverride
from marcelorian.domain.subjects import expected_subject
from marcelorian.services import approvals, handoff, recipients, runtime
from marcelorian.services import campaigns as campaign_svc

#: The follow-up is due this long after Email 1 is recorded as sent (transport_test
#: schedule override, decision B2), so the whole check fits in one sitting.
FOLLOWUP_AFTER_SECONDS = 60
#: Pacing for this test database only (the real default is 2-6 minutes).
TEST_PACING_SECONDS = (10, 20)
CHECKS = ("B1", "B2", "B3", "B4", "B5", "B6")
BANNER = "MARCELORIAN TEST - synthetic message, please ignore."


class Console(Protocol):
    def say(self, text: str) -> None: ...

    def ask(self, prompt: str, choices: tuple[str, ...]) -> str: ...

    def text(self, prompt: str) -> str: ...


class Opener(Protocol):
    def open_url(self, url: str) -> None: ...

    def open_file(self, path: Path) -> None: ...

    def copy(self, text: str) -> bool: ...


@dataclass
class CheckResult:
    status: str = "skipped"  # pass / fail / skipped / same / new
    note: str = ""


@dataclass
class Report:
    results: dict[str, CheckResult] = field(
        default_factory=lambda: {check: CheckResult() for check in CHECKS}
    )
    real_sends: int = 0

    def render(self) -> str:
        lines = [
            "Marcelorian P0.3b Outlook hand-off check - redacted report",
            f"Date: {datetime.now().astimezone().isoformat(timespec='minutes')}",
            f"Platform: {platform.platform()}",
            f"Python: {platform.python_version()}",
            "",
            "| # | Result | Notes |",
            "|---|---|---|",
        ]
        lines += [f"| {c} | {r.status} | {r.note} |" for c, r in self.results.items()]
        lines += ["", f"Real sends recorded by you: {self.real_sends} (must be <= 6)"]
        return "\n".join(lines) + "\n"


class CheckError(RuntimeError):
    pass


class OutlookCheck:
    """The check itself; console, opener, clock and sleep are injected (tests use fakes)."""

    def __init__(
        self,
        folder: Path,
        console: Console,
        opener: Opener,
        *,
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.folder = folder
        self.console = console
        self.opener = opener
        self.clock = clock or SystemClock()
        self.sleep = sleep
        self.report = Report()
        self.rng = random.SystemRandom()
        self.specialty = load_specialty().display_name
        self.factory: sessionmaker[Session]
        self.applicant_id = 0
        self.cv_id = 0
        self.r2_entry = 0
        self.s_entry = 0
        self.signature = ""

    # ------------------------------------------------------------------ #
    def run(self) -> Report:
        self._intro()
        self._setup()
        self._campaign_a()  # B1 link, B3 reply-and-paste, B6 reply
        self._campaign_b()  # B2 file, B4 file follow-up (thread test)
        self._campaign_c()  # B5 not sent
        text = self.report.render()
        (self.folder / "report.txt").write_text(text, encoding="utf-8")
        self.console.say("\n" + text)
        self.console.say(f"Report saved to {self.folder / 'report.txt'} (no addresses in it).")
        return self.report

    def _now(self) -> datetime:
        return self.clock.now()

    # ------------------------------------------------------------------ #
    def _intro(self) -> None:
        self.console.say(
            "Marcelorian Outlook hand-off check (P0.3b)\n"
            "This tool sends nothing. It prepares synthetic test emails and opens them in\n"
            "Outlook; you press Send yourself. Use only your own two addresses:\n"
            "  S  = the Outlook address you send from\n"
            "  R2 = a second address you control and agree to use (e.g. your personal Gmail)\n"
            "At most 4 test emails go out, all marked as tests. Takes about 15 minutes.\n"
        )

    def _setup(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        db = self.folder / "check.db"
        if db.exists():
            raise CheckError(f"{db} already exists; choose a new --folder for a fresh check")
        engine = make_engine(db)
        upgrade(engine)
        self.factory = make_session_factory(engine)
        sender = self._address("Your Outlook address (S)")
        second = self._address("Your second address (R2)")
        if second == sender:
            raise CheckError("S and R2 must be different addresses")
        self.signature = self.console.text(
            "Signature step (first use): type a short synthetic signature for this test,\n"
            "or press Enter to skip (Outlook on the web adds its own to links)"
        ).strip()
        now = self._now()
        with transaction(self.factory) as s:
            applicant = ApplicantProfile(created_at=now, updated_at=now)
            s.add(applicant)
            s.flush()
            self.applicant_id = applicant.id
            runtime.get_runtime(s, now)
            self.cv_id = approvals.upload_cv(
                s,
                applicant_id=applicant.id,
                filename="synthetic-test-cv.pdf",
                content_type="application/pdf",
                data=synthetic_pdf("Marcelorian test - synthetic CV (not a real document)"),
                actor=Actor.APPLICANT,
                now=now,
            ).id
            approvals.connect_handoff_mailbox(s, sender, actor=Actor.APPLICANT, now=now)
            set_signature(s, self.signature, actor=Actor.APPLICANT, now=now)
            low, high = TEST_PACING_SECONDS
            update_settings(
                s,
                {"pacing_min_seconds": low, "pacing_max_seconds": high},
                actor=Actor.APPLICANT,
                now=now,
                confirm_less_conservative=True,  # this separate test database only
            )
            note = "owner-assisted P0.3b check; consenting test address"
            self.r2_entry = recipients.add_allowlist_address(
                s, second, "R2", actor=Actor.APPLICANT, now=now, consent_note=note
            ).id
            self.s_entry = recipients.add_allowlist_address(
                s, sender, "S", actor=Actor.APPLICANT, now=now, consent_note=note
            ).id
            runtime.switch_mode(
                s, Mode.TRANSPORT_TEST, confirmed=True, actor=Actor.APPLICANT, now=now
            )
            runtime.resume_dispatch(s, actor=Actor.APPLICANT, now=now)

    def _address(self, label: str) -> str:
        while True:
            raw = self.console.text(label)
            try:
                return normalize_address(raw)
            except RecipientError:
                self.console.say("That is not a single plain email address; try again.")

    # ------------------------------------------------------------------ #
    def _campaign(self, letter: str, recipient_entry: int) -> int:
        now = self._now()
        with transaction(self.factory) as s:
            # Each test campaign is its own place, so the same-place rule (one active
            # application per institution) never holds campaign B behind campaign A.
            institution = Institution(
                name=f"Synthetic Test University {letter}",
                city="Testville",
                state="MN",
                iana_tz="America/Chicago",
                tz_verified=True,
                is_synthetic=True,
                created_at=now,
            )
            s.add(institution)
            s.flush()
            lab = LabGroup(
                institution_id=institution.id,
                name=f"Synthetic lab {letter}",
                is_synthetic=True,
                created_at=now,
            )
            s.add(lab)
            s.flush()
            pi = Pi(
                full_name=f"Pat Example{letter}",
                last_name=f"Example{letter}",
                salutation=f"Dr. Example{letter}",
                salutation_reviewed=True,
                institution_id=institution.id,
                lab_group_id=lab.id,
                is_synthetic=True,
                created_at=now,
            )
            s.add(pi)
            s.flush()
            s.add(
                PiEmail(
                    pi_id=pi.id,
                    address=f"pi-{letter.lower()}@synthetic-univ.test",
                    confidence="A",
                    ownership_verified=True,
                    source="synthetic",
                    created_at=now,
                )
            )
            s.flush()
            return approvals.create_campaign(
                s,
                applicant_id=self.applicant_id,
                pi_id=pi.id,
                mode=Mode.TRANSPORT_TEST,
                actor=Actor.APPLICANT,
                now=now,
                test_recipient_id=recipient_entry,
            ).id

    def _draft_and_approve(self, campaign_id: int, step: int, label: str, *, cv: bool) -> int:
        body = (
            f"{BANNER}\n\nDear Dr. Example,\n\nThis is synthetic test email {label} "
            "of the Outlook hand-off check. No reply is needed.\n\nBest regards,\nTest Applicant"
        )
        override = TestOverride(
            window_always_open=True,
            step_intervals_seconds=((step + 1, FOLLOWUP_AFTER_SECONDS),) if step < 4 else (),
        )
        now = self._now()
        with transaction(self.factory) as s:
            campaign = s.get(Campaign, campaign_id)
            assert campaign is not None
            message = approvals.save_draft(
                s,
                campaign,
                step,
                approvals.DraftContent(
                    subject=expected_subject(step, self.specialty),
                    body=body,
                    cv_version_ids=(self.cv_id,) if cv else (),
                    test_override=override,
                ),
                actor=Actor.APPLICANT,
                now=now,
            )
            approvals.approve(
                s,
                message,
                actor=Actor.APPLICANT,
                now=now,
                settings=load_settings(s),
                specialty_display_name=self.specialty,
            )
            return message.id

    def _wait_until_due(self, campaign_id: int) -> None:
        with transaction(self.factory) as s:
            campaign = s.get(Campaign, campaign_id)
            assert campaign is not None and campaign.next_due_at is not None
            due = campaign.next_due_at
        remaining = (due - self._now()).total_seconds()
        if remaining > 0:
            self.console.say(f"Waiting {int(remaining) + 1} s until the follow-up is due...")
            self.sleep(remaining + 1)

    def _hand_off(self, message_id: int, *, followup: bool) -> handoff.HandoffResult:
        """Hand off once the send button unlocks (pacing countdown), never earlier."""
        while True:
            with transaction(self.factory) as s:
                message = s.get(Message, message_id)
                assert message is not None
                ready = handoff.readiness(s, message, load_settings(s), self._now())
            if ready.ready:
                break
            if ready.unlock_at is None:
                raise CheckError(f"the send button is blocked: {ready.reason}")
            wait = (ready.unlock_at - self._now()).total_seconds()
            self.console.say(
                f"Send button unlocks in {int(wait) + 1} s (spacing between emails)..."
            )
            self.sleep(max(wait, 0) + 0.5)
        with transaction(self.factory) as s:
            message = s.get(Message, message_id)
            assert message is not None
            result = handoff.hand_off(
                s,
                message,
                handoff.Confirmations(prior_contact_checked=not followup, no_reply=followup),
                settings=load_settings(s),
                rng=self.rng,
                actor=Actor.APPLICANT,
                now=self._now(),
            )
        if result.outcome != "handed_off" or result.package is None:
            raise CheckError(f"hand-off refused: {result.reason}")
        return result

    def _record(self, attempt_uid: str, check: str) -> bool:
        """Ask whether the applicant sent it; record Sent or Not sent. True if sent."""
        answer = self.console.ask(
            "Did you send it from Outlook? [s] Sent / [n] Not sent", ("s", "n")
        )
        with transaction(self.factory) as s:
            if answer == "s":
                edited = self.console.ask("Did you edit it in Outlook first? [y/n]", ("y", "n"))
                handoff.record_sent(
                    s,
                    attempt_uid,
                    edited=edited == "y",
                    settings=load_settings(s),
                    actor=Actor.APPLICANT,
                    now=self._now(),
                )
                self.report.real_sends += 1
                return True
            handoff.record_not_sent(
                s, attempt_uid, note=f"{check}: not sent", actor=Actor.APPLICANT, now=self._now()
            )
        return False

    def _confirm_prior_contact(self) -> None:
        self.console.ask(
            "Before Email 1 the app asks you to search Outlook for the PI's addresses.\n"
            "Here the 'PI' is your own R2 address, so earlier mail is expected.\n"
            "Confirm to continue [y]",
            ("y",),
        )

    def _save_eml(self, package: HandoffPackage) -> Path:
        directory = self.folder / "email-files"
        directory.mkdir(exist_ok=True)
        path = directory / package.eml_filename
        path.write_bytes(package.eml)
        return path

    # ------------------------------------------------------------------ #
    def _campaign_a(self) -> None:
        self.console.say("\n== Campaign A: Email 1 by link (B1) ==")
        campaign = self._campaign("A", self.r2_entry)
        mid = self._draft_and_approve(campaign, 1, "A1", cv=False)
        self._confirm_prior_contact()
        result = self._hand_off(mid, followup=False)
        assert result.package and result.package.compose_url and result.attempt_uid
        self.opener.open_url(result.package.compose_url)
        ok = self.console.ask(
            "B1: did a NEW Outlook message open with To = R2, the subject\n"
            f"'{result.package.subject}' and the test text filled in (plus your Outlook\n"
            "signature, if you have one set up)? [y/n]",
            ("y", "n"),
        )
        self.report.results["B1"] = CheckResult("pass" if ok == "y" else "fail")
        if not self._record(result.attempt_uid, "B1"):
            self.report.results["B1"].note = "not sent; B3 and B6 skipped"
            self._close(campaign)  # frees R2 for campaign B (one test campaign per address)
            return

        self.console.say("\n== Campaign A: follow-up by Reply and paste (B3) ==")
        self._wait_until_due(campaign)
        mid2 = self._draft_and_approve(campaign, 2, "A2", cv=False)
        self.console.ask(
            "The app asks: has the PI (R2) replied? It should not have. [y=no reply]", ("y",)
        )
        result = self._hand_off(mid2, followup=True)
        assert result.package and result.attempt_uid
        copied = self.opener.copy(result.package.body)
        self.console.say(
            (
                "The follow-up text is on your clipboard.\n"
                if copied
                else "Follow-up text:\n\n" + result.package.body + "\n"
            )
            + "In Outlook open Sent Items, open the email you just sent to R2, click Reply,\n"
            "paste the text (Ctrl+V) and send it."
        )
        if self._record(result.attempt_uid, "B3"):
            same = self.console.ask(
                "B3: in R2's mailbox, did it arrive in the SAME conversation as the first\n"
                "email, with a single 'Re:' in the subject? [y/n]",
                ("y", "n"),
            )
            self.report.results["B3"] = CheckResult("pass" if same == "y" else "fail")
        else:
            self.report.results["B3"] = CheckResult("skipped", "not sent")

        self.console.say("\n== Campaign A: reply reported by you (B6) ==")
        self.console.ask(
            "From R2's mailbox, reply to that conversation (any short text).\n"
            "When the reply has arrived in Outlook, confirm [y]",
            ("y",),
        )
        with transaction(self.factory) as s:
            row = s.get(Campaign, campaign)
            assert row is not None
            handoff.report_reply(
                s, row, note="B6 test reply", actor=Actor.APPLICANT, now=self._now()
            )
        with transaction(self.factory) as s:
            row = s.get(Campaign, campaign)
            assert row is not None
            stopped = row.state == CampaignState.CONVERSATION.value and row.next_step is None
        self.report.results["B6"] = CheckResult(
            "pass" if stopped else "fail",
            "campaign in conversation, no next step" if stopped else "",
        )

    def _campaign_b(self) -> None:
        self.console.say("\n== Campaign B: Email 1 by file (B2) ==")
        campaign = self._campaign("B", self.r2_entry)
        mid = self._draft_and_approve(campaign, 1, "B1", cv=True)
        self._confirm_prior_contact()
        result = self._hand_off(mid, followup=False)
        assert result.package and result.attempt_uid
        path = self._save_eml(result.package)
        self.opener.open_file(path)
        signature = "your test signature at the end, " if self.signature else ""
        ok = self.console.ask(
            f"(If nothing opened, double-click {path}.)\n"
            "B2: did it open as an EDITABLE new message (with a Send button), with To = R2,\n"
            f"the subject, the test text, {signature}the synthetic CV attached,\n"
            "and no From field to fill in? [y/n]",
            ("y", "n"),
        )
        self.report.results["B2"] = CheckResult("pass" if ok == "y" else "fail")
        if not self._record(result.attempt_uid, "B2"):
            self.report.results["B2"].note = "not sent; B4 skipped"
            self._close(campaign)
            return

        self.console.say("\n== Campaign B: follow-up by file - the thread test (B4) ==")
        self._wait_until_due(campaign)
        mid2 = self._draft_and_approve(campaign, 2, "B2", cv=False)
        self.console.ask(
            "The app asks: has the PI (R2) replied? It should not have. [y=no reply]", ("y",)
        )
        result = self._hand_off(mid2, followup=True)
        assert result.package and result.attempt_uid
        path = self._save_eml(result.package)
        self.opener.open_file(path)
        self.console.say(f"(If nothing opened, double-click {path}.) Send it from Outlook.")
        if self._record(result.attempt_uid, "B4"):
            where = self.console.ask(
                "B4: in R2's mailbox, did it land in the SAME conversation as campaign B's\n"
                "first email, or as a NEW conversation? [same/new]",
                ("same", "new"),
            )
            self.report.results["B4"] = CheckResult(
                where, "the file keeps the thread" if where == "same" else "use Reply and paste"
            )
        else:
            self.report.results["B4"] = CheckResult("skipped", "not sent")
        self._close(campaign)

    def _campaign_c(self) -> None:
        self.console.say("\n== Campaign C: Not sent (B5) - do NOT send this one ==")
        campaign = self._campaign("C", self.s_entry)
        mid = self._draft_and_approve(campaign, 1, "C1", cv=False)
        self.console.ask(
            "This test email goes to your own address S. Confirm to continue [y]", ("y",)
        )
        result = self._hand_off(mid, followup=False)
        assert result.package and result.package.compose_url and result.attempt_uid
        self.opener.open_url(result.package.compose_url)
        self.console.ask(
            "Close that Outlook message WITHOUT sending it (discard the draft).\n"
            "Record 'Not sent'? This cannot be undone. [y]",
            ("y",),
        )
        with transaction(self.factory) as s:
            handoff.record_not_sent(
                s, result.attempt_uid, note="B5", actor=Actor.APPLICANT, now=self._now()
            )
        nothing = self.console.ask(
            "Confirm nothing arrived in S's inbox from this test [y/n]", ("y", "n")
        )
        with transaction(self.factory) as s:
            message = s.get(Message, mid)
            assert message is not None
            reapproval = message.state == MessageState.NEEDS_REAPPROVAL.value
        passed = reapproval and nothing == "y"
        self.report.results["B5"] = CheckResult(
            "pass" if passed else "fail", "needs re-approval" if reapproval else "state wrong"
        )
        self._close(campaign)

    def _close(self, campaign_id: int) -> None:
        with transaction(self.factory) as s:
            row = s.get(Campaign, campaign_id)
            assert row is not None
            if row.state not in (CampaignState.CLOSED.value, CampaignState.CANDIDATE.value):
                campaign_svc.close_campaign(
                    s,
                    row,
                    CloseOutcome.APPLICANT_CLOSED,
                    actor=Actor.APPLICANT,
                    now=self._now(),
                    reason="test complete",
                )


def synthetic_pdf(text: str) -> bytes:
    """A minimal valid one-page PDF showing ``text`` (synthetic attachment)."""
    safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 14 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1", "replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(out)


# --------------------------------------------------------------------------- #
# Real console and opener
# --------------------------------------------------------------------------- #


class TerminalConsole:
    def say(self, text: str) -> None:
        print(text)

    def ask(self, prompt: str, choices: tuple[str, ...]) -> str:
        while True:
            answer = input(prompt + " > ").strip().lower()
            if answer in choices:
                return answer
            print("Please answer one of: " + ", ".join(choices))

    def text(self, prompt: str) -> str:
        return input(prompt + " > ")


class DesktopOpener:
    def open_url(self, url: str) -> None:
        if not webbrowser.open(url):
            print("Open this link in your browser:\n" + url)

    def open_file(self, path: Path) -> None:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: S606 (a file this tool just wrote)
        else:
            print(f"Open this file in Outlook: {path}")

    def copy(self, text: str) -> bool:
        if sys.platform != "win32":
            return False
        try:  # clip.exe reads UTF-16 with a byte-order mark
            subprocess.run(["clip"], input=text.encode("utf-16"), check=True)  # noqa: S607
        except (OSError, subprocess.CalledProcessError):
            return False
        return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--folder", type=Path, default=Path("marcelorian-outlook-check"))
    args = parser.parse_args(argv)
    try:
        OutlookCheck(args.folder, TerminalConsole(), DesktopOpener()).run()
    except CheckError as exc:
        print(f"Stopped: {exc}")
        return 2
    except KeyboardInterrupt:
        print("\nStopped by you. Nothing is sent by this tool; start again with a new --folder.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
