import logging
import time

from application.webhook_metrics import WebhookMetrics


def test_metrics_correlate_processing_and_response_without_status_callbacks(monkeypatch, caplog):
    monkeypatch.setenv("WEBHOOK_METRICS_LOG_SECONDS", "1")
    monkeypatch.setenv("WEBHOOK_METRICS_SAMPLE_RATE", "0")
    metrics = WebhookMetrics(logging.getLogger("test.webhook.metrics"))
    payload = {
        "_webhook_received_monotonic": time.monotonic() - 0.05,
        "entry": [{"changes": [{"value": {"messages": [{"id": "wamid.inbound"}]}}]}],
    }
    invocation, received, event_epoch = metrics.invocation(payload)
    metrics.processing(invocation, 2.5)
    metrics.reply("reply-1", invocation, received, event_epoch)
    metrics._last_report -= 2

    with caplog.at_level(logging.INFO, logger="test.webhook.metrics"):
        metrics.response("reply-1", "SENT")

    message = caplog.messages[-1]
    assert "processing_count=1" in message
    assert "processing_avg_ms=2.50" in message
    assert "response_count=1" in message
    assert "pending_responses=0" in message
    status_payload = {"entry": [{"changes": [{"value": {"statuses": [{"id": "outbound"}]}}]}]}
    assert metrics.invocation(status_payload)[0] is None


def test_metrics_wait_for_all_replies_before_recording_response(monkeypatch):
    monkeypatch.setenv("WEBHOOK_METRICS_SAMPLE_RATE", "0")
    metrics = WebhookMetrics()
    received = time.monotonic()
    metrics.reply("one", "inbound", received)
    metrics.reply("two", "inbound", received)
    metrics.response("one", "SENT")
    assert metrics._responses == []
    metrics.response("two", "SENT")
    assert len(metrics._responses) == 1


def test_metrics_can_be_disabled_without_retaining_correlation(monkeypatch):
    monkeypatch.setenv("WEBHOOK_METRICS_ENABLED", "false")
    metrics = WebhookMetrics()
    metrics.processing("inbound", 1.0)
    metrics.reply("reply", "inbound", time.monotonic())
    metrics.response("reply", "SENT")
    assert metrics._processing == []
    assert metrics._responses == []
    assert metrics._pending == {}


def test_metrics_measure_user_event_to_meta_delivery(monkeypatch):
    monkeypatch.setenv("WEBHOOK_METRICS_SAMPLE_RATE", "0")
    metrics = WebhookMetrics()
    received = time.monotonic() - 0.05
    metrics.reply("reply", "inbound", received, event_epoch=1000)
    metrics.response("reply", "SENT", "wamid.outbound")
    assert metrics.health()["pending_invocations"] == 1

    metrics.delivered("wamid.outbound", event_epoch=1002)
    health = metrics.health()
    assert health["completed_invocations"] == 1
    assert health["pending_invocations"] == 0
    assert health["last_completed_invocation"]["reply_count"] == 1
    assert health["last_completed_invocation"]["webhook_to_delivery_ms"] >= 50
    assert health["last_completed_invocation"]["meta_event_e2e_ms"] == 2000


def test_metrics_wait_for_every_reply_to_be_delivered(monkeypatch):
    monkeypatch.setenv("WEBHOOK_METRICS_SAMPLE_RATE", "0")
    metrics = WebhookMetrics()
    received = time.monotonic()
    metrics.reply("one", "inbound", received)
    metrics.reply("two", "inbound", received)
    metrics.response("one", "SENT", "wamid.one")
    metrics.response("two", "SENT", "wamid.two")
    metrics.delivered("wamid.one")
    assert metrics.health()["completed_invocations"] == 0
    metrics.delivered("wamid.two")
    assert metrics.health()["completed_invocations"] == 1


def test_metrics_correlate_delivery_callback_that_overtakes_sender(monkeypatch):
    monkeypatch.setenv("WEBHOOK_METRICS_SAMPLE_RATE", "0")
    metrics = WebhookMetrics()
    received = time.monotonic()
    metrics.reply("reply", "inbound", received, event_epoch=1000)
    metrics.delivered("wamid.fast", event_epoch=1001)
    metrics.response("reply", "SENT", "wamid.fast")
    assert metrics.health()["last_completed_invocation"]["meta_event_e2e_ms"] == 1000
