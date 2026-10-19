"""Enumerations shared by the data model and the safety logic."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    SANDBOX = "sandbox"
    TRANSPORT_TEST = "transport_test"
    LIVE = "live"

    @property
    def is_real(self) -> bool:
        """True for modes that submit through the real Gmail mailbox."""
        return self in (Mode.LIVE, Mode.TRANSPORT_TEST)


class Channel(StrEnum):
    """How a message reaches the PI (spec §9.8; owner decision S2)."""

    SANDBOX = "sandbox"  # fake transport, sandbox mode only
    GMAIL = "gmail"  # the app submits through the Gmail API
    #: The app prepares the email; the applicant sends it from Outlook and records the
    #: outcome. The app has no access to that mailbox.
    OUTLOOK_HANDOFF = "outlook_handoff"


class Ledger(StrEnum):
    """Submission/pacing ledgers. Live and transport_test share REAL (B8, B17, C9)."""

    REAL = "real"
    SANDBOX = "sandbox"


def ledger_for(mode: Mode) -> Ledger:
    return Ledger.REAL if mode.is_real else Ledger.SANDBOX


class CampaignState(StrEnum):
    CANDIDATE = "candidate"
    OUTREACH_PENDING = "outreach_pending"
    PAUSED_REVIEW = "paused_review"
    CONVERSATION = "conversation"
    SEQUENCE_COMPLETE = "sequence_complete"
    CLOSED = "closed"


#: States in which a campaign holds a pending slot (spec §3, §11).
HOLDING_STATES: frozenset[CampaignState] = frozenset(
    {CampaignState.OUTREACH_PENDING, CampaignState.PAUSED_REVIEW}
)

#: States in which the outreach sequence has stopped for good.
STOPPED_STATES: frozenset[CampaignState] = frozenset(
    {CampaignState.CONVERSATION, CampaignState.SEQUENCE_COMPLETE, CampaignState.CLOSED}
)


class PauseReason(StrEnum):
    AUTO_REPLY = "auto_reply"
    DELIVERY_NOTICE = "delivery_notice"
    MANUAL_APPLICANT_MESSAGE = "manual_applicant_message"
    UNKNOWN_PARTICIPANT = "unknown_participant"
    AMBIGUOUS_THREAD = "ambiguous_thread"
    SEND_UNCERTAIN = "send_uncertain"
    SYNC_GAP = "sync_gap"
    PRIOR_CONTACT_FOUND = "prior_contact_found"
    MISSED_WINDOW = "missed_window"


class CloseOutcome(StrEnum):
    BOUNCE_CONFIRMED = "bounce_confirmed"
    NO_OPENING = "no_opening"
    DO_NOT_CONTACT = "do_not_contact"
    APPLICANT_CLOSED = "applicant_closed"


class MessageState(StrEnum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED_QUEUED = "approved_queued"
    SENDING = "sending"
    SENT = "sent"
    FAILED_DEFINITE = "failed_definite"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"
    NEEDS_REAPPROVAL = "needs_reapproval"
    #: Given to the applicant to send from Outlook; waiting for Sent / Not sent (§9.8).
    HANDED_OFF = "handed_off"


#: Possibly being submitted: cannot be edited, revoked or cancelled outright; a cancel
#: is recorded as a request that takes effect once non-submission is definite (B5).
IN_SUBMISSION_STATES: frozenset[MessageState] = frozenset(
    {MessageState.SENDING, MessageState.HANDED_OFF}
)

#: Message states that a genuine reply / closure / stop cancels outright.
UNSENT_CANCELLABLE_STATES: frozenset[MessageState] = frozenset(
    {
        MessageState.DRAFT,
        MessageState.AWAITING_APPROVAL,
        MessageState.APPROVED_QUEUED,
        MessageState.NEEDS_REAPPROVAL,
        MessageState.FAILED_DEFINITE,
    }
)


class SnapshotState(StrEnum):
    ACTIVE = "active"  # the current approval; claimable
    CONSUMED = "consumed"  # used by a claim; can never be claimed again
    REVOKED = "revoked"
    INVALIDATED = "invalidated"
    SUPERSEDED = "superseded"


class AttemptStatus(StrEnum):
    IN_FLIGHT = "in_flight"
    SENT = "sent"
    FAILED_DEFINITE = "failed_definite"
    UNCERTAIN = "uncertain"
    #: Definitely never submitted: an uncertain attempt resolved by recorded evidence, or a
    #: claimed attempt the gateway never handed to the transport (resolution "not_invoked").
    NOT_SUBMITTED = "not_submitted"
    #: Outlook hand-off given to the applicant; neither Sent nor Not sent recorded yet.
    HANDED_OFF = "handed_off"


#: Gmail attempts awaiting reconciliation or resolution. Never includes ``handed_off``:
#: the app cannot reconcile a hand-off, only the applicant can resolve it.
UNRESOLVED_ATTEMPT_STATES: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.IN_FLIGHT, AttemptStatus.UNCERTAIN}
)

#: Attempts that may have reached the recipient and are not settled: they hold budget,
#: block a second attempt to the same person and keep capacity (B14; §9.8).
POSSIBLY_SUBMITTED_STATES: frozenset[AttemptStatus] = UNRESOLVED_ATTEMPT_STATES | {
    AttemptStatus.HANDED_OFF
}


class BudgetStatus(StrEnum):
    HELD = "held"
    USED = "used"
    RELEASED = "released"


class ReservationKind(StrEnum):
    PENDING_SLOT = "pending_slot"
    NEW_CONTACT = "new_contact"


class ReservationStatus(StrEnum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    RELEASED = "released"


class FactStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"


class FactProvenance(StrEnum):
    APPLICANT_ENTERED = "applicant_entered"
    AI_EXTRACTED = "ai_extracted"
    IMPORTED = "imported"


class EmailConfidence(StrEnum):
    A = "A"
    B = "B"
    C = "C"


class Actor(StrEnum):
    APPLICANT = "applicant"
    SYSTEM = "system"
    GATEWAY = "gateway"
    RECONCILER = "reconciler"
    DISPATCHER = "dispatcher"


class SentAtProvenance(StrEnum):
    GATEWAY_RESPONSE = "gateway_response"
    GMAIL_RECONCILIATION = "gmail_reconciliation"
    APPLICANT_REPORTED = "applicant_reported"  # incl. an Outlook hand-off's Sent click


class DeferralReason(StrEnum):
    NOT_ELIGIBLE = "not_eligible"
    NOT_DUE = "not_due"
    OUTSIDE_WINDOW = "outside_window"
    WINDOW_PASSED = "window_passed"  # assigned window over; missed-window rule decides
    PACING = "pacing"
    BUDGET = "budget"
    OVER_CAPACITY = "over_capacity"
    PAUSED = "paused"
    MODE_INACTIVE = "mode_inactive"
    SEQUENCE_STOPPED = "sequence_stopped"
    CAMPAIGN_NOT_SENDABLE = "campaign_not_sendable"
    SYNC_UNAVAILABLE = "sync_unavailable"
    SYNC_GAP = "sync_gap"
    AUTH_REQUIRED = "auth_required"
    PRIOR_CONTACT_CHECK_INCOMPLETE = "prior_contact_check_incomplete"
    #: A deliberate safety hold by a functioning worker: the app's own earlier attempt
    #: to one of this person's addresses is still unresolved (B14). Not an outage.
    OWN_ATTEMPT_UNRESOLVED = "own_attempt_unresolved"
    REPLY_CHECK_INCOMPLETE = "reply_check_incomplete"
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


#: Deferrals that mean the safety evaluation itself could not run properly
#: (worker connectivity/authorization/sync unavailable). Such evaluations are
#: not "functioning" for the missed-window rule (B7).
NON_FUNCTIONING_DEFERRALS: frozenset[DeferralReason] = frozenset(
    {
        DeferralReason.SYNC_UNAVAILABLE,
        DeferralReason.SYNC_GAP,
        DeferralReason.AUTH_REQUIRED,
        DeferralReason.PRIOR_CONTACT_CHECK_INCOMPLETE,
        DeferralReason.REPLY_CHECK_INCOMPLETE,
        DeferralReason.TRANSPORT_UNAVAILABLE,
    }
)


class InboundClassification(StrEnum):
    GENUINE_REPLY = "genuine_reply"
    AUTO_REPLY = "auto_reply"
    DELIVERY_NOTICE = "delivery_notice"
    MANUAL_APPLICANT_MESSAGE = "manual_applicant_message"
    UNKNOWN_PARTICIPANT = "unknown_participant"
    AMBIGUOUS_THREAD = "ambiguous_thread"
    OWN_MESSAGE = "own_message"


class DependencyKind(StrEnum):
    """What an approval snapshot depends on (decision B18)."""

    PROFILE_FACT = "profile_fact"
    PI_EMAIL = "pi_email"
    PI = "pi"  # salutation / identity fields
    INSTITUTION = "institution"  # time zone
    CV_VERSION = "cv_version"
    MAILBOX_ACCOUNT = "mailbox_account"
    ALLOWLIST_ENTRY = "allowlist_entry"


class RuntimePauseReason(StrEnum):
    APPLICANT = "applicant_pause"
    MODE_SWITCH = "mode_switch"
    RESTORED_BACKUP = "restored_backup"
