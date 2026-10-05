"""Cross-process claim lock: one critical section shared by every Hermes process on a profile.

A confirm token must be spent, and a duplicate-send fingerprint reserved, by exactly one caller. Hermes's
gateway and its cron jobs are separate processes that share ``<HERMES_HOME>/plugin-data/bento/``, and
``ctx.state`` is atomic only per ``get``/``set``, not across the plugin's read-modify-write of
``used_tokens`` / ``email_ledger``. So the whole check -> consume -> reserve sequence runs under an
exclusive OS file lock (``flock`` on POSIX, ``msvcrt.locking`` on Windows) plus a thread lock. The lock is
never held across the HTTP send. If it cannot be taken, callers get ``ClaimError`` and must refuse.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

LOCK_FILENAME = "claims.lock"
LOCK_TIMEOUT_SECONDS = 10.0
_POLL_SECONDS = 0.01


class ClaimError(RuntimeError):
    """The cross-process claim could not be taken or persisted. The caller must refuse the send."""


class ClaimLock:
    """Context manager: thread lock + exclusive file lock on ``<plugin data dir>/claims.lock``."""

    def __init__(self, path_fn: Callable[[], Path | None], *, timeout: float = LOCK_TIMEOUT_SECONDS,
                 sleep: Callable[[float], None] = time.sleep, monotonic: Callable[[], float] = time.monotonic):
        self._path_fn, self._timeout, self._sleep, self._monotonic = path_fn, timeout, sleep, monotonic
        self._thread_lock = threading.Lock()
        self._fd: int | None = None

    def __enter__(self) -> ClaimLock:
        if not self._thread_lock.acquire(timeout=self._timeout):
            raise ClaimError("timed out waiting for another call in this process to finish its claim")
        try:
            self._fd = self._lock_file()
        except BaseException:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, *exc: Any) -> None:
        fd, self._fd = self._fd, None
        try:
            if fd is not None:
                self._unlock(fd)
        finally:
            self._thread_lock.release()

    # -- platform lock ------------------------------------------------------------------------------
    def _lock_file(self) -> int:
        if fcntl is None and msvcrt is None:  # pragma: no cover - exotic platform
            raise ClaimError("no file-locking primitive available on this platform")
        path = self._path_fn()
        if path is None:
            raise ClaimError("plugin data directory is unavailable, so the claim cannot be made")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as err:
            raise ClaimError(f"cannot open claim lock file: {err.strerror or err}") from None
        deadline = self._monotonic() + self._timeout
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - Windows
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return fd
            except OSError:
                if self._monotonic() >= deadline:
                    os.close(fd)
                    raise ClaimError("timed out waiting for another Hermes process to finish its claim") from None
                self._sleep(_POLL_SECONDS)

    @staticmethod
    def _unlock(fd: int) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:  # pragma: no cover - closing the fd drops the lock anyway
            pass
        finally:
            os.close(fd)
