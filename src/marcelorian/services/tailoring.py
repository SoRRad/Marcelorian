"""Every email is tailored to the PI's research or a posted position (owner decisions T1–T4).

* **Interests** of a PI come only from checked sources: publication titles from the free
  lookups, the research catalog's *verified* topics for the record this contact came
  from, and a posted position the applicant confirmed. Unverified subspecialty claims do
  not count (T3).
* **Tailored check** (Email 1 and Email 2, the steps with a PI-specific line): the line
  about the PI (``personalized_sentence`` / ``pi_topic``) must mention a topic word from
  those interests, or Email 1 must name the confirmed position. The checklist shows the
  word that matched and where it came from.
* **Not reused**: that line must not repeat (or nearly repeat) the same line in an email
  to another PI in the same mode.
* Both checks block approval in real modes and only warn in practice mode (T1).
* **Posted positions** linked to the contact in the research catalog are leads; Email 1
  names one only after the applicant confirms it is current (T2).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import (
    Campaign,
    Evidence,
    Message,
    Pi,
    PositionLink,
    ResearchLink,
    ResearchRecord,
)
from marcelorian.domain.enums import Mode

#: The PI-specific line per step (steps 3 and 4 carry no personal line).
PI_LINE = {1: "personalized_sentence", 2: "pi_topic"}
REUSE_RATIO = 0.9
MIN_WORD = 4
#: Words too generic to show that a line is about this PI's work.
_GENERIC = """
    about above after again against among analysis approach associated assessment based
    before being between both case cases change changes clinical cohort compared
    comparison control could data development different during each early effect effects
    evaluation evidence from future group groups have high however human impact including
    into large learning level long lower method methods model models more most much must
    novel outcome outcomes over patient patients population potential practice previous
    program randomized rate rates recent report research result results review risk role
    same several should since some study studies such than that their them then there
    these they this those through trial trials under using very versus well were what when
    where which while with within work would year years your interest interests group's
    ophthalmology ophthalmic ophthalmologic vision visual eyes research fellow fellowship
    position postdoctoral program opening application recent imaging-based new findings
    retina retinal retinas imaging image images disease diseases medicine medical therapy
    therapies treatment treatments implementation applications senior laboratory lab labs
    residency pre-residency 1-year 2-year translational science sciences health care eye
    ocular department university institute center hospital school
"""
STOPWORDS = frozenset(_GENERIC.split())
_WORD = re.compile(r"[a-z0-9][a-z0-9\-]*")


class TailoringError(ValueError):
    pass


def _key(word: str) -> str:
    """Light stemming: 'glaucomatous' and 'glaucoma' share the key 'glauco'."""
    return word[:6] if len(word) > 6 else word


def words(text: str) -> list[str]:
    found = []
    for raw in _WORD.findall(text.casefold()):
        word = raw.strip("-")
        if len(word) >= MIN_WORD and word not in STOPWORDS and not word.isdigit():
            found.append(word)
    return found


# --------------------------------------------------------------------------- #
# Posted positions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Posting:
    record_id: int
    title: str
    status: str
    program_type: str
    application_url: str
    deadline: str
    scope_note: str
    confirmed: bool


def _contact_records(session: Session, pi_id: int) -> list[ResearchRecord]:
    return list(
        session.scalars(
            select(ResearchRecord).where(
                ResearchRecord.promoted_pi_id == pi_id, ResearchRecord.record_type == "contact"
            )
        )
    )


def linked_postings(session: Session, pi_id: int) -> list[Posting]:
    """Opportunity records in the catalog that list one of this contact's records."""
    contacts = {r.external_record_id for r in _contact_records(session, pi_id)}
    if not contacts:
        return []
    opportunity_ids = set(
        session.scalars(
            select(ResearchLink.record_id).where(
                ResearchLink.target_external_id.in_(contacts),
                ResearchLink.kind == "related",
            )
        )
    )
    confirmed = {
        link.research_record_id
        for link in session.scalars(
            select(PositionLink).where(
                PositionLink.pi_id == pi_id, PositionLink.withdrawn_at.is_(None)
            )
        )
    }
    postings = []
    for record in session.scalars(
        select(ResearchRecord)
        .where(ResearchRecord.id.in_(opportunity_ids), ResearchRecord.record_type == "opportunity")
        .order_by(ResearchRecord.name)
    ):
        raw = _raw(record)
        details = _json_object(raw.get("details_json"))
        postings.append(
            Posting(
                record_id=record.id,
                title=record.name,
                status=record.opportunity_status,
                program_type=str(details.get("program_type", "")),
                application_url=str(raw.get("application_url", "")),
                deadline=record.application_deadline,
                scope_note=str(details.get("contact_link_scope", "")),
                confirmed=record.id in confirmed,
            )
        )
    return postings


def confirm_posting(session: Session, pi_id: int, record_id: int, *, now: datetime) -> None:
    if record_id not in {p.record_id for p in linked_postings(session, pi_id)}:
        raise TailoringError("that posting is not linked to this contact")
    record = session.get(ResearchRecord, record_id)
    assert record is not None
    link = session.scalar(
        select(PositionLink).where(
            PositionLink.pi_id == pi_id, PositionLink.research_record_id == record_id
        )
    )
    if link is None:
        session.add(
            PositionLink(
                pi_id=pi_id, research_record_id=record_id, title=record.name, confirmed_at=now
            )
        )
    else:
        link.title, link.confirmed_at, link.withdrawn_at = record.name, now, None


def withdraw_posting(session: Session, pi_id: int, record_id: int, *, now: datetime) -> None:
    link = session.scalar(
        select(PositionLink).where(
            PositionLink.pi_id == pi_id,
            PositionLink.research_record_id == record_id,
            PositionLink.withdrawn_at.is_(None),
        )
    )
    if link is not None:
        link.withdrawn_at = now


def confirmed_position(session: Session, pi_id: int) -> PositionLink | None:
    return session.scalar(
        select(PositionLink)
        .where(PositionLink.pi_id == pi_id, PositionLink.withdrawn_at.is_(None))
        .order_by(PositionLink.confirmed_at.desc(), PositionLink.id.desc())
    )


# --------------------------------------------------------------------------- #
# Interests
# --------------------------------------------------------------------------- #


@dataclass
class Interests:
    #: word key -> (word as written, where it came from)
    terms: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: (kind, text) shown to the applicant while writing
    sources: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, text: str) -> None:
        self.sources.append((kind, text))
        for word in words(text):
            self.terms.setdefault(_key(word), (word, f"{kind}: {text}"))


def verified_topics(session: Session, pi_id: int) -> list[str]:
    topics: list[str] = []
    for record in _contact_records(session, pi_id):
        value = str(_raw(record).get("research_topics_verified", ""))
        topics += [t.strip() for t in value.split(";") if t.strip()]
    return list(dict.fromkeys(topics))


def interests(session: Session, pi_id: int) -> Interests:
    found = Interests()
    position = confirmed_position(session, pi_id)
    if position is not None:
        found.add("Posted position", position.title)
    for topic in verified_topics(session, pi_id):
        found.add("Verified topic", topic)
    for item in session.scalars(
        select(Evidence)
        .where(Evidence.pi_id == pi_id)
        .order_by(Evidence.published_on.desc(), Evidence.id)
        .limit(40)
    ):
        found.add("Publication" if item.kind != "profile" else "Profile", item.title)
    return found


def practice_topic(session: Session, pi_id: int, limit: int = 60) -> str | None:
    """A short topic for practice text: a verified topic, else a publication title."""
    topics = verified_topics(session, pi_id)
    if topics:
        return topics[0][:limit]
    title = session.scalar(
        select(Evidence.title)
        .where(Evidence.pi_id == pi_id, Evidence.kind != "profile")
        .order_by(Evidence.published_on.desc(), Evidence.id)
    )
    if not title:
        return None
    if len(title) <= limit:
        return title.rstrip(".")
    return title[:limit].rsplit(" ", 1)[0].rstrip(",;:")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Assessment:
    applies: bool
    tailored: bool
    tailored_detail: str
    reused: bool
    reused_detail: str
    position_title: str | None
    position_named: bool


def assess(session: Session, message: Message, personal: dict[str, str]) -> Assessment:
    campaign = session.get(Campaign, message.campaign_id)
    assert campaign is not None
    body = (message.draft_body or "").casefold()
    position = confirmed_position(session, campaign.pi_id) if message.step == 1 else None
    position_named = position is not None and position.title.casefold() in body
    field_name = PI_LINE.get(message.step)
    if field_name is None:
        return Assessment(False, True, "", False, "", None, True)
    line = " ".join(str(personal.get(field_name, "")).split())
    found = interests(session, campaign.pi_id)
    pi = session.get(Pi, campaign.pi_id)
    for name_word in words(pi.full_name if pi else ""):  # naming the PI is not tailoring
        found.terms.pop(_key(name_word), None)
    match = None
    for word in words(line):
        hit = found.terms.get(_key(word))
        if hit is not None:
            match = (word, hit[1])
            break
    if match is not None:
        tailored, detail = True, f"mentions “{match[0]}” ({_short(match[1])})"
    elif position_named:
        assert position is not None
        tailored, detail = True, f"names the posted position “{position.title}”"
    elif not found.terms:
        tailored, detail = (
            False,
            (
                "no interests on file for this PI yet: run the free lookup on the contact page, "
                "or confirm a posted position"
            ),
        )
    else:
        tailored, detail = (
            False,
            (
                "the line about the PI does not mention any of their topics; see “This PI's "
                "interests” on the draft page"
            ),
        )
    reused_with = _reused(session, campaign, message, field_name, line)
    return Assessment(
        applies=True,
        tailored=tailored,
        tailored_detail=detail,
        reused=reused_with is not None,
        reused_detail=f"same as the email to {reused_with}" if reused_with else "",
        position_title=position.title if position else None,
        position_named=position is None or position_named,
    )


def _reused(
    session: Session, campaign: Campaign, message: Message, field_name: str, line: str
) -> str | None:
    if not line:
        return None
    rows = session.execute(
        select(Message, Campaign)
        .join(Campaign, Campaign.id == Message.campaign_id)
        .where(
            Message.mode == message.mode,
            Message.campaign_id != campaign.id,
            Message.draft_fields_json.is_not(None),
        )
    ).all()
    mine = line.casefold()
    for other, other_campaign in rows:
        if other_campaign.pi_id == campaign.pi_id:
            continue
        personal = _json_object(_json_object(other.draft_fields_json).get("personal"))
        theirs = " ".join(str(personal.get(field_name, "")).split()).casefold()
        if theirs and (
            theirs == mine or SequenceMatcher(None, mine, theirs).ratio() >= REUSE_RATIO
        ):
            pi = session.get(Pi, other_campaign.pi_id)
            return pi.full_name if pi else f"campaign #{other_campaign.id}"
    return None


def blocking(mode: Mode) -> bool:
    """Real emails must be tailored; practice only warns (T1)."""
    return mode is not Mode.SANDBOX


def _short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _raw(record: ResearchRecord) -> dict[str, object]:
    return _json_object(record.raw_json)


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        data = json.loads(value)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
