"""research catalog staging: seed imports, records, evidence, claims, links

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-19 12:00:00.000000

Non-authorizing research leads imported from a seed file (docs/SEED_IMPORT.md).
New tables only; nothing existing is altered.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "seed_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(length=64), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),
        sa.Column("imported_at", sa.String(length=26), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("counts_json", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seed_import")),
    )
    op.create_table(
        "research_record",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_record_id", sa.String(length=64), nullable=False),
        sa.Column("record_type", sa.String(length=16), nullable=False),
        sa.Column("contact_kind", sa.String(length=64), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("institution", sa.Text(), nullable=False),
        sa.Column("institution_key", sa.String(length=64), nullable=False),
        sa.Column("institution_group_key", sa.String(length=64), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=False),
        sa.Column("state_code", sa.String(length=16), nullable=False),
        sa.Column("country_code", sa.String(length=8), nullable=False),
        sa.Column("geography_status", sa.String(length=64), nullable=False),
        sa.Column("timezone_iana", sa.String(length=64), nullable=False),
        sa.Column("timezone_status", sa.String(length=64), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("role_status", sa.String(length=64), nullable=False),
        sa.Column("specialty", sa.Text(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("email_status", sa.String(length=64), nullable=False),
        sa.Column("email_confidence", sa.String(length=8), nullable=False),
        sa.Column("email_source_url", sa.Text(), nullable=False),
        sa.Column("email_verified_on", sa.String(length=10), nullable=False),
        sa.Column("email_is_shared", sa.Boolean(), nullable=True),
        sa.Column("recipient_dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("verification_status", sa.String(length=64), nullable=False),
        sa.Column("last_verified_on", sa.String(length=10), nullable=False),
        sa.Column("primary_source_url", sa.Text(), nullable=False),
        sa.Column("review_lane", sa.String(length=64), nullable=False),
        sa.Column("priority_band", sa.Integer(), nullable=True),
        sa.Column("funding_status", sa.String(length=64), nullable=False),
        sa.Column("unpaid_flag", sa.Boolean(), nullable=True),
        sa.Column("funding_unknown_flag", sa.Boolean(), nullable=True),
        sa.Column("opportunity_status", sa.String(length=64), nullable=False),
        sa.Column("application_opens_on", sa.String(length=10), nullable=False),
        sa.Column("application_deadline", sa.String(length=10), nullable=False),
        sa.Column("has_eligibility_restrictions", sa.Boolean(), nullable=False),
        sa.Column("freshness_date", sa.String(length=10), nullable=False),
        sa.Column("evidence_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_date", sa.String(length=10), nullable=False),
        sa.Column("row_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_json", sa.Text(), nullable=False),
        sa.Column("excluded_reason", sa.String(length=64), nullable=True),
        sa.Column("rereview_reason", sa.Text(), nullable=True),
        sa.Column("first_import_id", sa.Integer(), nullable=False),
        sa.Column("last_import_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.Column("local_status", sa.String(length=16), nullable=False),
        sa.Column("local_verification", sa.String(length=32), nullable=False),
        sa.Column("local_verification_source", sa.Text(), nullable=True),
        sa.Column("local_note", sa.Text(), nullable=True),
        sa.Column("local_updated_at", sa.String(length=26), nullable=True),
        sa.CheckConstraint(
            "local_status IN ('unreviewed','shortlisted','set_aside')",
            name=op.f("ck_research_record_local_status"),
        ),
        sa.CheckConstraint(
            "local_verification IN ('unchecked','confirmed_current','fellow_or_trainee','left_institution','not_faculty','retired_or_emeritus','wrong_person')",
            name=op.f("ck_research_record_local_verification"),
        ),
        sa.CheckConstraint(
            "record_type IN ('contact','opportunity')", name=op.f("ck_research_record_record_type")
        ),
        sa.ForeignKeyConstraint(
            ["first_import_id"],
            ["seed_import.id"],
            name=op.f("fk_research_record_first_import_id_seed_import"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_id"],
            ["seed_import.id"],
            name=op.f("fk_research_record_last_import_id_seed_import"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_record")),
        sa.UniqueConstraint(
            "external_record_id", name=op.f("uq_research_record_external_record_id")
        ),
    )
    op.create_index(
        "ix_research_record_dedupe", "research_record", ["recipient_dedupe_key"], unique=False
    )
    op.create_index(
        "ix_research_record_institution", "research_record", ["institution_key"], unique=False
    )
    op.create_index("ix_research_record_lane", "research_record", ["review_lane"], unique=False)
    op.create_table(
        "research_claim",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["research_record.id"],
            name=op.f("fk_research_claim_record_id_research_record"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_claim")),
    )
    op.create_index(
        op.f("ix_research_claim_record_id"), "research_claim", ["record_id"], unique=False
    )
    op.create_table(
        "research_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("field", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=120), nullable=False),
        sa.Column("source_access", sa.String(length=64), nullable=False),
        sa.Column("checked_on", sa.String(length=32), nullable=False),
        sa.Column("verification_origin", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("extra_json", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["research_record.id"],
            name=op.f("fk_research_evidence_record_id_research_record"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_evidence")),
    )
    op.create_index(
        op.f("ix_research_evidence_record_id"), "research_evidence", ["record_id"], unique=False
    )
    op.create_table(
        "research_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("target_external_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "kind IN ('related','same_recipient')", name=op.f("ck_research_link_kind")
        ),
        sa.ForeignKeyConstraint(
            ["record_id"],
            ["research_record.id"],
            name=op.f("fk_research_link_record_id_research_record"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_research_link")),
        sa.UniqueConstraint(
            "record_id", "target_external_id", "kind", name=op.f("uq_research_link_record_id")
        ),
    )
    op.create_index(
        op.f("ix_research_link_record_id"), "research_link", ["record_id"], unique=False
    )
    op.create_index(
        op.f("ix_research_link_target_external_id"),
        "research_link",
        ["target_external_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("research_link")
    op.drop_table("research_evidence")
    op.drop_table("research_claim")
    op.drop_table("research_record")
    op.drop_table("seed_import")
