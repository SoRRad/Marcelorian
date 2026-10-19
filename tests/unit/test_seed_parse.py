"""Seed file parsing and validation (schema marcelorian_seed_v1), synthetic rows only."""

from __future__ import annotations

import io
import json
from datetime import date
from pathlib import Path

import pytest

from marcelorian.domain.seed import (
    COLUMNS,
    SeedFormatError,
    dangling_references,
    read_seed,
    read_seed_file,
    row_hash,
)
from tests.seed_fixture import contact, csv_text, seal, standard_rows, write_csv


def parse(rows, extra=()):
    return read_seed(io.StringIO(csv_text(rows, extra), newline=""))


def reasons(parsed):
    return sorted(e.reason for e in parsed.errors)


def test_standard_fixture_parses_with_bom(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "seed.csv", standard_rows())
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    parsed = read_seed_file(str(path))
    assert parsed.ok, parsed.errors
    assert parsed.row_count == 6
    assert {r.record_id for r in parsed.records} >= {"C-FAC1", "O-PROG1"}
    fac = next(r for r in parsed.records if r.record_id == "C-FAC1")
    assert fac.bools["email_is_shared"] is False and fac.bools["send_authorized"] is False
    assert fac.dates["last_verified_on"] == date(2026, 10, 18)
    assert fac.json["related_record_ids_json"] == ["O-PROG1", "O-CLOSED1"]
    assert dangling_references(parsed) == []


def test_blank_boolean_is_unknown() -> None:
    parsed = parse([contact("C1", unpaid_flag="")])
    assert parsed.ok and parsed.records[0].bools["unpaid_flag"] is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"schema_version": "marcelorian_seed_v2"},
            "unsupported_schema_version:marcelorian_seed_v2",
        ),
        ({"record_type": "lab"}, "unknown_record_type:lab"),
        ({"details_json": "{not json"}, "malformed_json:details_json"),
        ({"details_json": "{'a': 1}"}, "malformed_json:details_json"),  # never eval
        ({"evidence_json": "{}"}, "wrong_json_type:evidence_json"),
        ({"related_record_ids_json": "[1]"}, "wrong_json_type:related_record_ids_json"),
        ({"unpaid_flag": "yes"}, "invalid_boolean:unpaid_flag"),
        ({"send_authorized": "true"}, "send_authorized_must_be_false"),
        ({"send_authorized": ""}, "send_authorized_must_be_false"),
        ({"applicant_review_required": "false"}, "applicant_review_required_must_be_true"),
        ({"application_deadline": "next week"}, "invalid_date:application_deadline"),
        ({"priority_band": "high"}, "invalid_priority_band"),
    ],
)
def test_invalid_rows_are_reported(overrides, reason) -> None:
    parsed = parse([contact("C1", **overrides)])
    assert reasons(parsed) == [reason]
    assert parsed.records == []


def test_bad_hash_is_rejected() -> None:
    row = contact("C1")
    row["name"] = "Edited after sealing"
    assert reasons(parse([row])) == ["row_sha256_mismatch"]


def test_hash_ignores_snapshot_date_only() -> None:
    row = contact("C1")
    later = dict(row, snapshot_date="2027-01-01")
    assert row_hash(later) == row["row_sha256"]
    assert row_hash(dict(row, name="Other")) != row["row_sha256"]


def test_duplicate_ids_remove_both_rows() -> None:
    parsed = parse([contact("C1"), contact("C1", name="Twin"), contact("C2")])
    assert reasons(parsed) == ["duplicate_record_id"]
    assert [r.record_id for r in parsed.records] == ["C2"]


def test_dangling_relationships_are_found() -> None:
    parsed = parse([contact("C1", related_record_ids_json=json.dumps(["MISSING"]))])
    assert parsed.ok
    assert [e.reason for e in dangling_references(parsed)] == ["dangling_reference:MISSING"]
    assert dangling_references(parsed, known_ids={"MISSING"}) == []


def test_same_recipient_ids_count_as_relationships() -> None:
    parsed = parse(
        [contact("C1", details_json=json.dumps({"same_recipient_record_ids": ["GONE"]}))]
    )
    assert [e.reason for e in dangling_references(parsed)] == ["dangling_reference:GONE"]


def test_unknown_columns_are_kept_and_reported() -> None:
    row = contact("C1")
    row["future_field"] = "kept"
    row = seal(row)  # the hash covers every column, unknown ones included
    parsed = parse([row], extra=("future_field",))
    assert parsed.ok and parsed.unknown_columns == ("future_field",)
    assert parsed.records[0].raw["future_field"] == "kept"


def test_missing_column_is_fatal() -> None:
    text = csv_text([contact("C1")]).replace("send_authorized", "send_ok", 1)
    with pytest.raises(SeedFormatError):
        read_seed(io.StringIO(text, newline=""))


def test_excluded_reason_for_non_us() -> None:
    parsed = parse([contact("C1", country_code="NL"), contact("C2")])
    flags = {r.record_id: r.excluded_reason for r in parsed.records}
    assert flags == {"C1": "excluded_non_us", "C2": None}


def test_columns_match_the_contract() -> None:
    assert len(COLUMNS) == 60 and len(set(COLUMNS)) == 60
