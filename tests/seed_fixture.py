"""Synthetic research-seed rows (schema marcelorian_seed_v1) with correct hashes.

All names, institutions, addresses and URLs are invented (.test domains).
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from marcelorian.domain.seed import COLUMNS, SCHEMA_VERSION, row_hash

SNAPSHOT = "2026-10-19"


def contact(record_id: str, **overrides: Any) -> dict[str, str]:
    row = {c: "" for c in COLUMNS}
    row.update(
        schema_version=SCHEMA_VERSION,
        snapshot_date=SNAPSHOT,
        record_id=record_id,
        record_type="contact",
        contact_kind="faculty_or_investigator",
        name=f"Dr Synthetic {record_id}",
        institution="Synthetic Eye Institute",
        institution_key="INST-SYN1",
        institution_group_key="GRP-SYN1",
        city="Testville",
        state_code="NY",
        country_code="US",
        geography_status="us_source_supported",
        timezone_iana="America/New_York",
        timezone_status="proposed_needs_confirmation",
        role="Associate professor",
        role_status="official_source_checked",
        specialty="Ophthalmology",
        email=f"{record_id.lower()}@synthetic-eye.test",
        email_status="official_published",
        email_confidence="A",
        email_source_url="https://synthetic-eye.test/people",
        email_verified_on="2026-10-18",
        email_is_shared="false",
        verification_status="official_contact_evidence",
        last_verified_on="2026-10-18",
        primary_source_url="https://synthetic-eye.test/people",
        review_lane="verified_contact_review",
        priority_band="30",
        funding_status="unknown",
        funding_scope="unsolicited_appointment_unknown; inspect linked opportunity rows",
        unpaid_flag="false",
        funding_unknown_flag="true",
        opportunity_status="not_applicable",
        related_record_ids_json="[]",
        details_json=json.dumps({"hiring_authority": "not_established"}),
        evidence_json=json.dumps(
            [
                {
                    "field": "role",
                    "value": "Associate professor",
                    "source_url": "https://synthetic-eye.test/people",
                    "source_type": "Official directory",
                    "source_access": "full_page",
                    "checked_on": "2026-10-18",
                    "verification_origin": "prior_research_pass",
                }
            ]
        ),
        unverified_claims_json=json.dumps({"email_claims": [], "department_claims": "Retina"}),
        provenance_json=json.dumps({"prior_dataset": "synthetic"}),
        source_urls_json=json.dumps(["https://synthetic-eye.test/people"]),
        review_flags_json=json.dumps(["prior_contact_check_not_performed"]),
        applicant_review_required="true",
        send_authorized="false",
    )
    row.update({k: v for k, v in overrides.items()})
    return seal(row)


def claim_only_author(record_id: str, **overrides: Any) -> dict[str, str]:
    base: dict[str, Any] = dict(
        contact_kind="author_unverified",
        role="",
        role_status="unverified",
        email="",
        email_status="claim_only",
        email_confidence="C",
        email_source_url="",
        email_verified_on="",
        verification_status="not_verified",
        last_verified_on="",
        review_lane="contact_verification",
        priority_band="60",
        evidence_json="[]",
        unverified_claims_json=json.dumps(
            {"email_claims": [f"{record_id.lower()}@claimed-only.test"], "role_claims": "MD"}
        ),
    )
    base.update(overrides)
    return contact(record_id, **base)


def opportunity(record_id: str, **overrides: Any) -> dict[str, str]:
    base: dict[str, Any] = dict(
        record_type="opportunity",
        contact_kind="",
        name="Synthetic Retina Research Fellowship",
        role="Fellowship coordinator",
        email="fellowship@synthetic-eye.test",
        email_status="official_program_mailbox",
        email_is_shared="true",
        recipient_dedupe_key="RCPT-SHARED1",
        verification_status="official_program_evidence",
        review_lane="paid_opportunity_review",
        priority_band="10",
        funding_status="paid_explicit",
        funding_scope="this_opportunity_only",
        unpaid_flag="false",
        funding_unknown_flag="false",
        opportunity_status="program_verify_intake",
        application_url="https://synthetic-eye.test/apply",
        degree_requirement="MD or DO",
        details_json=json.dumps({"compensation": "USD 1 synthetic stipend"}),
    )
    base.update(overrides)
    return contact(record_id, **base)


def seal(row: dict[str, str]) -> dict[str, str]:
    row["row_sha256"] = row_hash(row)
    return row


def csv_text(rows: list[dict[str, str]], extra_columns: tuple[str, ...] = ()) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=[*COLUMNS, *extra_columns], lineterminator="\r\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return out.getvalue()


def write_csv(path: Path, rows: list[dict[str, str]], extra_columns: tuple[str, ...] = ()) -> Path:
    # utf-8-sig: the real exports carry a BOM.
    path.write_text(csv_text(rows, extra_columns), encoding="utf-8-sig", newline="")
    return path


def standard_rows() -> list[dict[str, str]]:
    """A faculty contact, a claim-only author, a shared-mailbox program linked to the
    faculty member, a closed cycle linked to the same faculty member, and a non-US row."""
    return [
        contact("C-FAC1", related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"])),
        claim_only_author("C-AUTH1"),
        opportunity(
            "O-PROG1",
            related_record_ids_json=json.dumps(["C-FAC1"]),
            details_json=json.dumps(
                {"compensation": "USD 1 synthetic stipend", "same_recipient_record_ids": ["C-PROG"]}
            ),
        ),
        contact(
            "C-PROG",
            contact_kind="shared_mailbox",
            name="Synthetic Fellowship Office",
            role="Program application mailbox",
            email="fellowship@synthetic-eye.test",
            email_status="official_program_mailbox",
            email_is_shared="true",
            recipient_dedupe_key="RCPT-SHARED1",
        ),
        opportunity(
            "O-CLOSED1",
            name="Synthetic Glaucoma Fellowship 2025",
            email="",
            email_status="missing",
            email_is_shared="",
            recipient_dedupe_key="",
            opportunity_status="closed_cycle",
            review_lane="closed_cycle_archive",
            priority_band="90",
            funding_status="unpaid_explicit",
            unpaid_flag="true",
            related_record_ids_json=json.dumps(["C-FAC1"]),
        ),
        contact(
            "C-NL1",
            country_code="NL",
            geography_status="excluded_non_us",
            review_lane="excluded_non_us",
            priority_band="99",
            city="Synthdam",
            state_code="",
        ),
    ]
