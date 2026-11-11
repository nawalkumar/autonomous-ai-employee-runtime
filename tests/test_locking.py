"""Shared OS task locks used by both the CLI and the HTTP API."""

import threading
import time
from pathlib import Path

import pytest

from app.runtime.locking import LockBusyError, TaskLock, hold_task_lock


def test_lock_excludes_other_descriptors_in_the_same_process(tmp_path: Path) -> None:
    with hold_task_lock(tmp_path, "task_a"):
        with pytest.raises(LockBusyError):
            TaskLock(tmp_path, "task_a").acquire()
        with hold_task_lock(tmp_path, "task_b"):
            pass
    with hold_task_lock(tmp_path, "task_a"):
        pass


def test_lock_can_be_released_from_another_thread(tmp_path: Path) -> None:
    lock = TaskLock(tmp_path, "task_a")
    lock.acquire()
    worker = threading.Thread(target=lock.release)
    worker.start()
    worker.join(timeout=10)
    assert not lock.held
    with hold_task_lock(tmp_path, "task_a"):
        pass


def test_bounded_wait_acquires_once_released(tmp_path: Path) -> None:
    holder = TaskLock(tmp_path, "task_a")
    holder.acquire()
    threading.Timer(0.2, holder.release).start()
    started = time.monotonic()
    with hold_task_lock(tmp_path, "task_a", timeout=5):
        assert time.monotonic() - started < 5


def test_bounded_wait_times_out(tmp_path: Path) -> None:
    with hold_task_lock(tmp_path, "task_a"):
        started = time.monotonic()
        with pytest.raises(LockBusyError):
            TaskLock(tmp_path, "task_a").acquire(timeout=0.3)
        assert time.monotonic() - started >= 0.25


@pytest.mark.parametrize("name", ["../x", "a/b", "", "x" * 129, "a b"])
def test_lock_names_are_validated(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        TaskLock(tmp_path, name)


def test_release_is_idempotent(tmp_path: Path) -> None:
    lock = TaskLock(tmp_path, "task_a")
    lock.acquire()
    lock.release()
    lock.release()
