"""Run the free lookups for a contact or for the applicant, and store the results.

Network calls happen **outside** any database transaction (the caller reads the query in
one short transaction, calls ``gather_*``, then stores in another). Each source runs on
its own: one failing never loses another's results (spec §15). Results are evidence for
the applicant's review; nothing here changes identifiers, addresses or recipients.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import (
    ApplicantPublication,
    Evidence,
    GrantEvidence,
    Institution,
    LookupRun,
    Pi,
)
from marcelorian.lookups import sources
from marcelorian.lookups.http import Fetcher, LookupFailed
from marcelorian.services.audit import audit

YEARS_BACK = 3  # spec §6: the last 3 years


@dataclass(frozen=True)
class Keys:
    """Optional API keys (from the secret store) and the NCBI contact address.

    Lookups work without any of them; see ``lookups.keys``.
    """

    ncbi: str = ""
    openalex: str = ""
    #: The developer's address registered with NCBI for the ``tool`` name (not a secret).
    ncbi_email: str = ""


NO_KEYS = Keys()


@dataclass(frozen=True)
class PiQuery:
    pi_id: int
    first_name: str
    last_name: str
    institution: str
    orcid: str
    openalex_id: str


@dataclass
class SourceResult:
    source: str
    ok: bool
    items: list[Any] = field(default_factory=list)
    message: str = ""


def pi_query(session: Session, pi_id: int) -> PiQuery:
    pi = session.get(Pi, pi_id)
    if pi is None:
        raise LookupError(pi_id)
    institution = session.get(Institution, pi.institution_id)
    names = pi.full_name.split()
    first = next((n for n in names if n.rstrip(".").casefold() not in {"dr", "prof"}), "")
    return PiQuery(
        pi_id=pi.id,
        first_name=first if first.casefold() != pi.last_name.casefold() else "",
        last_name=pi.last_name,
        institution=institution.name if institution else "",
        orcid=pi.orcid or "",
        openalex_id=pi.openalex_id or "",
    )


def _run(source: str, call: Any) -> SourceResult:
    try:
        items = call()
    except LookupFailed as exc:
        return SourceResult(source, False, [], str(exc))
    except (KeyError, TypeError, ValueError, AttributeError):
        return SourceResult(source, False, [], f"{source}: unexpected response")
    return SourceResult(source, True, list(items), "")


def gather_for_pi(
    query: PiQuery, fetcher: Fetcher, today: date, keys: Keys = NO_KEYS
) -> list[SourceResult]:
    since = window_start(today)
    full = f"{query.last_name}, {query.first_name}".strip(", ")
    term = sources.pubmed_term(
        name=full, affiliation=query.institution, orcid=query.orcid, since_year=since.year
    )
    if not query.orcid and term:
        term = term.replace("[au]", "[fau]", 1)  # "Last, First" full-author search
    results = [
        _run("pubmed", lambda: sources.pubmed_search(fetcher, term, keys.ncbi, keys.ncbi_email))
    ]

    def openalex() -> list[Any]:
        try:
            author = sources.openalex_author(
                fetcher,
                orcid=query.orcid,
                openalex_id=query.openalex_id,
                api_key=keys.openalex,
                last_name=query.last_name,
            )
        except sources.NameMismatch as exc:
            raise LookupFailed(
                f"openalex: that ID belongs to a profile named {str(exc)[:80]!r}, "
                "not this PI; check the ORCID"
            ) from None
        if author is None:
            return []
        works = sources.openalex_recent_works(fetcher, author.openalex_id, since, keys.openalex)
        return [author, *works]

    if query.orcid or query.openalex_id:
        results.append(_run("openalex", openalex))
    else:
        results.append(SourceResult("openalex", True, [], "skipped: needs an ORCID or OpenAlex ID"))
    results.append(
        _run(
            "reporter",
            lambda: sources.reporter_grants(
                fetcher, first_name=query.first_name, last_name=query.last_name
            ),
        )
    )
    return results


def store_pi_results(
    session: Session, pi_id: int, results: list[SourceResult], now: datetime
) -> dict[str, int]:
    """Insert new evidence (existing rows by the same id are refreshed). Returns new counts."""
    added: dict[str, int] = {}
    for result in results:
        new = 0
        for item in result.items:
            if isinstance(item, sources.Publication):
                new += _upsert_evidence(session, pi_id, item, now)
            elif isinstance(item, sources.AuthorProfile):
                new += _upsert_profile(session, pi_id, item, now)
            elif isinstance(item, sources.Grant):
                new += _upsert_grant(session, pi_id, item, now)
        added[result.source] = new
        session.add(
            LookupRun(
                pi_id=pi_id,
                source=result.source,
                ran_at=now,
                ok=result.ok,
                items=len(result.items),
                message=result.message[:500],
            )
        )
    audit(
        session,
        at=now,
        actor="applicant",
        entity_type="pi",
        entity_id=pi_id,
        action="public_lookup",
        detail={"added": added, "failed": [r.source for r in results if not r.ok]},
    )
    return added


def _upsert_evidence(session: Session, pi_id: int, item: sources.Publication, now: datetime) -> int:
    row = session.scalar(
        select(Evidence).where(
            Evidence.pi_id == pi_id,
            Evidence.source == item.source,
            Evidence.external_id == item.external_id,
        )
    )
    created = row is None
    if row is None:
        row = Evidence(pi_id=pi_id, source=item.source, external_id=item.external_id)
        session.add(row)
    row.kind = "publication"
    row.title = item.title[:2000]
    row.venue = item.venue[:255]
    row.published_on = item.published_on[:10]
    row.url = item.url
    row.retrieved_at = now
    if item.authors:
        row.excerpt = "Authors: " + ", ".join(item.authors[:12])
    return int(created)


def _upsert_profile(
    session: Session, pi_id: int, item: sources.AuthorProfile, now: datetime
) -> int:
    row = session.scalar(
        select(Evidence).where(
            Evidence.pi_id == pi_id, Evidence.source == "openalex", Evidence.kind == "profile"
        )
    )
    created = row is None
    if row is None:
        row = Evidence(pi_id=pi_id, source="openalex", kind="profile")
        session.add(row)
    row.external_id = item.openalex_id[:64]
    row.title = f"OpenAlex profile: {item.display_name}"
    row.venue = "; ".join(item.institutions)[:255]
    row.url = f"https://openalex.org/{item.openalex_id}"
    row.excerpt = (
        f"ORCID {item.orcid or 'unknown'} · {item.works_count} works · topics: "
        + ", ".join(item.topics)
    )
    row.published_on = ""
    row.retrieved_at = now
    return int(created)


def _upsert_grant(session: Session, pi_id: int, item: sources.Grant, now: datetime) -> int:
    row = session.scalar(
        select(GrantEvidence).where(
            GrantEvidence.pi_id == pi_id, GrantEvidence.project_number == item.project_number
        )
    )
    created = row is None
    if row is None:
        row = GrantEvidence(pi_id=pi_id, project_number=item.project_number[:64])
        session.add(row)
    row.title = item.title[:2000]
    row.organization = item.organization[:255]
    row.agency = item.agency[:16]
    row.start_date = item.start_date[:10]
    row.end_date = item.end_date[:10]
    row.url = item.url
    row.retrieved_at = now
    return int(created)


def active_grants(session: Session, pi_id: int, today: date) -> list[GrantEvidence]:
    rows = session.scalars(select(GrantEvidence).where(GrantEvidence.pi_id == pi_id))
    return [g for g in rows if not g.end_date or g.end_date >= today.isoformat()]


def evidence_for(session: Session, pi_id: int) -> list[Evidence]:
    return list(
        session.scalars(
            select(Evidence)
            .where(Evidence.pi_id == pi_id)
            .order_by(Evidence.kind != "profile", Evidence.published_on.desc(), Evidence.id)
        )
    )


def last_runs(session: Session, pi_id: int | None) -> dict[str, LookupRun]:
    rows = session.scalars(
        select(LookupRun)
        .where(LookupRun.pi_id.is_(None) if pi_id is None else LookupRun.pi_id == pi_id)
        .order_by(LookupRun.ran_at, LookupRun.id)
    )
    return {row.source: row for row in rows}


# --------------------------------------------------------------------------- #
# The applicant's own publications (spec §4)
# --------------------------------------------------------------------------- #


def gather_applicant_publications(
    full_name: str, orcid: str, fetcher: Fetcher, keys: Keys = NO_KEYS
) -> SourceResult:
    names = full_name.split()
    if orcid:
        term = sources.pubmed_term(orcid=orcid)
    elif len(names) >= 2:
        term = f'"{names[-1]}, {" ".join(names[:-1])}"[fau]'
    else:
        return SourceResult("pubmed", False, [], "Enter your full name or ORCID iD first.")
    return _run("pubmed", lambda: sources.pubmed_search(fetcher, term, keys.ncbi, keys.ncbi_email))


def store_applicant_publications(
    session: Session, applicant_id: int, result: SourceResult, now: datetime
) -> int:
    new = 0
    for item in result.items:
        if not isinstance(item, sources.Publication):
            continue
        row = session.scalar(
            select(ApplicantPublication).where(
                ApplicantPublication.applicant_id == applicant_id,
                ApplicantPublication.source == item.source,
                ApplicantPublication.external_id == item.external_id,
            )
        )
        if row is not None:
            continue  # keep the applicant's earlier decision
        session.add(
            ApplicantPublication(
                applicant_id=applicant_id,
                source=item.source,
                external_id=item.external_id,
                title=item.title[:2000],
                venue=item.venue[:255],
                published_on=item.published_on[:10],
                url=item.url,
                status="proposed",
                retrieved_at=now,
            )
        )
        new += 1
    session.add(
        LookupRun(
            pi_id=None,
            source=result.source,
            ran_at=now,
            ok=result.ok,
            items=len(result.items),
            message=result.message[:500],
        )
    )
    return new


def decide_publication(
    session: Session, applicant_id: int, publication_id: int, *, confirm: bool, now: datetime
) -> ApplicantPublication:
    row = session.get(ApplicantPublication, publication_id)
    if row is None or row.applicant_id != applicant_id:
        raise LookupError(publication_id)
    row.status = "confirmed" if confirm else "rejected"
    row.decided_at = now
    return row


def applicant_publications(session: Session, applicant_id: int) -> list[ApplicantPublication]:
    return list(
        session.scalars(
            select(ApplicantPublication)
            .where(
                ApplicantPublication.applicant_id == applicant_id,
                ApplicantPublication.status != "rejected",
            )
            .order_by(ApplicantPublication.published_on.desc(), ApplicantPublication.id)
        )
    )


def window_start(today: date) -> date:
    """The start of the lookup window (also valid when today is 29 February)."""
    try:
        return today.replace(year=today.year - YEARS_BACK)
    except ValueError:
        return today.replace(year=today.year - YEARS_BACK, day=28)
