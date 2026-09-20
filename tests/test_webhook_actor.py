import threading

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
