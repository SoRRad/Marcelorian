"""Parsers for PubMed (E-utilities), OpenAlex and NIH RePORTER v2 responses.

Defensive by design: unexpected shapes yield fewer results, never an exception that
loses another source's results.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from marcelorian.lookups.http import Fetcher

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
OPENALEX = "https://api.openalex.org"
REPORTER = "https://api.reporter.nih.gov/v2/projects/search"
MAX_ITEMS = 20


@dataclass(frozen=True)
class Publication:
    source: str  # pubmed | openalex
    external_id: str
    title: str
    venue: str
    published_on: str  # YYYY-MM-DD, YYYY-MM or YYYY
    url: str
    authors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AuthorProfile:
    openalex_id: str
    orcid: str
    display_name: str
    institutions: tuple[str, ...]
    topics: tuple[str, ...]
    works_count: int


@dataclass(frozen=True)
class Grant:
    project_number: str
    title: str
    organization: str
    agency: str
    start_date: str
    end_date: str
    url: str


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


# --------------------------------------------------------------------------- #
# PubMed
# --------------------------------------------------------------------------- #


def pubmed_term(name: str = "", affiliation: str = "", orcid: str = "", since_year: int = 0) -> str:
    """An E-utilities search term. ORCID wins when known (author identifiers are exact)."""
    parts: list[str] = []
    if orcid:
        parts.append(f"{orcid}[auid]")
    elif name:
        parts.append(f'"{_text(name)}"[au]')
        if affiliation:
            parts.append(f'"{_text(affiliation)}"[ad]')
    if since_year:
        parts.append(f"{since_year}:3000[dp]")
    return " AND ".join(parts)


def pubmed_search(fetcher: Fetcher, term: str, api_key: str = "") -> list[Publication]:
    if not term:
        return []
    params = {
        "db": "pubmed",
        "term": term,
        "retmode": "json",
        "retmax": str(MAX_ITEMS),
        "sort": "pub_date",
    }
    if api_key:
        params["api_key"] = api_key
    found = fetcher.get_json(f"{EUTILS}/esearch.fcgi", params)
    ids = [i for i in (found.get("esearchresult", {}).get("idlist") or []) if str(i).isdigit()]
    if not ids:
        return []
    summary_params = {"db": "pubmed", "id": ",".join(ids[:MAX_ITEMS]), "retmode": "json"}
    if api_key:
        summary_params["api_key"] = api_key
    summary = fetcher.get_json(f"{EUTILS}/esummary.fcgi", summary_params)
    result = summary.get("result", {}) if isinstance(summary, dict) else {}
    publications = []
    for uid in result.get("uids", []):
        item = result.get(str(uid))
        if not isinstance(item, dict) or not _text(item.get("title")):
            continue
        authors = tuple(
            _text(a.get("name")) for a in item.get("authors") or [] if isinstance(a, dict)
        )
        publications.append(
            Publication(
                source="pubmed",
                external_id=str(uid),
                title=_text(item.get("title")).rstrip("."),
                venue=_text(item.get("source")),
                published_on=_pubmed_date(item),
                url=f"https://pubmed.ncbi.nlm.nih.gov/{uid}/",
                authors=authors,
            )
        )
    return publications


_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}


def _pubmed_date(item: dict[str, Any]) -> str:
    sort = _text(item.get("sortpubdate"))
    match = re.match(r"^(\d{4})/(\d{2})/(\d{2})", sort)
    if match:
        return "-".join(match.groups())
    pub = _text(item.get("pubdate")).split()
    if pub and pub[0].isdigit():
        if len(pub) > 1 and pub[1][:3].lower() in _MONTHS:
            return f"{pub[0]}-{_MONTHS[pub[1][:3].lower()]:02d}"
        return pub[0]
    return ""


# --------------------------------------------------------------------------- #
# OpenAlex
# --------------------------------------------------------------------------- #


def openalex_author(
    fetcher: Fetcher, *, orcid: str = "", openalex_id: str = "", api_key: str = ""
) -> AuthorProfile | None:
    params = {"per_page": "1"}
    if api_key:
        params["api_key"] = api_key
    if openalex_id:
        data = fetcher.get_json(f"{OPENALEX}/authors/{openalex_id}", dict(params))
        rows = [data] if isinstance(data, dict) and data.get("id") else []
    elif orcid:
        data = fetcher.get_json(f"{OPENALEX}/authors", {**params, "filter": f"orcid:{orcid}"})
        rows = data.get("results") or [] if isinstance(data, dict) else []
    else:
        return None
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    return AuthorProfile(
        openalex_id=_text(row.get("id")).rsplit("/", 1)[-1],
        orcid=_text(row.get("orcid")).rsplit("/", 1)[-1],
        display_name=_text(row.get("display_name")),
        institutions=tuple(
            _text(i.get("display_name"))
            for i in row.get("last_known_institutions") or []
            if isinstance(i, dict) and i.get("display_name")
        ),
        topics=tuple(
            _text(t.get("display_name"))
            for t in (row.get("topics") or [])[:8]
            if isinstance(t, dict) and t.get("display_name")
        ),
        works_count=int(row.get("works_count") or 0),
    )


def openalex_recent_works(
    fetcher: Fetcher, openalex_id: str, since: date, api_key: str = ""
) -> list[Publication]:
    if not openalex_id:
        return []
    params = {
        "filter": f"author.id:{openalex_id},from_publication_date:{since.isoformat()}",
        "sort": "publication_date:desc",
        "per_page": str(MAX_ITEMS),
    }
    if api_key:
        params["api_key"] = api_key
    data = fetcher.get_json(f"{OPENALEX}/works", params)
    works = []
    for row in data.get("results") or [] if isinstance(data, dict) else []:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("title") or row.get("display_name"))
        if not title:
            continue
        location = row.get("primary_location") or {}
        source = location.get("source") or {} if isinstance(location, dict) else {}
        doi = _text(row.get("doi"))
        works.append(
            Publication(
                source="openalex",
                external_id=_text(row.get("id")).rsplit("/", 1)[-1],
                title=title.rstrip("."),
                venue=_text(source.get("display_name")) if isinstance(source, dict) else "",
                published_on=_text(row.get("publication_date") or row.get("publication_year")),
                url=doi if doi.startswith("https://") else _text(row.get("id")),
            )
        )
    return works


# --------------------------------------------------------------------------- #
# NIH RePORTER
# --------------------------------------------------------------------------- #


def reporter_grants(
    fetcher: Fetcher, *, first_name: str, last_name: str, agency: str = "NEI"
) -> list[Grant]:
    if not last_name:
        return []
    body = {
        "criteria": {
            "pi_names": [{"first_name": first_name, "last_name": last_name}],
            "agencies": [agency],
            "include_active_projects": True,
        },
        "limit": MAX_ITEMS,
        "offset": 0,
    }
    data = fetcher.post_json(REPORTER, body)
    grants = []
    for row in data.get("results") or [] if isinstance(data, dict) else []:
        if not isinstance(row, dict) or not row.get("project_num"):
            continue
        organization = row.get("organization") or {}
        grants.append(
            Grant(
                project_number=_text(row.get("project_num")),
                title=_text(row.get("project_title")),
                organization=_text(organization.get("org_name"))
                if isinstance(organization, dict)
                else "",
                agency=agency,
                start_date=_text(row.get("project_start_date"))[:10],
                end_date=_text(row.get("project_end_date"))[:10],
                url=_text(row.get("project_detail_url"))
                or f"https://reporter.nih.gov/project-details/{_text(row.get('appl_id'))}",
            )
        )
    return grants
