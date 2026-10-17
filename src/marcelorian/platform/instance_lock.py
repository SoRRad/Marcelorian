"""Single-instance lock per data directory (spec §2).

Exactly one tray worker and one dispatcher may run against a data directory.
The OS releases the lock automatically if the process dies.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import TracebackType


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> InstanceLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunning("Marcelorian is already running for this data folder") from exc
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":  # pragma: no cover - Windows only
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
