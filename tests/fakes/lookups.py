"""A scripted fetcher for the public lookups: canned JSON per endpoint, no network."""

from __future__ import annotations

from typing import Any

from marcelorian.lookups.http import LookupFailed

ESEARCH = {"esearchresult": {"count": "2", "idlist": ["39000001", "39000002"]}}
ESUMMARY = {
    "result": {
        "uids": ["39000001", "39000002"],
        "39000001": {
            "uid": "39000001",
            "title": "Deep learning on synthetic OCT scans.",
            "source": "Synthetic Ophthalmology",
            "pubdate": "2025 Mar 3",
            "sortpubdate": "2025/03/03 00:00",
            "authors": [{"name": "Example A", "authtype": "Author"}, {"name": "Doe J"}],
        },
        "39000002": {
            "uid": "39000002",
            "title": "Glaucoma progression in a synthetic cohort",
            "source": "Synthetic Eye J",
            "pubdate": "2024 Nov",
            "authors": [],
        },
    }
}
OPENALEX_AUTHOR = {
    "results": [
        {
            "id": "https://openalex.org/A5000000001",
            "orcid": "https://orcid.org/0000-0002-1825-0097",
            "display_name": "Alex Example",
            "works_count": 42,
            "last_known_institutions": [{"display_name": "Synthetic Eye Institute"}],
            "topics": [{"display_name": "Retinal imaging"}, {"display_name": "Glaucoma"}],
        }
    ]
}
OPENALEX_WORKS = {
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.0000/synthetic.1",
            "title": "Choroidal thickness in a synthetic population",
            "publication_date": "2025-06-01",
            "primary_location": {"source": {"display_name": "Synthetic Retina"}},
        },
        {"id": "https://openalex.org/W2", "title": None},
    ]
}
REPORTER = {
    "results": [
        {
            "project_num": "5R01EY000001-03",
            "project_title": "Synthetic retinal degeneration mechanisms",
            "organization": {"org_name": "SYNTHETIC EYE INSTITUTE"},
            "project_start_date": "2023-09-01T00:00:00",
            "project_end_date": "2028-08-31T00:00:00",
            "appl_id": 1000001,
        },
        {
            "project_num": "5R21EY000002-02",
            "project_title": "An ended synthetic project",
            "organization": {"org_name": "SYNTHETIC EYE INSTITUTE"},
            "project_start_date": "2019-09-01T00:00:00",
            "project_end_date": "2021-08-31T00:00:00",
            "project_detail_url": "https://reporter.nih.gov/project-details/1000002",
        },
    ]
}


class FakeFetcher:
    def __init__(self, fail: set[str] | None = None, overrides: dict[str, Any] | None = None):
        self.fail = fail or set()
        self.overrides = overrides or {}
        self.calls: list[tuple[str, Any]] = []

    def _answer(self, key: str, default: Any) -> Any:
        if key in self.fail:
            raise LookupFailed(f"{key}: HTTP 503")
        return self.overrides.get(key, default)

    def get_json(self, url: str, params: dict[str, str]) -> Any:
        self.calls.append((url, params))
        if url.endswith("esearch.fcgi"):
            return self._answer("esearch", ESEARCH)
        if url.endswith("esummary.fcgi"):
            return self._answer("esummary", ESUMMARY)
        if "/authors" in url:
            return self._answer("openalex_author", OPENALEX_AUTHOR)
        if url.endswith("/works"):
            return self._answer("openalex_works", OPENALEX_WORKS)
        raise AssertionError(url)

    def post_json(self, url: str, body: dict[str, Any]) -> Any:
        self.calls.append((url, body))
        return self._answer("reporter", REPORTER)
