"""position_link: posted positions the applicant confirmed for a contact (tailoring)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-20 04:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "position_link",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("research_record_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("confirmed_at", sa.String(length=26), nullable=False),
        sa.Column("withdrawn_at", sa.String(length=26), nullable=True),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_position_link_pi_id_pi")),
        sa.ForeignKeyConstraint(
            ["research_record_id"],
            ["research_record.id"],
            name=op.f("fk_position_link_research_record_id_research_record"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_position_link")),
        sa.UniqueConstraint("pi_id", "research_record_id", name=op.f("uq_position_link_pi_id")),
    )
    op.create_index(op.f("ix_position_link_pi_id"), "position_link", ["pi_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_position_link_pi_id"), table_name="position_link")
    op.drop_table("position_link")
