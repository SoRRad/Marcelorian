"""pi.department for the same-place rule's department scope

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-19 20:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("pi") as batch_op:
        batch_op.add_column(sa.Column("department", sa.String(length=255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("pi") as batch_op:
        batch_op.drop_column("department")
