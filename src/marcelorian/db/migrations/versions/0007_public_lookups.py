"""public lookups and assessment: evidence, grants, applicant publications, runs

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-19 21:30:00.000000

Owner decision U1: free read-only PubMed, OpenAlex and NIH RePORTER lookups.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applicant_publication",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("venue", sa.String(length=255), nullable=False),
        sa.Column("published_on", sa.String(length=10), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("retrieved_at", sa.String(length=26), nullable=False),
        sa.Column("decided_at", sa.String(length=26), nullable=True),
        sa.CheckConstraint(
            "status IN ('proposed','confirmed','rejected')",
            name=op.f("ck_applicant_publication_status"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicant_profile.id"],
            name=op.f("fk_applicant_publication_applicant_id_applicant_profile"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_publication")),
        sa.UniqueConstraint(
            "applicant_id",
            "source",
            "external_id",
            name=op.f("uq_applicant_publication_applicant_id"),
        ),
    )
    op.create_table(
        "assessment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("research_fit", sa.String(length=16), nullable=True),
        sa.Column("fit_source", sa.String(length=16), nullable=False),
        sa.Column("fit_note", sa.Text(), nullable=True),
        sa.Column("skipped", sa.Boolean(), nullable=False),
        sa.Column("skip_reason", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "research_fit IS NULL OR research_fit IN ('strong','moderate','weak')",
            name=op.f("ck_assessment_research_fit"),
        ),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_assessment_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment")),
        sa.UniqueConstraint("pi_id", name=op.f("uq_assessment_pi_id")),
    )
    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("venue", sa.String(length=255), nullable=False),
        sa.Column("published_on", sa.String(length=10), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("retrieved_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_evidence_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence")),
        sa.UniqueConstraint("pi_id", "source", "external_id", name=op.f("uq_evidence_pi_id")),
    )
    op.create_index(op.f("ix_evidence_pi_id"), "evidence", ["pi_id"], unique=False)
    op.create_table(
        "grant_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("project_number", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("organization", sa.String(length=255), nullable=False),
        sa.Column("agency", sa.String(length=16), nullable=False),
        sa.Column("start_date", sa.String(length=10), nullable=False),
        sa.Column("end_date", sa.String(length=10), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_grant_evidence_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_grant_evidence")),
        sa.UniqueConstraint("pi_id", "project_number", name=op.f("uq_grant_evidence_pi_id")),
    )
    op.create_index(op.f("ix_grant_evidence_pi_id"), "grant_evidence", ["pi_id"], unique=False)
    op.create_table(
        "lookup_run",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("ran_at", sa.String(length=26), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_lookup_run_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lookup_run")),
    )
    op.create_index(op.f("ix_lookup_run_pi_id"), "lookup_run", ["pi_id"], unique=False)


def downgrade() -> None:
    op.drop_table("assessment")
    op.drop_table("lookup_run")
    op.drop_table("grant_evidence")
    op.drop_table("evidence")
    op.drop_table("applicant_publication")
