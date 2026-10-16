"""Secret storage (A3, acceptance 66), log redaction, single-instance lock, paths."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from marcelorian.logging_setup import RedactingFilter, redact
from marcelorian.platform.instance_lock import AlreadyRunning, InstanceLock
from marcelorian.platform.paths import is_onedrive_synced, resolve_app_paths
from marcelorian.platform.secrets import (
    ALLOWED_BACKENDS,
    KeyringSecretStore,
    SecretStoreUnavailable,
    open_secret_store,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "marcelorian"


def _fake_backend(module: str, qualname: str):
    store: dict[tuple[str, str], str] = {}

    def get_password(self, service, name):
        return store.get((service, name))

    def set_password(self, service, name, value):
        store[(service, name)] = value

    def delete_password(self, service, name):
        from keyring.errors import PasswordDeleteError

        if (service, name) not in store:
            raise PasswordDeleteError(name)
        del store[(service, name)]

    cls = type(
        qualname,
        (),
        {
            "get_password": get_password,
            "set_password": set_password,
            "delete_password": delete_password,
        },
    )
    cls.__module__ = module
    cls.__qualname__ = qualname
    return cls()


@pytest.mark.parametrize(
    ("module", "qualname"),
    [
        ("keyring.backends.fail", "Keyring"),
        ("keyring.backends.null", "Keyring"),
        ("keyring.backends.chainer", "ChainerBackend"),
        ("keyrings.alt.file", "PlaintextKeyring"),
        ("keyrings.alt.file", "EncryptedKeyring"),
        ("tests.fakes.secrets", "InMemorySecretStore"),
        ("somewhere.else", "Keyring"),
    ],
)
def test_unapproved_backends_rejected(module, qualname) -> None:
    with pytest.raises(SecretStoreUnavailable):
        KeyringSecretStore(_fake_backend(module, qualname))


def test_approved_backend_round_trip() -> None:
    store = KeyringSecretStore(_fake_backend("keyring.backends.Windows", "WinVaultKeyring"))
    store.set("anthropic_api_key", "synthetic-not-a-real-key")
    assert store.get("anthropic_api_key") == "synthetic-not-a-real-key"
    store.delete("anthropic_api_key")
    store.delete("anthropic_api_key")  # idempotent
    assert store.get("anthropic_api_key") is None
    with pytest.raises(ValueError):
        store.set("x", "")


def test_allowlist_contains_no_plaintext_or_fallback_backend() -> None:
    lowered = {b.lower() for b in ALLOWED_BACKENDS}
    assert not any(
        word in b
        for b in lowered
        for word in ("plaintext", "file", "fail", "null", "chainer", "memory")
    )


def test_production_code_never_references_the_test_store() -> None:
    for path in SRC.rglob("*.py"):
        text = path.read_text("utf-8")
        assert "InMemorySecretStore" not in text, path
        assert "tests.fakes" not in text, path


def test_platform_store_is_os_store_or_fails_clearly() -> None:
    try:
        store = open_secret_store()
    except SecretStoreUnavailable:
        assert sys.platform != "win32" or pytest.fail("Credential Manager should be available")
        return
    assert store.backend_name in ALLOWED_BACKENDS


# -- logging (spec §2) ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("sending to pat.example@synthetic-univ.test", "pat.example@synthetic-univ.test"),
        ("Authorization: Bearer ya29.a0AfH6SMBsecret", "ya29.a0AfH6SMBsecret"),
        ("refresh_token=1//0gSecretRefresh", "1//0gSecretRefresh"),
        ("api key sk-ant-api03-abcdefSECRET", "sk-ant-api03-abcdefSECRET"),
        ("open http://127.0.0.1:5000/bootstrap#t=abcDEF123_-", "abcDEF123_-"),
        ("code=4/0AbCdEf oauth callback", "4/0AbCdEf"),
        ("csrf_token=deadbeef", "deadbeef"),
    ],
)
def test_redaction(raw, forbidden) -> None:
    assert forbidden not in redact(raw)


def test_masked_email_keeps_a_hint() -> None:
    assert redact("to pat@synthetic-univ.test") == "to p***@s***.test"


def test_filter_applies_to_formatted_args() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "send to %s", ("pi@a.test",), None)
    RedactingFilter().filter(record)
    assert "pi@a.test" not in record.getMessage()


# -- single instance lock (spec §2) ---------------------------------------------------------------


def test_single_instance_lock(tmp_path) -> None:
    lock_path = tmp_path / "data" / "instance.lock"
    with InstanceLock(lock_path), pytest.raises(AlreadyRunning):
        InstanceLock(lock_path).acquire()
    with InstanceLock(lock_path):  # released on exit
        pass


# -- paths (spec §2) ------------------------------------------------------------------------------


def test_onedrive_detection() -> None:
    env = {"OneDrive": "/users/a/OneDrive"}
    assert is_onedrive_synced(Path("/users/a/OneDrive/Documents/Marcelorian/exports"), env)
    assert not is_onedrive_synced(Path("/users/a/Documents/Marcelorian/exports"), env)
    assert not is_onedrive_synced(Path("/x"), {})


def test_app_paths_shape() -> None:
    paths = resolve_app_paths()
    assert paths.data_dir.name == "Marcelorian"
    assert paths.exports_dir.parts[-2:] == ("Marcelorian", "exports")
    assert paths.db_path.parent == paths.data_dir
