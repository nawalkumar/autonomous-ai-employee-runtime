"""Exclusive, OS-backed per-task locks shared by the CLI, HTTP API and runtime.

A lock is a byte-range/flock lock on ``<lock_dir>/lock-<task_id>.lock``. The OS
releases it when the descriptor closes, including on process termination, so a
leftover lock file never blocks a later command. Separate open descriptors
exclude each other across threads and processes.

Nested acquire of the same task from the same stack is **not** supported: callers
that already hold the lock (CLI/API) must pass ``execution_locked=True`` into
``ExecutionRuntime`` instead of re-entering ``hold_task_lock``.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised on Windows only
    import msvcrt

    LOCK_BACKEND = "msvcrt"

    def _try_lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    LOCK_BACKEND = "fcntl"

    def _try_lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


LOCK_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")


class LockBusyError(RuntimeError):
    """The lock is held by another process or descriptor."""


class TaskLock:
    """One acquisition of a per-task lock; release() may run on any thread."""

    def __init__(self, lock_dir: Path, name: str) -> None:
        if not LOCK_NAME.fullmatch(name or ""):
            raise ValueError(f"Invalid lock name: {name!r}")
        self.path = Path(lock_dir) / f"lock-{name}.lock"
        self._fd: int | None = None
        self._guard = threading.Lock()

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, timeout: float = 0.0, poll_interval: float = 0.05) -> None:
        """Acquire or raise LockBusyError; waits at most ``timeout`` seconds."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            try:
                _try_lock(fd)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockBusyError(str(self.path)) from None
                time.sleep(poll_interval)
        self._fd = fd

    def release(self) -> None:
        with self._guard:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock(fd)
        finally:
            os.close(fd)


@contextmanager
def hold_task_lock(lock_dir: Path, name: str, timeout: float = 0.0) -> Iterator[TaskLock]:
    lock = TaskLock(lock_dir, name)
    lock.acquire(timeout=timeout)
    try:
        yield lock
    finally:
        lock.release()
