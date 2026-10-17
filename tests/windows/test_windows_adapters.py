"""Real Windows adapters. Run on Windows only (CI ``windows-latest`` or the pilot laptop).

These prove the adapters work on a Windows host. They do not prove interactive
desktop behaviour (OAuth consent, tray, toasts, sleep/resume) - decision A3.
"""

from __future__ import annotations

import sys
import uuid

import pytest

pytestmark = [
    pytest.mark.windows_only,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows adapter test"),
]


def test_known_folders_resolve_through_the_windows_api() -> None:
    from marcelorian.platform.paths import (
        FOLDERID_DOCUMENTS,
        FOLDERID_LOCAL_APPDATA,
        _windows_known_folder,
        resolve_app_paths,
    )

    assert _windows_known_folder(FOLDERID_LOCAL_APPDATA).is_dir()
    assert _windows_known_folder(FOLDERID_DOCUMENTS).is_dir()
    paths = resolve_app_paths()
    assert paths.data_dir.parent == _windows_known_folder(FOLDERID_LOCAL_APPDATA)


def test_credential_manager_round_trip_with_synthetic_value() -> None:
    from marcelorian.platform.secrets import open_secret_store

    store = open_secret_store()
    assert store.backend_name == "keyring.backends.Windows.WinVaultKeyring"
    name = f"ci-selftest-{uuid.uuid4().hex}"
    try:
        store.set(name, "synthetic-value-not-a-secret")
        assert store.get(name) == "synthetic-value-not-a-secret"
    finally:
        store.delete(name)
    assert store.get(name) is None


def test_msvcrt_instance_lock(tmp_path) -> None:
    from marcelorian.platform.instance_lock import AlreadyRunning, InstanceLock

    with InstanceLock(tmp_path / "instance.lock"), pytest.raises(AlreadyRunning):
        InstanceLock(tmp_path / "instance.lock").acquire()
