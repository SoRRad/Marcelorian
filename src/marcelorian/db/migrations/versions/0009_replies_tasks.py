"""campaign.reply_tag and the task table (P2 replies)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-19 22:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("campaign") as batch_op:
        batch_op.add_column(sa.Column("reply_tag", sa.String(length=32), nullable=True))
    op.create_table(
        "task",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("due_on", sa.String(length=10), nullable=True),
        sa.Column("details", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("done_at", sa.String(length=26), nullable=True),
        sa.CheckConstraint("kind IN ('referral','contact_later')", name=op.f("ck_task_kind")),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_task_campaign_id_campaign")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_task")),
    )
    op.create_index(op.f("ix_task_campaign_id"), "task", ["campaign_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_task_campaign_id"), table_name="task")
    op.drop_table("task")
    with op.batch_alter_table("campaign", recreate="always") as batch_op:
        batch_op.drop_column("reply_tag")
