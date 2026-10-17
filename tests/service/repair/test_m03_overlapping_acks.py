"""M03 regression tests: overlapping exact-message prior-contact acknowledgments must be idempotent.

Owner brief (M03): make overlapping exact-message prior-contact acknowledgments
idempotent under the existing unique key (mailbox_email, campaign_id,
gmail_message_id). Preserve audit history and review new IDs. Do not use blanket
ignore-by-sender or weaken duplicate-outreach checks.

Every test asserts the DESIRED SAFE OUTCOME. Tests whose name starts with
``test_guard_`` describe behaviour that must survive the fix and are expected to pass
before the fix as well.

Synthetic data, fake transports and the injected clock only.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent, CampaignPause, PriorContactAck
from marcelorian.domain.enums import Actor, CampaignState, Mode, PauseReason
from marcelorian.services import prior_contact
from marcelorian.services.gateway import Outcome
from tests.factories import APPLICANT_EMAIL

PI_EMAIL = "overlap-pi@synthetic-univ.test"


@pytest.fixture
def live(world):
    world.connect_mailbox()
    world.set_mode(Mode.LIVE)
    return world


# -- helpers -----------------------------------------------------------------------------


def _prior(world, *, days: int = 400):
    return world.gmail.inject(
        sender=PI_EMAIL, to=APPLICANT_EMAIL, when=world.clock.now() - timedelta(days=days)
    ).gmail_message_id


def _open_reviews(world, camp: int) -> list[CampaignPause]:
    with transaction(world.factory) as s:
        rows = s.scalars(
            select(CampaignPause)
            .where(
                CampaignPause.campaign_id == camp,
                CampaignPause.reason == PauseReason.PRIOR_CONTACT_FOUND.value,
                CampaignPause.resolved_at.is_(None),
            )
            .order_by(CampaignPause.id)
        ).all()
        s.expunge_all()
        return list(rows)


def _ids(pause: CampaignPause) -> list[str]:
    return list(json.loads(pause.detail_json or "{}").get("gmail_ids", []))


def _ack(world, pause_id: int) -> list[str]:
    with transaction(world.factory) as s:
        pause = s.get(CampaignPause, pause_id)
        assert pause is not None
        return prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=world.clock.now()
        )


def _ack_rows(world, camp: int) -> list[tuple[str, object]]:
    with transaction(world.factory) as s:
        return [
            (r.gmail_message_id, r.acknowledged_at)
            for r in s.scalars(
                select(PriorContactAck)
                .where(PriorContactAck.campaign_id == camp)
                .order_by(PriorContactAck.id)
            )
        ]


def _ack_audits(world, camp: int) -> list[dict]:
    with transaction(world.factory) as s:
        return [
            json.loads(e.detail_json or "{}")
            for e in s.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.entity_type == "campaign",
                    AuditEvent.entity_id == str(camp),
                    AuditEvent.action == "prior_contact_acknowledged",
                )
                .order_by(AuditEvent.id)
            )
        ]


def _first_review(live) -> tuple[int, int, str, str]:
    """Campaign with hits {a, b} pausing on its first dispatch; returns (camp, mid, a, b)."""
    pi = live.pi(live.institution(), email=PI_EMAIL)
    a, b = _prior(live), _prior(live, days=300)
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    result = live.dispatch(mid)
    assert (result.outcome, result.reason) == (Outcome.PAUSED, "prior_contact_found")
    return camp, mid, a, b


def _racing_overlap(live) -> tuple[int, int, str, str, str, int, int]:
    """Two dispatch callers race the Email 1 preflight (note 18): the inner caller's
    search sees {a, b} and opens review P1; a new message c arrives; the outer caller's
    search, already in flight, sees {a, b, c} and opens overlapping review P2."""
    pi = live.pi(live.institution(), email=PI_EMAIL)
    a, b = _prior(live), _prior(live, days=300)
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    box: dict[str, object] = {}

    def racing_caller(query: str) -> None:
        live.gmail.on_search = None
        box["inner"] = live.dispatch(mid)
        box["c"] = _prior(live, days=1)

    live.gmail.on_search = racing_caller
    outer = live.dispatch(mid)
    inner = box["inner"]
    assert (inner.outcome, inner.reason) == (Outcome.PAUSED, "prior_contact_found")
    assert (outer.outcome, outer.reason) == (Outcome.PAUSED, "prior_contact_found")
    c = str(box["c"])
    reviews = _open_reviews(live, camp)
    assert [_ids(p) for p in reviews] == [sorted([a, b]), sorted([a, b, c])], (
        "precondition: two overlapping open prior-contact reviews"
    )
    return camp, mid, a, b, c, reviews[0].id, reviews[1].id


# -- regression tests (each reproduced M03 before the fix) -------------------------


def test_racing_preflights_overlapping_reviews_are_both_acknowledgeable(live) -> None:
    """Gateway-reachable path: P1 {a,b} then P2 {a,b,c}; acknowledging both must work."""
    camp, mid, a, b, c, p1, p2 = _racing_overlap(live)
    assert sorted(_ack(live, p1)) == sorted([a, b])
    t_first = live.clock.now()
    live.clock.advance(timedelta(minutes=1))
    _ack(live, p2)  # desired: no IntegrityError; a, b reused; c newly acknowledged
    rows = _ack_rows(live, camp)
    assert sorted(g for g, _ in rows) == sorted([a, b, c])  # one row per exact ID
    first_at = {g: at for g, at in rows}
    assert first_at[a] == t_first and first_at[b] == t_first  # original rows preserved
    assert _open_reviews(live, camp) == []
    assert live.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value
    assert live.dispatch(mid).outcome is Outcome.SENT  # nothing unreviewed remains


def test_overlapping_review_acknowledged_first_then_subset_review(live) -> None:
    """Reverse order: acknowledging P2 {a,b,c} first, then P1 {a,b} (all already
    acknowledged) must be an idempotent no-insert acknowledgment that resolves P1."""
    camp, _mid, a, b, c, p1, p2 = _racing_overlap(live)
    assert sorted(_ack(live, p2)) == sorted([a, b, c])
    _ack(live, p1)  # desired: no IntegrityError
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b, c])
    assert _open_reviews(live, camp) == []
    assert live.campaign_row(camp).state == CampaignState.OUTREACH_PENDING.value


def test_overlapping_ack_service_contract(live) -> None:
    """Service contract without the race: two open reviews for one campaign whose exact
    Gmail IDs overlap (open_pause keys them by source_ref, so both stay open)."""
    camp, _mid, a, b = _first_review(live)
    c = _prior(live, days=1)
    with transaction(live.factory) as s:
        campaign = live.campaign_row(camp)
        campaign = s.merge(campaign)
        p2 = prior_contact.open_review(
            s, campaign, tuple(sorted([a, b, c])), actor=Actor.GATEWAY, now=live.clock.now()
        ).id
    p1 = _open_reviews(live, camp)[0].id
    assert p1 != p2
    _ack(live, p1)
    _ack(live, p2)  # desired: no IntegrityError
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b, c])
    assert _open_reviews(live, camp) == []


def test_overlapping_ack_audit_records_each_acknowledgment_and_new_ids(live) -> None:
    """Audit history: one ``prior_contact_acknowledged`` event per acknowledgment,
    naming the review, the IDs newly acknowledged and the IDs already acknowledged."""
    camp, _mid, a, b, c, p1, p2 = _racing_overlap(live)
    _ack(live, p1)
    _ack(live, p2)
    audits = _ack_audits(live, camp)
    assert len(audits) == 2
    assert audits[0].get("pause_id") == p1
    assert sorted(audits[0].get("new_ids", [])) == sorted([a, b])
    assert audits[0].get("already_acknowledged_ids", []) == []
    assert audits[1].get("pause_id") == p2
    assert audits[1].get("new_ids") == [c]
    assert sorted(audits[1].get("already_acknowledged_ids", [])) == sorted([a, b])


def test_overlapping_ack_failure_does_not_strand_the_campaign(live) -> None:
    """Evidence of impact: when the second acknowledgment fails, the review stays open,
    every retry fails the same way and Email 1 is deferred indefinitely; the applicant's
    only exits are close/cancel. Desired: acknowledging proceeds."""
    from sqlalchemy.exc import IntegrityError

    camp, mid, _a, _b, _c, p1, p2 = _racing_overlap(live)
    _ack(live, p1)
    errors = []
    for _ in range(2):  # the applicant retries
        try:
            _ack(live, p2)
            break
        except IntegrityError as exc:
            errors.append(type(exc).__name__)
    state = live.campaign_row(camp).state
    still_open = [p.id for p in _open_reviews(live, camp)]
    result = live.dispatch(mid)
    print("M03-OBSERVED", errors, state, still_open, result.outcome, result.reason)
    assert (errors, state, still_open, result.outcome) == (
        [],
        CampaignState.OUTREACH_PENDING.value,
        [],
        Outcome.SENT,
    )


def test_boundary_review_listing_an_id_twice_acknowledges_it_once(live) -> None:
    """Boundary: a review detail that lists one exact ID twice (not produced by the
    gateway today, which de-duplicates hits) must still be acknowledgeable."""
    camp, _mid, a, b = _first_review(live)
    (p1,) = _open_reviews(live, camp)
    with transaction(live.factory) as s:
        pause = s.get(CampaignPause, p1.id)
        pause.detail_json = json.dumps({"gmail_ids": [a, a, b]})
    _ack(live, p1.id)  # desired: no IntegrityError within the single acknowledgment
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b])


# -- guards (behaviour that held before the fix and must keep holding) ----------------------


def test_guard_ack_is_scoped_to_campaign_not_sender_or_message(live) -> None:
    """No blanket ignore: one exact Gmail ID acknowledged in campaign X is still a
    review hit for campaign Y (same mailbox, different PI on the same message)."""
    other = "second-pi@synthetic-univ.test"
    pi_x = live.pi(live.institution(), email=PI_EMAIL)
    pi_y = live.pi(live.institution(), email=other)
    shared = live.gmail.inject(
        sender=APPLICANT_EMAIL,
        to=f"{PI_EMAIL}, {other}",
        when=live.clock.now() - timedelta(days=200),
    ).gmail_message_id
    camp_x = live.campaign(pi_x, Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(camp_x)).reason == "prior_contact_found"
    (px,) = _open_reviews(live, camp_x)
    _ack(live, px.id)
    camp_y = live.campaign(pi_y, Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(camp_y)).reason == "prior_contact_found"
    (py,) = _open_reviews(live, camp_y)
    assert _ids(py) == [shared]


def test_guard_sequential_rereview_lists_only_new_ids(live) -> None:
    """The ordinary sequential path never re-lists acknowledged IDs: a later search
    returning {a,b,c} opens a review for {c} only; a new message from the same sender is
    reviewed again (no ignore-by-sender)."""
    camp, mid, _a, _b = _first_review(live)
    (p1,) = _open_reviews(live, camp)
    _ack(live, p1.id)
    c = _prior(live, days=1)
    result = live.dispatch(mid)
    assert result.reason == "prior_contact_found"
    (p2,) = _open_reviews(live, camp)
    assert _ids(p2) == [c]
    _ack(live, p2.id)
    assert live.dispatch(mid).outcome is Outcome.SENT


def test_guard_resolved_review_cannot_be_acknowledged_again(live) -> None:
    """Acknowledging an already-resolved review stays a clean refusal with no new rows."""
    camp, _mid, a, b = _first_review(live)
    (p1,) = _open_reviews(live, camp)
    _ack(live, p1.id)
    with pytest.raises(ValueError, match="not an open prior-contact review"):
        _ack(live, p1.id)
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b])
    assert len(_ack_audits(live, camp)) == 1


def test_guard_acknowledged_id_never_masks_duplicate_outreach(live) -> None:
    """Even an acknowledged ID that is the app's own message for another campaign still
    blocks as duplicate_outreach (acks are excluded from review hits, not from the
    duplicate check)."""
    first = live.campaign(live.pi(live.institution()), Mode.LIVE)
    assert live.dispatch(live.draft_and_approve(first)).outcome is Outcome.SENT
    own_gid = live.gmail.sent_messages()[0].meta.gmail_message_id
    live.clock.advance(timedelta(minutes=10))
    pi = live.pi(live.institution(), email=PI_EMAIL)
    a = _prior(live)
    camp = live.campaign(pi, Mode.LIVE)
    mid = live.draft_and_approve(camp)
    with transaction(live.factory) as s:
        campaign = s.merge(live.campaign_row(camp))
        pause = prior_contact.open_review(
            s, campaign, tuple(sorted([a, own_gid])), actor=Actor.GATEWAY, now=live.clock.now()
        )
        prior_contact.acknowledge_and_proceed(
            s, pause, mailbox_email=APPLICANT_EMAIL, actor=Actor.APPLICANT, now=live.clock.now()
        )
    original = live.gmail.search_message_ids

    def search(query, *, include_spam_trash):
        result = original(query, include_spam_trash=include_spam_trash)
        if query.startswith("("):
            return type(result)(ids=(*result.ids, own_gid), complete=result.complete)
        return result

    live.gmail.search_message_ids = search  # type: ignore[method-assign]
    assert live.dispatch(mid).reason == "duplicate_outreach"
    assert len(live.gmail.send_calls) == 1
