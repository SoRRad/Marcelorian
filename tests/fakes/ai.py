"""Fake Claude client for tests: no network, no key, no credit."""

from __future__ import annotations

import json
from typing import Any

from marcelorian.services.ai import AIError, Completion


class FakeAIClient:
    def __init__(self, answer: dict[str, Any] | str | None = None, *, error: str = "") -> None:
        self.answer = answer
        self.error = error
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.key_checks = 0

    def check_key(self) -> None:
        self.key_checks += 1
        if self.error:
            raise AIError(self.error)

    def complete(self, system: str, prompt: str, schema: dict[str, Any]) -> Completion:
        self.calls.append((system, prompt, schema))
        if self.error:
            raise AIError(self.error)
        text = self.answer if isinstance(self.answer, str) else json.dumps(self.answer or {})
        return Completion(text=text, input_tokens=1200, output_tokens=300)


class FakeAIFactory:
    """Stands in for ``AnthropicClient``: records the keys it was given."""

    def __init__(self, client: FakeAIClient) -> None:
        self.client = client
        self.keys: list[str] = []

    def __call__(self, key: str) -> FakeAIClient:
        self.keys.append(key)
        return self.client
