"""SQLAlchemy models: the authoritative local record (spec §3, §11).

P0 creates the sending, identity, capacity, budget and audit tables.
Discovery-only tables (evidence, grant_evidence, opportunity, assessment,
task) arrive with their phases through new Alembic migrations.

Every row that relates to sending carries ``mode``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, validates

from marcelorian.db.types import UTCDateTime
from marcelorian.domain.recipients import normalize_address

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

MODES = "('sandbox','transport_test','live')"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {datetime: UTCDateTime()}  # noqa: RUF012 (SQLAlchemy API)


# --------------------------------------------------------------------------- #
# Applicant and profile
# --------------------------------------------------------------------------- #


class ApplicantProfile(Base):
    __tablename__ = "applicant_profile"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ProfileFact(Base):
    __tablename__ = "profile_fact"
    __table_args__ = (
        UniqueConstraint("applicant_id", "key"),
        CheckConstraint("status IN ('proposed','confirmed')", name="status"),
        CheckConstraint(
            "provenance IN ('applicant_entered','ai_extracted','imported')", name="provenance"
        ),
        CheckConstraint("version >= 1", name="version"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicant_profile.id"))
    key: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16))
    provenance: Mapped[str] = mapped_column(String(24))
    version: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime]


class Blob(Base):
    """Content-addressed immutable bytes (CV versions, frozen attachments)."""

    __tablename__ = "blob"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    size: Mapped[int]
    created_at: Mapped[datetime]


class CvVersion(Base):
    __tablename__ = "cv_version"
    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicant_profile.id"))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(ForeignKey("blob.sha256"))
    size_bytes: Mapped[int]
    uploaded_at: Mapped[datetime]


# --------------------------------------------------------------------------- #
# Institutions, labs, PIs
# --------------------------------------------------------------------------- #


class InstitutionGroup(Base):
    __tablename__ = "institution_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime]


class Institution(Base):
    __tablename__ = "institution"
    id: Mapped[int] = mapped_column(primary_key=True)
    ror_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(64))
    iana_tz: Mapped[str | None] = mapped_column(String(64))
    tz_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    tz_verified_by: Mapped[str | None] = mapped_column(String(64))
    tz_verified_at: Mapped[datetime | None]
    group_id: Mapped[int | None] = mapped_column(ForeignKey("institution_group.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]


class LabGroup(Base):
    __tablename__ = "lab_group"
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"))
    name: Mapped[str] = mapped_column(String(255))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime]


class Pi(Base):
    __tablename__ = "pi"
    id: Mapped[int] = mapped_column(primary_key=True)
    openalex_id: Mapped[str | None] = mapped_column(String(64))
    orcid: Mapped[str | None] = mapped_column(String(32))
    full_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str] = mapped_column(String(120))
    salutation: Mapped[str | None] = mapped_column(String(120))
    salutation_reviewed: Mapped[bool] = mapped_column(Boolean, default=False)
    salutation_reviewed_at: Mapped[datetime | None]
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"))
    lab_group_id: Mapped[int] = mapped_column(ForeignKey("lab_group.id"))
    current_role_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    current_role_evidence: Mapped[str | None] = mapped_column(Text)
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("pi.id"))
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]


class PiEmail(Base):
    __tablename__ = "pi_email"
    __table_args__ = (
        UniqueConstraint("pi_id", "address"),
        CheckConstraint("confidence IN ('A','B','C')", name="confidence"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    pi_id: Mapped[int] = mapped_column(ForeignKey("pi.id"))
    address: Mapped[str] = mapped_column(String(320))
    confidence: Mapped[str] = mapped_column(String(1))
    source: Mapped[str | None] = mapped_column(Text)
    ownership_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[str | None] = mapped_column(String(64))
    verified_at: Mapped[datetime | None]
    verification_evidence: Mapped[str | None] = mapped_column(Text)
    invalid: Mapped[bool] = mapped_column(Boolean, default=False)
    invalid_reason: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]

    @validates("address")
    def _canonical_address(self, _key: str, value: str) -> str:
        # Every identity comparison assumes the canonical addr-spec (spec §3, §12).
        return normalize_address(value)


class LabSuggestion(Base):
    """Unverified shared-lab suggestion (spec §6). Populated by discovery in P1."""

    __tablename__ = "lab_suggestion"
    __table_args__ = (
        CheckConstraint("status IN ('unverified','merged','dismissed')", name="status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    institution_id: Mapped[int] = mapped_column(ForeignKey("institution.id"))
    pi_a_id: Mapped[int] = mapped_column(ForeignKey("pi.id"))
    pi_b_id: Mapped[int] = mapped_column(ForeignKey("pi.id"))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    reviewed_at: Mapped[datetime | None]
    reviewed_by: Mapped[str | None] = mapped_column(String(64))


class DoNotContact(Base):
    """``scope_mode='live'`` entries apply in every mode; simulated entries only in theirs (C7)."""

    __tablename__ = "do_not_contact"
    __table_args__ = (CheckConstraint(f"scope_mode IN {MODES}", name="scope_mode"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    scope_mode: Mapped[str] = mapped_column(String(16))
    pi_id: Mapped[int | None] = mapped_column(ForeignKey("pi.id"))
    email: Mapped[str | None] = mapped_column(String(320))
    orcid: Mapped[str | None] = mapped_column(String(32))
    openalex_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    created_by: Mapped[str] = mapped_column(String(32))


class RecipientAllowlist(Base):
    """Consenting transport_test addresses (spec §12; B11)."""

    __tablename__ = "recipient_allowlist"
    id: Mapped[int] = mapped_column(primary_key=True)
    address: Mapped[str] = mapped_column(String(320), unique=True)
    label: Mapped[str] = mapped_column(String(120))
    consent_note: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    added_at: Mapped[datetime]
    added_by: Mapped[str] = mapped_column(String(32))


class MailboxAccount(Base):
    """The connected primary Gmail identity (no send-as aliases)."""

    __tablename__ = "mailbox_account"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320))
    scopes_json: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    verified_at: Mapped[datetime]


# --------------------------------------------------------------------------- #
# Campaigns, messages, approvals, attempts
# --------------------------------------------------------------------------- #


class Campaign(Base):
    __tablename__ = "campaign"
    __table_args__ = (
        UniqueConstraint("applicant_id", "pi_id", "mode"),
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        CheckConstraint(
            "state IN ('candidate','outreach_pending','paused_review','conversation',"
            "'sequence_complete','closed')",
            name="state",
        ),
        CheckConstraint(
            "(mode = 'transport_test') OR (test_recipient_id IS NULL)", name="test_recipient_mode"
        ),
        # One active transport_test campaign per actual allowlisted recipient (B11).
        Index(
            "uq_campaign_active_test_recipient",
            "test_recipient_id",
            unique=True,
            sqlite_where=text(
                "mode = 'transport_test' AND state IN ('outreach_pending','paused_review')"
            ),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    applicant_id: Mapped[int] = mapped_column(ForeignKey("applicant_profile.id"))
    pi_id: Mapped[int] = mapped_column(ForeignKey("pi.id"))
    mode: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24))
    close_outcome: Mapped[str | None] = mapped_column(String(32))
    close_reason: Mapped[str | None] = mapped_column(Text)
    sequence_stopped: Mapped[bool] = mapped_column(Boolean, default=False)
    test_recipient_id: Mapped[int | None] = mapped_column(ForeignKey("recipient_allowlist.id"))
    next_step: Mapped[int | None]
    next_due_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class CampaignPause(Base):
    """One row per pause reason; resolving one never clears another (B13)."""

    __tablename__ = "campaign_pause"
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"))
    reason: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(120))
    detail_json: Mapped[str | None] = mapped_column(Text)
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    resolution: Mapped[str | None] = mapped_column(String(64))
    resolved_by: Mapped[str | None] = mapped_column(String(32))


class Message(Base):
    __tablename__ = "message"
    __table_args__ = (
        UniqueConstraint("campaign_id", "step", "mode"),
        CheckConstraint("step BETWEEN 1 AND 4", name="step"),
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        CheckConstraint(
            "state IN ('draft','awaiting_approval','approved_queued','sending','sent',"
            "'failed_definite','uncertain','cancelled','needs_reapproval')",
            name="state",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"))
    mode: Mapped[str] = mapped_column(String(16))
    step: Mapped[int]
    state: Mapped[str] = mapped_column(String(24))
    draft_subject: Mapped[str | None] = mapped_column(Text)
    draft_body: Mapped[str | None] = mapped_column(Text)
    draft_attachments_json: Mapped[str] = mapped_column(Text, default="[]")
    draft_dependencies_json: Mapped[str] = mapped_column(Text, default="[]")
    draft_test_override_json: Mapped[str | None] = mapped_column(Text)
    current_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("approval_snapshot.id", use_alter=True)
    )
    due_at: Mapped[datetime | None]
    eligible_window_start: Mapped[datetime | None]
    eligible_window_end: Mapped[datetime | None]
    pi_timezone: Mapped[str | None] = mapped_column(String(64))
    sent_at: Mapped[datetime | None]
    sent_at_provenance: Mapped[str | None] = mapped_column(String(32))
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    rfc_message_id: Mapped[str | None] = mapped_column(String(255))
    cap_block_reason: Mapped[str | None] = mapped_column(String(64))
    last_deferral_reason: Mapped[str | None] = mapped_column(String(64))
    cancel_requested_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class ApprovalSnapshot(Base):
    __tablename__ = "approval_snapshot"
    __table_args__ = (
        UniqueConstraint("message_id", "version"),
        CheckConstraint(
            "state IN ('active','consumed','revoked','invalidated','superseded')", name="state"
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id"))
    version: Mapped[int]
    state: Mapped[str] = mapped_column(String(16))
    canonical_json: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    sender: Mapped[str] = mapped_column(String(320))
    to_address: Mapped[str] = mapped_column(String(320))
    intended_recipient: Mapped[str] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    has_test_override: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime]
    approved_by: Mapped[str] = mapped_column(String(32))
    ended_at: Mapped[datetime | None]
    end_reason: Mapped[str | None] = mapped_column(String(64))


class SnapshotAttachment(Base):
    __tablename__ = "snapshot_attachment"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("approval_snapshot.id"))
    position: Mapped[int]
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(ForeignKey("blob.sha256"))
    size: Mapped[int]
    cv_version_id: Mapped[int | None] = mapped_column(ForeignKey("cv_version.id"))


class ApprovalDependency(Base):
    """What an approval actually uses, with the version seen at approval (B18)."""

    __tablename__ = "approval_dependency"
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("approval_snapshot.id"))
    kind: Mapped[str] = mapped_column(String(24))
    ref_id: Mapped[int]
    ref_version: Mapped[int]


class SendAttempt(Base):
    __tablename__ = "send_attempt"
    __table_args__ = (
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        CheckConstraint(
            "status IN ('in_flight','sent','failed_definite','uncertain','not_submitted')",
            name="status",
        ),
        # At most one unresolved attempt per message (spec §9.6 step 2).
        Index(
            "uq_send_attempt_unresolved",
            "message_id",
            unique=True,
            sqlite_where=text("status IN ('in_flight','uncertain')"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_uid: Mapped[str] = mapped_column(String(32), unique=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id"))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("approval_snapshot.id"))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    sender: Mapped[str] = mapped_column(String(320))
    recipient: Mapped[str] = mapped_column(String(320))
    intended_recipient: Mapped[str] = mapped_column(String(320))
    app_message_id: Mapped[str] = mapped_column(String(255), unique=True)
    x_marcelorian_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    claimed_at: Mapped[datetime]
    submit_started_at: Mapped[datetime | None]
    submit_completed_at: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    sent_at_provenance: Mapped[str | None] = mapped_column(String(32))
    gmail_message_id: Mapped[str | None] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    reconcile_count: Mapped[int] = mapped_column(Integer, default=0)
    last_reconciled_at: Mapped[datetime | None]
    resolution: Mapped[str | None] = mapped_column(String(32))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    resolved_by: Mapped[str | None] = mapped_column(String(32))
    resolved_at: Mapped[datetime | None]


class BudgetLedger(Base):
    """Rolling 24 h submission ledger; one row per attempt, counted once (B17)."""

    __tablename__ = "budget_ledger"
    __table_args__ = (
        CheckConstraint("ledger IN ('real','sandbox')", name="ledger"),
        CheckConstraint("status IN ('held','used','released')", name="status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    ledger: Mapped[str] = mapped_column(String(16))
    attempt_id: Mapped[int] = mapped_column(ForeignKey("send_attempt.id"), unique=True)
    status: Mapped[str] = mapped_column(String(16))
    held_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    released_at: Mapped[datetime | None]
    release_reason: Mapped[str | None] = mapped_column(String(64))


class Reservation(Base):
    """Pending-slot and projected new-contact reservations (spec §11; B4, B6)."""

    __tablename__ = "reservation"
    __table_args__ = (
        CheckConstraint(f"mode IN {MODES}", name="mode"),
        CheckConstraint("kind IN ('pending_slot','new_contact')", name="kind"),
        CheckConstraint("status IN ('active','consumed','released')", name="status"),
        Index(
            "uq_reservation_active_kind",
            "campaign_id",
            "kind",
            unique=True,
            sqlite_where=text("status = 'active'"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"))
    mode: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
    released_at: Mapped[datetime | None]
    release_reason: Mapped[str | None] = mapped_column(String(64))
    #: Optional stricter setting: pending slot held until this instant after Email 4.
    hold_until: Mapped[datetime | None]


class PacingState(Base):
    """Persisted spacing between actual submissions (B8)."""

    __tablename__ = "pacing_state"
    ledger: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_submission_at: Mapped[datetime | None]
    next_allowed_at: Mapped[datetime | None]


class DispatchEvaluation(Base):
    """Recorded dispatch/safety evaluations; evidence for the missed-window rule (B7)."""

    __tablename__ = "dispatch_evaluation"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("message.id"))
    mode: Mapped[str] = mapped_column(String(16))
    evaluated_at: Mapped[datetime]
    outcome: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(64))
    functioning: Mapped[bool] = mapped_column(Boolean)


class InboundEvent(Base):
    __tablename__ = "inbound_event"
    __table_args__ = (UniqueConstraint("gmail_message_id", "campaign_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(16))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaign.id"))
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    gmail_thread_id: Mapped[str | None] = mapped_column(String(64))
    from_address: Mapped[str | None] = mapped_column(String(320))
    classification: Mapped[str] = mapped_column(String(32))
    received_at: Mapped[datetime | None]
    recorded_at: Mapped[datetime]
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolution: Mapped[str | None] = mapped_column(String(64))


class PriorContactAck(Base):
    """Acknowledged prior-contact hits: mailbox + campaign + exact Gmail ID (B14)."""

    __tablename__ = "prior_contact_ack"
    __table_args__ = (UniqueConstraint("mailbox_email", "campaign_id", "gmail_message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    mailbox_email: Mapped[str] = mapped_column(String(320))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaign.id"))
    gmail_message_id: Mapped[str] = mapped_column(String(64))
    acknowledged_at: Mapped[datetime]
    acknowledged_by: Mapped[str] = mapped_column(String(32))


# --------------------------------------------------------------------------- #
# Runtime, sync, settings, consent, audit
# --------------------------------------------------------------------------- #


class RuntimeState(Base):
    __tablename__ = "runtime_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(f"active_mode IN {MODES}", name="active_mode"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    active_mode: Mapped[str] = mapped_column(String(16))
    dispatch_paused: Mapped[bool] = mapped_column(Boolean)
    pause_reason: Mapped[str | None] = mapped_column(String(32))
    live_readiness_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    auth_block_reason: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime]


class RuntimeStateLog(Base):
    """Every change of active mode / pause, for deliberate-interval evidence (B7, B9)."""

    __tablename__ = "runtime_state_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    active_mode: Mapped[str] = mapped_column(String(16))
    dispatch_paused: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(String(64))


class SyncState(Base):
    """Mailbox-level coverage ('real' mailbox shared by live+transport_test; 'sandbox')."""

    __tablename__ = "sync_state"
    mailbox_key: Mapped[str] = mapped_column(String(16), primary_key=True)
    history_cursor: Mapped[str | None] = mapped_column(String(64))
    last_gmail_coverage_at: Mapped[datetime | None]
    gap_open: Mapped[bool] = mapped_column(Boolean, default=False)
    gap_since: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime]


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeat"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    component: Mapped[str] = mapped_column(String(32))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime]


class Consent(Base):
    __tablename__ = "consent"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    granted: Mapped[bool] = mapped_column(Boolean)
    detail_json: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime]


class AuditEvent(Base):
    __tablename__ = "audit_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime]
    actor: Mapped[str] = mapped_column(String(32))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(64))
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str | None] = mapped_column(String(16))
