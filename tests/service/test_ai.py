"""Optional Claude suggestions: context, deterministic validation, C4 edits, usage, SDK call.

No real request is made: the service tests use a fake client and the SDK test replaces
``anthropic.Anthropic`` with a recording stand-in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from marcelorian.db.engine import transaction
from marcelorian.db.models import AuditEvent, Campaign, Evidence, Message
from marcelorian.services import ai, drafting, profile
from tests.fakes.ai import FakeAIClient
from tests.fakes.secrets import InMemorySecretStore
from tests.service.test_drafting import FACTS, campaign_for, confirm_facts

FIELDS = ["personalized_sentence", "skills_line", "contribution_line"]


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


def fact_id(world, key: str) -> int:
    with transaction(world.factory) as s:
        return profile.facts(s, world.applicant_id)[key].id


def answer(evidence_id: int, skills_fact: int, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "personalized_sentence": {
            "text": "Your recent work on OCT biomarkers is close to my own interests.",
            "evidence_ids": [evidence_id],
            "fact_ids": [],
        },
        "skills_line": {
            "text": "OCT image analysis",
            "evidence_ids": [],
            "fact_ids": [skills_fact],
        },
        "contribution_line": {
            "text": "your imaging studies",
            "evidence_ids": [evidence_id],
            "fact_ids": [],
        },
    }
    data.update(overrides)
    return data


def run(world, campaign_id: int, client: FakeAIClient, wanted=FIELDS) -> tuple[Any, ai.Result]:
    with transaction(world.factory) as s:
        ctx = ai.build_context(s, s.get(Campaign, campaign_id), 1, list(wanted))
    completion = ai.request(client, ctx)
    with transaction(world.factory) as s:
        return ctx, ai.validate(s, ctx, completion)


@pytest.fixture
def ready(world):
    confirm_facts(world, {**FACTS, "research_interests": "retinal imaging", "visa_status": "J-1"})
    campaign_id = campaign_for(world)
    return campaign_id, add_evidence(world, campaign_id), fact_id(world, "skills")


def test_valid_suggestions_kept_and_prompt_sends_only_shared_facts(world, ready) -> None:
    campaign_id, evidence_id, skills = ready
    client = FakeAIClient(answer(evidence_id, skills))
    ctx, result = run(world, campaign_id, client)
    assert sorted(result.suggestions) == sorted(FIELDS) and not result.problems
    system, prompt, schema = client.calls[0]
    assert "Synthetic OCT biomarkers" in prompt and "retinal imaging" in prompt
    assert "J-1" not in prompt and "Alex Synthetic" not in prompt  # visa and name stay local
    assert schema["additionalProperties"] is False and schema["required"] == list(ctx.fields)
    assert "Never invent" in system


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"text": "See https://example.org"}, "link"),
        ({"text": "As we discussed, your lab is great."}, "contact or promise"),
        ({"text": "Your {topic} work"}, "braces"),
        ({"text": "x" * (ai.MAX_LINE + 1)}, "too long"),
        ({"evidence_ids": []}, "did not cite the PI's evidence"),
        ({"evidence_ids": [999_999]}, "does not belong to this PI"),
    ],
)
def test_bad_personal_sentence_is_dropped(world, ready, override, reason) -> None:
    campaign_id, evidence_id, skills = ready
    data = answer(evidence_id, skills)
    data["personalized_sentence"] = {**data["personalized_sentence"], **override}
    _ctx, result = run(world, campaign_id, FakeAIClient(data))
    assert "personalized_sentence" not in result.suggestions
    assert "skills_line" in result.suggestions
    assert any(reason in p for p in result.problems)


def test_fact_changed_during_request_is_rejected(world, ready) -> None:
    campaign_id, evidence_id, skills = ready
    with transaction(world.factory) as s:
        ctx = ai.build_context(s, s.get(Campaign, campaign_id), 1, FIELDS)
    completion = ai.request(FakeAIClient(answer(evidence_id, skills)), ctx)
    confirm_facts(world, {"skills": "Something new"})  # new version while Claude was writing
    with transaction(world.factory) as s:
        result = ai.validate(s, ctx, completion)
    assert "skills_line" not in result.suggestions
    assert any("not confirmed or has changed" in p for p in result.problems)


def test_unreadable_answer_and_missing_evidence(world) -> None:
    confirm_facts(world)
    campaign_id = campaign_for(world)
    with transaction(world.factory) as s, pytest.raises(ai.AIError, match="no public evidence"):
        ai.build_context(s, s.get(Campaign, campaign_id), 1, FIELDS)
    add_evidence(world, campaign_id)
    _ctx, result = run(world, campaign_id, FakeAIClient("not json"))
    assert not result.suggestions and "could not be read" in result.problems[0]
    with transaction(world.factory) as s, pytest.raises(ai.AIError, match="already has text"):
        ai.build_context(s, s.get(Campaign, campaign_id), 1, [])


def test_c4_changed_line_becomes_applicants(world, ready) -> None:
    campaign_id, evidence_id, skills = ready
    _ctx, result = run(world, campaign_id, FakeAIClient(answer(evidence_id, skills)))
    personal = {k: s.text for k, s in result.suggestions.items()}
    with transaction(world.factory) as s:
        campaign = s.get(Campaign, campaign_id)
        message = drafting.save(
            s,
            campaign,
            1,
            personal,
            {k: "ai" for k in personal},
            body_override=None,
            specialty_display_name="Ophthalmology",
            now=world.clock.now(),
            ai_basis=ai.basis(result),
        )
        message_id = message.id
    edited = {**personal, "skills_line": "OCT analysis and statistics"}
    with transaction(world.factory) as s:
        previous = s.get(Message, message_id)
        sources, basis = drafting.carry_forward(previous, edited)
        assert sources["skills_line"] == "applicant" and "skills_line" not in basis
        assert sources["personalized_sentence"] == "ai" and "personalized_sentence" in basis
        drafting.save(
            s,
            s.get(Campaign, campaign_id),
            1,
            edited,
            sources,
            body_override=None,
            specialty_display_name="Ophthalmology",
            now=world.clock.now(),
            ai_basis=basis,
        )
    with transaction(world.factory) as s:
        fields = drafting.draft_fields(s.get(Message, message_id))
    assert fields["sources"]["skills_line"] == "applicant"
    assert set(fields["ai"]) == {"personalized_sentence", "contribution_line"}


def test_usage_recorded_and_estimated(world, ready) -> None:
    campaign_id, evidence_id, skills = ready
    ctx, result = run(world, campaign_id, FakeAIClient(answer(evidence_id, skills)))
    with transaction(world.factory) as s:
        ai.record_usage(s, ctx, ai.Completion("{}", 1200, 300), result, now=world.clock.now())
    with transaction(world.factory) as s:
        usage = ai.usage(s)
        detail = json.loads(s.query(AuditEvent).filter_by(action="ai_suggestion").one().detail_json)
    assert (usage.requests, usage.input_tokens, usage.output_tokens) == (1, 1200, 300)
    assert usage.estimated_usd == pytest.approx((1200 * 4 + 300 * 20) / 1_000_000)
    assert "text" not in json.dumps(detail)  # usage only, never the suggested text


def test_key_check_and_store(world) -> None:
    store = InMemorySecretStore()
    client = FakeAIClient()
    with pytest.raises(ai.AIError, match="does not look like"):
        ai.check_and_store_key(store, lambda key: client, "hello")
    ai.check_and_store_key(store, lambda key: client, " sk-ant-synthetic-key-0000 ")
    assert store.get(ai.KEY_NAME) == "sk-ant-synthetic-key-0000" and client.key_checks == 1
    bad = FakeAIClient(error="The API key was not accepted.")
    other = InMemorySecretStore()
    with pytest.raises(ai.AIError, match="not accepted"):
        ai.check_and_store_key(other, lambda key: bad, "sk-ant-synthetic-key-1111")
    assert other.get(ai.KEY_NAME) is None
    with transaction(world.factory) as s:
        assert not ai.key_saved(s)
        ai.record_key(s, True, now=world.clock.now())
    with transaction(world.factory) as s:
        assert ai.key_saved(s)
        events = s.query(AuditEvent).filter_by(action="ai_key_saved").all()
        assert events and all("sk-ant" not in (e.detail_json or "") for e in events)


# --------------------------------------------------------------------------- #
# The SDK call itself, against a recording stand-in (no network)
# --------------------------------------------------------------------------- #


class _Recorder:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        return self.response


def _sdk_client(monkeypatch, response: Any) -> tuple[ai.AnthropicClient, _Recorder]:
    import anthropic

    recorder = _Recorder(response)
    retrieved: list[str] = []

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["api_key"] == "sk-ant-synthetic-key-0000"
            self.beta = SimpleNamespace(messages=recorder)
            self.models = SimpleNamespace(retrieve=retrieved.append)

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    client = ai.AnthropicClient("sk-ant-synthetic-key-0000")
    client.check_key()
    assert retrieved == [ai.MODEL]
    return client, recorder


def _response(stop_reason: str, text: str = "{}") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, iterations=None),
    )


def test_sdk_request_shape_uses_fallback_and_structured_output(monkeypatch) -> None:
    client, recorder = _sdk_client(monkeypatch, _response("end_turn", '{"a": 1}'))
    completion = client.complete("system", "prompt", {"type": "object"})
    kwargs = recorder.kwargs
    assert kwargs["model"] == "claude-opus-5-5"
    assert kwargs["betas"] == ["server-side-fallback-2026-07-01"]
    assert kwargs["fallbacks"] == "default"
    assert kwargs["output_config"]["format"] == {
        "type": "json_schema",
        "schema": {"type": "object"},
    }
    assert kwargs["output_config"]["effort"] == "medium" and "thinking" not in kwargs
    assert completion.text == '{"a": 1}' and completion.input_tokens == 10


@pytest.mark.parametrize(("stop", "message"), [("refusal", "declined"), ("max_tokens", "cut off")])
def test_sdk_refusal_and_truncation_are_plain_errors(monkeypatch, stop, message) -> None:
    client, _ = _sdk_client(monkeypatch, _response(stop))
    with pytest.raises(ai.AIError, match=message):
        client.complete("system", "prompt", {"type": "object"})
