"""Application paths (spec §2 "Paths").

* App data: ``%LOCALAPPDATA%\\Marcelorian\\`` (DB, logs, backups, CV versions).
* Exports: the Windows *Documents* known folder resolved through the Windows
  API (``SHGetKnownFolderPath``), never a hardcoded path, plus
  ``\\Marcelorian\\exports\\``. A OneDrive-synced Documents folder is flagged.

On non-Windows platforms (development only) XDG-style locations are used.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "Marcelorian"


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path
    exports_dir: Path
    exports_synced_by_onedrive: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "marcelorian.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "instance.lock"


def _windows_known_folder(folder_guid: str) -> Path:  # pragma: no cover - Windows only
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = (
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        )

    if sys.platform != "win32":  # also lets type checkers see windll only on Windows
        raise OSError("known folders exist only on Windows")
    raw = uuid.UUID(folder_guid).bytes_le
    guid = GUID.from_buffer_copy(raw)
    path_ptr = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path_ptr))
    try:
        if result != 0 or path_ptr.value is None:
            raise OSError(f"SHGetKnownFolderPath failed with HRESULT {result:#x}")
        return Path(path_ptr.value)
    finally:
        ole32.CoTaskMemFree(path_ptr)


FOLDERID_DOCUMENTS = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"
FOLDERID_LOCAL_APPDATA = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"


def is_onedrive_synced(path: Path, environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    resolved = str(path).lower()
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = env.get(var)
        if root and resolved.startswith(str(Path(root)).lower()):
            return True
    return False


def resolve_app_paths() -> AppPaths:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI
        local = _windows_known_folder(FOLDERID_LOCAL_APPDATA)
        documents = _windows_known_folder(FOLDERID_DOCUMENTS)
    else:
        home = Path.home()
        local = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
        documents = Path(os.environ.get("XDG_DOCUMENTS_DIR", home / "Documents"))
    exports = documents / APP_DIR_NAME / "exports"
    return AppPaths(
        data_dir=local / APP_DIR_NAME,
        exports_dir=exports,
        exports_synced_by_onedrive=is_onedrive_synced(exports),
    )
