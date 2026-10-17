"""Approval prerequisites, snapshot binding, revoke/cancel/stop.

Decisions B3, B5, B18-B20; acceptance 9-10.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    ApprovalDependency,
    ApprovalSnapshot,
    AuditEvent,
    Campaign,
    Institution,
    Message,
    Pi,
    PiEmail,
    ProfileFact,
)
from marcelorian.domain.enums import (
    Actor,
    CampaignState,
    MessageState,
    Mode,
    SnapshotState,
)
from marcelorian.domain.snapshot import TestOverride
from marcelorian.services import approvals, dnc
from marcelorian.services.approvals import ApprovalError, CancelRefused, CannotRecall
from marcelorian.services.gateway import Outcome

WEEK = timedelta(days=7)


def get(world, model, ident):
    with transaction(world.factory) as s:
        return s.get(model, ident)


# -- prerequisites (B20, acceptance 68) -----------------------------------------------------


def test_unverified_timezone_blocks_approval(world) -> None:
    inst = world.institution(verified=False)
    mid = world.draft(world.campaign(world.pi(inst)))
    with pytest.raises(ApprovalError) as info:
        world.approve(mid)
    assert "timezone_unverified" in info.value.reasons


def test_missing_timezone_blocks_approval(world) -> None:
    inst = world.institution(tz=None)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(world.pi(inst))))
    assert "timezone_unverified" in info.value.reasons


@pytest.mark.parametrize(("confidence", "ownership"), [("C", True), ("A", False), ("B", False)])
def test_non_send_ready_recipient_blocks_approval(world, confidence, ownership) -> None:
    pi = world.pi(world.institution(), confidence=confidence, ownership=ownership)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(pi)))
    assert "recipient_not_send_ready" in info.value.reasons


def test_invalid_address_blocks_approval(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        s.scalars(select(PiEmail).where(PiEmail.pi_id == pi)).one().invalid = True
    with pytest.raises(ApprovalError):
        world.approve(world.draft(world.campaign(pi)))


def test_unreviewed_salutation_blocks_approval(world) -> None:
    pi = world.pi(world.institution(), salutation_reviewed=False)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(pi)))
    assert "salutation_not_reviewed" in info.value.reasons


def test_prerequisites_are_rechecked_at_dispatch(world) -> None:
    pi = world.pi(world.institution())
    camp = world.campaign(pi)
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        email = s.scalars(select(PiEmail).where(PiEmail.pi_id == pi)).one()
        approvals.update_pi_email(
            s, email, invalid=True, actor=Actor.APPLICANT, now=world.clock.now()
        )
    assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL
    assert world.dispatch(mid).outcome is Outcome.NOT_ELIGIBLE
    assert world.sandbox.messages == {}


def test_unconfirmed_fact_blocks_approval(world) -> None:
    fact = world.fact("current_role_phrase", "a research fellow", confirmed=False)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(world.pi(world.institution())), facts=(fact,)))
    assert "fact_not_confirmed" in info.value.reasons


def test_do_not_contact_blocks_approval(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        dnc.add_entry(
            s, scope_mode=Mode.LIVE, pi_id=pi, actor=Actor.APPLICANT, now=world.clock.now()
        )
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(pi)))
    assert "do_not_contact" in info.value.reasons


def test_inactive_mode_blocks_approval(world) -> None:
    world.connect_mailbox()
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        approvals.create_campaign(
            s,
            applicant_id=world.applicant_id,
            pi_id=pi,
            mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
        camp = s.scalars(select(Campaign)).one().id
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(camp))  # active mode is sandbox
    assert "mode_not_active" in info.value.reasons


def test_real_modes_need_connected_mailbox(world) -> None:
    world.set_mode(Mode.LIVE)
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(world.pi(world.institution()), Mode.LIVE)))
    assert "mailbox_not_connected" in info.value.reasons


def test_duplicate_outreach_through_merged_identity_blocked(world) -> None:
    inst = world.institution()
    pi_a, pi_b = world.pi(inst), world.pi(inst)
    world.dispatch(world.draft_and_approve(world.campaign(pi_a)))
    with transaction(world.factory) as s:
        s.get(Pi, pi_a).merged_into_id = pi_b  # confirmed merge: a is an alias of b
    with pytest.raises(ApprovalError) as info:
        world.approve(world.draft(world.campaign(pi_b)))
    assert "duplicate_outreach" in info.value.reasons


def test_synthetic_pi_cannot_be_campaigned_in_live(world) -> None:
    world.set_mode(Mode.LIVE)
    pi = world.pi(world.institution(), synthetic=True)
    with pytest.raises(ApprovalError):
        world.campaign(pi, Mode.LIVE)


# -- snapshot binding -----------------------------------------------------------------------------


def test_approval_binds_snapshot_and_dependencies(world) -> None:
    fact = world.fact("full_name", "A. Applicant")
    camp = world.campaign(world.pi(world.institution()))
    outcome = world.approve(world.draft(camp, facts=(fact,)))
    with transaction(world.factory) as s:
        snap = s.get(ApprovalSnapshot, outcome.snapshot_id)
        kinds = sorted(
            d.kind
            for d in s.scalars(
                select(ApprovalDependency).where(ApprovalDependency.snapshot_id == snap.id)
            )
        )
    assert snap.state == SnapshotState.ACTIVE.value and snap.sha256 == outcome.snapshot_sha256
    assert kinds == ["cv_version", "institution", "pi", "pi_email", "profile_fact"]


def test_button_label_and_expected_time(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    outcome = world.approve(world.draft(camp))
    assert outcome.button_label == "Approve and send"  # Tuesday 09:00 New York
    camp2 = world.campaign(world.pi(world.institution()))
    world.clock.advance(timedelta(hours=3))  # 12:00 New York
    outcome2 = world.approve(world.draft(camp2))
    assert outcome2.button_label == "Approve and queue"
    assert outcome2.expected_dispatch_at == outcome2.window.start  # Wednesday 08:00 New York


def test_edit_after_approval_invalidates(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    world.draft(camp, body="Dear Dr. Example,\n\nA revised body.\n")  # acceptance 9 (body)
    assert world.message_state(mid) is MessageState.AWAITING_APPROVAL
    with transaction(world.factory) as s:
        states = [
            x.state
            for x in s.scalars(select(ApprovalSnapshot).where(ApprovalSnapshot.message_id == mid))
        ]
    assert states == [SnapshotState.INVALIDATED.value]


def test_new_cv_upload_does_not_invalidate_and_is_never_substituted(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        approvals.upload_cv(
            s,
            applicant_id=world.applicant_id,
            filename="cv-v2.pdf",
            content_type="application/pdf",
            data=b"%PDF-1.4 synthetic CV v2",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED  # B19
    assert world.dispatch(mid).outcome is Outcome.SENT
    sent = next(iter(world.sandbox.messages.values()))
    assert sent is not None
    # acceptance 10: the frozen v1 bytes went out, never the newer upload
    raw = world.attempts(mid)[0]
    assert raw.status == "sent"
    with transaction(world.factory) as s:
        snap = s.get(ApprovalSnapshot, world.message(mid).current_snapshot_id)
        assert "cv-synthetic-v1.pdf" in snap.canonical_json


def test_replacing_the_attachment_of_that_message_invalidates(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        v2 = approvals.upload_cv(
            s,
            applicant_id=world.applicant_id,
            filename="cv-v2.pdf",
            content_type="application/pdf",
            data=b"%PDF v2",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        ).id
    world.draft(camp, cv_id=v2)
    assert world.message_state(mid) is MessageState.AWAITING_APPROVAL


def test_fact_change_invalidates_only_dependent_approvals(world) -> None:
    used = world.fact("current_role_phrase", "a research fellow at Synthetic")
    unused = world.fact("recent_news", "nothing new")
    inst = world.institution()
    camp_a = world.campaign(world.pi(inst))
    camp_b = world.campaign(world.pi(world.institution()))
    mid_a = world.draft_and_approve(camp_a, facts=(used,))
    mid_b = world.draft_and_approve(camp_b)
    with transaction(world.factory) as s:
        affected = approvals.update_fact(
            s,
            s.get(ProfileFact, unused),
            "changed",
            confirm=True,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    assert affected == []  # B18: unrelated edit
    with transaction(world.factory) as s:
        affected = approvals.update_fact(
            s,
            s.get(ProfileFact, used),
            "a fellow at Other",
            confirm=True,
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    assert affected == [mid_a]
    assert world.message_state(mid_a) is MessageState.NEEDS_REAPPROVAL
    assert world.message_state(mid_b) is MessageState.APPROVED_QUEUED


@pytest.mark.parametrize("change", ["salutation", "timezone", "recipient", "sender_row_changed"])
def test_dependency_changes_invalidate(world, change) -> None:
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    inst = world.institution()
    pi = world.pi(inst)
    camp = world.campaign(pi, Mode.LIVE)
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        now = world.clock.now()
        if change == "salutation":
            approvals.set_salutation(
                s, s.get(Pi, pi), "Professor Example", actor=Actor.APPLICANT, now=now
            )
        elif change == "timezone":
            approvals.set_institution_timezone(
                s,
                s.get(Institution, inst),
                "America/Chicago",
                verified=True,
                actor=Actor.APPLICANT,
                now=now,
            )
        elif change == "recipient":
            email = s.scalars(select(PiEmail).where(PiEmail.pi_id == pi)).one()
            approvals.update_pi_email(s, email, confidence="C", actor=Actor.APPLICANT, now=now)
        else:
            # A different account can no longer be connected once real history exists (M02),
            # so a changed sender row can only come from an older database; the approval
            # dependency still catches it at the claim (acceptance 9, defence in depth).
            from marcelorian.db.models import MailboxAccount

            s.scalars(select(MailboxAccount)).one().version += 1
    if change == "sender_row_changed":
        assert world.dispatch(mid).reason == "dependency_changed"
        return
    assert world.message_state(mid) is MessageState.NEEDS_REAPPROVAL  # acceptance 9


def test_switching_sender_account_after_real_history_is_refused(world) -> None:
    """M02: a different Gmail account is refused and nothing is changed."""
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    mid = world.draft_and_approve(world.campaign(world.pi(world.institution()), Mode.LIVE))
    with transaction(world.factory) as s, pytest.raises(approvals.MailboxIdentityRefused) as info:
        approvals.connect_mailbox(
            s,
            "other-applicant@example.test",
            frozenset(),
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
    assert info.value.reasons == ["mailbox_identity_switch_after_real_history"]
    assert world.message_state(mid) is MessageState.APPROVED_QUEUED


def test_mode_is_bound_into_the_snapshot(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    assert world.dispatch(mid).reason == "mode_inactive"
    assert world.message(mid).mode == Mode.SANDBOX.value  # switching never changes stored mode


# -- revoke / cancel / stop (B3, B5) --------------------------------------------------------------


def test_cancel_initial_preserves_message_and_audit_and_allows_new_revision(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )
    with transaction(world.factory) as s:
        assert (
            s.scalar(select(AuditEvent).where(AuditEvent.action == "cancelled_before_first_send"))
            is not None
        )
    mid2 = world.draft(camp)
    assert mid2 == mid  # same logical message
    outcome = world.approve(mid2)
    with transaction(world.factory) as s:
        versions = [
            x.version
            for x in s.scalars(
                select(ApprovalSnapshot)
                .where(ApprovalSnapshot.message_id == mid)
                .order_by(ApprovalSnapshot.version)
            )
        ]
    assert versions == [1, 2] and outcome.snapshot_id


def test_cancel_refused_with_uncertain_attempt(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    original_search = world.sandbox.search_message_ids

    def search(query: str, *, include_spam_trash: bool):
        if query.startswith("rfc822msgid:"):
            return _search_fails(query, include_spam_trash=include_spam_trash)
        return original_search(query, include_spam_trash=include_spam_trash)

    world.sandbox.send = _raise_ambiguous  # type: ignore[method-assign]
    world.sandbox.search_message_ids = search  # type: ignore[method-assign]
    assert world.dispatch(mid).outcome is Outcome.UNCERTAIN
    with transaction(world.factory) as s, pytest.raises(CancelRefused):
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )
    assert world.message_state(mid) is MessageState.UNCERTAIN


def _raise_ambiguous(raw: bytes, thread_id: str | None) -> None:
    from marcelorian.transport.base import AmbiguousOutcome

    raise AmbiguousOutcome("timeout")


def _search_fails(query: str, *, include_spam_trash: bool) -> None:
    from marcelorian.transport.base import TransportUnavailable

    raise TransportUnavailable("down")


def test_cancel_refused_after_send(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    with transaction(world.factory) as s, pytest.raises(CancelRefused):
        approvals.cancel_initial(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )


def test_revoke_cannot_recall_claimed_message(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        message = s.get(Message, mid)
        message.state = MessageState.SENDING.value
        with pytest.raises(CannotRecall):
            approvals.revoke(s, message, actor=Actor.APPLICANT, now=world.clock.now())


def test_stop_followup_sequence_keeps_slot_and_allows_deliberate_redraft(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    world.clock.advance(WEEK)
    m2 = world.draft_and_approve(camp, 2)
    with transaction(world.factory) as s:
        approvals.stop_sequence(
            s, s.get(Campaign, camp), actor=Actor.APPLICANT, now=world.clock.now()
        )
    row = world.campaign_row(camp)
    assert row.sequence_stopped and row.state == CampaignState.OUTREACH_PENDING.value
    assert world.message_state(m2) is MessageState.CANCELLED
    assert world.dispatch(m2).outcome is Outcome.NOT_ELIGIBLE
    world.draft(camp, 2)  # deliberate re-draft of the next unsent step
    assert not world.campaign_row(camp).sequence_stopped


def test_sent_step_is_never_redrafted(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    with pytest.raises(ApprovalError):
        world.draft(camp, 1)


def test_only_next_step_and_only_when_due(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    world.dispatch(world.draft_and_approve(camp))
    with pytest.raises(ApprovalError) as info:
        world.draft(camp, 2)
    assert info.value.reasons == ["step_not_due_yet"]
    with pytest.raises(ApprovalError):
        world.draft(camp, 3)
    world.clock.advance(WEEK)
    world.draft(camp, 2)


def test_override_only_in_transport_test(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    with pytest.raises(ApprovalError):
        world.draft(camp, override=TestOverride(window_always_open=True))
