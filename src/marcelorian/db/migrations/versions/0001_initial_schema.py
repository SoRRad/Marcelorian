"""initial schema: sending, identity, capacity, audit

Revision ID: 0001
Revises:
Create Date: 2026-10-16 02:17:26.014160
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "applicant_profile",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applicant_profile")),
    )
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.String(length=26), nullable=False),
        sa.Column("actor", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=32), nullable=True),
        sa.Column("to_state", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_table(
        "blob",
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("sha256", name=op.f("pk_blob")),
    )
    op.create_table(
        "consent",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("granted", sa.Boolean(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consent")),
    )
    op.create_table(
        "institution_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institution_group")),
    )
    op.create_table(
        "mailbox_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("verified_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mailbox_account")),
    )
    op.create_table(
        "pacing_state",
        sa.Column("ledger", sa.String(length=16), nullable=False),
        sa.Column("last_submission_at", sa.String(length=26), nullable=True),
        sa.Column("next_allowed_at", sa.String(length=26), nullable=True),
        sa.PrimaryKeyConstraint("ledger", name=op.f("pk_pacing_state")),
    )
    op.create_table(
        "recipient_allowlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("consent_note", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.String(length=26), nullable=False),
        sa.Column("added_by", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recipient_allowlist")),
        sa.UniqueConstraint("address", name=op.f("uq_recipient_allowlist_address")),
    )
    op.create_table(
        "runtime_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("active_mode", sa.String(length=16), nullable=False),
        sa.Column("dispatch_paused", sa.Boolean(), nullable=False),
        sa.Column("pause_reason", sa.String(length=32), nullable=True),
        sa.Column("live_readiness_complete", sa.Boolean(), nullable=False),
        sa.Column("auth_blocked", sa.Boolean(), nullable=False),
        sa.Column("auth_block_reason", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "active_mode IN ('sandbox','transport_test','live')",
            name=op.f("ck_runtime_state_active_mode"),
        ),
        sa.CheckConstraint("id = 1", name=op.f("ck_runtime_state_singleton")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_state")),
    )
    op.create_table(
        "runtime_state_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.String(length=26), nullable=False),
        sa.Column("active_mode", sa.String(length=16), nullable=False),
        sa.Column("dispatch_paused", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runtime_state_log")),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_settings")),
    )
    op.create_table(
        "sync_state",
        sa.Column("mailbox_key", sa.String(length=16), nullable=False),
        sa.Column("history_cursor", sa.String(length=64), nullable=True),
        sa.Column("last_gmail_coverage_at", sa.String(length=26), nullable=True),
        sa.Column("gap_open", sa.Boolean(), nullable=False),
        sa.Column("gap_since", sa.String(length=26), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.PrimaryKeyConstraint("mailbox_key", name=op.f("pk_sync_state")),
    )
    op.create_table(
        "worker_heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("at", sa.String(length=26), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_worker_heartbeat")),
    )
    op.create_table(
        "cv_version",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("uploaded_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicant_profile.id"],
            name=op.f("fk_cv_version_applicant_id_applicant_profile"),
        ),
        sa.ForeignKeyConstraint(
            ["sha256"], ["blob.sha256"], name=op.f("fk_cv_version_sha256_blob")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cv_version")),
    )
    op.create_table(
        "institution",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ror_id", sa.String(length=64), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("state", sa.String(length=64), nullable=True),
        sa.Column("iana_tz", sa.String(length=64), nullable=True),
        sa.Column("tz_verified", sa.Boolean(), nullable=False),
        sa.Column("tz_verified_by", sa.String(length=64), nullable=True),
        sa.Column("tz_verified_at", sa.String(length=26), nullable=True),
        sa.Column("group_id", sa.Integer(), nullable=True),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["institution_group.id"],
            name=op.f("fk_institution_group_id_institution_group"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_institution")),
        sa.UniqueConstraint("ror_id", name=op.f("uq_institution_ror_id")),
    )
    op.create_table(
        "profile_fact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=120), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provenance", sa.String(length=24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.String(length=26), nullable=True),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "provenance IN ('applicant_entered','ai_extracted','imported')",
            name=op.f("ck_profile_fact_provenance"),
        ),
        sa.CheckConstraint(
            "status IN ('proposed','confirmed')", name=op.f("ck_profile_fact_status")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_profile_fact_version")),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicant_profile.id"],
            name=op.f("fk_profile_fact_applicant_id_applicant_profile"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_fact")),
        sa.UniqueConstraint("applicant_id", "key", name=op.f("uq_profile_fact_applicant_id")),
    )
    op.create_table(
        "lab_group",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institution.id"],
            name=op.f("fk_lab_group_institution_id_institution"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_group")),
    )
    op.create_table(
        "pi",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("openalex_id", sa.String(length=64), nullable=True),
        sa.Column("orcid", sa.String(length=32), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("last_name", sa.String(length=120), nullable=False),
        sa.Column("salutation", sa.String(length=120), nullable=True),
        sa.Column("salutation_reviewed", sa.Boolean(), nullable=False),
        sa.Column("salutation_reviewed_at", sa.String(length=26), nullable=True),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("lab_group_id", sa.Integer(), nullable=False),
        sa.Column("current_role_verified", sa.Boolean(), nullable=False),
        sa.Column("current_role_evidence", sa.Text(), nullable=True),
        sa.Column("merged_into_id", sa.Integer(), nullable=True),
        sa.Column("is_synthetic", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["institution_id"], ["institution.id"], name=op.f("fk_pi_institution_id_institution")
        ),
        sa.ForeignKeyConstraint(
            ["lab_group_id"], ["lab_group.id"], name=op.f("fk_pi_lab_group_id_lab_group")
        ),
        sa.ForeignKeyConstraint(
            ["merged_into_id"], ["pi.id"], name=op.f("fk_pi_merged_into_id_pi")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pi")),
    )
    op.create_table(
        "campaign",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("close_outcome", sa.String(length=32), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
        sa.Column("sequence_stopped", sa.Boolean(), nullable=False),
        sa.Column("test_recipient_id", sa.Integer(), nullable=True),
        sa.Column("next_step", sa.Integer(), nullable=True),
        sa.Column("next_due_at", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "(mode = 'transport_test') OR (test_recipient_id IS NULL)",
            name=op.f("ck_campaign_test_recipient_mode"),
        ),
        sa.CheckConstraint(
            "mode IN ('sandbox','transport_test','live')", name=op.f("ck_campaign_mode")
        ),
        sa.CheckConstraint(
            "state IN ('candidate','outreach_pending','paused_review','conversation','sequence_complete','closed')",
            name=op.f("ck_campaign_state"),
        ),
        sa.ForeignKeyConstraint(
            ["applicant_id"],
            ["applicant_profile.id"],
            name=op.f("fk_campaign_applicant_id_applicant_profile"),
        ),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_campaign_pi_id_pi")),
        sa.ForeignKeyConstraint(
            ["test_recipient_id"],
            ["recipient_allowlist.id"],
            name=op.f("fk_campaign_test_recipient_id_recipient_allowlist"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign")),
        sa.UniqueConstraint("applicant_id", "pi_id", "mode", name=op.f("uq_campaign_applicant_id")),
    )
    with op.batch_alter_table("campaign", schema=None) as batch_op:
        batch_op.create_index(
            "uq_campaign_active_test_recipient",
            ["test_recipient_id"],
            unique=True,
            sqlite_where=sa.text(
                "mode = 'transport_test' AND state IN ('outreach_pending','paused_review')"
            ),
        )

    op.create_table(
        "do_not_contact",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("scope_mode", sa.String(length=16), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=True),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("orcid", sa.String(length=32), nullable=True),
        sa.Column("openalex_id", sa.String(length=64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "scope_mode IN ('sandbox','transport_test','live')",
            name=op.f("ck_do_not_contact_scope_mode"),
        ),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_do_not_contact_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_do_not_contact")),
    )
    op.create_table(
        "lab_suggestion",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("institution_id", sa.Integer(), nullable=False),
        sa.Column("pi_a_id", sa.Integer(), nullable=False),
        sa.Column("pi_b_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("reviewed_at", sa.String(length=26), nullable=True),
        sa.Column("reviewed_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('unverified','merged','dismissed')", name=op.f("ck_lab_suggestion_status")
        ),
        sa.ForeignKeyConstraint(
            ["institution_id"],
            ["institution.id"],
            name=op.f("fk_lab_suggestion_institution_id_institution"),
        ),
        sa.ForeignKeyConstraint(["pi_a_id"], ["pi.id"], name=op.f("fk_lab_suggestion_pi_a_id_pi")),
        sa.ForeignKeyConstraint(["pi_b_id"], ["pi.id"], name=op.f("fk_lab_suggestion_pi_b_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_lab_suggestion")),
    )
    op.create_table(
        "pi_email",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("pi_id", sa.Integer(), nullable=False),
        sa.Column("address", sa.String(length=320), nullable=False),
        sa.Column("confidence", sa.String(length=1), nullable=False),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("ownership_verified", sa.Boolean(), nullable=False),
        sa.Column("verified_by", sa.String(length=64), nullable=True),
        sa.Column("verified_at", sa.String(length=26), nullable=True),
        sa.Column("verification_evidence", sa.Text(), nullable=True),
        sa.Column("invalid", sa.Boolean(), nullable=False),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint("confidence IN ('A','B','C')", name=op.f("ck_pi_email_confidence")),
        sa.ForeignKeyConstraint(["pi_id"], ["pi.id"], name=op.f("fk_pi_email_pi_id_pi")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pi_email")),
        sa.UniqueConstraint("pi_id", "address", name=op.f("uq_pi_email_pi_id")),
    )
    op.create_table(
        "campaign_pause",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("source_ref", sa.String(length=120), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.String(length=26), nullable=False),
        sa.Column("resolved_at", sa.String(length=26), nullable=True),
        sa.Column("resolution", sa.String(length=64), nullable=True),
        sa.Column("resolved_by", sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_campaign_pause_campaign_id_campaign")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_pause")),
    )
    op.create_table(
        "inbound_event",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=False),
        sa.Column("gmail_thread_id", sa.String(length=64), nullable=True),
        sa.Column("from_address", sa.String(length=320), nullable=True),
        sa.Column("classification", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.String(length=26), nullable=True),
        sa.Column("recorded_at", sa.String(length=26), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False),
        sa.Column("resolution", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_inbound_event_campaign_id_campaign")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_inbound_event")),
        sa.UniqueConstraint(
            "gmail_message_id", "campaign_id", name=op.f("uq_inbound_event_gmail_message_id")
        ),
    )
    op.create_table(
        "message",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("step", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=24), nullable=False),
        sa.Column("draft_subject", sa.Text(), nullable=True),
        sa.Column("draft_body", sa.Text(), nullable=True),
        sa.Column("draft_attachments_json", sa.Text(), nullable=False),
        sa.Column("draft_dependencies_json", sa.Text(), nullable=False),
        sa.Column("draft_test_override_json", sa.Text(), nullable=True),
        sa.Column("current_snapshot_id", sa.Integer(), nullable=True),
        sa.Column("due_at", sa.String(length=26), nullable=True),
        sa.Column("eligible_window_start", sa.String(length=26), nullable=True),
        sa.Column("eligible_window_end", sa.String(length=26), nullable=True),
        sa.Column("pi_timezone", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.String(length=26), nullable=True),
        sa.Column("sent_at_provenance", sa.String(length=32), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=64), nullable=True),
        sa.Column("rfc_message_id", sa.String(length=255), nullable=True),
        sa.Column("cap_block_reason", sa.String(length=64), nullable=True),
        sa.Column("last_deferral_reason", sa.String(length=64), nullable=True),
        sa.Column("cancel_requested_at", sa.String(length=26), nullable=True),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("updated_at", sa.String(length=26), nullable=False),
        sa.CheckConstraint(
            "mode IN ('sandbox','transport_test','live')", name=op.f("ck_message_mode")
        ),
        sa.CheckConstraint(
            "state IN ('draft','awaiting_approval','approved_queued','sending','sent','failed_definite','uncertain','cancelled','needs_reapproval')",
            name=op.f("ck_message_state"),
        ),
        sa.CheckConstraint("step BETWEEN 1 AND 4", name=op.f("ck_message_step")),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_message_campaign_id_campaign")
        ),
        sa.ForeignKeyConstraint(
            ["current_snapshot_id"],
            ["approval_snapshot.id"],
            name=op.f("fk_message_current_snapshot_id_approval_snapshot"),
            use_alter=True,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message")),
        sa.UniqueConstraint("campaign_id", "step", "mode", name=op.f("uq_message_campaign_id")),
    )
    op.create_table(
        "prior_contact_ack",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mailbox_email", sa.String(length=320), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=False),
        sa.Column("acknowledged_at", sa.String(length=26), nullable=False),
        sa.Column("acknowledged_by", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_prior_contact_ack_campaign_id_campaign")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prior_contact_ack")),
        sa.UniqueConstraint(
            "mailbox_email",
            "campaign_id",
            "gmail_message_id",
            name=op.f("uq_prior_contact_ack_mailbox_email"),
        ),
    )
    op.create_table(
        "reservation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.String(length=26), nullable=False),
        sa.Column("consumed_at", sa.String(length=26), nullable=True),
        sa.Column("released_at", sa.String(length=26), nullable=True),
        sa.Column("release_reason", sa.String(length=64), nullable=True),
        sa.Column("hold_until", sa.String(length=26), nullable=True),
        sa.CheckConstraint(
            "kind IN ('pending_slot','new_contact')", name=op.f("ck_reservation_kind")
        ),
        sa.CheckConstraint(
            "mode IN ('sandbox','transport_test','live')", name=op.f("ck_reservation_mode")
        ),
        sa.CheckConstraint(
            "status IN ('active','consumed','released')", name=op.f("ck_reservation_status")
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaign.id"], name=op.f("fk_reservation_campaign_id_campaign")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservation")),
    )
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.create_index(
            "uq_reservation_active_kind",
            ["campaign_id", "kind"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
        )

    op.create_table(
        "approval_snapshot",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("canonical_json", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("to_address", sa.String(length=320), nullable=False),
        sa.Column("intended_recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("has_test_override", sa.Boolean(), nullable=False),
        sa.Column("approved_at", sa.String(length=26), nullable=False),
        sa.Column("approved_by", sa.String(length=32), nullable=False),
        sa.Column("ended_at", sa.String(length=26), nullable=True),
        sa.Column("end_reason", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "state IN ('active','consumed','revoked','invalidated','superseded')",
            name=op.f("ck_approval_snapshot_state"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["message.id"], name=op.f("fk_approval_snapshot_message_id_message")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_snapshot")),
        sa.UniqueConstraint("message_id", "version", name=op.f("uq_approval_snapshot_message_id")),
    )
    op.create_table(
        "dispatch_evaluation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("evaluated_at", sa.String(length=26), nullable=False),
        sa.Column("outcome", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("functioning", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["message_id"], ["message.id"], name=op.f("fk_dispatch_evaluation_message_id_message")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_dispatch_evaluation")),
    )
    op.create_table(
        "approval_dependency",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("ref_id", sa.Integer(), nullable=False),
        sa.Column("ref_version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["approval_snapshot.id"],
            name=op.f("fk_approval_dependency_snapshot_id_approval_snapshot"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_approval_dependency")),
    )
    op.create_table(
        "send_attempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attempt_uid", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_sha256", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("sender", sa.String(length=320), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("intended_recipient", sa.String(length=320), nullable=False),
        sa.Column("app_message_id", sa.String(length=255), nullable=False),
        sa.Column("x_marcelorian_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("claimed_at", sa.String(length=26), nullable=False),
        sa.Column("submit_started_at", sa.String(length=26), nullable=True),
        sa.Column("submit_completed_at", sa.String(length=26), nullable=True),
        sa.Column("sent_at", sa.String(length=26), nullable=True),
        sa.Column("sent_at_provenance", sa.String(length=32), nullable=True),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=True),
        sa.Column("gmail_thread_id", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("reconcile_count", sa.Integer(), nullable=False),
        sa.Column("last_reconciled_at", sa.String(length=26), nullable=True),
        sa.Column("resolution", sa.String(length=32), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(length=32), nullable=True),
        sa.Column("resolved_at", sa.String(length=26), nullable=True),
        sa.CheckConstraint(
            "mode IN ('sandbox','transport_test','live')", name=op.f("ck_send_attempt_mode")
        ),
        sa.CheckConstraint(
            "status IN ('in_flight','sent','failed_definite','uncertain','not_submitted')",
            name=op.f("ck_send_attempt_status"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"], ["message.id"], name=op.f("fk_send_attempt_message_id_message")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["approval_snapshot.id"],
            name=op.f("fk_send_attempt_snapshot_id_approval_snapshot"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_send_attempt")),
        sa.UniqueConstraint("app_message_id", name=op.f("uq_send_attempt_app_message_id")),
        sa.UniqueConstraint("attempt_uid", name=op.f("uq_send_attempt_attempt_uid")),
        sa.UniqueConstraint("x_marcelorian_id", name=op.f("uq_send_attempt_x_marcelorian_id")),
    )
    with op.batch_alter_table("send_attempt", schema=None) as batch_op:
        batch_op.create_index(
            "uq_send_attempt_unresolved",
            ["message_id"],
            unique=True,
            sqlite_where=sa.text("status IN ('in_flight','uncertain')"),
        )

    op.create_table(
        "snapshot_attachment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("snapshot_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("cv_version_id", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["cv_version_id"],
            ["cv_version.id"],
            name=op.f("fk_snapshot_attachment_cv_version_id_cv_version"),
        ),
        sa.ForeignKeyConstraint(
            ["sha256"], ["blob.sha256"], name=op.f("fk_snapshot_attachment_sha256_blob")
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["approval_snapshot.id"],
            name=op.f("fk_snapshot_attachment_snapshot_id_approval_snapshot"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_snapshot_attachment")),
    )
    op.create_table(
        "budget_ledger",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ledger", sa.String(length=16), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("held_at", sa.String(length=26), nullable=False),
        sa.Column("used_at", sa.String(length=26), nullable=True),
        sa.Column("released_at", sa.String(length=26), nullable=True),
        sa.Column("release_reason", sa.String(length=64), nullable=True),
        sa.CheckConstraint("ledger IN ('real','sandbox')", name=op.f("ck_budget_ledger_ledger")),
        sa.CheckConstraint(
            "status IN ('held','used','released')", name=op.f("ck_budget_ledger_status")
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"],
            ["send_attempt.id"],
            name=op.f("fk_budget_ledger_attempt_id_send_attempt"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_budget_ledger")),
        sa.UniqueConstraint("attempt_id", name=op.f("uq_budget_ledger_attempt_id")),
    )


def downgrade() -> None:
    op.drop_table("budget_ledger")
    op.drop_table("snapshot_attachment")
    with op.batch_alter_table("send_attempt", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_send_attempt_unresolved",
            sqlite_where=sa.text("status IN ('in_flight','uncertain')"),
        )

    op.drop_table("send_attempt")
    op.drop_table("approval_dependency")
    op.drop_table("dispatch_evaluation")
    op.drop_table("approval_snapshot")
    with op.batch_alter_table("reservation", schema=None) as batch_op:
        batch_op.drop_index("uq_reservation_active_kind", sqlite_where=sa.text("status = 'active'"))

    op.drop_table("reservation")
    op.drop_table("prior_contact_ack")
    op.drop_table("message")
    op.drop_table("inbound_event")
    op.drop_table("campaign_pause")
    op.drop_table("pi_email")
    op.drop_table("lab_suggestion")
    op.drop_table("do_not_contact")
    with op.batch_alter_table("campaign", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_campaign_active_test_recipient",
            sqlite_where=sa.text(
                "mode = 'transport_test' AND state IN ('outreach_pending','paused_review')"
            ),
        )

    op.drop_table("campaign")
    op.drop_table("pi")
    op.drop_table("lab_group")
    op.drop_table("profile_fact")
    op.drop_table("institution")
    op.drop_table("cv_version")
    op.drop_table("worker_heartbeat")
    op.drop_table("sync_state")
    op.drop_table("settings")
    op.drop_table("runtime_state_log")
    op.drop_table("runtime_state")
    op.drop_table("recipient_allowlist")
    op.drop_table("pacing_state")
    op.drop_table("mailbox_account")
    op.drop_table("institution_group")
    op.drop_table("consent")
    op.drop_table("blob")
    op.drop_table("audit_event")
    op.drop_table("applicant_profile")
