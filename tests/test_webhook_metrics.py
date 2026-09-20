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
    invocation, received = metrics.invocation(payload)
    metrics.processing(invocation, 2.5)
    metrics.reply("reply-1", invocation, received)
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
