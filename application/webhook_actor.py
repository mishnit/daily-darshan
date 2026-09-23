"""Best-effort single-writer webhook batching for ephemeral free-tier hosts."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone


class BestEffortWebhookActor:
    """Bounded ingress queue with one state writer and periodic snapshot flush.

    Enqueue deliberately never blocks.  A full queue drops the newest event;
    callers still acknowledge it because this mode explicitly prefers latency
    over durability.
    """

    def __init__(self, processor, flusher, *, critical_processor=None, control_processor=None,
                 capacity=5000, batch_size=100,
                 flush_seconds=900, batch_wait_seconds=0.025, logger=None):
        self._processor = processor
        self._critical_processor = critical_processor or processor
        self._control_processor = control_processor or (lambda _items: None)
        self._flusher = flusher
        self._queue = queue.Queue(maxsize=max(1, int(capacity)))
        self._controls = queue.SimpleQueue()
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._batch_size = max(1, int(batch_size))
        self._flush_seconds = max(1.0, float(flush_seconds))
        self._batch_wait = max(0.0, float(batch_wait_seconds))
        self._log = logger or logging.getLogger(__name__)
        self._started = False
        self._start_lock = threading.Lock()
        self._thread = None
        self.dropped = 0
        self.accepted = 0
        self.processed = 0
        self.failed = 0
        self.last_batch_ms = 0.0
        self.oldest_queue_ms = 0.0
        self.last_snapshot_ms = 0.0
        self.last_snapshot_at = None
        self.last_snapshot_succeeded = None
        self.last_snapshot_error = None
        self._started_at = None
        self._next_flush_at = None
        self._last_metrics_at = time.monotonic()

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
            self._started_at = time.time()
            self._thread.start()

    @staticmethod
    def _iso8601(timestamp):
        if timestamp is None:
            return None
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")

    def metrics(self) -> dict:
        """Return non-sensitive queue and persistence diagnostics for /health."""
        now_monotonic = time.monotonic()
        now_wall = time.time()
        return {
            "queue": {
                "worker_started": self._started,
                "worker_started_at": self._iso8601(self._started_at),
                "depth": self.depth,
                "capacity": self._queue.maxsize,
                "accepted": self.accepted,
                "processed": self.processed,
                "failed": self.failed,
                "dropped": self.dropped,
                "last_batch_ms": round(self.last_batch_ms, 3),
                "oldest_processed_event_ms": round(self.oldest_queue_ms, 3),
            },
            "snapshot": {
                "interval_seconds": self._flush_seconds,
                "last_snapshot_at": self._iso8601(self.last_snapshot_at),
                "last_snapshot_age_seconds": (
                    None if self.last_snapshot_at is None
                    else round(max(0.0, now_wall - self.last_snapshot_at), 3)
                ),
                "last_snapshot_ms": round(self.last_snapshot_ms, 3),
                "last_snapshot_succeeded": self.last_snapshot_succeeded,
                "last_snapshot_error": self.last_snapshot_error,
                "next_snapshot_in_seconds": (
                    None if self._next_flush_at is None
                    else round(max(0.0, self._next_flush_at - now_monotonic), 3)
                ),
            },
        }

    def enqueue(self, payload, *, critical=False) -> bool:
        self.start()
        try:
            with self._sequence_lock:
                sequence = self._sequence
                self._sequence += 1
            self._queue.put_nowait((bool(critical), sequence, time.monotonic(), payload))
            self.accepted += 1
            return True
        except queue.Full:
            self.dropped += 1
            self._log.error("Webhook queue full; dropping acknowledged event depth=%d dropped=%d",
                            self.depth, self.dropped)
            return False

    def enqueue_control(self, payload) -> None:
        """Return immutable transport outcomes to the sole state writer."""
        self.start()
        self._controls.put(payload)

    def _drain_controls(self) -> None:
        controls = []
        while len(controls) < self._batch_size:
            try:
                controls.append(self._controls.get_nowait())
            except queue.Empty:
                break
        if controls:
            self._control_processor(controls)

    def _run(self):
        next_flush = time.monotonic() + self._flush_seconds
        self._next_flush_at = next_flush
        while True:
            self._drain_controls()
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
                started = time.monotonic()
                oldest = min(queued_at for _priority, _sequence, queued_at, _payload in batch)
                try:
                    normal = []
                    for is_critical, _sequence, _queued_at, payload in batch:
                        if is_critical:
                            # Preserve arrival order: a UTR confirmation must
                            # never overtake the message that created its draft.
                            if normal:
                                self._processor(normal)
                                normal = []
                            self._critical_processor([payload])
                        else:
                            normal.append(payload)
                    if normal:
                        self._processor(normal)
                    self.processed += len(batch)
                except Exception:
                    # Best-effort mode acknowledges before processing; failures
                    # are observable but intentionally do not trigger Meta retry.
                    self._log.exception("Best-effort webhook batch failed size=%d", len(batch))
                    self.failed += len(batch)
                finally:
                    finished = time.monotonic()
                    self.last_batch_ms = (finished - started) * 1000
                    self.oldest_queue_ms = (finished - oldest) * 1000
                    for _ in batch:
                        self._queue.task_done()
                if finished - self._last_metrics_at >= 10:
                    self._log.info(
                        "Webhook queue metrics accepted=%d processed=%d failed=%d "
                        "dropped=%d depth=%d batch=%d batch_ms=%.1f oldest_ms=%.1f",
                        self.accepted, self.processed, self.failed, self.dropped,
                        self.depth, len(batch), self.last_batch_ms, self.oldest_queue_ms,
                    )
                    self._last_metrics_at = finished
            if time.monotonic() >= next_flush:
                snapshot_started = time.monotonic()
                try:
                    self._flusher()
                    self.last_snapshot_succeeded = True
                    self.last_snapshot_error = None
                except Exception as exc:
                    self.last_snapshot_succeeded = False
                    self.last_snapshot_error = type(exc).__name__
                    self._log.exception("Best-effort 15-minute Git snapshot failed")
                finally:
                    self.last_snapshot_at = time.time()
                    self.last_snapshot_ms = (time.monotonic() - snapshot_started) * 1000
                    self._log.info(
                        "Webhook snapshot complete snapshot_ms=%.1f resume_depth=%d",
                        self.last_snapshot_ms, self.depth,
                    )
                next_flush = time.monotonic() + self._flush_seconds
                self._next_flush_at = next_flush
            self._drain_controls()
