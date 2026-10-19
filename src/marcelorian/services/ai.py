"""Optional Claude suggestions for the personal lines of a draft (spec §8; owner decision U2).

* Off unless the applicant saves an Anthropic API key. The key lives in the OS credential
  store (never in the database); the database keeps only a "key saved" flag so pages never
  touch the credential store. Saving checks the key with a model lookup, which creates
  no text and spends no credit.
* A suggestion is one request: only the selected profile answers (research interests,
  skills, role, degrees, tone) and the PI's public evidence (publication titles, venues,
  dates) are sent. Visa, training status, contact details and email text are never sent.
* Every suggested line must cite the evidence or confirmed facts it rests on. The app
  checks each line deterministically (cited ids exist for this PI / are confirmed at the
  current version, no braces, links, addresses or relationship claims, length) and drops
  lines that fail, so the applicant writes those. Suggestions are never approved
  automatically: they are saved as a draft the applicant reads and edits (C4: a line the
  applicant changes becomes their own).
* Refusal fallback is on (``fallbacks="default"``): if the model declines, the API reruns
  the request on Anthropic's recommended fallback model; if the whole chain declines, the
  applicant is told and writes the lines.
* Token usage is recorded in the audit log for the cost estimate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from marcelorian.db.models import AuditEvent, Campaign, Pi, Setting
from marcelorian.domain.enums import Actor, FactStatus
from marcelorian.lookups.enrich import evidence_for
from marcelorian.platform.secrets import SecretStore
from marcelorian.services import drafting, profile
from marcelorian.services.audit import audit

MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
KEY_NAME = "anthropic_api_key"
KEY_FLAG = "ai_key_saved"
MAX_TOKENS = 8000
MAX_EVIDENCE = 25
MAX_LINE = 300
#: USD per million tokens (input, output) for the model above; used only for estimates.
PRICE_PER_MTOK = (4.0, 20.0)
#: Profile answers that may be sent. Everything else stays on this computer.
SHARED_FACTS = (
    "current_role_phrase",
    "degrees",
    "research_interests",
    "subspecialty_interests",
    "skills",
    "tone",
)
#: Fields whose text is about the PI's work and so must cite evidence.
NEEDS_EVIDENCE = frozenset({"personalized_sentence", "pi_topic"})
#: Fields whose text is about the applicant and so must cite a confirmed fact.
NEEDS_FACT = frozenset({"skills_line"})
_URL = re.compile(r"https?://|www\.", re.IGNORECASE)
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
BANNED_PHRASES = (
    "as we discussed",
    "as discussed",
    "we met",
    "when we met",
    "you told me",
    "your email",
    "your recent message",
    "our conversation",
    "guarantee",
    "i have worked with you",
    "i worked in your lab",
)

SYSTEM_PROMPT = (
    "You help a physician write short personal lines for a polite email to a US "
    "ophthalmology researcher, asking about research fellow openings. You receive the "
    "researcher's public publications and the applicant's own profile answers, each with "
    "an id. Write each requested line in plain, modest, specific English. Only state what "
    "the cited items support: name a topic from the researcher's work using the evidence, "
    "and describe the applicant only from their profile answers. Never invent results, "
    "numbers, meetings, prior contact, shared colleagues or publications. Do not use "
    "links, email addresses, quotation of titles longer than ten words, flattery, or "
    "placeholders in braces. Each line completes the sentence shown in its description, "
    "so it must read naturally inside that sentence. Cite the ids you used."
)


class AIError(RuntimeError):
    """A plain-language reason the suggestion or key check did not work."""


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Completion:
    text: str
    input_tokens: int
    output_tokens: int
    fallback_used: bool = False


class AIClient(Protocol):
    def check_key(self) -> None: ...

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> Completion: ...


ClientFactory = Callable[[str], AIClient]


class AnthropicClient:
    """The official SDK, used only after the applicant saves a key and clicks a button."""

    def __init__(self, api_key: str) -> None:
        import anthropic

        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=2, timeout=90.0)

    def check_key(self) -> None:
        try:
            self._client.models.retrieve(MODEL)
        except Exception as exc:
            raise AIError(_explain_sdk_error(self._sdk, exc)) from exc

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> Completion:
        try:
            response = self._client.beta.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": schema},
                },
            )
        except Exception as exc:
            raise AIError(_explain_sdk_error(self._sdk, exc)) from exc
        if response.stop_reason == "refusal":
            raise AIError("Claude declined to write these lines. Please write them yourself.")
        if response.stop_reason == "max_tokens":
            raise AIError("Claude's answer was cut off. Try again, or write the lines yourself.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        iterations = getattr(response.usage, "iterations", None) or []
        return Completion(
            text=text,
            input_tokens=int(response.usage.input_tokens or 0),
            output_tokens=int(response.usage.output_tokens or 0),
            fallback_used=any(getattr(i, "type", "") == "fallback_message" for i in iterations),
        )


def _explain_sdk_error(sdk: Any, exc: Exception) -> str:
    if isinstance(exc, sdk.AuthenticationError):
        return "The API key was not accepted. Check it in Anthropic's console and save it again."
    if isinstance(exc, sdk.PermissionDeniedError):
        return "This API key is not allowed to use the model. Check the key's workspace."
    if isinstance(exc, sdk.NotFoundError):
        return "The model is not available to this API key."
    if isinstance(exc, sdk.RateLimitError):
        return "Anthropic says too many requests or no credit left. Wait, or check billing."
    if isinstance(exc, sdk.APIStatusError):
        return f"Anthropic returned an error ({exc.status_code}). Try again later."
    if isinstance(exc, sdk.APIConnectionError):
        return "Could not reach Anthropic. Check the internet connection."
    return "Claude is unavailable right now. Write the lines yourself."


# --------------------------------------------------------------------------- #
# Key
# --------------------------------------------------------------------------- #


def key_saved(session: Session) -> bool:
    row = session.get(Setting, KEY_FLAG)
    return bool(row is not None and json.loads(row.value_json))


def _set_flag(session: Session, value: bool, now: datetime) -> None:
    row = session.get(Setting, KEY_FLAG)
    if row is None:
        session.add(Setting(key=KEY_FLAG, value_json=json.dumps(value), updated_at=now))
    else:
        row.value_json, row.updated_at = json.dumps(value), now


def check_and_store_key(store: SecretStore, factory: ClientFactory, key: str) -> None:
    """Network and credential store only; call outside a database transaction."""
    key = key.strip()
    if not key.startswith("sk-ant-") or len(key) < 20 or any(c.isspace() for c in key):
        raise AIError("That does not look like an Anthropic API key (it starts with sk-ant-).")
    factory(key).check_key()
    store.set(KEY_NAME, key)


def record_key(session: Session, saved: bool, *, now: datetime) -> None:
    _set_flag(session, saved, now)
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="setting",
        entity_id=KEY_FLAG,
        action="ai_key_saved" if saved else "ai_key_removed",
    )


def load_key(store: SecretStore) -> str:
    key = store.get(KEY_NAME)
    if not key:
        raise AIError("No API key is saved. Add one in Settings to use Claude suggestions.")
    return key


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceItem:
    id: int
    title: str
    venue: str
    published_on: str
    url: str


@dataclass(frozen=True)
class FactItem:
    id: int
    key: str
    value: str
    version: int


@dataclass(frozen=True)
class SuggestContext:
    campaign_id: int
    pi_id: int
    pi_name: str
    step: int
    fields: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    facts: tuple[FactItem, ...]


@dataclass(frozen=True)
class Suggestion:
    text: str
    evidence_ids: tuple[int, ...]
    fact_ids: tuple[int, ...]


@dataclass
class Result:
    suggestions: dict[str, Suggestion] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def build_context(
    session: Session, campaign: Campaign, step: int, wanted: list[str]
) -> SuggestContext:
    pi = session.get(Pi, campaign.pi_id)
    assert pi is not None
    needed = drafting.personal_fields_for(session, step)
    fields = tuple(k for k in needed if k in wanted)
    if not fields:
        raise AIError("Every personal line already has text. Clear a line to get a suggestion.")
    evidence = tuple(
        EvidenceItem(e.id, e.title, e.venue, e.published_on, e.url)
        for e in evidence_for(session, pi.id)[:MAX_EVIDENCE]
    )
    if any(f in NEEDS_EVIDENCE for f in fields) and not evidence:
        raise AIError(
            "There is no public evidence for this PI yet. Run the free lookups on the "
            "contact page first, or write the lines yourself."
        )
    facts = profile.facts(session, campaign.applicant_id)
    shared = tuple(
        FactItem(f.id, key, str(f.value), f.version)
        for key in SHARED_FACTS
        if (f := facts.get(key)) is not None and f.status == FactStatus.CONFIRMED.value and f.value
    )
    return SuggestContext(
        campaign_id=campaign.id,
        pi_id=pi.id,
        pi_name=pi.full_name,
        step=step,
        fields=fields,
        evidence=evidence,
        facts=shared,
    )


def schema_for(fields: tuple[str, ...]) -> dict[str, Any]:
    line = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "integer"}},
            "fact_ids": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["text", "evidence_ids", "fact_ids"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {name: line for name in fields},
        "required": list(fields),
        "additionalProperties": False,
    }


def prompt_for(ctx: SuggestContext) -> str:
    evidence = [
        {"id": e.id, "title": e.title, "venue": e.venue, "date": e.published_on}
        for e in ctx.evidence
    ]
    facts = [{"id": f.id, "question": f.key, "answer": f.value} for f in ctx.facts]
    lines = {name: drafting.PERSONAL_FIELDS[name] for name in ctx.fields}
    return (
        f"Researcher: {ctx.pi_name}\n"
        f"Email number in the sequence: {ctx.step}\n\n"
        f"Researcher's public evidence (JSON):\n{json.dumps(evidence, ensure_ascii=False)}\n\n"
        f"Applicant's profile answers (JSON):\n{json.dumps(facts, ensure_ascii=False)}\n\n"
        f"Write these lines (JSON, name: what the line completes):\n"
        f"{json.dumps(lines, ensure_ascii=False)}\n\n"
        "Lines about the researcher's work must cite at least one evidence id; lines about "
        "the applicant must cite at least one profile answer id. Keep each line under "
        f"{MAX_LINE} characters."
    )


def request(client: AIClient, ctx: SuggestContext) -> Completion:
    """The one network call; run it outside any database transaction."""
    return client.complete(SYSTEM_PROMPT, prompt_for(ctx), schema_for(ctx.fields))


def line_problem(text: str) -> str | None:
    lower = text.casefold()
    if not text:
        return "was empty"
    if len(text) > MAX_LINE:
        return "was too long"
    if "{" in text or "}" in text:
        return "contained braces"
    if _URL.search(text) or _EMAIL.search(text):
        return "contained a link or address"
    if drafting.PRACTICE_PREFIX.strip().casefold() in lower:
        return "contained practice text"
    if any(phrase in lower for phrase in BANNED_PHRASES):
        return "claimed a contact or promise that the app cannot check"
    return None


def validate(session: Session, ctx: SuggestContext, completion: Completion) -> Result:
    """Keep only lines that pass every deterministic check, against fresh database state."""
    result = Result()
    try:
        data = json.loads(completion.text)
    except ValueError:
        result.problems.append("Claude's answer could not be read. Write the lines yourself.")
        return result
    if not isinstance(data, dict):
        result.problems.append("Claude's answer could not be read. Write the lines yourself.")
        return result
    evidence_ids = {e.id for e in evidence_for(session, ctx.pi_id)}
    facts = {f.id: f for f in profile.facts(session, _applicant_id(session, ctx)).values()}
    current = {
        f.id
        for f in ctx.facts
        if (live := facts.get(f.id)) is not None
        and live.version == f.version
        and live.status == FactStatus.CONFIRMED.value
    }
    for name in ctx.fields:
        label = drafting.PERSONAL_FIELDS[name]
        item = data.get(name)
        if not isinstance(item, dict):
            result.problems.append(f"No suggestion for “{label}”.")
            continue
        text = " ".join(str(item.get("text", "")).split())
        cited_evidence = _ids(item.get("evidence_ids"))
        cited_facts = _ids(item.get("fact_ids"))
        problem = line_problem(text)
        if problem is None and not set(cited_evidence) <= evidence_ids:
            problem = "cited evidence that does not belong to this PI"
        if problem is None and not set(cited_facts) <= current:
            problem = "cited a profile answer that is not confirmed or has changed"
        if problem is None and name in NEEDS_EVIDENCE and not cited_evidence:
            problem = "did not cite the PI's evidence"
        if problem is None and name in NEEDS_FACT and not cited_facts:
            problem = "did not cite your profile answers"
        if problem is None and not cited_evidence and not cited_facts:
            problem = "cited nothing"
        if problem is not None:
            result.problems.append(f"The suggestion for “{label}” {problem}, so it was left out.")
            continue
        result.suggestions[name] = Suggestion(text, cited_evidence, cited_facts)
    return result


def _ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict.fromkeys(v for v in value if isinstance(v, int) and not isinstance(v, bool)))


def _applicant_id(session: Session, ctx: SuggestContext) -> int:
    campaign = session.get(Campaign, ctx.campaign_id)
    assert campaign is not None
    return campaign.applicant_id


def record_usage(
    session: Session, ctx: SuggestContext, completion: Completion, result: Result, *, now: datetime
) -> None:
    audit(
        session,
        at=now,
        actor=Actor.APPLICANT,
        entity_type="campaign",
        entity_id=ctx.campaign_id,
        action="ai_suggestion",
        detail={
            "model": MODEL,
            "input_tokens": completion.input_tokens,
            "output_tokens": completion.output_tokens,
            "fallback_used": completion.fallback_used,
            "fields": list(ctx.fields),
            "kept": sorted(result.suggestions),
        },
    )


@dataclass(frozen=True)
class Usage:
    requests: int
    input_tokens: int
    output_tokens: int

    @property
    def estimated_usd(self) -> float:
        price_in, price_out = PRICE_PER_MTOK
        return (self.input_tokens * price_in + self.output_tokens * price_out) / 1_000_000


def usage(session: Session) -> Usage:
    requests = input_tokens = output_tokens = 0
    for detail in session.scalars(
        select(AuditEvent.detail_json).where(AuditEvent.action == "ai_suggestion")
    ):
        try:
            data = json.loads(detail or "{}")
        except ValueError:
            continue
        requests += 1
        input_tokens += int(data.get("input_tokens", 0) or 0)
        output_tokens += int(data.get("output_tokens", 0) or 0)
    return Usage(requests, input_tokens, output_tokens)


def basis(result: Result) -> dict[str, dict[str, Any]]:
    """What draft_fields_json keeps per suggested line, for the review page and C4."""
    return {
        name: {
            "text": s.text,
            "evidence_ids": list(s.evidence_ids),
            "fact_ids": list(s.fact_ids),
        }
        for name, s in result.suggestions.items()
    }
