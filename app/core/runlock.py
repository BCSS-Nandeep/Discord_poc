"""Cross-process single-instance lock for background workers.

``uvicorn --reload`` runs the application in a child process and respawns it on every
edit, and ``--workers N`` forks N independent processes.  Without a guard each of them
would start its own Gateway connection and its own reconciliation worker.

This module takes an advisory OS-level lock on a file.  Only the process that wins the
lock starts the Gateway and the reconciler; the others serve HTTP only.  The lock is
released automatically when the process exits, even if it is killed.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from types import TracebackType
from typing import TextIO

from app.core.logging import get_logger

logger = get_logger(__name__)

if sys.platform == "win32":  # pragma: no cover - platform specific
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        with contextlib.suppress(OSError):
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:  # pragma: no cover - platform specific
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProcessLock:
    """Advisory, non-blocking, single-holder lock backed by a file."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None
        self._acquired = False

    @property
    def acquired(self) -> bool:
        return self._acquired

    def acquire(self) -> bool:
        """Try to take the lock. Returns ``True`` only for the winning process."""

        if self._acquired:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # "a+" never truncates, so a running holder's lock byte is preserved.
            handle = open(self.path, "a+", encoding="utf-8")
        except OSError as exc:
            logger.warning(
                "Could not open worker lock file; continuing without it",
                extra={"lock_file": str(self.path), "error": str(exc)},
            )
            # Failing open would be worse than a possible duplicate worker in dev,
            # but the database-level claim still keeps reconciliation idempotent.
            return True

        handle.seek(0)
        if not _try_lock(handle):
            handle.close()
            logger.info(
                "Worker lock already held by another process; "
                "background workers will not start here",
                extra={"lock_file": str(self.path), "pid": os.getpid()},
            )
            return False

        self._handle = handle
        self._acquired = True
        logger.info(
            "Acquired worker lock",
            extra={"lock_file": str(self.path), "pid": os.getpid()},
        )
        return True

    def release(self) -> None:
        """Release the lock if this process holds it."""

        if not self._acquired or self._handle is None:
            return
        _unlock(self._handle)
        with contextlib.suppress(OSError):  # pragma: no cover - defensive
            self._handle.close()
        self._handle = None
        self._acquired = False
        logger.info("Released worker lock", extra={"lock_file": str(self.path)})

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
