"""Secret storage via the OS credential store (spec §2; decision A3).

* Production uses ``keyring`` with Windows Credential Manager. On other
  platforms (Linux development only) the Secret Service backend is accepted.
* There is **no plaintext fallback**: fail, null, chained-without-viable,
  plaintext/encrypted-file (keyrings.alt) or any unknown backend makes setup
  fail clearly with ``SecretStoreUnavailable``.
* The in-memory store used by tests lives under ``tests/`` and is never
  importable or selectable from production code.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol

SERVICE_NAME = "Marcelorian"

#: Fully qualified backend classes that may hold Marcelorian secrets.
ALLOWED_BACKENDS: frozenset[str] = frozenset(
    {
        "keyring.backends.Windows.WinVaultKeyring",
        "keyring.backends.SecretService.Keyring",  # Linux development only
        "keyring.backends.libsecret.Keyring",  # Linux development only
    }
)


class SecretStoreUnavailable(RuntimeError):
    """No acceptable OS credential store; setup must stop (no plaintext fallback)."""


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...

    def set(self, name: str, value: str) -> None: ...

    def delete(self, name: str) -> None: ...


def _qualified(backend: Any) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


class KeyringSecretStore:
    """Secrets in the OS credential store under the ``Marcelorian`` service name."""

    def __init__(self, backend: Any) -> None:
        name = _qualified(backend)
        if name not in ALLOWED_BACKENDS:
            raise SecretStoreUnavailable(
                f"credential backend {name!r} is not an approved OS credential store; "
                "Marcelorian never stores secrets in plaintext"
            )
        self._backend = backend

    @property
    def backend_name(self) -> str:
        return _qualified(self._backend)

    def get(self, name: str) -> str | None:
        value = self._backend.get_password(SERVICE_NAME, name)
        return value if value is None else str(value)

    def set(self, name: str, value: str) -> None:
        if not value:
            raise ValueError("refusing to store an empty secret")
        self._backend.set_password(SERVICE_NAME, name, value)

    def delete(self, name: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            self._backend.delete_password(SERVICE_NAME, name)
        except PasswordDeleteError:
            return


def platform_backend() -> Any:
    """Instantiate the platform's OS credential store explicitly (no discovery)."""
    try:
        if sys.platform == "win32":
            from keyring.backends.Windows import WinVaultKeyring

            backend: Any = WinVaultKeyring()
        else:
            from keyring.backends.SecretService import Keyring as SecretServiceKeyring

            backend = SecretServiceKeyring()  # type: ignore[no-untyped-call]
        priority = type(backend).priority  # raises if the backend is not viable here
    except Exception as exc:
        raise SecretStoreUnavailable(
            "the OS credential store is unavailable; setup cannot continue"
        ) from exc
    if not priority or priority <= 0:
        raise SecretStoreUnavailable("the OS credential store is not viable on this system")
    return backend


def open_secret_store() -> KeyringSecretStore:
    return KeyringSecretStore(platform_backend())
