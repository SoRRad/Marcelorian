"""Drafting: template filling from confirmed facts, checklist, practice text, templates."""

from __future__ import annotations

import pytest

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Evidence, Message
from marcelorian.domain.enums import Actor, MessageState, Mode
from marcelorian.services import approvals, drafting, profile
from tests.factories import SPECIALTY

FACTS = {
    "full_name": "Alex Synthetic",
    "current_role_phrase": "a research fellow at the Synthetic Eye Institute",
    "earliest_start": "2027-07",
    "funding_preference": "paid_preferred",
    "skills": "OCT image analysis",
}
PERSONAL = {
    "personalized_sentence": "Your work on synthetic OCT biomarkers caught my attention.",
    "skills_line": "OCT image analysis and Python",
    "contribution_line": "your imaging projects",
}


def confirm_facts(world, facts=FACTS, *, confirm: bool = True) -> None:
    with transaction(world.factory) as s:
        for key, value in facts.items():
            profile.set_fact(
                s,
                world.applicant_id,
                key,
                value,
                confirm=confirm,
                actor=Actor.APPLICANT,
                now=world.clock.now(),
            )


def add_evidence(world, campaign_id: int, title: str = "Synthetic OCT biomarkers") -> int:
    with transaction(world.factory) as s:
        pi_id = s.get(Campaign, campaign_id).pi_id
        item = Evidence(
            pi_id=pi_id,
            source="pubmed",
            external_id=f"pmid-{title[:8]}",
            title=title,
            venue="Synthetic Journal",
            published_on="2026-01-01",
            url="https://example.org/paper",
            retrieved_at=world.clock.now(),
        )
        s.add(item)
        s.flush()
        return item.id


def campaign_for(world, mode: Mode = Mode.SANDBOX) -> int:
    pi = world.pi(world.institution(), name="Pat Example")
    return world.campaign(pi, mode)


def save(world, campaign_id: int, personal=PERSONAL, sources=None, body=None, step=1) -> int:
    with transaction(world.factory) as s:
        campaign = s.get(Campaign, campaign_id)
        return drafting.save(
            s,
            campaign,
            step,
            dict(personal),
            sources or {},
            body_override=body,
            specialty_display_name=SPECIALTY,
            now=world.clock.now(),
        ).id


def checks(world, message_id: int) -> dict[str, drafting.Check]:
    with transaction(world.factory) as s:
        message = s.get(Message, message_id)
        return {
            c.key: c
            for c in drafting.checklist(
                s, message, specialty_display_name=SPECIALTY, today=world.clock.now().date()
            )
        }


def test_email1_is_filled_from_confirmed_facts(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    with transaction(world.factory) as s:
        rendered = drafting.render(
            s, s.get(Campaign, camp), 1, PERSONAL, specialty_display_name=SPECIALTY
        )
    body = rendered.body
    assert body.startswith("Dear Dr. ")
    assert (
        "My name is Alex Synthetic, and I am a research fellow at the Synthetic Eye Institute."
        in body
    )
    assert "July 2027" not in body and "starting" not in body  # no start date (2026-10-27)
    assert "has, or expects, an opening for a research fellow" in body
    assert "Your work on synthetic OCT biomarkers caught my attention." in body
    assert body.rstrip().endswith("Best regards,\nAlex Synthetic")
    assert "{" not in body and "\n\n\n" not in body  # empty funding line removed
    assert rendered.missing == [] and rendered.cv_ids == [world.cv_id]
    assert rendered.subject.startswith("Research Position Application – ")


def test_duration_and_funding_line_only_when_confirmed(world) -> None:
    confirm_facts(world, {**FACTS, "funding_line": "I have support for my first year."})
    confirm_facts(world, {"desired_duration": "one year"}, confirm=False)
    camp = campaign_for(world)
    with transaction(world.factory) as s:
        body = drafting.render(
            s, s.get(Campaign, camp), 1, PERSONAL, specialty_display_name=SPECIALTY
        ).body
    assert "I have support for my first year." in body
    with transaction(world.factory) as s:  # a custom template may still name availability
        drafting.set_template(
            s,
            1,
            drafting.DEFAULT_TEMPLATES[1].replace(
                "{opening_line}", "I could start in {availability}."
            ),
            now=world.clock.now(),
        )
        body = drafting.render(
            s, s.get(Campaign, camp), 1, PERSONAL, specialty_display_name=SPECIALTY
        ).body
    assert "I could start in July 2027." in body  # the unconfirmed duration is left out
    confirm_facts(world, {"desired_duration": "one year"})
    with transaction(world.factory) as s:
        body = drafting.render(
            s, s.get(Campaign, camp), 1, PERSONAL, specialty_display_name=SPECIALTY
        ).body
    assert "I could start in July 2027 for one year." in body


def test_unconfirmed_facts_block_the_checklist_and_approval(world) -> None:
    confirm_facts(world, confirm=False)
    camp = campaign_for(world)
    message_id = save(world, camp)
    result = checks(world, message_id)
    assert not result["facts"].ok and "Full name" in result["facts"].detail
    assert not drafting.ready_to_approve(list(result.values()))


def test_save_review_and_approve_in_practice_mode(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    add_evidence(world, camp)  # the PI's work, so the personal line is tailored
    message_id = save(world, camp)
    message = world.message(message_id)
    assert message.state == MessageState.AWAITING_APPROVAL.value
    assert drafting.draft_fields(message)["sources"]["skills_line"] == "applicant"
    result = checks(world, message_id)
    assert all(c.ok for c in result.values()), [c for c in result.values() if not c.ok]
    world.approve(message_id)
    assert world.message(message_id).state == MessageState.APPROVED_QUEUED.value


def test_whole_body_edit_is_checked_again(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    message_id = save(world, camp)
    edited = world.message(message_id).draft_body.replace("Dear Dr.", "Hello {name}")
    message_id = save(world, camp, body=edited)
    message = world.message(message_id)
    assert drafting.draft_fields(message)["body_edited"] is True
    result = checks(world, message_id)
    assert not result["no_braces"].ok and not result["salutation"].ok


def test_practice_text_is_fine_in_sandbox_but_never_in_live(world) -> None:
    confirm_facts(world)
    sandbox = campaign_for(world)
    message_id = save(
        world, sandbox, personal=drafting.CANNED, sources={k: "practice" for k in drafting.CANNED}
    )
    assert checks(world, message_id)["practice_text"].ok
    live = world.campaign(world.pi(world.institution()), Mode.LIVE)
    live_message = save(world, live, personal=drafting.CANNED)
    assert not checks(world, live_message)["practice_text"].ok


def test_updated_cv_wording_needs_a_newer_cv(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    save(world, camp)
    with transaction(world.factory) as s:
        rendered = drafting.render(
            s, s.get(Campaign, camp), 4, {}, specialty_display_name=SPECIALTY
        )
    assert "attached my CV" in rendered.body
    with transaction(world.factory) as s:
        approvals.upload_cv(
            s,
            applicant_id=world.applicant_id,
            filename="cv2.pdf",
            content_type="application/pdf",
            data=b"%PDF-1.4 v2",
            actor=Actor.APPLICANT,
            now=world.clock.now(),
        )
        rendered = drafting.render(
            s, s.get(Campaign, camp), 4, {}, specialty_display_name=SPECIALTY
        )
    assert "attached my updated CV" in rendered.body


def test_updated_cv_claim_without_a_newer_cv_fails(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    message_id = save(world, camp)
    body = world.message(message_id).draft_body.replace("my CV", "my updated CV")
    message_id = save(world, camp, body=body)
    assert not checks(world, message_id)["updated_cv"].ok


def test_template_editing_rules(world) -> None:
    confirm_facts(world)
    with transaction(world.factory) as s:
        for bad in ("Hi {nickname}", "Dear {salutation}, {", "No salutation here"):
            with pytest.raises(drafting.DraftError):
                drafting.set_template(s, 2, bad, now=world.clock.now())
        drafting.set_template(
            s,
            2,
            "Dear {salutation},\n\nJust checking in on {pi_topic}.\n\n{signature_name}",
            now=world.clock.now(),
        )
        assert "Just checking in" in drafting.template(s, 2)
        assert drafting.personal_fields_for(s, 2) == ["pi_topic"]
        drafting.set_template(s, 2, "", now=world.clock.now())
        assert drafting.template(s, 2) == drafting.DEFAULT_TEMPLATES[2]


def test_draftable_step_and_campaign_creation(world) -> None:
    pi = world.pi(world.institution())
    with transaction(world.factory) as s:
        campaign = drafting.ensure_campaign(
            s, world.applicant_id, pi, Mode.SANDBOX, world.clock.now()
        )
        again = drafting.ensure_campaign(s, world.applicant_id, pi, Mode.SANDBOX, world.clock.now())
        assert campaign.id == again.id
        assert drafting.draftable_step(s, campaign, world.clock.now()) == 1


def test_month_year() -> None:
    assert drafting.month_year("2027-01") == "January 2027"
    assert drafting.month_year("2026-12") == "December 2026"
