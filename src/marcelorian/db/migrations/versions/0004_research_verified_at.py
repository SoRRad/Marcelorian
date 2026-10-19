"""research catalog: when the applicant's current finding was made

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19 18:30:00.000000

Owner decision R3: a "confirmed current" check expires after 90 days. Existing
findings take the time they were last saved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("research_record") as batch_op:
        batch_op.add_column(sa.Column("local_verified_at", sa.String(length=26), nullable=True))
    op.execute(
        "UPDATE research_record SET local_verified_at = local_updated_at "
        "WHERE local_verification != 'unchecked'"
    )


def downgrade() -> None:
    with op.batch_alter_table("research_record") as batch_op:
        batch_op.drop_column("local_verified_at")
