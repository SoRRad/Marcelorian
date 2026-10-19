"""message.draft_fields_json: personal lines and their sources (P2 drafting)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 22:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("message") as batch_op:
        batch_op.add_column(sa.Column("draft_fields_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("message", recreate="always") as batch_op:
        batch_op.drop_column("draft_fields_json")
