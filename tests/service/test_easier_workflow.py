"""Owner requests of 2026-10-27: prefilled lines, the board, editable addresses,
no PhD-student postings, and a PubMed search that finds the applicant's papers."""

from __future__ import annotations

import pytest

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message, PiEmail
from marcelorian.domain.enums import Mode
from marcelorian.lookups.enrich import applicant_term
from marcelorian.services import autofill, contact_emails, pipeline, tailoring
from marcelorian.services.recipients import send_ready_email
from tests.service.test_drafting import add_evidence, confirm_facts
from tests.service.test_tailoring import link_seed, pi_of


def test_lines_are_prefilled_from_the_pi_and_the_profile(world) -> None:
    confirm_facts(world, {"skills": "OCT image analysis", "research_interests": "glaucoma"})
    camp = world.campaign(world.pi(world.institution()))
    add_evidence(world, camp, "Choroidal thickness in glaucoma progression")
    link_seed(world, pi_of(world, camp), topics="Retinal imaging; OCT angiography")
    with transaction(world.factory) as s:
        found = autofill.suggest(s, s.get(Campaign, camp), 1, world.clock.now().date())
    first = found.first()
    assert first["personalized_sentence"].startswith(
        "I read your recent paper, “Choroidal thickness in glaucoma progression”"
    )
    assert "my interest in glaucoma" in first["personalized_sentence"]
    assert first["skills_line"] == "OCT image analysis"
    assert first["contribution_line"] == "your research on retinal imaging"
    topics = [o.text for o in found.options["pi_topic"]]
    assert topics[:2] == ["retinal imaging", "OCT angiography"]  # acronyms keep their case
    assert len(found.options["personalized_sentence"]) >= 3  # paper and topic choices


def test_nothing_on_file_still_gives_a_safe_default(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    with transaction(world.factory) as s:
        found = autofill.suggest(s, s.get(Campaign, camp), 1, world.clock.now().date())
    assert found.first() == {"contribution_line": "your group's ongoing projects"}


def test_the_board_moves_a_person_along(world) -> None:
    inst = world.institution(name="Synthetic Eye Institute")
    pi = world.pi(inst, name="Pat Board")

    def stage() -> str:
        with transaction(world.factory) as s:
            board = pipeline.board(s, Mode.SANDBOX, world.clock.now().date())
        return next(c.stage for col in board.columns for c in col.cards if c.name == "Pat Board")

    assert stage() == "contact"
    camp = world.campaign(pi)
    mid = world.draft(camp)
    assert stage() == "drafted"
    world.approve(mid)
    assert stage() == "ready"
    world.dispatch(mid)
    if world.message(mid).state == "sent":
        assert stage() == "sent"
    with transaction(world.factory) as s:
        found = pipeline.board(s, Mode.SANDBOX, world.clock.now().date(), query="synthetic eye")
        assert found.total >= 1
        assert pipeline.board(s, Mode.SANDBOX, world.clock.now().date(), query="zzz").total == 0


def test_an_address_can_be_changed_and_never_shared_between_contacts(world) -> None:
    inst = world.institution()
    pi = world.pi(inst)
    other = world.pi(inst)
    camp = world.campaign(pi)
    mid = world.draft_and_approve(camp)
    with transaction(world.factory) as s:
        old = send_ready_email(s, pi)
        assert old is not None
        contact_emails.add_address(
            s,
            pi,
            "New.Address@Synthetic-Univ.test",
            source="institutional_page",
            source_url="https://synthetic-univ.test/people",
            replace_current=True,
            now=world.clock.now(),
        )
        ready = send_ready_email(s, pi)
        assert ready is not None and ready.address == "new.address@synthetic-univ.test"
        assert s.get(PiEmail, old.id).invalid
        assert s.get(Message, mid).state == "needs_reapproval"  # approval withdrawn (B18)
        with pytest.raises(contact_emails.AddressError, match="already belongs"):
            contact_emails.add_address(
                s,
                other,
                "new.address@synthetic-univ.test",
                source="institutional_page",
                source_url="https://synthetic-univ.test/people",
                replace_current=True,
                now=world.clock.now(),
            )
        with pytest.raises(contact_emails.AddressError):
            contact_emails.add_address(
                s, pi, "not an address", source="institutional_page",
                source_url="https://x.test", replace_current=False, now=world.clock.now(),
            )  # fmt: skip


@pytest.mark.parametrize(
    "text,phd",
    [
        ("PhD student position in retinal imaging", True),
        ("Fully funded PhD studentship", True),
        ("Doctoral program in vision science", True),
        ("Graduate student opening", True),
        ("Postdoctoral Fellowship in Retinal Imaging", False),
        ("Research fellow (MD or PhD required)", False),
        ("PhD/MD postdoctoral position", False),
        ("Clinical Research Fellowship", False),
    ],
)
def test_phd_student_postings_are_recognised(text: str, phd: bool) -> None:
    assert tailoring.is_phd_posting(text) is phd


def test_phd_postings_are_never_offered_for_a_contact(world) -> None:
    camp = world.campaign(world.pi(world.institution()))
    pi_id = pi_of(world, camp)
    link_seed(world, pi_id, posting="PhD student position in ocular genetics")
    with transaction(world.factory) as s:
        assert tailoring.linked_postings(s, pi_id) == []


@pytest.mark.parametrize(
    "name,orcid,pubmed_name,expected",
    [
        ("Reza Shahriari", "", "", '("Shahriari, Reza"[fau]) OR ("Shahriari R"[au])'),
        ("Dr. Reza Shahriari, MD", "", "", '("Shahriari, Reza"[fau]) OR ("Shahriari R"[au])'),
        (
            "Alex B. Synthetic",
            "0000-0002-1825-0097",
            "Synthetic AB",
            '(0000-0002-1825-0097[auid]) OR ("Synthetic, Alex B."[fau]) OR ("Synthetic AB"[au])',
        ),
        ("Alex", "", "", ""),
    ],
)
def test_applicant_search_term(name: str, orcid: str, pubmed_name: str, expected: str) -> None:
    assert applicant_term(name, orcid, pubmed_name) == expected
