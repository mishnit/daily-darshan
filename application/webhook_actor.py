"""Best-effort single-writer webhook batching for ephemeral free-tier hosts."""
from __future__ import annotations

import logging
import queue
import threading
import time


class BestEffortWebhookActor:
    """Bounded ingress queue with one state writer and periodic snapshot flush.

    Enqueue deliberately never blocks.  A full queue drops the newest event;
    callers still acknowledge it because this mode explicitly prefers latency
    over durability.
    """

    def __init__(self, processor, flusher, *, capacity=5000, batch_size=100,
                 flush_seconds=900, batch_wait_seconds=0.025, logger=None):
        self._processor = processor
        self._flusher = flusher
        self._queue = queue.Queue(maxsize=max(1, int(capacity)))
        self._batch_size = max(1, int(batch_size))
        self._flush_seconds = max(1.0, float(flush_seconds))
        self._batch_wait = max(0.0, float(batch_wait_seconds))
        self._log = logger or logging.getLogger(__name__)
        self._started = False
        self._start_lock = threading.Lock()
        self._thread = None
        self.dropped = 0
        self.processed = 0

    @property
    def depth(self):
        return self._queue.qsize()

    def start(self):
        if self._started:
            return
        with self._start_lock:
            if self._started:
                return
            self._thread = threading.Thread(target=self._run, name="webhook-state-writer", daemon=True)
            self._started = True
            self._thread.start()

    def enqueue(self, payload) -> bool:
        self.start()
        try:
            self._queue.put_nowait(payload)
            return True
        except queue.Full:
            self.dropped += 1
            self._log.error("Webhook queue full; dropping acknowledged event depth=%d dropped=%d",
                            self.depth, self.dropped)
            return False

    def _run(self):
        next_flush = time.monotonic() + self._flush_seconds
        while True:
            timeout = max(0.0, min(self._batch_wait, next_flush - time.monotonic()))
            try:
                first = self._queue.get(timeout=timeout)
            except queue.Empty:
                first = None
            batch = [] if first is None else [first]
            deadline = time.monotonic() + self._batch_wait
            while len(batch) < self._batch_size and time.monotonic() < deadline:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if batch:
                try:
                    self._processor(batch)
                    self.processed += len(batch)
                except Exception:
                    # Best-effort mode acknowledges before processing; failures
                    # are observable but intentionally do not trigger Meta retry.
                    self._log.exception("Best-effort webhook batch failed size=%d", len(batch))
                finally:
                    for _ in batch:
                        self._queue.task_done()
            if time.monotonic() >= next_flush:
                try:
                    self._flusher()
                except Exception:
                    self._log.exception("Best-effort 15-minute Git snapshot failed")
                next_flush = time.monotonic() + self._flush_seconds
