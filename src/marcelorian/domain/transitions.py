"""Allowed state transitions (spec §3; decisions B3, B5, B13).

Every state change in the services goes through ``check_campaign`` /
``check_message`` so an illegal transition is impossible to write by mistake.
"""

from __future__ import annotations

from marcelorian.domain.enums import CampaignState as C
from marcelorian.domain.enums import MessageState as M


class InvalidTransition(RuntimeError):
    pass


CAMPAIGN_TRANSITIONS: dict[C, frozenset[C]] = {
    C.CANDIDATE: frozenset({C.OUTREACH_PENDING, C.CLOSED}),
    # outreach_pending -> candidate only via cancel-before-first-send (B3).
    C.OUTREACH_PENDING: frozenset(
        {C.CANDIDATE, C.PAUSED_REVIEW, C.CONVERSATION, C.SEQUENCE_COMPLETE, C.CLOSED}
    ),
    C.PAUSED_REVIEW: frozenset(
        {C.CANDIDATE, C.OUTREACH_PENDING, C.CONVERSATION, C.SEQUENCE_COMPLETE, C.CLOSED}
    ),
    C.SEQUENCE_COMPLETE: frozenset({C.CONVERSATION, C.CLOSED}),
    C.CONVERSATION: frozenset({C.CLOSED}),
    C.CLOSED: frozenset(),
}

MESSAGE_TRANSITIONS: dict[M, frozenset[M]] = {
    M.DRAFT: frozenset({M.AWAITING_APPROVAL, M.CANCELLED}),
    M.AWAITING_APPROVAL: frozenset({M.APPROVED_QUEUED, M.DRAFT, M.CANCELLED}),
    M.APPROVED_QUEUED: frozenset(
        {M.SENDING, M.HANDED_OFF, M.AWAITING_APPROVAL, M.NEEDS_REAPPROVAL, M.CANCELLED}
    ),
    # NEEDS_REAPPROVAL: the gateway never invoked the transport (stale claim hand-off).
    M.SENDING: frozenset({M.SENT, M.FAILED_DEFINITE, M.UNCERTAIN, M.NEEDS_REAPPROVAL}),
    M.UNCERTAIN: frozenset({M.SENT, M.NEEDS_REAPPROVAL}),
    # Outlook hand-off: the applicant records Sent, or Not sent (re-approval required).
    M.HANDED_OFF: frozenset({M.SENT, M.NEEDS_REAPPROVAL}),
    M.FAILED_DEFINITE: frozenset({M.APPROVED_QUEUED, M.AWAITING_APPROVAL, M.CANCELLED}),
    M.NEEDS_REAPPROVAL: frozenset({M.APPROVED_QUEUED, M.AWAITING_APPROVAL, M.CANCELLED}),
    # A cancelled message may be re-drafted (Email 1 after B3; next unsent step after B5).
    M.CANCELLED: frozenset({M.AWAITING_APPROVAL}),
    M.SENT: frozenset(),
}


def check_campaign(current: C, target: C) -> None:
    if target not in CAMPAIGN_TRANSITIONS[current]:
        raise InvalidTransition(f"campaign {current.value} -> {target.value} is not allowed")


def check_message(current: M, target: M) -> None:
    if target not in MESSAGE_TRANSITIONS[current]:
        raise InvalidTransition(f"message {current.value} -> {target.value} is not allowed")
