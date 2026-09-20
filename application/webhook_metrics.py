"""Low-overhead webhook latency aggregation and sampled correlation logs."""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time


def _percentile(values, fraction):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))]


class WebhookMetrics:
    def __init__(self, logger=None):
        self._log = logger or logging.getLogger(__name__)
        self._lock = threading.Lock()
        self._processing = []
        self._responses = []
        self._pending = {}
        self._reply_to_invocation = {}
        self._last_report = time.monotonic()

    @staticmethod
    def invocation(payload):
        received = float(payload.get("_webhook_received_monotonic") or time.monotonic())
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                messages = change.get("value", {}).get("messages", [])
                if messages:
                    return str(messages[0].get("id") or "unknown"), received
        return None, received

    def processing(self, invocation_id, milliseconds):
        if not invocation_id:
            return
        with self._lock:
            self._processing.append(milliseconds)
            self._sample(invocation_id, "processing", processing_ms=milliseconds)
            self._report_if_due()

    def reply(self, reply_id, invocation_id, received):
        if not invocation_id:
            return
        with self._lock:
            state = self._pending.setdefault(invocation_id, {"received": received, "replies": set()})
            state["replies"].add(reply_id)
            self._reply_to_invocation[reply_id] = invocation_id

    def response(self, reply_id, status=""):
        now = time.monotonic()
        with self._lock:
            invocation_id = self._reply_to_invocation.pop(reply_id, None)
            if not invocation_id:
                return
            state = self._pending.get(invocation_id)
            if not state:
                return
            state["replies"].discard(reply_id)
            if state["replies"]:
                return
            milliseconds = (now - state["received"]) * 1000
            self._pending.pop(invocation_id, None)
            self._responses.append(milliseconds)
            self._sample(invocation_id, "response", response_ms=milliseconds, status=status)
            self._report_if_due()

    def _sample(self, invocation_id, phase, **values):
        rate = min(1.0, max(0.0, float(os.environ.get("WEBHOOK_METRICS_SAMPLE_RATE", "0.01"))))
        bucket = int(hashlib.sha256(invocation_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        if bucket < rate:
            fields = " ".join(f"{name}={value:.1f}" if isinstance(value, float) else f"{name}={value}"
                              for name, value in values.items())
            self._log.info("Webhook invocation metric id=%s phase=%s %s", invocation_id, phase, fields)

    def _report_if_due(self):
        now = time.monotonic()
        interval = max(1.0, float(os.environ.get("WEBHOOK_METRICS_LOG_SECONDS", "10")))
        if now - self._last_report < interval:
            return
        processing, responses = self._processing, self._responses
        self._processing, self._responses = [], []
        self._last_report = now
        self._log.info(
            "Webhook latency metrics window_s=%.1f processing_count=%d processing_avg_ms=%.2f "
            "processing_p50_ms=%.2f processing_p95_ms=%.2f processing_p99_ms=%.2f "
            "response_count=%d response_avg_ms=%.2f response_p50_ms=%.2f "
            "response_p95_ms=%.2f response_p99_ms=%.2f pending_responses=%d",
            interval, len(processing), sum(processing) / len(processing) if processing else 0,
            _percentile(processing, .50), _percentile(processing, .95), _percentile(processing, .99),
            len(responses), sum(responses) / len(responses) if responses else 0,
            _percentile(responses, .50), _percentile(responses, .95), _percentile(responses, .99),
            len(self._pending),
        )
