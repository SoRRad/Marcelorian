"""research catalog: link a record to the contact it was turned into

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-19 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_record", recreate="always") as batch_op:
        batch_op.add_column(sa.Column("promoted_pi_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("promoted_at", sa.String(length=26), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_research_record_promoted_pi_id_pi"), "pi", ["promoted_pi_id"], ["id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("research_record", recreate="always") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_research_record_promoted_pi_id_pi"), type_="foreignkey"
        )
        batch_op.drop_column("promoted_at")
        batch_op.drop_column("promoted_pi_id")
