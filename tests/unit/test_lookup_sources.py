"""Public lookup parsers and the restricted HTTP client (no network)."""

from __future__ import annotations

import io
import itertools
import json
import time
import urllib.error
from datetime import date

import pytest

from marcelorian.lookups import sources
from marcelorian.lookups.http import HttpFetcher, LookupFailed
from tests.fakes.lookups import OPENALEX_AUTHOR, FakeFetcher


def test_pubmed_term() -> None:
    assert sources.pubmed_term(orcid="0000-0002-1825-0097", name="x") == "0000-0002-1825-0097[auid]"
    assert sources.pubmed_term(name="Doe, Jane", affiliation="Synthetic  Eye", since_year=2023) == (
        '"Doe, Jane"[au] AND "Synthetic Eye"[ad] AND 2023:3000[dp]'
    )
    assert sources.pubmed_term() == ""


def test_pubmed_search_parses_titles_dates_and_authors() -> None:
    pubs = sources.pubmed_search(FakeFetcher(), "term", api_key="k")
    assert [p.external_id for p in pubs] == ["39000001", "39000002"]
    first, second = pubs
    assert first.title == "Deep learning on synthetic OCT scans"
    assert (first.published_on, second.published_on) == ("2025-03-03", "2024-11")
    assert first.url == "https://pubmed.ncbi.nlm.nih.gov/39000001/"
    assert first.authors == ("Example A", "Doe J")


def test_pubmed_unexpected_shapes_give_no_results() -> None:
    assert sources.pubmed_search(FakeFetcher(overrides={"esearch": {}}), "t") == []
    weird = {"esearchresult": {"idlist": ["123", "drop;table"]}}
    fetcher = FakeFetcher(overrides={"esearch": weird, "esummary": {"result": {"uids": ["123"]}}})
    assert sources.pubmed_search(fetcher, "t") == []
    assert fetcher.calls[1][1]["id"] == "123"  # non-numeric ids never reach the next call


def test_openalex_author_and_works() -> None:
    fetcher = FakeFetcher()
    author = sources.openalex_author(fetcher, orcid="0000-0002-1825-0097")
    assert author.openalex_id == "A5000000001" and author.orcid == "0000-0002-1825-0097"
    assert author.topics == ("Retinal imaging", "Glaucoma") and author.works_count == 42
    works = sources.openalex_recent_works(fetcher, author.openalex_id, date(2023, 1, 1))
    assert [w.title for w in works] == ["Choroidal thickness in a synthetic population"]
    assert works[0].url == "https://doi.org/10.0000/synthetic.1"
    assert sources.openalex_author(fetcher) is None


def test_reporter_grants() -> None:
    grants = sources.reporter_grants(FakeFetcher(), first_name="Alex", last_name="Example")
    assert [g.project_number for g in grants] == ["5R01EY000001-03", "5R21EY000002-02"]
    assert grants[0].end_date == "2028-08-31" and grants[0].agency == "NEI"
    assert grants[0].url == "https://reporter.nih.gov/project-details/1000001"
    assert sources.reporter_grants(FakeFetcher(), first_name="", last_name="") == []


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTime:
    """A monotonic clock that only moves when the fetcher sleeps (or a test says so)."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(round(seconds, 6))
        self.now += seconds


def _ok(request, timeout):
    return _Response(b"{}")


def test_http_client_paces_each_service_separately() -> None:
    clock = FakeTime()
    client = HttpFetcher(sleep=clock.sleep, opener=_ok, monotonic=clock.monotonic)
    pubmed = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    for _ in range(3):
        client.get_json(pubmed, {})
    assert clock.sleeps == [0.34, 0.34]  # at most 3 a second without a key
    client.post_json("https://api.reporter.nih.gov/v2/projects/search", {})
    client.get_json("https://api.openalex.org/works", {})
    assert clock.sleeps == [0.34, 0.34]  # other services have their own clocks
    client.post_json("https://api.reporter.nih.gov/v2/projects/search", {})
    assert clock.sleeps == [0.34, 0.34, 1.0]  # NIH RePORTER: one a second
    clock.now += 5
    client.get_json(pubmed, {})
    assert len(clock.sleeps) == 3  # no wait after a quiet spell


def test_http_client_pacing_is_shared_between_threads() -> None:
    import threading

    starts: list[float] = []
    lock = threading.Lock()

    def opener(request, timeout):
        with lock:
            starts.append(time.monotonic())
        return _Response(b"{}")

    client = HttpFetcher(opener=opener)
    url = "https://api.openalex.org/works"
    threads = [threading.Thread(target=client.get_json, args=(url, {})) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert len(starts) == 4 and min(gaps) >= 0.09


def test_pubmed_identifies_the_tool_and_optional_contact() -> None:
    fetcher = FakeFetcher()
    sources.pubmed_search(fetcher, "term")
    for _, params in fetcher.calls:
        assert params["tool"] == "marcelorian"
        assert "email" not in params and "api_key" not in params
    fetcher = FakeFetcher()
    sources.pubmed_search(fetcher, "term", api_key="k", email="owner@example.org")
    for _, params in fetcher.calls:
        assert (params["tool"], params["email"], params["api_key"]) == (
            "marcelorian",
            "owner@example.org",
            "k",
        )


@pytest.mark.parametrize(
    "last_name,names,ok",
    [
        ("Example", ["Alex Example"], True),
        ("Example", ["A. EXAMPLE"], True),
        ("García-López", ["Maria Garcia Lopez"], True),
        ("Nguyen", ["Someone Else", "T. Nguyen"], True),  # an alternative name
        ("Lee", ["Ashley Leeds"], False),  # whole words only
        ("Example", ["Someone Else"], False),
        ("", ["Anyone"], True),
    ],
)
def test_name_matches(last_name: str, names: list[str], ok: bool) -> None:
    assert sources.name_matches(last_name, names) is ok


def test_openalex_profile_under_another_name_is_refused() -> None:
    fetcher = FakeFetcher()
    assert sources.openalex_author(fetcher, orcid="0000-0002-1825-0097", last_name="Example")
    with pytest.raises(sources.NameMismatch, match="Alex Example"):
        sources.openalex_author(fetcher, orcid="0000-0002-1825-0097", last_name="Nguyen")
    alt = {
        "results": [{**OPENALEX_AUTHOR["results"][0], "display_name_alternatives": ["T Nguyen"]}]
    }
    found = sources.openalex_author(
        FakeFetcher(overrides={"openalex_author": alt}), orcid="x", last_name="Nguyen"
    )
    assert found is not None and found.openalex_id == "A5000000001"


def test_http_client_allows_only_fixed_hosts() -> None:
    client = HttpFetcher(opener=lambda *a, **k: pytest.fail("no request expected"))
    for url in ("https://example.com/x", "http://api.openalex.org/works", "file:///etc/passwd"):
        with pytest.raises(LookupFailed):
            client.get_json(url, {})


def test_http_client_retries_politely_then_gives_a_safe_message() -> None:
    attempts: list[str] = []

    def opener(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, None)
        return _Response(json.dumps({"ok": True}).encode())

    clock = FakeTime()
    client = HttpFetcher(sleep=clock.sleep, opener=opener, monotonic=clock.monotonic)
    assert client.get_json("https://api.openalex.org/works", {"api_key": "secret"}) == {"ok": True}
    assert clock.sleeps == [1.0, 2.0]  # the backoff already exceeds the pacing gap

    def broken(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    with pytest.raises(LookupFailed) as info:
        HttpFetcher(sleep=lambda s: None, opener=broken).get_json(
            "https://api.openalex.org/works", {"api_key": "secret"}
        )
    assert str(info.value) == "api.openalex.org: HTTP 404" and "secret" not in str(info.value)


def test_http_client_refuses_huge_or_invalid_responses() -> None:
    big = HttpFetcher(opener=lambda r, timeout: _Response(b"[" + b"0," * (3 * 1024 * 1024) + b"0]"))
    with pytest.raises(LookupFailed, match="too large"):
        big.get_json("https://api.reporter.nih.gov/v2/projects/search", {})
    bad = HttpFetcher(opener=lambda r, timeout: _Response(b"<html>"))
    with pytest.raises(LookupFailed, match="unexpected response"):
        bad.post_json("https://api.reporter.nih.gov/v2/projects/search", {"a": 1})
