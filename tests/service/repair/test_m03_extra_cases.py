"""M03 additional regression cases (synthetic data, fake transports, injected clock only)."""

from __future__ import annotations

import pytest

from marcelorian.db.engine import transaction
from marcelorian.db.models import CampaignPause
from marcelorian.domain.enums import Actor, CampaignState
from marcelorian.services import campaigns as campaign_svc
from marcelorian.services import prior_contact
from marcelorian.services.gateway import Outcome
from tests.factories import APPLICANT_EMAIL
from tests.service.repair.test_m03_overlapping_acks import (
    _ack,
    _ack_audits,
    _ack_rows,
    _open_reviews,
    _racing_overlap,
)


def test_partial_ack_keeps_campaign_paused_and_c_unsent(live) -> None:
    """Guard: acknowledging P1 {a,b} must NOT auto-resolve P2 {a,b,c}; Email 1 must not
    send while c is unreviewed."""
    camp, mid, _a, _b, _c, p1, p2 = _racing_overlap(live)
    _ack(live, p1)
    assert [p.id for p in _open_reviews(live, camp)] == [p2]
    assert live.campaign_row(camp).state == CampaignState.PAUSED_REVIEW.value
    sends_before = len(live.gmail.send_calls)
    result = live.dispatch(mid)
    assert result.outcome is not Outcome.SENT
    assert len(live.gmail.send_calls) == sends_before


def test_overlap_ack_with_mailbox_case_variant(live) -> None:
    """Boundary: P1 acknowledged with an upper-cased mailbox, P2 with the lower-case one.
    Must be idempotent under the lowered unique key (no IntegrityError, one row per ID)."""
    camp, _mid, a, b, c, p1, p2 = _racing_overlap(live)
    with transaction(live.factory) as s:
        pause = s.get(CampaignPause, p1)
        prior_contact.acknowledge_and_proceed(
            s,
            pause,
            mailbox_email=APPLICANT_EMAIL.upper(),
            actor=Actor.APPLICANT,
            now=live.clock.now(),
        )
    _ack(live, p2)
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b, c])


def test_crash_inside_overlap_ack_rolls_back_and_retry_succeeds(live, monkeypatch) -> None:
    """Crash point: a failure after the inserts (inside resolve_pause) of the overlapping
    P2 acknowledgment rolls the whole transaction back (no c row, no audit, P2 open);
    a retry after 'restart' succeeds."""
    camp, mid, a, b, c, p1, p2 = _racing_overlap(live)
    _ack(live, p1)
    real = campaign_svc.resolve_pause

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(campaign_svc, "resolve_pause", boom)
    with pytest.raises(RuntimeError):
        _ack(live, p2)
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b])
    assert len(_ack_audits(live, camp)) == 1
    assert [p.id for p in _open_reviews(live, camp)] == [p2]
    monkeypatch.setattr(campaign_svc, "resolve_pause", real)
    _ack(live, p2)
    assert sorted(g for g, _ in _ack_rows(live, camp)) == sorted([a, b, c])
    assert live.dispatch(mid).outcome is Outcome.SENT


def test_ack_with_new_unseen_id_after_both_acks_rereviews(live) -> None:
    """Guard after the fix: after both overlapping reviews are acknowledged, a further new
    message d from the same PI pauses Email 1 again (no ignore-by-sender)."""
    camp, mid, _a, _b, _c, p1, p2 = _racing_overlap(live)
    _ack(live, p1)
    _ack(live, p2)
    from tests.service.repair.test_m03_overlapping_acks import _ids, _prior

    d = _prior(live, days=0)
    result = live.dispatch(mid)
    assert result.reason == "prior_contact_found"
    (p3,) = _open_reviews(live, camp)
    assert _ids(p3) == [d]
