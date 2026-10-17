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
        # quoted / JSON / dict-repr shapes of the same secrets
        ('{"installed": {"client_secret": "GOCSPX-SyntheticSecret123"}}', "SyntheticSecret123"),
        ("{'code': '4/0ASyntheticAuthCode', 'scope': 'gmail.send'}", "4/0ASyntheticAuthCode"),
        ('{"access_token": "SyntheticAccessToken9", "expires_in": 3599}', "SyntheticAccessToken9"),
        ("password = 'SyntheticPassword9'", "SyntheticPassword9"),
        ("client secret GOCSPX-BareSecret9 in text", "BareSecret9"),
        ("id_token eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln", "eyJzdWIiOiIxIn0"),
        # URL-encoded address, as Gmail API request URLs carry it
        ("q=from%3A%22pat.example%40synthetic-univ.test%22", "pat.example%40synthetic-univ.test"),
    ],
)
def test_redaction(raw, forbidden) -> None:
    assert forbidden not in redact(raw)


@pytest.fixture
def log_file(tmp_path: Path):
    from marcelorian.logging_setup import configure_logging

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    uvicorn = {n: logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")}
    saved_uvicorn = {n: (lg.handlers[:], lg.propagate, lg.disabled) for n, lg in uvicorn.items()}
    configure_logging(tmp_path)
    try:
        yield tmp_path / "marcelorian.log"
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers, root.level = saved_handlers, saved_level
        for name, (handlers, propagate, disabled) in saved_uvicorn.items():
            uvicorn[name].handlers, uvicorn[name].propagate = handlers, propagate
            uvicorn[name].disabled = disabled


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        handler.flush()


def test_exception_tracebacks_are_redacted_in_the_log_file(log_file: Path) -> None:
    log = logging.getLogger("marcelorian.services.gateway")
    try:
        raise RuntimeError("submission to pat.example@synthetic-univ.test; access_token=Tok9")
    except RuntimeError:
        log.exception("commit after accepted send failed for attempt %s", "att-1")
    log.warning("stack check", stack_info=True, extra={})
    _flush()
    text = log_file.read_text("utf-8")
    assert "commit after accepted send failed" in text
    assert "pat.example@synthetic-univ.test" not in text and "Tok9" not in text


def test_uvicorn_loggers_go_through_the_redacting_handlers(log_file: Path) -> None:
    logging.getLogger("uvicorn.error").error("Exception in ASGI app for pi@synthetic-univ.test")
    _flush()
    text = log_file.read_text("utf-8")
    assert "Exception in ASGI app" in text and "pi@synthetic-univ.test" not in text


def test_malformed_record_never_raises_into_the_caller(log_file: Path) -> None:
    logging.getLogger("marcelorian.review").warning("attempt %s outcome %s", "att-1")
    _flush()
    assert "unformattable log arguments" in log_file.read_text("utf-8")


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


# -- review round 3: linear redaction, encoded addresses, unprintable messages, uvicorn ---------


@pytest.mark.parametrize(
    "text",
    [
        "key-" * 5000,
        "-".join(["retina", "glaucoma", "session", "token", "cookie", "code", "eye"]) * 700,
        "a." * 20000,
        "abc.def-" * 5000 + "x",
        "x-" * 3000 + "@",
    ],
)
def test_redaction_stays_linear_on_long_hyphenated_or_dotted_runs(text) -> None:
    """A quadratic pattern took minutes on 4-20 KB of such text (a logging DoS)."""
    import time

    started = time.perf_counter()
    redact(text)
    assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    [
        ("q=from%3A%22applicant.synthetic%2Bt1%40gmail.com%22", "applicant.synthetic"),
        ("requesting ...?q=%28to%3A%22pat.example%2Bcv%40synthetic-univ.test%22", "pat.example"),
        ("oauth2-token=SECRET9", "SECRET9"),
        ("v1-session-id=SECRET9", "SECRET9"),
        ("oauth2_token=SECRET9", "SECRET9"),
    ],
)
def test_redaction_covers_encoded_plus_addresses_and_prefixed_keys(raw, forbidden) -> None:
    assert forbidden not in redact(raw)


def test_redaction_decodes_only_address_characters() -> None:
    assert redact("100%25 done %0A next") == "100%25 done %0A next"


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("cannot render")


def test_unprintable_message_object_never_raises_into_the_caller(log_file: Path) -> None:
    log = logging.getLogger("marcelorian.review")
    log.warning(_Unprintable())
    log.warning(_Unprintable(), "extra-arg")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log.exception(_Unprintable())
    _flush()
    assert "unprintable _Unprintable log message" in log_file.read_text("utf-8")


def test_uvicorn_started_with_the_log_kwargs_keeps_redaction(log_file: Path) -> None:
    import uvicorn

    from marcelorian.logging_setup import UVICORN_LOG_KWARGS

    async def app(scope, receive, send) -> None:  # pragma: no cover - never served
        return None

    uvicorn.Config(app, host="127.0.0.1", port=0, **UVICORN_LOG_KWARGS)
    error_logger = logging.getLogger("uvicorn.error")
    assert error_logger.propagate and not logging.getLogger("uvicorn").handlers
    error_logger.error("Exception in ASGI application for %s", "pi@synthetic-univ.test")
    _flush()
    text = log_file.read_text("utf-8")
    assert "Exception in ASGI application" in text and "pi@synthetic-univ.test" not in text
    assert logging.getLogger("uvicorn.access").disabled
