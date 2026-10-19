"""Research-catalog seed file: parsing and validation (schema ``marcelorian_seed_v1``).

Pure functions, no database and no network. A seed row is a *research lead*, never an
authorization: every row must say ``applicant_review_required=true`` and
``send_authorized=false``. Nothing here creates PIs, emails, campaigns or approvals.

Parsing rules (docs/SEED_IMPORT.md):

* ``csv.DictReader`` over text opened with ``encoding="utf-8-sig", newline=""``.
* ``*_json`` columns are parsed with ``json.loads`` (never ``eval``) and must have the
  expected JSON type.
* Booleans are explicit ``true`` / ``false``; blank means unknown (``None``).
* Dates are ISO ``YYYY-MM-DD`` or blank. ``snapshot_date`` (when the file was made) is
  distinct from the evidence dates (when a source was checked).
* ``row_sha256`` is SHA-256 of the compact, key-sorted JSON (UTF-8, non-ASCII kept) of
  every column except ``row_sha256`` and ``snapshot_date``, so re-exporting unchanged
  data on another day keeps the hash.
* Unknown extra columns are kept with the record and reported, never rejected.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import IO, Any

SCHEMA_VERSION = "marcelorian_seed_v1"

COLUMNS: tuple[str, ...] = (
    "schema_version",
    "snapshot_date",
    "record_id",
    "record_type",
    "contact_kind",
    "name",
    "institution",
    "institution_key",
    "institution_group_key",
    "city",
    "state_code",
    "country_code",
    "geography_status",
    "timezone_iana",
    "timezone_status",
    "role",
    "role_status",
    "specialty",
    "subspecialties_claimed",
    "research_topics_verified",
    "email",
    "email_status",
    "email_confidence",
    "email_source_url",
    "email_verified_on",
    "email_is_shared",
    "recipient_dedupe_key",
    "verification_status",
    "last_verified_on",
    "primary_source_url",
    "review_lane",
    "priority_band",
    "priority_reason",
    "next_action",
    "outreach_approach",
    "funding_status",
    "funding_scope",
    "unpaid_flag",
    "funding_unknown_flag",
    "opportunity_status",
    "application_url",
    "application_opens_on",
    "application_deadline",
    "deadline_note",
    "degree_requirement",
    "prior_training_requirement",
    "visa_requirement",
    "related_record_ids_json",
    "orcid",
    "openalex_id",
    "institution_ror_id",
    "details_json",
    "evidence_json",
    "unverified_claims_json",
    "provenance_json",
    "source_urls_json",
    "review_flags_json",
    "row_sha256",
    "applicant_review_required",
    "send_authorized",
)

#: Expected JSON type of each ``*_json`` column.
JSON_COLUMNS: dict[str, type] = {
    "related_record_ids_json": list,
    "details_json": dict,
    "evidence_json": list,
    "unverified_claims_json": dict,
    "provenance_json": dict,
    "source_urls_json": list,
    "review_flags_json": list,
}
BOOL_COLUMNS = (
    "email_is_shared",
    "unpaid_flag",
    "funding_unknown_flag",
    "applicant_review_required",
    "send_authorized",
)
DATE_COLUMNS = (
    "snapshot_date",
    "email_verified_on",
    "last_verified_on",
    "application_opens_on",
    "application_deadline",
)
RECORD_TYPES = ("contact", "opportunity")
#: Excluded from the hash: the row's own hash and the day the file was generated.
HASH_EXCLUDED = ("row_sha256", "snapshot_date")


class SeedFormatError(ValueError):
    """The file as a whole cannot be read as this schema (fatal, nothing is staged)."""


@dataclass(frozen=True)
class SeedRecord:
    """One validated row. ``raw`` keeps every column exactly as read (incl. unknown)."""

    record_id: str
    record_type: str
    row_sha256: str
    raw: Mapping[str, str]
    json: Mapping[str, Any]
    bools: Mapping[str, bool | None]
    dates: Mapping[str, date | None]

    def text(self, column: str) -> str:
        return self.raw.get(column, "")

    @property
    def excluded_reason(self) -> str | None:
        """Out of scope (spec §1: US institutions only); kept in staging, flagged."""
        if self.text("geography_status") == "excluded_non_us":
            return "excluded_non_us"
        if self.text("review_lane") == "excluded_non_us":
            return "excluded_non_us"
        country = self.text("country_code")
        if country and country != "US":
            return "excluded_non_us"
        return None

    def related_ids(self) -> list[str]:
        """Every record this one points at: related ids and same-recipient ids."""
        ids = [str(v) for v in self.json["related_record_ids_json"]]
        same = self.json["details_json"].get("same_recipient_record_ids") or []
        if isinstance(same, list):
            ids += [str(v) for v in same]
        return list(dict.fromkeys(ids))


@dataclass(frozen=True)
class RowError:
    line: int  # 1-based line in the file (header is line 1)
    record_id: str
    reason: str


@dataclass
class ParsedSeed:
    records: list[SeedRecord] = field(default_factory=list)
    errors: list[RowError] = field(default_factory=list)
    unknown_columns: tuple[str, ...] = ()
    row_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def snapshot_dates(self) -> set[str]:
        return {r.text("snapshot_date") for r in self.records}


def row_hash(row: Mapping[str, str]) -> str:
    payload = {k: v for k, v in row.items() if k not in HASH_EXCLUDED}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def parse_bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered == "":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"not true/false: {value!r}")


def parse_date(value: str) -> date | None:
    if value.strip() == "":
        return None
    return date.fromisoformat(value.strip())


def read_seed(stream: IO[str]) -> ParsedSeed:
    """Parse a seed opened with ``encoding="utf-8-sig", newline=""``."""
    reader = csv.DictReader(stream)
    header = tuple(reader.fieldnames or ())
    missing = [c for c in COLUMNS if c not in header]
    if missing:
        raise SeedFormatError("missing columns: " + ", ".join(missing))
    if len(set(header)) != len(header):
        raise SeedFormatError("duplicate column names in the header")
    parsed = ParsedSeed(unknown_columns=tuple(c for c in header if c not in COLUMNS))
    for index, row in enumerate(reader, start=2):
        parsed.row_count += 1
        if None in row:  # more cells than header columns
            parsed.errors.append(RowError(index, row.get("record_id") or "", "extra_cells"))
            continue
        clean = {k: (v if v is not None else "") for k, v in row.items()}
        record, reason = _validate_row(clean)
        if record is None:
            parsed.errors.append(RowError(index, clean.get("record_id", ""), reason))
        else:
            parsed.records.append(record)
    _check_file_rules(parsed)
    return parsed


def read_seed_file(path: str) -> ParsedSeed:
    with open(path, encoding="utf-8-sig", newline="") as stream:
        return read_seed(stream)


def _validate_row(row: dict[str, str]) -> tuple[SeedRecord | None, str]:
    record_id = row["record_id"].strip()
    if row["schema_version"] != SCHEMA_VERSION:
        return None, f"unsupported_schema_version:{row['schema_version']}"
    if not record_id:
        return None, "missing_record_id"
    if row["record_type"] not in RECORD_TYPES:
        return None, f"unknown_record_type:{row['record_type']}"
    if row_hash(row) != row["row_sha256"].strip().lower():
        return None, "row_sha256_mismatch"
    parsed_json: dict[str, Any] = {}
    for column, expected in JSON_COLUMNS.items():
        try:
            value = json.loads(row[column]) if row[column].strip() else expected()
        except json.JSONDecodeError:
            return None, f"malformed_json:{column}"
        if not isinstance(value, expected):
            return None, f"wrong_json_type:{column}"
        parsed_json[column] = value
    if any(not isinstance(v, str) for v in parsed_json["related_record_ids_json"]):
        return None, "wrong_json_type:related_record_ids_json"
    if any(not isinstance(v, dict) for v in parsed_json["evidence_json"]):
        return None, "wrong_json_type:evidence_json"
    bools: dict[str, bool | None] = {}
    for column in BOOL_COLUMNS:
        try:
            bools[column] = parse_bool(row[column])
        except ValueError:
            return None, f"invalid_boolean:{column}"
    if bools["applicant_review_required"] is not True:
        return None, "applicant_review_required_must_be_true"
    if bools["send_authorized"] is not False:
        return None, "send_authorized_must_be_false"
    dates: dict[str, date | None] = {}
    for column in DATE_COLUMNS:
        try:
            dates[column] = parse_date(row[column])
        except ValueError:
            return None, f"invalid_date:{column}"
    if dates["snapshot_date"] is None:
        return None, "missing_snapshot_date"
    band = row["priority_band"].strip()
    if band and not band.isdigit():
        return None, "invalid_priority_band"
    return (
        SeedRecord(
            record_id=record_id,
            record_type=row["record_type"],
            row_sha256=row["row_sha256"].strip().lower(),
            raw=dict(row),
            json=parsed_json,
            bools=bools,
            dates=dates,
        ),
        "",
    )


def _check_file_rules(parsed: ParsedSeed) -> None:
    seen: dict[str, int] = {}
    duplicates: set[str] = set()
    for record in parsed.records:
        seen[record.record_id] = seen.get(record.record_id, 0) + 1
        if seen[record.record_id] > 1:
            duplicates.add(record.record_id)
    for record_id in sorted(duplicates):
        parsed.errors.append(RowError(0, record_id, "duplicate_record_id"))
    if duplicates:
        parsed.records = [r for r in parsed.records if r.record_id not in duplicates]


def dangling_references(parsed: ParsedSeed, known_ids: Iterable[str] = ()) -> list[RowError]:
    """Relationships pointing at an id that is neither in this file nor already staged."""
    ids = {r.record_id for r in parsed.records} | set(known_ids)
    return [
        RowError(0, record.record_id, f"dangling_reference:{target}")
        for record in parsed.records
        for target in record.related_ids()
        if target not in ids
    ]
