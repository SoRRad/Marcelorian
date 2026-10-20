"""Every email is tailored to the PI's research or a confirmed posted position (T1–T4)."""

from __future__ import annotations

import io
import json

import pytest
from sqlalchemy import select

from marcelorian.db.engine import transaction
from marcelorian.db.models import Campaign, Message, ResearchRecord
from marcelorian.domain.enums import Mode
from marcelorian.domain.seed import read_seed
from marcelorian.services import drafting, tailoring
from marcelorian.services.research_seed import import_seed
from tests.seed_fixture import contact, csv_text, opportunity
from tests.service.test_drafting import add_evidence, campaign_for, checks, confirm_facts, save

TOPIC_LINE = {
    "personalized_sentence": "Your adaptive optics imaging of photoreceptors fits my training.",
    "skills_line": "OCT image analysis and Python",
    "contribution_line": "your imaging projects",
}
GENERIC_LINE = {**TOPIC_LINE, "personalized_sentence": "I admire your excellent work."}


def link_seed(world, pi_id: int, *, topics: str = "", posting: str = "") -> int | None:
    """Stage a catalog contact for this PI (optionally with a linked posting)."""
    rows = [
        contact(
            "C-TAIL1",
            research_topics_verified=topics,
            related_record_ids_json=json.dumps(["O-TAIL1"] if posting else []),
        )
    ]
    if posting:
        rows.append(
            opportunity("O-TAIL1", name=posting, related_record_ids_json=json.dumps(["C-TAIL1"]))
        )
    with transaction(world.factory) as s:
        import_seed(
            s,
            read_seed(io.StringIO(csv_text(rows), newline="")),
            source_name="synthetic.csv",
            file_sha256="0" * 64,
            now=world.clock.now(),
        )
        records = {r.external_record_id: r for r in s.scalars(select(ResearchRecord))}
        records["C-TAIL1"].promoted_pi_id = pi_id
        return records["O-TAIL1"].id if posting else None


def pi_of(world, campaign_id: int) -> int:
    with transaction(world.factory) as s:
        return s.get(Campaign, campaign_id).pi_id


def by_key(world, message_id: int) -> dict[str, drafting.Check]:
    return checks(world, message_id)


def test_words_skip_generic_terms_and_stem_lightly() -> None:
    assert tailoring.words("A novel study of Glaucomatous optic neuropathy") == [
        "glaucomatous",
        "optic",
        "neuropathy",
    ]
    assert tailoring._key("glaucomatous") == tailoring._key("glaucoma")


def test_line_naming_a_publication_topic_passes(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    add_evidence(world, camp, "Adaptive optics imaging of cone photoreceptors")
    result = by_key(world, save(world, camp, TOPIC_LINE))
    assert result["tailored"].ok and "adaptive" in result["tailored"].detail
    assert result["not_reused"].ok
    assert drafting.ready_to_approve(list(result.values()))


def test_verified_topic_counts_but_generic_line_does_not(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    link_seed(world, pi_of(world, camp), topics="Adaptive optics; glaucoma")
    assert by_key(world, save(world, camp, TOPIC_LINE))["tailored"].ok
    generic = by_key(world, save(world, camp, GENERIC_LINE))["tailored"]
    assert not generic.ok and "does not mention" in generic.detail


def test_no_interests_on_file_says_what_to_do(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    tailored = by_key(world, save(world, camp, TOPIC_LINE))["tailored"]
    assert not tailored.ok and "free lookup" in tailored.detail


def test_practice_warns_but_live_blocks(world) -> None:
    confirm_facts(world)
    practice = by_key(world, save(world, campaign_for(world), GENERIC_LINE))
    assert practice["tailored"].warn_only and drafting.ready_to_approve(list(practice.values()))
    live = by_key(world, save(world, campaign_for(world, Mode.LIVE), GENERIC_LINE))
    assert not live["tailored"].warn_only
    assert not drafting.ready_to_approve(list(live.values()))


def test_reused_line_for_another_pi_is_flagged(world) -> None:
    confirm_facts(world)
    first, second = campaign_for(world), campaign_for(world)
    for camp in (first, second):
        add_evidence(world, camp, "Adaptive optics imaging")
    save(world, first, TOPIC_LINE)
    reused = by_key(world, save(world, second, TOPIC_LINE))["not_reused"]
    assert not reused.ok and "same as the email to" in reused.detail
    nearly = {**TOPIC_LINE, "personalized_sentence": TOPIC_LINE["personalized_sentence"][:-1]}
    assert not by_key(world, save(world, second, nearly))["not_reused"].ok


def test_confirmed_posting_is_named_in_email_one(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    pi_id = pi_of(world, camp)
    record_id = link_seed(world, pi_id, posting="Postdoctoral Fellowship in Retinal Imaging")
    assert record_id is not None
    with transaction(world.factory) as s:
        postings = tailoring.linked_postings(s, pi_id)
        assert [(p.title, p.confirmed) for p in postings] == [
            ("Postdoctoral Fellowship in Retinal Imaging", False)
        ]
    message_id = save(world, camp, GENERIC_LINE)
    assert "expects an opening" in world.message(message_id).draft_body
    with transaction(world.factory) as s:
        tailoring.confirm_posting(s, pi_id, record_id, now=world.clock.now())
    message_id = save(world, camp, GENERIC_LINE)
    body = world.message(message_id).draft_body
    assert "“Postdoctoral Fellowship in Retinal Imaging”" in body
    assert "expects an opening" not in body
    result = by_key(world, message_id)
    assert result["position_named"].ok
    assert result["tailored"].ok and "posted position" in result["tailored"].detail
    with transaction(world.factory) as s:
        tailoring.withdraw_posting(s, pi_id, record_id, now=world.clock.now())
    assert "expects an opening" in world.message(save(world, camp, GENERIC_LINE)).draft_body


def test_custom_template_without_the_position_fails_the_position_check(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world, Mode.LIVE)
    pi_id = pi_of(world, camp)
    record_id = link_seed(world, pi_id, posting="Synthetic Retina Fellowship")
    with transaction(world.factory) as s:
        tailoring.confirm_posting(s, pi_id, record_id, now=world.clock.now())
        drafting.set_template(
            s,
            1,
            drafting.DEFAULT_TEMPLATES[1].replace("{opening_line}", "I am writing to you."),
            now=world.clock.now(),
        )
    named = by_key(world, save(world, camp, TOPIC_LINE))["position_named"]
    assert not named.ok and not named.warn_only


def test_unlinked_posting_cannot_be_confirmed(world) -> None:
    camp = campaign_for(world)
    with transaction(world.factory) as s, pytest.raises(tailoring.TailoringError):
        tailoring.confirm_posting(s, pi_of(world, camp), 99999, now=world.clock.now())


def test_follow_ups_without_a_pi_line_are_not_checked(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)
    message_id = save(world, camp, TOPIC_LINE)
    with transaction(world.factory) as s:
        message = s.get(Message, message_id)
        message.step = 3  # Email 3 has no line about the PI
        assert not tailoring.assess(s, message, TOPIC_LINE).applies


def test_practice_text_names_a_pi_topic(world) -> None:
    camp = campaign_for(world)
    add_evidence(world, camp, "Adaptive optics imaging of cone photoreceptors in glaucoma")
    with transaction(world.factory) as s:
        topic = tailoring.practice_topic(s, pi_of(world, camp))
    text = drafting.practice_text(topic)
    assert topic and topic in text["personalized_sentence"]
    assert text["personalized_sentence"].startswith(drafting.PRACTICE_PREFIX)
    assert drafting.practice_text(None) == drafting.CANNED


def test_opening_line_wording() -> None:
    from types import SimpleNamespace

    assert drafting.opening_line(None, "July 2027").endswith("position starting July 2027.")
    named = drafting.opening_line(SimpleNamespace(title="Retina Fellowship"), "July 2027")
    assert named == (
        "I am writing regarding the posted opportunity “Retina Fellowship”, and I would be "
        "glad to be considered for a start in July 2027."
    )


def test_naming_the_pi_or_broad_field_words_is_not_tailoring(world) -> None:
    confirm_facts(world)
    camp = campaign_for(world)  # the PI is "Pat Example"
    add_evidence(world, camp, "Example retinal imaging in disease")
    line = {**TOPIC_LINE, "personalized_sentence": "Dr Example's retinal imaging work is great."}
    tailored = by_key(world, save(world, camp, line))["tailored"]
    assert not tailored.ok
