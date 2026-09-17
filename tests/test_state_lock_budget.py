"""Thread and process lock waits share one request latency budget."""
import pytest
from repositories import state_lock as module


def test_thread_wait_does_not_restart_process_lock_budget(tmp_path, monkeypatch):
    clock = [0.0]
    class Lock:
        def acquire(self, timeout=None):
            clock[0] += 0.8
            return True
        def release(self):
            pass
    def flock(*args):
        raise BlockingIOError()
    monkeypatch.setattr(module, '_threads', Lock())
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    monkeypatch.setattr(module.fcntl, 'flock', flock)
    with pytest.raises(module.StateLockTimeout):
        with module.state_lock(tmp_path, timeout=1):
            pytest.fail('Busy lock was acquired')
    assert clock[0] == pytest.approx(1.0)
