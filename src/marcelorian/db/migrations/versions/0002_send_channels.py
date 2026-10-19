"""send channels: Outlook hand-off mailbox kind, attempt channel, handed_off states

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19 00:00:00.000000

Owner decisions S1-S6 (spec §9.8). SQLite cannot alter CHECK constraints in place,
so the affected tables are rebuilt in batch mode.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MESSAGE_STATES_OLD = (
    "state IN ('draft','awaiting_approval','approved_queued','sending','sent',"
    "'failed_definite','uncertain','cancelled','needs_reapproval')"
)
_MESSAGE_STATES_NEW = (
    "state IN ('draft','awaiting_approval','approved_queued','sending','sent',"
    "'failed_definite','uncertain','cancelled','needs_reapproval','handed_off')"
)
_ATTEMPT_STATUS_OLD = "status IN ('in_flight','sent','failed_definite','uncertain','not_submitted')"
_ATTEMPT_STATUS_NEW = (
    "status IN ('in_flight','sent','failed_definite','uncertain','not_submitted','handed_off')"
)


def upgrade() -> None:
    with op.batch_alter_table("mailbox_account", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.String(length=16), server_default="gmail", nullable=False)
        )
        batch_op.create_check_constraint("kind", "kind IN ('gmail','outlook_handoff')")

    with op.batch_alter_table("message", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_message_state"), type_="check")
        batch_op.create_check_constraint("state", _MESSAGE_STATES_NEW)

    with op.batch_alter_table("send_attempt", recreate="always") as batch_op:
        batch_op.drop_index("uq_send_attempt_unresolved")
        batch_op.add_column(
            sa.Column("channel", sa.String(length=16), server_default="gmail", nullable=False)
        )
        batch_op.drop_constraint(op.f("ck_send_attempt_status"), type_="check")
        batch_op.create_check_constraint("status", _ATTEMPT_STATUS_NEW)
        batch_op.create_check_constraint(
            "channel", "channel IN ('sandbox','gmail','outlook_handoff')"
        )
        batch_op.create_index(
            "uq_send_attempt_unresolved",
            ["message_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('in_flight','uncertain','handed_off')"),
        )
    op.execute("UPDATE send_attempt SET channel = 'sandbox' WHERE mode = 'sandbox'")


def downgrade() -> None:
    with op.batch_alter_table("send_attempt", recreate="always") as batch_op:
        batch_op.drop_index("uq_send_attempt_unresolved")
        batch_op.drop_constraint(op.f("ck_send_attempt_channel"), type_="check")
        batch_op.drop_constraint(op.f("ck_send_attempt_status"), type_="check")
        batch_op.create_check_constraint("status", _ATTEMPT_STATUS_OLD)
        batch_op.drop_column("channel")
        batch_op.create_index(
            "uq_send_attempt_unresolved",
            ["message_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('in_flight','uncertain')"),
        )
    with op.batch_alter_table("message", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_message_state"), type_="check")
        batch_op.create_check_constraint("state", _MESSAGE_STATES_OLD)
    with op.batch_alter_table("mailbox_account", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_mailbox_account_kind"), type_="check")
        batch_op.drop_column("kind")
