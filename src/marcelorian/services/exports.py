"""CSV exports and the owner report file (spec §13; owner decision U4).

* Files go to the Documents ``Marcelorian\\exports`` folder: live mode at the top, practice
  mode in ``practice\\`` and transport tests in ``test\\`` (modes never share exports).
* Each file is written to a temporary file in the same folder and swapped in with an
  atomic replace, as UTF-8 with BOM (Excel) and fully quoted. If Excel holds the file
  open, the export is marked stale and retried on the next worker cycle; sending state is
  never touched.
* **Formula-injection protection (reversible):** a cell that begins with ``= + - @``, a
  tab, a carriage return, or an apostrophe gets one leading apostrophe. ``unescape``
  removes exactly one leading apostrophe, so the original text is reconstructed exactly;
  SQLite keeps the exact text.
* Files: ``outreach_contacts.csv`` (one row per campaign), ``email_log.csv`` (one row
  per email and attempt, with the exact approved text), and for the owner (U4, instead
  of a Google Sheet) ``owner_report.csv`` (the spec's default column allowlist, no
  notes, CV, visa details or correspondence) and ``status_summary.csv`` (counts).
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from marcelorian.clock import Clock
from marcelorian.config.settings import load_settings
from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalSnapshot,
    Assessment,
    AuditEvent,
    Campaign,
    CampaignPause,
    GrantEvidence,
    Institution,
    LabGroup,
    Message,
    Pi,
    SendAttempt,
    Setting,
)
from marcelorian.domain.enums import Mode
from marcelorian.domain.snapshot import SnapshotError, verify_canonical
from marcelorian.services import ai, status
from marcelorian.services.recipients import send_ready_email

TRIGGERS = ("=", "+", "-", "@", "\t", "\r", "'")
STATE_KEY = "export_state"
MODE_FOLDERS = {Mode.LIVE: "", Mode.SANDBOX: "practice", Mode.TRANSPORT_TEST: "test"}

OUTREACH_COLUMNS = (
    "campaign_id", "mode", "pi_name", "salutation", "department", "institution",
    "institution_state", "lab_group", "lab_group_verified", "pi_email", "email_confidence",
    "ownership_verified", "current_role_verified", "research_fit", "grant_evidence",
    "campaign_state", "status", "pause_reason", "email1_sent_at", "email2_sent_at",
    "email3_sent_at", "email4_sent_at", "reply_detected", "reply_tag", "next_action",
    "next_due_at", "next_eligible_window", "cap_block_reason", "close_outcome", "notes",
)  # fmt: skip
EMAIL_LOG_COLUMNS = (
    "message_id", "campaign_id", "mode", "step", "approval_version", "snapshot_hash",
    "intended_recipient", "actual_recipient", "subject", "body", "attachment_name",
    "attachment_sha256", "approved_at", "attempt_id", "channel", "app_message_id", "sent_at",
    "outcome", "error",
)  # fmt: skip
#: Spec §13 default allowlist for the owner (Gmail coverage omitted: Outlook-only pilot).
OWNER_COLUMNS = (
    "campaign_id", "pi_name", "institution", "institution_state", "grant_evidence",
    "campaign_state", "status", "pause_reason", "email1_sent_at", "email2_sent_at",
    "email3_sent_at", "email4_sent_at", "reply_detected", "reply_tag", "next_action",
    "next_due_at", "cap_block_reason",
)  # fmt: skip


def escape(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(TRIGGERS) else text


def unescape(text: str) -> str:
    return text[1:] if text.startswith("'") else text


def _stamp(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else ""


def render_csv(columns: Sequence[str], rows: Iterable[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_ALL, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([escape(row.get(column, "")) for column in columns])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def write_atomic(path: Path, data: bytes) -> None:
    """Temp file in the same folder, then ``os.replace`` (raises if Excel locks the file)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #


def outreach_rows(session: Session, mode: Mode, now: datetime) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    campaigns = session.scalars(
        select(Campaign).where(Campaign.mode == mode.value).order_by(Campaign.id)
    ).all()
    for campaign in campaigns:
        pi = session.get(Pi, campaign.pi_id)
        assert pi is not None
        institution = session.get(Institution, pi.institution_id)
        lab = session.get(LabGroup, pi.lab_group_id) if pi.lab_group_id else None
        email = send_ready_email(session, pi.id)
        assessment = session.scalar(select(Assessment).where(Assessment.pi_id == pi.id))
        messages = {
            m.step: m
            for m in session.scalars(select(Message).where(Message.campaign_id == campaign.id))
        }
        pause = session.scalar(
            select(CampaignPause)
            .where(CampaignPause.campaign_id == campaign.id, CampaignPause.resolved_at.is_(None))
            .order_by(CampaignPause.id)
        )
        grants = int(
            session.scalar(
                select(func.count())
                .select_from(GrantEvidence)
                .where(
                    GrantEvidence.pi_id == pi.id, GrantEvidence.end_date >= now.date().isoformat()
                )
            )
            or 0
        )
        upcoming = messages.get(campaign.next_step or 0)
        any_sent = any(m.sent_at for m in messages.values())
        rows.append(
            {
                "campaign_id": campaign.id,
                "mode": campaign.mode,
                "pi_name": pi.full_name,
                "salutation": pi.salutation or "",
                "department": pi.department or "",
                "institution": institution.name if institution else "",
                "institution_state": institution.state if institution else "",
                "lab_group": lab.name if lab else "",
                "lab_group_verified": "Y" if lab and lab.verified else "N",
                "pi_email": email.address if email else "",
                "email_confidence": email.confidence if email else "",
                "ownership_verified": "Y" if email and email.ownership_verified else "N",
                "current_role_verified": "Y" if pi.current_role_verified else "N",
                "research_fit": assessment.research_fit if assessment else "",
                "grant_evidence": grants,
                "campaign_state": campaign.state,
                "status": status.STATUS_LABELS.get(status.category(campaign, any_sent), ""),
                "pause_reason": pause.reason if pause else "",
                **{
                    f"email{step}_sent_at": _stamp(messages[step].sent_at)
                    if step in messages
                    else ""
                    for step in (1, 2, 3, 4)
                },
                "reply_detected": "Y"
                if campaign.state == "conversation" or campaign.reply_tag
                else "N",
                "reply_tag": campaign.reply_tag or "",
                "next_action": _next_action(campaign, messages),
                "next_due_at": _stamp(campaign.next_due_at),
                "next_eligible_window": _stamp(upcoming.eligible_window_start) if upcoming else "",
                "cap_block_reason": (upcoming.cap_block_reason or "") if upcoming else "",
                "close_outcome": campaign.close_outcome or "",
                "notes": campaign.close_reason or "",
            }
        )
    return rows


def _next_action(campaign: Campaign, messages: dict[int, Message]) -> str:
    if campaign.state == "closed":
        return ""
    for step in sorted(messages, reverse=True):
        state = messages[step].state
        if state in ("awaiting_approval", "needs_reapproval", "failed_definite"):
            return f"review Email {step}"
        if state == "approved_queued":
            return f"send Email {step}"
        if state == "handed_off":
            return f"record Email {step} as sent or not sent"
    if campaign.next_step and not campaign.sequence_stopped:
        return f"Email {campaign.next_step} when due"
    if not messages:
        return "draft Email 1"
    return ""


def email_log_rows(session: Session, mode: Mode) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    messages = session.scalars(
        select(Message).where(Message.mode == mode.value).order_by(Message.id)
    ).all()
    for message in messages:
        snapshots = session.scalars(
            select(ApprovalSnapshot)
            .where(ApprovalSnapshot.message_id == message.id)
            .order_by(ApprovalSnapshot.version)
        ).all()
        for snapshot in snapshots:
            try:
                content = verify_canonical(snapshot.canonical_json, snapshot.sha256)
                body, attachments = content.body, content.attachments
            except SnapshotError:
                body, attachments = "(snapshot failed its integrity check)", ()
            base: dict[str, object] = {
                "message_id": message.id,
                "campaign_id": message.campaign_id,
                "mode": message.mode,
                "step": message.step,
                "approval_version": snapshot.version,
                "snapshot_hash": snapshot.sha256,
                "intended_recipient": snapshot.intended_recipient,
                "subject": snapshot.subject,
                "body": body,
                "attachment_name": "; ".join(a.filename for a in attachments),
                "attachment_sha256": "; ".join(a.sha256 for a in attachments),
                "approved_at": _stamp(snapshot.approved_at),
                "outcome": snapshot.state,
            }
            attempts = session.scalars(
                select(SendAttempt)
                .where(SendAttempt.snapshot_id == snapshot.id)
                .order_by(SendAttempt.id)
            ).all()
            if not attempts:
                rows.append(base)
            for attempt in attempts:
                rows.append(
                    {
                        **base,
                        "actual_recipient": attempt.recipient or "",
                        "attempt_id": attempt.attempt_uid,
                        "channel": attempt.channel,
                        "app_message_id": attempt.app_message_id or "",
                        "sent_at": _stamp(attempt.sent_at),
                        "outcome": attempt.status,
                        "error": attempt.error_code or "",
                    }
                )
    return rows


def summary_rows(
    session: Session, mode: Mode, scope: str, now: datetime
) -> list[dict[str, object]]:
    result = status.outreach_status(session, mode, scope, now)
    usage = ai.usage(session)
    rows: list[dict[str, object]] = [
        {"item": "Generated", "value": _stamp(now)},
        {"item": "Mode", "value": mode.value},
        {"item": "Applications contacted", "value": result.contacted},
        {"item": "Replies", "value": result.responded},
        {
            "item": "Response rate",
            "value": "" if result.response_rate is None else f"{result.response_rate}%",
        },
        {"item": "Waiting or needing attention", "value": result.pending},
    ]
    rows += [
        {"item": label, "value": result.counts[key]} for key, label in status.STATUS_LABELS.items()
    ]
    rows += [
        {"item": "Places in use", "value": len(result.places)},
        {"item": "Claude suggestions used", "value": usage.requests},
        {"item": "Estimated Claude cost (USD)", "value": f"{usage.estimated_usd:.2f}"},
    ]
    return rows


# --------------------------------------------------------------------------- #
# Writing and freshness
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExportState:
    last_success_at: str = ""
    stale: bool = False
    error: str = ""
    fingerprint: str = ""


def load_state(session: Session) -> ExportState:
    row = session.get(Setting, STATE_KEY)
    if row is None:
        return ExportState()
    data = json.loads(row.value_json)
    return ExportState(**{k: data.get(k, v) for k, v in ExportState().__dict__.items()})


def save_state(session: Session, state: ExportState, now: datetime) -> None:
    value = json.dumps(state.__dict__)
    row = session.get(Setting, STATE_KEY)
    if row is None:
        session.add(Setting(key=STATE_KEY, value_json=value, updated_at=now))
    else:
        row.value_json, row.updated_at = value, now


def fingerprint(session: Session) -> str:
    """Changes whenever anything is recorded (every change is audited)."""
    last = session.scalar(select(func.max(AuditEvent.id)))
    return str(last or 0)


def build_files(session: Session, mode: Mode, scope: str, now: datetime) -> dict[str, bytes]:
    return {
        "outreach_contacts.csv": render_csv(OUTREACH_COLUMNS, outreach_rows(session, mode, now)),
        "email_log.csv": render_csv(EMAIL_LOG_COLUMNS, email_log_rows(session, mode)),
        "owner_report.csv": render_csv(OWNER_COLUMNS, outreach_rows(session, mode, now)),
        "status_summary.csv": render_csv(
            ("item", "value"), summary_rows(session, mode, scope, now)
        ),
    }


def folder_for(exports_dir: Path, mode: Mode) -> Path:
    sub = MODE_FOLDERS.get(mode, mode.value)
    return exports_dir / sub if sub else exports_dir


def write_files(folder: Path, files: dict[str, bytes]) -> list[str]:
    """Write every file; returns the names that could not be replaced (e.g. open in Excel)."""
    failed = []
    for name, data in files.items():
        try:
            write_atomic(folder / name, data)
        except OSError:
            failed.append(name)
    return failed


def record_result(session: Session, failed: list[str], print_: str, now: datetime) -> ExportState:
    if failed:
        previous = load_state(session)
        state = ExportState(
            last_success_at=previous.last_success_at,
            stale=True,
            error="Could not update " + ", ".join(failed) + " (open in Excel?). Retrying.",
            fingerprint=previous.fingerprint,
        )
    else:
        state = ExportState(last_success_at=_stamp(now), fingerprint=print_)
    save_state(session, state, now)
    return state


def list_files(exports_dir: Path) -> list[tuple[str, Path, datetime]]:
    found = []
    for mode in MODE_FOLDERS:
        folder = folder_for(exports_dir, mode)
        for name in (
            "owner_report.csv",
            "status_summary.csv",
            "outreach_contacts.csv",
            "email_log.csv",
        ):
            path = folder / name
            if path.is_file():
                found.append((mode.value, path, datetime.fromtimestamp(path.stat().st_mtime, UTC)))
    return found


def parse(data: bytes) -> list[list[str]]:
    """Read an export back exactly (tests and support): BOM, quoting and escaping undone."""
    text = data.decode("utf-8-sig")
    return [[unescape(cell) for cell in row] for row in csv.reader(io.StringIO(text, newline=""))]


def refresh(
    session_factory: sessionmaker[Session], exports_dir: Path, clock: Clock, *, force: bool = False
) -> ExportState:
    """Rebuild the files when anything changed (or after a stale run); IO outside transactions."""
    with transaction(session_factory) as session:
        state = load_state(session)
        print_ = fingerprint(session)
        if not force and print_ == state.fingerprint and not state.stale:
            return state
        now = clock.now()
        scope = load_settings(session).same_place_scope
        modes = {Mode.LIVE} | {Mode(m) for m in session.scalars(select(Campaign.mode).distinct())}
        built = {mode: build_files(session, mode, scope, now) for mode in modes}
    failed: list[str] = []
    for mode, files in built.items():
        folder = folder_for(exports_dir, mode)
        failed += [
            f"{MODE_FOLDERS.get(mode) or 'live'}/{name}" for name in write_files(folder, files)
        ]
    with transaction(session_factory) as session:
        return record_result(session, failed, print_, now)
