"""Plain-language text for the app's reason codes (shown to the applicant)."""

# ruff: noqa: E501 (a table of sentences)

from __future__ import annotations

REASONS: dict[str, str] = {
    "campaign_not_sendable": "This campaign is not in a state where an email can go out.",
    "campaign_not_draftable": "This campaign cannot take a new draft right now.",
    "cancel_pending": "A cancel request is pending for this email.",
    "dependency_changed_since_draft": "Your profile or this contact changed since the draft was saved: save the draft again.",
    "do_not_contact": "This person is on your do-not-contact list.",
    "due_time_missing": "The follow-up has no due time yet.",
    "duplicate_outreach": "You already have outreach to this person.",
    "fact_not_confirmed": "The draft uses a profile answer that is not confirmed.",
    "mailbox_not_connected": "Add your Outlook address on the Profile page first.",
    "mode_not_active": "This email belongs to a mode that is not active.",
    "recipient_not_send_ready": "The address is not send-ready (needs confidence A or B with ownership evidence).",
    "salutation_not_reviewed": "Review the salutation on the contact first.",
    "sequence_stopped": "The follow-up sequence was stopped.",
    "synthetic_pi_in_live": "A practice contact cannot be emailed in live mode.",
    "timezone_invalid": "The institution's time zone is not valid.",
    "timezone_unverified": "Confirm the institution's time zone first.",
    "thread_headers_unverified": "The earlier email's thread details are not verified.",
    "message_not_approvable": "This email cannot be approved in its current state.",
    "message_not_editable": "This email can no longer be edited.",
    "previous_step_not_sent": "The previous email has not been sent yet.",
    "step_not_next": "This is not the next follow-up.",
    "step_not_due_yet": "This follow-up is not due yet.",
    "no_permitted_window": "There is no allowed send time for this institution's time zone.",
    "institution_pending_full": "Too many open applications at this institution.",
    "lab_pending_full": "This lab already has an open application.",
    "new_contacts_30d_full": "The limit of new contacts at this institution in 30 days is reached.",
    "same_place_active": "You already have an active application at this place. It frees after a rejection, a close, or 14 days after the last unanswered follow-up.",
    "lab_suggestion_unreviewed": "Review the possible shared-lab suggestion first.",
    "signature_step_required": "Set up (or skip) your signature on the Profile page first.",
    "not_approved_queued": "This email is not approved and waiting.",
    "outside_window": "Outside the send window (Mon–Fri 08:00–16:00 at the PI's location).",
    "pacing": "A short pause between emails is still running.",
    "budget": "The 24-hour email limit is reached.",
    "over_capacity": "An application limit is reached.",
    "too_many_open_handoffs": "Three emails are already open in Outlook: mark them Sent or Not sent first.",
    "not_due": "This email is not due yet.",
    "own_attempt_unresolved": "An earlier email to this person is not marked Sent or Not sent yet.",
    "reply_check_incomplete": "Confirm the PI has not replied before sending a follow-up.",
    "window_passed": "The send window passed; the email rolls to the next window.",
    "paused": "Sending is paused (dashboard: Resume sending).",
    "mode_inactive": "This email belongs to a mode that is not active.",
    "prior_contact_check_incomplete": "Confirm you checked Outlook for earlier contact with this person.",
    "unresolved_attempt": "An earlier email to this person is not resolved yet.",
    "handoff_real_modes_only": "Practice emails are sent by the practice mailbox, not Outlook.",
}


def explain(code: str | None) -> str:
    if not code:
        return ""
    base = code.split(":", 1)[0]
    if base == "confirmation_required":
        return "Confirm the mailbox check before opening the email."
    if base == "over_capacity" and ":" in code:
        return "; ".join(explain(part) for part in code.split(":", 1)[1].split(","))
    return REASONS.get(code, REASONS.get(base, code.replace("_", " ")))
