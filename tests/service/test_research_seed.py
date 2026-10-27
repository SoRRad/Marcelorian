"""Research-seed staging import: repeatable, non-authorizing, preserves local decisions.

Synthetic rows only (tests/seed_fixture.py); the session-wide network guard applies.
"""

from __future__ import annotations

import io
import json
import socket

import pytest
from sqlalchemy import func, inspect, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    AuditEvent,
    Base,
    Campaign,
    DoNotContact,
    Pi,
    PiEmail,
    ResearchClaim,
    ResearchEvidence,
    ResearchLink,
    ResearchRecord,
    SeedImport,
)
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.research import confidence
from marcelorian.domain.seed import read_seed
from marcelorian.services import dnc, research_seed, runtime
from marcelorian.services.research_seed import SeedRejected, import_seed, preview
from tests.seed_fixture import claim_only_author, contact, csv_text, standard_rows

RESEARCH_TABLES = {
    "seed_import",
    "research_record",
    "research_evidence",
    "research_claim",
    "research_link",
    "audit_event",
}


def parse(rows, extra=()):
    return read_seed(io.StringIO(csv_text(rows, extra), newline=""))


def run_import(world, rows, extra=()):
    with transaction(world.factory) as s:
        return import_seed(
            s,
            parse(rows, extra),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )


def table_counts(world) -> dict[str, int]:
    with transaction(world.factory) as s:
        return {
            t.name: s.execute(select(func.count()).select_from(t)).scalar_one()
            for t in Base.metadata.sorted_tables
        }


def staged(world, external_id: str) -> ResearchRecord:
    with transaction(world.factory) as s:
        return s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == external_id)
        ).one()


def review(world, external_id: str, verification: str = "confirmed_current", **kw) -> None:
    record_id = staged(world, external_id).id
    with transaction(world.factory) as s:
        research_seed.set_local_review(
            s,
            record_id,
            status=kw.get("status", "shortlisted"),
            verification=verification,
            source=kw.get("source", "https://synthetic-eye.test/faculty"),
            note=kw.get("note", "checked"),
            now=world.clock.now(),
        )


# --------------------------------------------------------------------------- #
# Dry run and categories
# --------------------------------------------------------------------------- #


def test_dry_run_writes_nothing(world) -> None:
    before = table_counts(world)
    with transaction(world.factory) as s:
        result = preview(s, parse(standard_rows()))
    assert table_counts(world) == before
    assert result.counts() == {
        "new": 5,
        "duplicate": 0,
        "unchanged": 0,
        "changed": 0,
        "conflicting": 0,
        "excluded": 1,
        "invalid": 0,
        "absent_from_file": 0,
    }
    assert result.record_types == {"contact": 4, "opportunity": 2}


def test_repeated_import_is_unchanged_and_idempotent(world) -> None:
    run_import(world, standard_rows())
    first = table_counts(world)
    result = run_import(world, standard_rows())
    assert result.counts()["unchanged"] == 5 and result.counts()["new"] == 0
    second = table_counts(world)
    assert {k: second[k] - first[k] for k in second if second[k] != first[k]} == {
        "seed_import": 1,
        "audit_event": 1,
    }


def test_snapshot_date_alone_does_not_change_a_record(world) -> None:
    run_import(world, standard_rows())
    rows = [dict(r, snapshot_date="2026-11-01") for r in standard_rows()]  # hash unaffected
    result = run_import(world, rows)
    assert result.counts()["unchanged"] == 5
    assert staged(world, "C-FAC1").snapshot_date == "2026-11-01"


def test_changed_record_replaces_seed_fields_and_children(world) -> None:
    run_import(world, standard_rows())
    rows = standard_rows()
    rows[0] = contact(
        "C-FAC1",
        role="Professor",
        related_record_ids_json=json.dumps(["O-PROG1"]),
        evidence_json="[]",
    )
    result = run_import(world, rows)
    assert result.categories["changed"] == ["C-FAC1"]
    record = staged(world, "C-FAC1")
    assert record.role == "Professor" and record.evidence_count == 0
    assert record.rereview_reason is None
    with transaction(world.factory) as s:
        links = s.scalars(
            select(ResearchLink.target_external_id).where(ResearchLink.record_id == record.id)
        ).all()
        assert links == ["O-PROG1"]
        assert (
            s.scalar(
                select(func.count())
                .select_from(ResearchEvidence)
                .where(ResearchEvidence.record_id == record.id)
            )
            == 0
        )


def test_change_after_local_review_conflicts_and_keeps_decisions(world) -> None:
    run_import(world, standard_rows())
    review(world, "C-FAC1", "fellow_or_trainee", status="set_aside", note="is a fellow")
    rows = standard_rows()
    rows[0] = contact(
        "C-FAC1", role="Professor", related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"])
    )
    with transaction(world.factory) as s:
        assert preview(s, parse(rows)).categories["conflicting"] == ["C-FAC1"]
    run_import(world, rows)
    record = staged(world, "C-FAC1")
    assert record.role == "Professor"  # seed field updated
    assert (record.local_verification, record.local_status, record.local_note) == (
        "fellow_or_trainee",
        "set_aside",
        "is a fellow",
    )
    assert record.rereview_reason and "your decisions were kept" in record.rereview_reason
    level = confidence(research_seed.facts_for(record, world.clock.now().date()))
    assert level.level == "blocked"  # the applicant's finding still wins
    review(world, "C-FAC1", "fellow_or_trainee", status="set_aside")
    assert staged(world, "C-FAC1").rereview_reason is None


def test_absent_records_are_reported_and_kept(world) -> None:
    run_import(world, standard_rows())
    rows = [r for r in standard_rows() if r["record_id"] != "C-AUTH1"]
    result = run_import(world, rows)
    assert result.absent == ["C-AUTH1"]
    assert staged(world, "C-AUTH1").name


def test_unknown_columns_survive_in_the_raw_record(world) -> None:
    from tests.seed_fixture import seal

    row = contact("C-NEW")
    row["future_field"] = "kept verbatim"
    run_import(world, [seal(row)], extra=("future_field",))
    raw = json.loads(staged(world, "C-NEW").raw_json)
    assert raw["future_field"] == "kept verbatim"
    assert raw["evidence_json"]  # the complete row, JSON columns as text


def test_external_ids_are_not_primary_keys(world) -> None:
    run_import(world, standard_rows())
    with transaction(world.factory) as s:
        ids = s.execute(select(ResearchRecord.id, ResearchRecord.external_record_id)).all()
    assert sorted(i for i, _ in ids) == list(range(1, len(ids) + 1))
    assert all(isinstance(i, int) and not ext.isdigit() for i, ext in ids)


# --------------------------------------------------------------------------- #
# Whole-file refusal
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": "other"},
        {"details_json": "{oops"},
        {"send_authorized": "true"},
        {"related_record_ids_json": json.dumps(["NOWHERE"])},
    ],
)
def test_one_invalid_row_refuses_the_whole_file(world, bad) -> None:
    rows = [*standard_rows(), contact("C-BAD", **bad)]
    before = table_counts(world)
    with pytest.raises(SeedRejected) as caught:
        run_import(world, rows)
    assert caught.value.preview.invalid
    assert table_counts(world) == before


def test_tampered_hash_and_duplicates_refuse(world) -> None:
    tampered = contact("C-T")
    tampered["name"] = "changed"
    with pytest.raises(SeedRejected):
        run_import(world, [*standard_rows(), tampered])
    with pytest.raises(SeedRejected):
        run_import(world, [*standard_rows(), contact("C-FAC1", name="twin")])
    assert table_counts(world)["research_record"] == 0


def test_record_type_change_is_invalid(world) -> None:
    run_import(world, standard_rows())
    rows = standard_rows()
    rows[1] = contact("C-AUTH1", record_type="opportunity")
    with pytest.raises(SeedRejected) as caught:
        run_import(world, rows)
    assert [e.reason for e in caught.value.preview.invalid] == ["record_type_changed"]


def test_links_may_point_at_already_staged_records(world) -> None:
    run_import(world, standard_rows())
    result = run_import(world, [contact("C-LATE", related_record_ids_json='["C-FAC1"]')])
    assert result.categories["new"] == ["C-LATE"]


# --------------------------------------------------------------------------- #
# Semantics
# --------------------------------------------------------------------------- #


def test_shared_mailbox_and_program_person_links_form_one_route_group(world) -> None:
    run_import(world, standard_rows())
    with transaction(world.factory) as s:
        prog = s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == "O-PROG1")
        ).one()
        group = {r.external_record_id for r in research_seed.route_group(s, prog)}
        mailbox = s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == "C-PROG")
        ).one()
        mailbox_group = {r.external_record_id for r in research_seed.route_group(s, mailbox)}
        rows = research_seed.list_records(s, research_seed.Filters(), world.clock.now().date())
    assert group == {"C-FAC1", "C-PROG"}
    assert "O-PROG1" in mailbox_group
    shared = {r.record.external_record_id for r in rows if r.shared_route}
    assert shared == {"O-PROG1", "C-PROG"}


def test_program_mailbox_is_not_a_pi_and_compensation_stays_on_the_opportunity(world) -> None:
    run_import(world, standard_rows())
    mailbox, faculty, prog = (staged(world, i) for i in ("C-PROG", "C-FAC1", "O-PROG1"))
    assert mailbox.contact_kind == "shared_mailbox" and mailbox.email_is_shared is True
    assert prog.funding_status == "paid_explicit"
    assert faculty.funding_status == "unknown"  # not inherited from the linked program
    assert mailbox.funding_status == "unknown"
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(Pi)) == 0


def test_closed_cycle_stays_closed_and_does_not_exclude_its_faculty(world) -> None:
    run_import(world, standard_rows())
    closed, faculty = staged(world, "O-CLOSED1"), staged(world, "C-FAC1")
    assert closed.opportunity_status == "closed_cycle" and closed.unpaid_flag is True
    assert faculty.excluded_reason is None
    run_import(world, standard_rows())  # re-import never reopens it
    assert staged(world, "O-CLOSED1").opportunity_status == "closed_cycle"


def test_institution_and_timezone_stay_proposals(world) -> None:
    from marcelorian.db.models import Institution

    run_import(world, standard_rows())
    record = staged(world, "C-FAC1")
    assert record.timezone_status == "proposed_needs_confirmation"
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(Institution)) == 0


def test_claims_and_evidence_are_stored_separately(world) -> None:
    run_import(world, standard_rows())
    author, faculty = staged(world, "C-AUTH1"), staged(world, "C-FAC1")
    with transaction(world.factory) as s:
        claims = dict(research_seed.claims_for(s, author))
        evidence = research_seed.evidence_for(s, faculty)
        assert (
            s.scalar(
                select(func.count())
                .select_from(ResearchEvidence)
                .where(ResearchEvidence.record_id == author.id)
            )
            == 0
        )
        assert s.scalar(select(func.count()).select_from(ResearchClaim)) >= 2
    assert claims["email_claims"] == ["c-auth1@claimed-only.test"]
    assert evidence[0].source_url == "https://synthetic-eye.test/people"
    assert author.email == ""  # a claimed address never becomes the record's address


def test_never_infers_not_previously_contacted(world) -> None:
    run_import(world, standard_rows())
    flags = research_seed.review_flags(staged(world, "C-FAC1"))
    assert "prior_contact_check_not_performed" in flags


# --------------------------------------------------------------------------- #
# Cannot send, approve, reserve, change modes or promote an address
# --------------------------------------------------------------------------- #


def test_import_touches_only_research_tables(world) -> None:
    inst = world.institution()
    pi = world.pi(inst, email="c-fac1@synthetic-eye.test")
    campaign = world.campaign(pi)
    message = world.draft_and_approve(campaign)
    with transaction(world.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            email="c-auth1@claimed-only.test",
            reason="asked not to be contacted",
        )
        mode_before = runtime.get_runtime(s, world.clock.now())
    before = table_counts(world)
    state_before = world.message_state(message)
    campaign_before = world.campaign_row(campaign).state
    run_import(world, [*standard_rows(), claim_only_author("C-AUTH2")])
    after = table_counts(world)
    changed = {k for k in after if after[k] != before[k]}
    assert changed <= RESEARCH_TABLES
    assert world.message_state(message) == state_before
    with transaction(world.factory) as s:
        assert s.get(Campaign, campaign).state == campaign_before
        assert runtime.get_runtime(s, world.clock.now()) == mode_before
        audit_actions = set(
            s.scalars(select(AuditEvent.action).where(AuditEvent.at == world.clock.now()))
        )
        assert "research_seed_imported" in audit_actions
        # The existing PI, its address and the DNC entry are untouched and visible.
        fac = s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == "C-FAC1")
        ).one()
        matches = research_seed.local_matches(s, fac)
        assert matches.pi_ids == [pi] and matches.campaign_states
        assert s.scalar(select(func.count()).select_from(DoNotContact)) == 1
        assert s.scalar(select(func.count()).select_from(PiEmail)) == 1


def test_unverified_email_is_never_promoted(world) -> None:
    run_import(world, [claim_only_author("C-AUTH9")])
    with transaction(world.factory) as s:
        assert s.scalar(select(func.count()).select_from(PiEmail)) == 0
        assert s.scalar(select(func.count()).select_from(Pi)) == 0
    assert staged(world, "C-AUTH9").email == ""


def test_dnc_match_is_shown_on_the_research_record(world) -> None:
    run_import(world, standard_rows())
    with transaction(world.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
            email="C-FAC1@synthetic-eye.test",
            reason="synthetic",
        )
    with transaction(world.factory) as s:
        fac = s.scalars(
            select(ResearchRecord).where(ResearchRecord.external_record_id == "C-FAC1")
        ).one()
        assert len(research_seed.local_matches(s, fac).dnc) == 1


def test_import_opens_no_network_connection(world, monkeypatch) -> None:
    calls: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        calls.append(args)
        raise AssertionError("network used by import")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    run_import(world, standard_rows())
    assert calls == []


def test_research_tables_have_no_link_to_the_send_path() -> None:
    send_tables = {
        "pi",
        "pi_email",
        "campaign",
        "message",
        "approval_snapshot",
        "reservation",
        "send_attempt",
        "institution",
    }
    for name in ("research_record", "research_evidence", "research_claim", "research_link"):
        table = Base.metadata.tables[name]
        links = {(fk.parent.name, fk.column.table.name) for fk in table.foreign_keys}
        # The only link: the applicant's promotion records which contact a lead became.
        assert {pair for pair in links if pair[1] in send_tables} <= {("promoted_pi_id", "pi")}
    for name in send_tables:
        targets = {fk.column.table.name for fk in Base.metadata.tables[name].foreign_keys}
        assert not targets & {"research_record", "seed_import"}
    assert inspect(ResearchRecord).primary_key[0].name == "id"


# --------------------------------------------------------------------------- #
# Local review
# --------------------------------------------------------------------------- #


def test_local_review_validates_and_audits(world) -> None:
    run_import(world, standard_rows())
    record_id = staged(world, "C-FAC1").id
    with transaction(world.factory) as s:
        with pytest.raises(ValueError):
            research_seed.set_local_review(
                s,
                record_id,
                status="approved",
                verification="unchecked",
                source="",
                note="",
                now=world.clock.now(),
            )
        with pytest.raises(ValueError):
            research_seed.set_local_review(
                s,
                record_id,
                status="unreviewed",
                verification="sent",
                source="",
                note="",
                now=world.clock.now(),
            )
    review(world, "C-FAC1", "left_institution", note="moved to another synthetic university")
    record = staged(world, "C-FAC1")
    assert record.local_verification == "left_institution"
    assert record.local_updated_at == world.clock.now()
    with transaction(world.factory) as s:
        event = s.scalars(
            select(AuditEvent).where(AuditEvent.action == "research_local_review")
        ).one()
        assert (event.from_state, event.to_state) == ("unchecked", "left_institution")


def test_filters(world) -> None:
    run_import(world, standard_rows())
    today = world.clock.now().date()
    with transaction(world.factory) as s:

        def ids(**kw):
            return {
                r.record.external_record_id
                for r in research_seed.list_records(s, research_seed.Filters(**kw), today)
            }

        assert ids(record_type="opportunity") == {"O-PROG1", "O-CLOSED1"}
        assert ids(review_lane="closed_cycle_archive") == {"O-CLOSED1"}
        assert ids(funding_status="paid_explicit") == {"O-PROG1"}
        assert ids(restrictions="yes") == {"O-PROG1", "O-CLOSED1"}
        assert ids(email_status="claim_only") == {"C-AUTH1"}
        assert ids(freshness="unknown") == {"C-AUTH1"}
        assert "C-FAC1" in ids(freshness="recent")
        assert ids(confidence="none") == {"C-AUTH1"}
        assert ids(confidence="blocked") == {"C-NL1"}
        assert ids(query="fellowship office") == {"C-PROG"}
        assert ids(institution_key="INST-SYN1") == ids()
        assert SeedImport.__tablename__ == "seed_import"
