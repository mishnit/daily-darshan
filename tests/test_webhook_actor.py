import threading
import time

from application.webhook_actor import BestEffortWebhookActor


def test_actor_processes_in_bounded_batches_and_flushes(monkeypatch):
    batches = []
    flushed = threading.Event()
    actor = BestEffortWebhookActor(
        lambda items: batches.append(list(items)),
        flushed.set,
        capacity=10,
        batch_size=3,
        flush_seconds=0.01,
        batch_wait_seconds=0.01,
    )
    for value in range(7):
        assert actor.enqueue(value)
    assert flushed.wait(2)
    actor._queue.join()
    assert sorted(item for batch in batches for item in batch) == list(range(7))
    assert all(len(batch) <= 3 for batch in batches)
    metrics = actor.metrics()
    assert metrics["queue"]["worker_started"] is True
    assert metrics["queue"]["processed"] == 7
    assert metrics["queue"]["estimated_queue_wait_ms"] >= 0
    assert metrics["queue"]["oldest_processed_event_ms"] >= metrics["queue"]["estimated_queue_wait_ms"]
    assert metrics["snapshot"]["last_snapshot_succeeded"] is True
    assert metrics["snapshot"]["last_snapshot_at"].endswith("Z")
    assert metrics["snapshot"]["next_snapshot_in_seconds"] is not None


def test_actor_metrics_record_snapshot_failure_without_sensitive_details():
    flushed = threading.Event()
    attempts = 0

    def fail():
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return
        flushed.set()
        raise ValueError("secret customer content")

    actor = BestEffortWebhookActor(
        lambda _items: None, fail, flush_seconds=0.1, batch_wait_seconds=0.001,
    )
    actor.start()
    assert flushed.wait(2)
    deadline = time.monotonic() + 2
    while actor.last_snapshot_succeeded is None and time.monotonic() < deadline:
        time.sleep(0.001)
    snapshot = actor.metrics()["snapshot"]
    assert snapshot["last_snapshot_succeeded"] is False
    assert snapshot["last_snapshot_error"] == "ValueError"
    assert "secret" not in str(snapshot)


def test_actor_drops_without_blocking_when_queue_is_full():
    actor = BestEffortWebhookActor(lambda _items: None, lambda: None, capacity=1)
    # Fill directly so the daemon cannot race the capacity assertion.
    actor._started = True
    actor._queue.put_nowait("first")
    assert actor.enqueue("second") is False
    assert actor.dropped == 1


def test_failed_batch_does_not_cascade_to_later_batches():
    seen = []
    completed = threading.Event()

    def process(items):
        if items == ["bad"]:
            raise RuntimeError("isolated failure")
        seen.extend(items)
        completed.set()

    actor = BestEffortWebhookActor(
        process, lambda: None, capacity=5, batch_size=1, batch_wait_seconds=0.001
    )
    assert actor.enqueue("bad")
    assert actor.enqueue("good")
    assert completed.wait(2)
    actor._queue.join()
    assert seen == ["good"]
    assert actor.failed == 1


def test_actor_resumes_pending_work_after_snapshot():
    seen = []
    snapshot_started = threading.Event()
    release_snapshot = threading.Event()

    def flush():
        snapshot_started.set()
        assert release_snapshot.wait(2)

    actor = BestEffortWebhookActor(
        lambda items: seen.extend(items), flush,
        capacity=20, batch_size=1, flush_seconds=0.01, batch_wait_seconds=0.001,
    )
    assert actor.enqueue("before")
    assert snapshot_started.wait(2)
    assert actor.enqueue("during-1")
    assert actor.enqueue("during-2")
    assert actor.depth == 2
    release_snapshot.set()
    actor._queue.join()
    deadline = time.monotonic() + 2
    while actor.processed != 3 and time.monotonic() < deadline:
        time.sleep(0.001)
    assert seen == ["before", "during-1", "during-2"]
    assert actor.processed == 3
    assert actor.last_snapshot_ms > 0


def test_critical_lane_preserves_arrival_order_and_runs_separately():
    order = []
    actor = BestEffortWebhookActor(
        lambda items: order.extend(("normal", item) for item in items),
        lambda: None,
        critical_processor=lambda items: order.extend(("critical", item) for item in items),
        capacity=10,
    )
    now = time.monotonic()
    actor._queue.put_nowait((False, 0, now, "normal"))
    actor._queue.put_nowait((True, 1, now, "admin"))
    actor.start()
    actor._queue.join()
    assert order == [("normal", "normal"), ("critical", "admin")]


def test_sender_outcomes_return_through_control_lane():
    received = []
    ready = threading.Event()
    actor = BestEffortWebhookActor(
        lambda _items: None, lambda: None,
        control_processor=lambda items: (received.extend(items), ready.set()),
        batch_wait_seconds=0.001,
    )
    actor.enqueue_control({"id": "reply-1", "status": "SENT"})
    assert ready.wait(2)
    assert received == [{"id": "reply-1", "status": "SENT"}]
