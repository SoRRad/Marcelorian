"""A second lead file adds new people but never the same person twice (owner, 2026-10-27)."""

from __future__ import annotations

import json

from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import ResearchLink, ResearchRecord
from marcelorian.services.research_seed import identity_keys, preview
from tests.seed_fixture import contact, opportunity, standard_rows
from tests.service.test_research_seed import parse, run_import


def external_ids(world) -> set[str]:
    with transaction(world.factory) as s:
        return set(s.scalars(select(ResearchRecord.external_record_id)))


def test_second_file_skips_people_already_in_the_list(world) -> None:
    run_import(world, standard_rows())
    second = [
        contact(  # same address as C-FAC1 under a new id (and a different spelling)
            "X-FAC1-AGAIN", name="Synthetic C-FAC1, MD", email="C-FAC1@synthetic-eye.test"
        ),
        contact(  # same name at the same institution, another address
            "X-FAC1-NEWMAIL",
            name="Dr. Synthetic C-FAC1",
            email="a.different.address@synthetic-eye.test",
        ),
        contact("X-NEW", name="Dr. Brand New", email="brand.new@synthetic-eye.test"),
        opportunity(
            "X-OPP",
            name="Synthetic Imaging Fellowship",
            application_url="https://synthetic-eye.test/imaging",
            related_record_ids_json=json.dumps(["X-FAC1-AGAIN"]),
        ),
    ]
    with transaction(world.factory) as s:
        plan = preview(s, parse(second))
    assert plan.categories["duplicate"] == ["X-FAC1-AGAIN", "X-FAC1-NEWMAIL"]
    assert plan.duplicate_of == {"X-FAC1-AGAIN": "C-FAC1", "X-FAC1-NEWMAIL": "C-FAC1"}
    result = run_import(world, second)
    assert result.counts()["new"] == 2 and result.counts()["duplicate"] == 2
    ids = external_ids(world)
    assert {"X-NEW", "X-OPP"} <= ids and not {"X-FAC1-AGAIN", "X-FAC1-NEWMAIL"} & ids
    with transaction(world.factory) as s:
        opp = s.scalar(select(ResearchRecord).where(ResearchRecord.external_record_id == "X-OPP"))
        links = s.scalars(select(ResearchLink).where(ResearchLink.record_id == opp.id)).all()
        assert [link.target_external_id for link in links] == ["C-FAC1"]  # the kept record


def test_duplicates_inside_one_file_keep_the_first(world) -> None:
    rows = [
        contact("A-1", name="Dr. Same Person", email="same@synthetic-eye.test"),
        contact("A-2", name="Dr Same Person", email="SAME@synthetic-eye.test"),
    ]
    result = run_import(world, rows)
    assert result.duplicate_of == {"A-2": "A-1"}
    assert external_ids(world) == {"A-1"}


def test_shared_mailboxes_and_reused_portals_are_not_duplicates() -> None:
    shared = identity_keys(
        "contact",
        name="",
        institution_key="",
        email="fellowship@x.test",
        email_is_shared=True,
        application_url="",
    )
    assert shared == set()
    first = identity_keys(
        "opportunity",
        name="Retina Fellowship 2025",
        institution_key="x",
        email="",
        email_is_shared=False,
        application_url="https://x.test/apply",
    )
    second = identity_keys(
        "opportunity",
        name="Glaucoma Fellowship 2026",
        institution_key="x",
        email="",
        email_is_shared=False,
        application_url="https://x.test/apply/",
    )
    assert not first & second


def test_reimporting_the_same_file_still_reports_unchanged(world) -> None:
    run_import(world, standard_rows())
    again = run_import(world, standard_rows())
    assert again.counts()["duplicate"] == 0 and again.counts()["unchanged"] == 5
