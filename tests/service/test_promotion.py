"""Turning a confirmed research record into a contact (P1)."""

from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from marcelorian.db.engine import transaction
from marcelorian.db.models import (
    AuditEvent,
    Campaign,
    Institution,
    LabGroup,
    Message,
    Pi,
    PiEmail,
    ResearchRecord,
    Reservation,
)
from marcelorian.domain.enums import Actor, Mode
from marcelorian.domain.seed import read_seed
from marcelorian.services import dnc, promotion, research_seed
from marcelorian.services.recipients import send_ready_email
from marcelorian.services.research_seed import import_seed
from tests.seed_fixture import contact, csv_text, standard_rows

SOURCE = "https://synthetic-eye.test/people/fac1"


def stage(world, rows=None) -> None:
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(rows or standard_rows()), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )


def confirm(world, external_id: str) -> None:
    with transaction(world.factory) as s:
        research_seed.quick_finding(
            s,
            _id(s, external_id),
            verification="confirmed_current",
            source="https://synthetic-eye.test/faculty",
            note="",
            now=world.clock.now(),
        )


def _id(s, external_id: str) -> int:
    return s.scalars(
        select(ResearchRecord.id).where(ResearchRecord.external_record_id == external_id)
    ).one()


def data(**changes) -> promotion.PromotionInput:
    base = dict(
        last_name="C-FAC1",
        salutation="Dr. C-FAC1",
        email_source="institutional_page",
        email_source_url=SOURCE,
        timezone="America/New_York",
        timezone_confirmed=True,
        department="Retina Service",
    )
    base.update(changes)
    return promotion.PromotionInput(**base)


def promote(world, external_id: str, **changes) -> Pi:
    with transaction(world.factory) as s:
        return promotion.promote(s, _id(s, external_id), data(**changes), now=world.clock.now())


def blockers(world, external_id: str) -> list[str]:
    with transaction(world.factory) as s:
        record = s.get(ResearchRecord, _id(s, external_id))
        return promotion.blockers(s, record, world.clock.now().date())


@pytest.fixture
def staged(world):
    stage(world)
    return world


def test_only_a_confirmed_person_with_an_address_can_be_promoted(staged) -> None:
    assert "Confirm the person yourself first" in blockers(staged, "C-FAC1")[0]
    for external_id in ("C-PROG", "O-PROG1", "C-AUTH1", "C-NL1"):
        confirm(staged, external_id)
    assert any("not a PI" in b for b in blockers(staged, "C-PROG"))
    assert any("Opportunities are not contacts" in b for b in blockers(staged, "O-PROG1"))
    assert any("No address on record" in b for b in blockers(staged, "C-AUTH1"))
    assert any("Out of scope" in b for b in blockers(staged, "C-NL1"))
    confirm(staged, "C-FAC1")
    assert blockers(staged, "C-FAC1") == []
    staged.clock.advance(timedelta(days=91))
    assert any("over 90 days old" in b for b in blockers(staged, "C-FAC1"))


def test_promotion_creates_a_send_ready_contact_and_nothing_else(staged) -> None:
    confirm(staged, "C-FAC1")
    pi = promote(staged, "C-FAC1")
    with transaction(staged.factory) as s:
        pi = s.get(Pi, pi.id)
        institution = s.get(Institution, pi.institution_id)
        assert (institution.name, institution.iana_tz, institution.tz_verified) == (
            "Synthetic Eye Institute",
            "America/New_York",
            True,
        )
        assert s.get(LabGroup, pi.lab_group_id).institution_id == institution.id
        assert pi.current_role_verified and pi.salutation_reviewed
        assert pi.department == "Retina Service" and pi.salutation == "Dr. C-FAC1"
        assert "confirmed current role" in pi.current_role_evidence
        email = send_ready_email(s, pi.id)
        assert email is not None and email.address == "c-fac1@synthetic-eye.test"
        assert (email.confidence, email.ownership_verified) == ("A", True)
        assert SOURCE in email.verification_evidence
        record = s.get(ResearchRecord, _id(s, "C-FAC1"))
        assert record.promoted_pi_id == pi.id
        for model in (Campaign, Message, Reservation):
            assert s.scalar(select(func.count()).select_from(model)) == 0
        assert (
            s.scalar(
                select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "promoted")
            )
            == 1
        )
    assert any("Already turned into a contact" in b for b in blockers(staged, "C-FAC1"))


def test_recent_publication_gives_confidence_b(staged) -> None:
    confirm(staged, "C-FAC1")
    pi = promote(staged, "C-FAC1", email_source="recent_publication")
    with transaction(staged.factory) as s:
        assert send_ready_email(s, pi.id).confidence == "B"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"email_source_url": "not a url"}, "Give the web address"),
        ({"email_source_url": "javascript:alert(1)"}, "Give the web address"),
        ({"email_source": "guess"}, "Say where you saw the address"),
        ({"timezone": "Mars/Base"}, "Choose a valid time zone"),
        ({"timezone_confirmed": False}, "Confirm the institution's time zone"),
        ({"last_name": " "}, "last name"),
        ({"salutation": ""}, "salutation"),
    ],
)
def test_promotion_input_is_checked(staged, changes, message) -> None:
    confirm(staged, "C-FAC1")
    with pytest.raises(promotion.PromotionError) as info:
        promote(staged, "C-FAC1", **changes)
    assert any(message in reason for reason in info.value.reasons)
    with transaction(staged.factory) as s:
        assert s.scalar(select(func.count()).select_from(Pi)) == 0


def test_live_do_not_contact_blocks_promotion(staged) -> None:
    confirm(staged, "C-FAC1")
    with transaction(staged.factory) as s:
        dnc.add_entry(
            s,
            scope_mode=Mode.LIVE,
            actor=Actor.APPLICANT,
            now=staged.clock.now(),
            email="C-FAC1@synthetic-eye.test",
            reason="asked not to be contacted",
        )
    with pytest.raises(promotion.PromotionError) as info:
        promote(staged, "C-FAC1")
    assert any("do-not-contact" in r for r in info.value.reasons)


def test_the_same_person_is_linked_never_duplicated(staged) -> None:
    inst = staged.institution(name="Synthetic Eye Institute")
    existing = staged.pi(inst, email="c-fac1@synthetic-eye.test")
    confirm(staged, "C-FAC1")
    pi = promote(staged, "C-FAC1")
    assert pi.id == existing
    with transaction(staged.factory) as s:
        assert s.scalar(select(func.count()).select_from(Pi)) == 1
        assert s.scalar(select(func.count()).select_from(PiEmail)) == 1


def test_same_name_at_the_institution_needs_a_check(staged) -> None:
    inst = staged.institution(name="Synthetic Eye Institute")
    staged.pi(inst, name="Dr Synthetic C-FAC1", email="someone.else@synthetic-eye.test")
    confirm(staged, "C-FAC1")
    with pytest.raises(promotion.PromotionError) as info:
        promote(staged, "C-FAC1")
    assert any("same name" in r for r in info.value.reasons)
    pi = promote(staged, "C-FAC1", not_same_person=True)
    with transaction(staged.factory) as s:
        assert s.scalar(select(func.count()).select_from(Pi)) == 2
        assert s.get(Pi, pi.id).institution_id == inst  # the institution is reused


def test_a_seed_change_after_promotion_is_a_conflict_and_keeps_the_link(staged) -> None:
    confirm(staged, "C-FAC1")
    pi = promote(staged, "C-FAC1")
    rows = standard_rows()
    rows[0] = contact(
        "C-FAC1", role="Professor", related_record_ids_json=json.dumps(["O-PROG1", "O-CLOSED1"])
    )
    with transaction(staged.factory) as s:
        assert research_seed.preview(
            s, read_seed(io.StringIO(csv_text(rows), newline=""))
        ).categories["conflicting"] == ["C-FAC1"]
    stage(staged, rows)
    with transaction(staged.factory) as s:
        assert s.get(ResearchRecord, _id(s, "C-FAC1")).promoted_pi_id == pi.id
