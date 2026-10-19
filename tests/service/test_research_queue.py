"""Verification queue, reopen and confirmation expiry (owner decisions R1-R4)."""

from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from marcelorian.db.engine import make_engine, transaction
from marcelorian.db.migrate import upgrade
from marcelorian.db.models import ResearchRecord
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.research import confidence
from marcelorian.domain.seed import read_seed
from marcelorian.services import dnc, research_seed
from marcelorian.services.research_seed import SOURCE_REQUIRED, import_seed
from tests.seed_fixture import contact, csv_text, standard_rows

SOURCE = "https://synthetic-eye.test/faculty"


def rows():
    return [
        *standard_rows(),
        contact(
            "C-PART",
            role="",
            role_status="unverified",
            email_status="publication_attribution_observed",
            email_confidence="C",
            verification_status="partial_evidence",
            priority_band="60",
        ),
    ]


def run_import(world, seed_rows) -> None:
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(seed_rows), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )


def queue_ids(world, tier=None) -> list[str]:
    with transaction(world.factory) as s:
        items = research_seed.queue(s, world.clock.now().date(), tier=tier)
        return [i.record.external_record_id for i in items]


def queue_reasons(world) -> dict[str, str]:
    with transaction(world.factory) as s:
        items = research_seed.queue(s, world.clock.now().date())
        return {i.record.external_record_id: i.reason for i in items}


def find(world, external_id: str, verification: str, source: str = SOURCE, note: str = ""):
    with transaction(world.factory) as s:
        return research_seed.quick_finding(
            s,
            _id(s, external_id),
            verification=verification,
            source=source,
            note=note,
            now=world.clock.now(),
        )


def _id(s, external_id: str) -> int:
    return s.scalars(
        select(ResearchRecord.id).where(ResearchRecord.external_record_id == external_id)
    ).one()


def record(world, external_id: str) -> ResearchRecord:
    with transaction(world.factory) as s:
        return s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == external_id)
        ).one()


@pytest.fixture
def staged(world):
    run_import(world, rows())
    return world


def test_most_sendable_first_and_out_of_scope_never_queued(staged) -> None:
    assert queue_ids(staged) == ["C-FAC1", "C-PART", "O-PROG1", "C-PROG", "O-CLOSED1", "C-AUTH1"]
    assert queue_ids(staged, tier=3) == ["O-PROG1", "C-PROG", "O-CLOSED1"]
    with transaction(staged.factory) as s:
        counts = research_seed.queue_counts(research_seed.queue(s, staged.clock.now().date()))
    assert counts == {1: 1, 2: 1, 3: 3, 4: 1}


def test_confirming_requires_a_source_and_leaves_the_queue(staged) -> None:
    with pytest.raises(ValueError, match="Say where you checked"):
        find(staged, "C-FAC1", "confirmed_current", source="")
    assert SOURCE_REQUIRED.startswith("Say where")
    find(staged, "C-FAC1", "confirmed_current")
    assert "C-FAC1" not in queue_ids(staged)
    fac = record(staged, "C-FAC1")
    assert fac.local_verified_at == staged.clock.now()
    level = confidence(research_seed.facts_for(fac, staged.clock.now().date()))
    assert level.level == "confirmed"


def test_confirmation_expires_after_90_days_and_reconfirm_resets(staged) -> None:
    find(staged, "C-FAC1", "confirmed_current")
    staged.clock.advance(timedelta(days=90))
    assert "C-FAC1" not in queue_ids(staged)  # day 90 is still valid
    staged.clock.advance(timedelta(days=1))
    assert queue_reasons(staged)["C-FAC1"].startswith("Confirm again")
    assert queue_ids(staged)[0] == "C-FAC1"  # flagged items lead their tier
    fac = record(staged, "C-FAC1")
    level = confidence(research_seed.facts_for(fac, staged.clock.now().date()))
    assert level.level == "source_checked" and "confirm again" in level.reasons[0]
    find(staged, "C-FAC1", "confirmed_current")
    assert record(staged, "C-FAC1").local_verified_at == staged.clock.now()
    assert "C-FAC1" not in queue_ids(staged)


def test_saving_an_unchanged_confirmation_does_not_extend_it(staged) -> None:
    find(staged, "C-FAC1", "confirmed_current")
    first = record(staged, "C-FAC1").local_verified_at
    staged.clock.advance(timedelta(days=10))
    with transaction(staged.factory) as s:
        research_seed.set_local_review(
            s,
            _id(s, "C-FAC1"),
            status="shortlisted",
            verification="confirmed_current",
            source=SOURCE,
            note="triage only",
            now=staged.clock.now(),
        )
    assert record(staged, "C-FAC1").local_verified_at == first


def test_negative_finding_flagged_on_new_evidence_then_reopened(staged) -> None:
    find(staged, "C-FAC1", "fellow_or_trainee", note="listed as a fellow")
    assert "C-FAC1" not in queue_ids(staged)
    changed = rows()
    changed[0] = contact(
        "C-FAC1",
        role="Assistant professor",
        related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"]),
    )
    run_import(staged, changed)
    assert queue_reasons(staged)["C-FAC1"].startswith("New evidence since your finding")
    fac = record(staged, "C-FAC1")
    assert fac.local_verification == "fellow_or_trainee"  # never lifted automatically
    assert confidence(research_seed.facts_for(fac, staged.clock.now().date())).level == "blocked"
    with transaction(staged.factory) as s:
        research_seed.reopen(s, fac.id, now=staged.clock.now())
    fac = record(staged, "C-FAC1")
    assert (fac.local_verification, fac.rereview_reason, fac.local_verified_at) == (
        "unchecked",
        None,
        None,
    )
    assert queue_reasons(staged)["C-FAC1"] == "Not checked yet"
    with transaction(staged.factory) as s:
        events = [(e.action, e.from_state, e.to_state) for e in research_seed.history(s, fac)]
    assert events == [
        ("research_reopened", "fellow_or_trainee", "unchecked"),
        ("research_local_review", "unchecked", "fellow_or_trainee"),
    ]


def test_keeping_a_finding_clears_the_flag(staged) -> None:
    find(staged, "C-FAC1", "left_institution")
    changed = rows()
    changed[0] = contact(
        "C-FAC1", role="Professor", related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"])
    )
    run_import(staged, changed)
    assert "C-FAC1" in queue_ids(staged)
    find(staged, "C-FAC1", "left_institution", note="still gone")
    assert "C-FAC1" not in queue_ids(staged)
    assert record(staged, "C-FAC1").local_note == "still gone"


def test_only_negative_findings_can_be_reopened(staged) -> None:
    with transaction(staged.factory) as s, pytest.raises(ValueError):
        research_seed.reopen(s, _id(s, "C-FAC1"), now=staged.clock.now())


def test_quick_finding_keeps_triage_and_appends_notes(staged) -> None:
    with transaction(staged.factory) as s:
        research_seed.set_local_review(
            s,
            _id(s, "C-PART"),
            status="shortlisted",
            verification="unchecked",
            source="",
            note="first look",
            now=staged.clock.now(),
        )
    find(staged, "C-PART", "not_faculty", note="industry")
    part = record(staged, "C-PART")
    assert part.local_status == "shortlisted"
    assert part.local_note == "first look\nindustry"


def test_set_aside_records_leave_the_queue_until_flagged(staged) -> None:
    with transaction(staged.factory) as s:
        research_seed.set_local_review(
            s,
            _id(s, "C-AUTH1"),
            status="set_aside",
            verification="unchecked",
            source="",
            note="",
            now=staged.clock.now(),
        )
    assert "C-AUTH1" not in queue_ids(staged)


def test_live_dnc_matches_never_queue_but_simulated_ones_do(staged) -> None:
    with transaction(staged.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.SANDBOX,
            actor=Actor.APPLICANT,
            now=staged.clock.now(),
            email="c-part@synthetic-eye.test",
            reason="sandbox only",
        )
    assert "C-PART" in queue_ids(staged)
    with transaction(staged.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=staged.clock.now(),
            email="C-PART@synthetic-eye.test",
            reason="asked not to be contacted",
        )
    assert "C-PART" not in queue_ids(staged)


def test_migration_0004_dates_existing_findings(tmp_path) -> None:
    engine = make_engine(tmp_path / "old.db")
    upgrade(engine, "0003")
    stamp = "2026-10-19 10:00:00.000000"
    saved = "2026-10-19 11:00:00.000000"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO seed_import VALUES (1, 'v1', 's', 'x', '2026-10-19', :t, 'a', '{}')"),
            {"t": stamp},
        )
        columns = [
            c["name"] for c in conn.execute(text("PRAGMA table_info(research_record)")).mappings()
        ]
        for n, verification in ((1, "confirmed_current"), (2, "unchecked")):
            values: dict[str, object] = dict.fromkeys(columns, "")
            values.update(
                id=n,
                external_record_id=f"E{n}",
                record_type="contact",
                has_eligibility_restrictions=0,
                evidence_count=0,
                priority_band=None,
                email_is_shared=None,
                unpaid_flag=None,
                funding_unknown_flag=None,
                excluded_reason=None,
                rereview_reason=None,
                first_import_id=1,
                last_import_id=1,
                created_at=stamp,
                updated_at=stamp,
                local_status="unreviewed",
                local_verification=verification,
                local_verification_source=None,
                local_note=None,
                local_updated_at=saved,
            )
            names = ", ".join(columns)
            marks = ", ".join(f":{c}" for c in columns)
            sql = f"INSERT INTO research_record ({names}) VALUES ({marks})"  # noqa: S608 (schema names)
            conn.execute(text(sql), values)
    upgrade(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT external_record_id, local_verified_at FROM research_record")
        ).all()
    assert dict(rows) == {"E1": saved, "E2": None}
    engine.dispose()
