"""Serialize local state transactions across threads and worker processes."""
from contextlib import contextmanager
import fcntl
import os
import threading
import time

_threads = threading.RLock()


class StateLockTimeout(TimeoutError):
    """The local webhook transaction lock was busy beyond its safe budget."""


@contextmanager
def state_lock(root, timeout=None):
    os.makedirs(root, exist_ok=True)
    acquired = _threads.acquire() if timeout is None else _threads.acquire(timeout=timeout)
    if not acquired:
        raise StateLockTimeout("webhook state is busy")
    try:
        with open(os.path.join(root, ".webhook-state.lock"), "a") as handle:
            if timeout is None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise StateLockTimeout("webhook state is busy")
                        time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        _threads.release()
