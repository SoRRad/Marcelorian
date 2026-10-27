"""Prefilled personal lines with alternatives (owner request 2026-10-27; no API, no cost).

Every line is built from what is already on file and checked: the PI's publication titles
from the free lookups, verified catalog topics, OpenAlex profile topics, active NIH grants,
a posting the applicant confirmed, and the applicant's own confirmed profile answers. The
first option of each line fills the draft; the others appear in a drop-down. The applicant
reads and edits everything before approving, as with any line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import Campaign, Evidence
from marcelorian.domain.enums import FactStatus
from marcelorian.lookups.enrich import active_grants
from marcelorian.services import profile, tailoring

MAX_OPTIONS = 6
MAX_TITLE = 140


@dataclass(frozen=True)
class Option:
    text: str
    #: Where the option comes from, shown in the drop-down ("Paper, 2025").
    basis: str


@dataclass
class Suggestions:
    options: dict[str, list[Option]] = field(default_factory=dict)

    def first(self) -> dict[str, str]:
        return {key: items[0].text for key, items in self.options.items() if items}


def _lower_first(text: str) -> str:
    """'Retinal imaging' -> 'retinal imaging', but keep 'OCT angiography' as written."""
    head = text.split(" ", 1)[0]
    if len(head) > 1 and head[:2].isupper():
        return text
    return text[:1].lower() + text[1:]


def _clip(text: str, limit: int = MAX_TITLE) -> str:
    text = " ".join(text.split()).rstrip(".")
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _sentence_case(title: str) -> str:
    """NIH titles are often in capitals; show them as a sentence."""
    letters = [c for c in title if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
        title = title.lower()
        return title[:1].upper() + title[1:]
    return title


def _profile_topics(session: Session, pi_id: int) -> list[str]:
    excerpt = session.scalar(
        select(Evidence.excerpt).where(Evidence.pi_id == pi_id, Evidence.kind == "profile")
    )
    if not excerpt or "topics:" not in excerpt:
        return []
    return [t.strip() for t in excerpt.split("topics:", 1)[1].split(",") if t.strip()]


def _fact(facts: dict[str, object], key: str) -> str:
    fact = facts.get(key)
    if fact is None or getattr(fact, "status", "") != FactStatus.CONFIRMED.value:
        return ""
    return " ".join(str(getattr(fact, "value", "") or "").split()).rstrip(".")


def suggest(session: Session, campaign: Campaign, step: int, today: date) -> Suggestions:
    pi_id = campaign.pi_id
    facts: dict[str, object] = dict(profile.facts(session, campaign.applicant_id))
    mine = _fact(facts, "research_interests")
    skills = _fact(facts, "skills")
    topics = list(
        dict.fromkeys(tailoring.verified_topics(session, pi_id) + _profile_topics(session, pi_id))
    )
    papers = list(
        session.scalars(
            select(Evidence)
            .where(Evidence.pi_id == pi_id, Evidence.kind != "profile")
            .order_by(Evidence.published_on.desc(), Evidence.id)
            .limit(5)
        )
    )
    grants = active_grants(session, pi_id, today)[:2]
    position = tailoring.confirmed_position(session, pi_id) if step == 1 else None
    interest_tail = f" and closely matches my interest in {_lower_first(mine)}" if mine else ""

    sentences: list[Option] = []
    if position is not None:
        sentences.append(
            Option(
                f"The posted {position.title} stood out to me"
                + (f" because it fits my interest in {_lower_first(mine)}." if mine else "."),
                "Posted position you confirmed",
            )
        )
    for paper in papers[:3]:
        year = (paper.published_on or "")[:4]
        where = ", ".join(v for v in (paper.venue, year) if v)
        sentences.append(
            Option(
                f"I read your recent paper, “{_clip(paper.title)}”"
                + (f" ({where})" if where else "")
                + (
                    f", and it closely matches my interest in {_lower_first(mine)}."
                    if mine
                    else ", and I would be glad to contribute to work like it."
                ),
                f"Paper{', ' + year if year else ''}",
            )
        )
    for topic in topics[:3]:
        sentences.append(
            Option(
                f"Your work on {_lower_first(topic)} is what drew me to your group"
                + (f", as it builds on my interest in {_lower_first(mine)}." if mine else "."),
                "Research topic",
            )
        )
    for grant in grants:
        sentences.append(
            Option(
                f"I was particularly interested in your NIH-funded project, "
                f"“{_clip(_sentence_case(grant.title))}”{interest_tail}.",
                f"NIH grant {grant.project_number}",
            )
        )

    # Without any topic on file, the latest paper's title stands in as the topic.
    short = tailoring.practice_topic(session, pi_id) if papers and not topics else None
    topic_lines = [Option(_lower_first(t), "Research topic") for t in topics[:4]]
    if short:
        topic_lines.append(Option(_lower_first(short), "Recent paper"))
    contribution: list[Option] = []
    if position is not None:
        contribution.append(Option(f"the {position.title}", "Posted position you confirmed"))
    contribution += [
        Option(f"your research on {_lower_first(t)}", "Research topic") for t in topics[:3]
    ]
    general = Option("your group's ongoing projects", "General")
    if short:  # a paper title reads awkwardly here, so it is the second choice
        contribution += [general, Option(f"your work on “{short}”", "Recent paper")]
    else:
        contribution.append(general)

    skill_lines: list[Option] = []
    if skills:
        skill_lines.append(Option(skills, "Your profile: methods and skills"))
        if mine:
            skill_lines.append(
                Option(f"{skills}, with a focus on {_lower_first(mine)}", "Your profile")
            )

    result = Suggestions()
    for key, items in (
        ("personalized_sentence", sentences),
        ("pi_topic", topic_lines),
        ("skills_line", skill_lines),
        ("contribution_line", contribution),
    ):
        unique = list({o.text: o for o in items}.values())[:MAX_OPTIONS]
        result.options[key] = unique
    return result
