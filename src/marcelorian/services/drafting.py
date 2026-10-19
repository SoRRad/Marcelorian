"""Drafting: templates filled from confirmed facts plus personal lines (spec §8; U2).

* Rule-based fields come only from **confirmed** profile facts and reviewed PI data
  (spec §8.2): salutation, full name, current role, availability (Month YYYY, plus the
  duration only when confirmed), funding line (empty unless the applicant wrote one),
  Email 3's update (only from "recent news"), the CV phrase and the closing name.
* Personal lines (``personalized_sentence``, ``skills_line``, ``contribution_line``,
  ``pi_topic``) are written by the applicant, suggested by Claude when a key is set
  (P2, optional), or filled with clearly labelled practice text in sandbox (D5), which
  is never send-ready in a real mode.
* The closing ``{signature}`` renders the applicant's closing name; the signature block
  is added at hand-off (owner decision S8), so it never appears twice.
* The checklist (spec §8.3) is deterministic and reruns on the final edited text; it
  checks provenance and structure, and the applicant's review is the final factual check.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, CvVersion, Message, Pi, Setting
from marcelorian.domain.enums import Actor, FactStatus, MessageState, Mode
from marcelorian.domain.subjects import expected_subject
from marcelorian.services import approvals, dnc, profile
from marcelorian.services.recipients import send_ready_email

DEFAULT_TEMPLATES: dict[int, str] = {
    1: (
        "Dear {salutation},\n\n"
        "My name is {full_name}, and I am {current_role_phrase}. I am writing to ask whether "
        "your group expects an opening for a research fellow or similar position starting "
        "{availability}.\n\n"
        "{personalized_sentence}\n\n"
        "My background includes {skills_line}. I would be glad to contribute to "
        "{contribution_line}.\n\n"
        "{funding_line}\n\n"
        "I have attached my CV for your reference. Thank you for your time and "
        "consideration.\n\n"
        "Best regards,\n{signature}"
    ),
    2: (
        "Dear {salutation},\n\n"
        "I am following up briefly on my earlier email about research fellow opportunities "
        "in your group. Your work on {pi_topic} remains of strong interest to me, and I "
        "would welcome any guidance on openings.\n\n"
        "Best regards,\n{signature_name}"
    ),
    3: (
        "Dear {salutation},\n\n"
        "I am writing once more in case my earlier email was missed. {optional_update}\n\n"
        "If there is no opening at present, I would be grateful if you could suggest a "
        "colleague who may be recruiting.\n\n"
        "Best regards,\n{signature_name}"
    ),
    4: (
        "Dear {salutation},\n\n"
        "I am reaching out a final time about research opportunities in your group. I have "
        "attached {cv_phrase} for your reference. If the timing is better later, I would be "
        "glad to reconnect then.\n\n"
        "Thank you for your consideration.\n\n"
        "Best regards,\n{signature_name}"
    ),
}
PERSONAL_FIELDS: dict[str, str] = {
    "personalized_sentence": "A sentence about this PI's work and why it interests you",
    "skills_line": 'Completes "My background includes …"',
    "contribution_line": 'Completes "I would be glad to contribute to …"',
    "pi_topic": 'Completes "Your work on … remains of strong interest to me"',
}
RULE_FIELDS = (
    "salutation",
    "full_name",
    "current_role_phrase",
    "availability",
    "funding_line",
    "optional_update",
    "cv_phrase",
    "signature",
    "signature_name",
)
ALLOWED = frozenset(PERSONAL_FIELDS) | frozenset(RULE_FIELDS)
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
MAX_TEMPLATE = 6000
MAX_FIELD = 600
PRACTICE_PREFIX = "[Practice text] "
CANNED: dict[str, str] = {
    "personalized_sentence": PRACTICE_PREFIX + "I read about your group's recent imaging work.",
    "skills_line": PRACTICE_PREFIX + "clinical research and data analysis",
    "contribution_line": PRACTICE_PREFIX + "your ongoing projects",
    "pi_topic": PRACTICE_PREFIX + "retinal imaging",
}
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


class DraftError(ValueError):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def template(session: Session, step: int) -> str:
    row = session.get(Setting, f"template_email{step}")
    if row is None:
        return DEFAULT_TEMPLATES[step]
    value = json.loads(row.value_json)
    return value if isinstance(value, str) and value else DEFAULT_TEMPLATES[step]


def set_template(session: Session, step: int, text: str, *, now: datetime) -> None:
    """Save an edited body template (the subject is fixed, C5). Empty restores the default."""
    if step not in DEFAULT_TEMPLATES:
        raise DraftError(["unknown email step"])
    body = text.replace("\r\n", "\n").strip() or DEFAULT_TEMPLATES[step]
    if len(body) > MAX_TEMPLATE:
        raise DraftError([f"template longer than {MAX_TEMPLATE} characters"])
    unknown = sorted(set(PLACEHOLDER.findall(body)) - ALLOWED)
    if unknown:
        raise DraftError(["unknown placeholders: " + ", ".join(unknown)])
    stray = PLACEHOLDER.sub("", body)
    if "{" in stray or "}" in stray:
        raise DraftError(["unmatched { or } in the template"])
    if "{salutation}" not in body:
        raise DraftError(["the template must start with the salutation placeholder"])
    key = f"template_email{step}"
    row = session.get(Setting, key)
    value = json.dumps(body if body != DEFAULT_TEMPLATES[step] else "")
    if row is None:
        session.add(Setting(key=key, value_json=value, updated_at=now))
    else:
        row.value_json, row.updated_at = value, now


def personal_fields_for(session: Session, step: int) -> list[str]:
    return [f for f in PERSONAL_FIELDS if "{" + f + "}" in template(session, step)]


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


@dataclass
class Rendered:
    subject: str
    body: str
    fact_ids: list[int] = field(default_factory=list)
    cv_ids: list[int] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


def month_year(value: str) -> str:
    year, month = value.split("-")
    return f"{_MONTHS[int(month) - 1]} {year}"


def _confirmed(facts: dict[str, Any], key: str) -> Any:
    fact = facts.get(key)
    if fact is None or fact.status != FactStatus.CONFIRMED.value:
        return None
    return fact


def latest_cv(session: Session, applicant_id: int) -> CvVersion | None:
    versions = profile.cv_versions(session, applicant_id)
    return versions[0] if versions else None


def first_cv_id(session: Session, campaign: Campaign) -> int | None:
    message = session.scalar(
        select(Message).where(Message.campaign_id == campaign.id, Message.step == 1)
    )
    if message is None:
        return None
    ids = json.loads(message.draft_attachments_json or "[]")
    return int(ids[0]) if ids else None


def render(
    session: Session,
    campaign: Campaign,
    step: int,
    personal: dict[str, str],
    *,
    specialty_display_name: str,
) -> Rendered:
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    facts = profile.facts(session, campaign.applicant_id)
    used: list[int] = []
    missing: list[str] = []

    def fact_value(key: str, label: str, *, required: bool) -> str:
        fact = _confirmed(facts, key)
        if fact is None or not fact.value:
            if required:
                missing.append(label)
            return ""
        used.append(fact.id)
        return str(fact.value)

    text = template(session, step)
    names = set(PLACEHOLDER.findall(text))
    values: dict[str, str] = {}
    if "salutation" in names:
        values["salutation"] = pi.salutation or ""
        if not pi.salutation:
            missing.append("PI salutation")
    if "full_name" in names:
        values["full_name"] = fact_value("full_name", "Full name", required=True)
    if "current_role_phrase" in names:
        values["current_role_phrase"] = fact_value(
            "current_role_phrase", "Current role", required=True
        )
    if "availability" in names:
        start = fact_value("earliest_start", "Earliest start", required=True)
        duration = fact_value("desired_duration", "Desired duration", required=False)
        values["availability"] = (
            month_year(start) + (f" for {duration}" if duration else "") if start else ""
        )
    if "funding_line" in names:
        values["funding_line"] = fact_value("funding_line", "Funding line", required=False)
    if "optional_update" in names:
        values["optional_update"] = fact_value("recent_news", "Recent news", required=False)
    closing = fact_value("signature_name", "Closing name", required=False) or fact_value(
        "full_name", "Full name", required=True
    )
    values["signature"] = closing
    values["signature_name"] = closing
    cv = latest_cv(session, campaign.applicant_id)
    cv_ids = [cv.id] if cv is not None and step in (1, 4) else []
    if step in (1, 4) and cv is None:
        missing.append("CV")
    if "cv_phrase" in names:
        first = first_cv_id(session, campaign)
        values["cv_phrase"] = "my updated CV" if cv and first and cv.id != first else "my CV"
    for name in PERSONAL_FIELDS:
        if name in names:
            value = " ".join(personal.get(name, "").split())
            values[name] = value
            if not value:
                missing.append(PERSONAL_FIELDS[name])
    body = PLACEHOLDER.sub(lambda m: values.get(m.group(1), m.group(0)), text)
    body = _tidy(body)
    return Rendered(
        subject=expected_subject(step, specialty_display_name),
        body=body,
        fact_ids=sorted(set(used)),
        cv_ids=cv_ids,
        missing=list(dict.fromkeys(missing)),
    )


def _tidy(body: str) -> str:
    """Drop lines left empty by an empty optional field and collapse blank runs."""
    lines = [line.rstrip() for line in body.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


# --------------------------------------------------------------------------- #
# Saving
# --------------------------------------------------------------------------- #


def save(
    session: Session,
    campaign: Campaign,
    step: int,
    personal: dict[str, str],
    sources: dict[str, str],
    *,
    body_override: str | None,
    specialty_display_name: str,
    now: datetime,
    ai_basis: dict[str, dict[str, Any]] | None = None,
) -> Message:
    """Render, then save through ``approvals.save_draft`` (which handles versions, B18).

    ``ai_basis`` keeps, per Claude-suggested line, the text and the evidence and facts it
    cites; a line whose saved text differs from the suggestion is the applicant's (C4).
    """
    clean = {
        k: " ".join(v.split())[:MAX_FIELD] for k, v in personal.items() if k in PERSONAL_FIELDS
    }
    rendered = render(
        session,
        campaign,
        step,
        clean,
        specialty_display_name=specialty_display_name,
    )
    body = rendered.body
    edited = False
    if body_override is not None:
        override = body_override.replace("\r\n", "\n").strip() + "\n"
        if override.strip() and override != body:
            body, edited = override, True
    message = approvals.save_draft(
        session,
        campaign,
        step,
        approvals.DraftContent(
            subject=rendered.subject,
            body=body,
            cv_version_ids=tuple(rendered.cv_ids),
            fact_ids=tuple(rendered.fact_ids),
        ),
        actor=Actor.APPLICANT,
        now=now,
    )
    final_sources = {k: sources.get(k, "applicant") for k in clean}
    kept_basis: dict[str, dict[str, Any]] = {}
    for key, source in final_sources.items():
        item = (ai_basis or {}).get(key)
        if source == "ai" and item is not None and item.get("text") == clean[key]:
            kept_basis[key] = item
        elif source == "ai":
            final_sources[key] = "applicant"
    message.draft_fields_json = json.dumps(
        {
            "personal": clean,
            "sources": final_sources,
            "ai": kept_basis,
            "body_edited": edited,
        },
        sort_keys=True,
    )
    return message


def carry_forward(
    previous: Message | None, personal: dict[str, str]
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Sources for a re-save: unchanged suggested or practice lines keep their source.

    Any line the applicant changed (or wrote) is theirs (C4), and its AI basis is dropped.
    """
    fields = draft_fields(previous) if previous is not None else {}

    def section(name: str) -> dict[str, Any]:
        value = fields.get(name)
        return value if isinstance(value, dict) else {}

    old_personal, old_sources, old_basis = section("personal"), section("sources"), section("ai")
    sources: dict[str, str] = {}
    kept: dict[str, dict[str, Any]] = {}
    for key, text in personal.items():
        same = " ".join(text.split()) == old_personal.get(key)
        source = old_sources.get(key, "applicant") if same and text.strip() else "applicant"
        sources[key] = source if source in ("ai", "practice") else "applicant"
        if sources[key] == "ai" and isinstance(old_basis.get(key), dict):
            kept[key] = old_basis[key]
        elif sources[key] == "ai":
            sources[key] = "applicant"
    return sources, kept


def draft_fields(message: Message) -> dict[str, Any]:
    try:
        data = json.loads(message.draft_fields_json or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------- #
# Checklist (spec §8.3)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Check:
    key: str
    label: str
    ok: bool
    detail: str = ""


def checklist(
    session: Session, message: Message, *, specialty_display_name: str, today: date
) -> list[Check]:
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    body = message.draft_body or ""
    lower = body.lower()
    mode = Mode(message.mode)
    attachments = json.loads(message.draft_attachments_json or "[]")
    fields = draft_fields(message)
    personal = fields.get("personal", {}) if isinstance(fields.get("personal"), dict) else {}
    first_line = body.split("\n", 1)[0]
    ready = send_ready_email(session, pi.id)
    rendered = render(
        session,
        campaign,
        message.step,
        personal,
        specialty_display_name=specialty_display_name,
    )
    deps = json.loads(message.draft_dependencies_json or "{}")
    stale = [
        fact_id
        for fact_id, version in deps.get("facts", [])
        if not _fact_current(session, fact_id, version)
    ]
    first = first_cv_id(session, campaign)
    updated_ok = "updated cv" not in lower or (
        bool(attachments) and first is not None and int(attachments[0]) != first
    )
    checks = [
        Check("no_braces", "No { or } left in the text", "{" not in body and "}" not in body),
        Check(
            "salutation",
            "Salutation present and names this PI",
            first_line.startswith("Dear ") and pi.last_name.casefold() in first_line.casefold(),
            first_line,
        ),
        Check(
            "recipient",
            "Recipient address is send-ready (A or B, ownership verified)",
            ready is not None,
            ready.address if ready else "no send-ready address",
        ),
        Check(
            "attachment",
            'CV attached whenever the text says "attached"',
            ("attached" not in lower) or bool(attachments),
        ),
        Check("updated_cv", '"updated CV" only with a newer CV than Email 1', updated_ok),
        Check(
            "subject",
            "Subject is exact",
            message.draft_subject == expected_subject(message.step, specialty_display_name),
            message.draft_subject or "",
        ),
        Check("dnc", "Not on the do-not-contact list", not dnc.matches(session, pi.id, mode)),
        Check(
            "facts",
            "Uses only your confirmed, current profile answers",
            not rendered.missing and not stale,
            ", ".join(rendered.missing) or ("profile changed: re-save the draft" if stale else ""),
        ),
        Check(
            "practice_text",
            "No practice text in a real email",
            mode is Mode.SANDBOX or PRACTICE_PREFIX.strip() not in body,
        ),
        Check(
            "funding",
            "Your funding preference is respected",
            True,
            "No explicitly unpaid opportunity is linked to this contact.",
        ),
    ]
    return checks


def _fact_current(session: Session, fact_id: int, version: int) -> bool:
    from marcelorian.db.models import ProfileFact

    fact = session.get(ProfileFact, fact_id)
    return (
        fact is not None and fact.version == version and fact.status == FactStatus.CONFIRMED.value
    )


def ready_to_approve(checks: list[Check]) -> bool:
    return all(c.ok for c in checks)


def ensure_campaign(
    session: Session, applicant_id: int, pi_id: int, mode: Mode, now: datetime
) -> Campaign:
    campaign = session.scalar(
        select(Campaign).where(
            Campaign.applicant_id == applicant_id,
            Campaign.pi_id == pi_id,
            Campaign.mode == mode.value,
        )
    )
    if campaign is None:
        campaign = approvals.create_campaign(
            session,
            applicant_id=applicant_id,
            pi_id=pi_id,
            mode=mode,
            actor=Actor.APPLICANT,
            now=now,
        )
    return campaign


def draftable_step(session: Session, campaign: Campaign, now: datetime) -> int | None:
    """The step the applicant can draft now: Email 1 before outreach, else the next due."""
    messages = {
        m.step: m
        for m in session.scalars(select(Message).where(Message.campaign_id == campaign.id))
    }
    first = messages.get(1)
    if first is None or first.state not in (MessageState.SENT.value,):
        if first is None or first.state in (
            MessageState.DRAFT.value,
            MessageState.AWAITING_APPROVAL.value,
            MessageState.NEEDS_REAPPROVAL.value,
            MessageState.CANCELLED.value,
            MessageState.FAILED_DEFINITE.value,
        ):
            return 1
        return None
    step = campaign.next_step
    if (
        step
        and campaign.next_due_at
        and now >= campaign.next_due_at
        and not campaign.sequence_stopped
    ):
        return int(step)
    existing = [
        s for s, m in messages.items() if s > 1 and m.state == MessageState.AWAITING_APPROVAL.value
    ]
    return existing[0] if existing else None
