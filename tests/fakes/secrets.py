"""In-memory secret store for tests only (decision A3).

Lives under ``tests/`` so production code can never import, discover or
select it; ``tests/security/test_secret_store.py`` asserts that.
"""

from __future__ import annotations


class InMemorySecretStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("refusing to store an empty secret")
        self._values[name] = value

    def delete(self, name: str) -> None:
        self._values.pop(name, None)
