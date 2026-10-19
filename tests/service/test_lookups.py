"""Running the free lookups for a contact and for the applicant (fake fetcher)."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Evidence, GrantEvidence, LookupRun, Pi
from marcelorian.lookups import enrich
from tests.fakes.lookups import FakeFetcher

TODAY = date(2026, 10, 20)


def lookup(world, pi_id: int, fetcher: FakeFetcher) -> dict[str, int]:
    with transaction(world.factory) as s:
        query = enrich.pi_query(s, pi_id)
    results = enrich.gather_for_pi(query, fetcher, TODAY)  # outside any transaction
    with transaction(world.factory) as s:
        return enrich.store_pi_results(s, pi_id, results, world.clock.now())


def test_lookup_stores_publications_profile_and_grants(world) -> None:
    pi = world.pi(world.institution(name="Synthetic Eye Institute"), name="Alex Example")
    with transaction(world.factory) as s:
        s.get(Pi, pi).orcid = "0000-0002-1825-0097"
    fetcher = FakeFetcher()
    added = lookup(world, pi, fetcher)
    assert added == {"pubmed": 2, "openalex": 2, "reporter": 2}
    assert fetcher.calls[0][1]["term"] == "0000-0002-1825-0097[auid] AND 2023:3000[dp]"
    with transaction(world.factory) as s:
        evidence = enrich.evidence_for(s, pi)
        assert evidence[0].kind == "profile" and "Retinal imaging" in evidence[0].excerpt
        grants = enrich.active_grants(s, pi, TODAY)
        assert [g.project_number for g in grants] == ["5R01EY000001-03"]
        assert set(enrich.last_runs(s, pi)) == {"pubmed", "openalex", "reporter"}
        assert s.get(Pi, pi).openalex_id is None  # identifiers are never set by a lookup
    assert lookup(world, pi, FakeFetcher()) == {"pubmed": 0, "openalex": 0, "reporter": 0}
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(Evidence)) == 4
        assert s.scalar(select(func.count()).select_from(GrantEvidence)) == 2


def test_name_search_without_identifiers_and_a_failing_source(world) -> None:
    pi = world.pi(world.institution(name="Synthetic Eye Institute"), name="Dr. Alex Example")
    with transaction(world.factory) as s:
        s.get(Pi, pi).last_name = "Example"
    fetcher = FakeFetcher(fail={"reporter"})
    added = lookup(world, pi, fetcher)
    assert added == {"pubmed": 2, "openalex": 0, "reporter": 0}
    assert fetcher.calls[0][1]["term"] == (
        '"Example, Alex"[fau] AND "Synthetic Eye Institute"[ad] AND 2023:3000[dp]'
    )
    with transaction(world.factory) as s:
        runs = enrich.last_runs(s, pi)
        assert runs["reporter"].ok is False and "503" in runs["reporter"].message
        assert runs["openalex"].message.startswith("skipped")
        assert s.scalar(select(func.count()).select_from(LookupRun)) == 3


def test_applicant_publications_are_proposed_then_decided(world) -> None:
    result = enrich.gather_applicant_publications("Alex Example", "", FakeFetcher())
    with transaction(world.factory) as s:
        assert (
            enrich.store_applicant_publications(s, world.applicant_id, result, world.clock.now())
            == 2
        )
        pubs = enrich.applicant_publications(s, world.applicant_id)
        assert {p.status for p in pubs} == {"proposed"}
        enrich.decide_publication(
            s, world.applicant_id, pubs[0].id, confirm=True, now=world.clock.now()
        )
        enrich.decide_publication(
            s, world.applicant_id, pubs[1].id, confirm=False, now=world.clock.now()
        )
    with transaction(world.factory) as s:  # a re-import keeps the decisions
        assert (
            enrich.store_applicant_publications(s, world.applicant_id, result, world.clock.now())
            == 0
        )
        assert [p.status for p in enrich.applicant_publications(s, world.applicant_id)] == [
            "confirmed"
        ]
    assert not enrich.gather_applicant_publications("Alex", "", FakeFetcher()).ok


def test_window_start_handles_leap_day() -> None:
    assert enrich.window_start(date(2028, 2, 29)) == date(2025, 2, 28)
