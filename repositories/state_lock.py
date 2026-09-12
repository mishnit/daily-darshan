"""Serialize local state transactions across threads and worker processes."""
from contextlib import contextmanager
import fcntl
import os
import threading

_threads = threading.RLock()


@contextmanager
def state_lock(root):
    os.makedirs(root, exist_ok=True)
    with _threads, open(os.path.join(root, ".webhook-state.lock"), "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
