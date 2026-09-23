"""Low-overhead webhook latency aggregation and sampled correlation logs."""
from __future__ import annotations

from datetime import datetime, timezone
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
        self._enabled = os.environ.get("WEBHOOK_METRICS_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self._lock = threading.Lock()
        self._processing = []
        self._responses = []
        self._pending = {}
        self._reply_to_invocation = {}
        self._message_to_invocation = {}
        self._early_deliveries = {}
        self._completed_message_ids = {}
        self._delivery_count = 0
        self._last_delivery = None
        self._last_event_lag_ms = 0.0
        self._delayed_events = 0
        self._duplicate_events = 0
        self._out_of_order_statuses = 0
        self._seen_meta_events = {}
        self._last_report = time.monotonic()

    def meta_event(self, event_epoch, received_epoch=None, event_key=None):
        """Measure provider timestamp lag at Render ingress, not after queueing."""
        if not self._enabled:
            return
        try:
            event_epoch = float(event_epoch)
        except (TypeError, ValueError):
            event_epoch = None
        received_epoch = time.time() if received_epoch is None else float(received_epoch)
        threshold_ms = max(
            0.0, float(os.environ.get("WEBHOOK_META_DELAY_THRESHOLD_SECONDS", "30")) * 1000,
        )
        with self._lock:
            if event_key:
                marker = hashlib.sha256(str(event_key).encode()).digest()
                if marker in self._seen_meta_events:
                    self._duplicate_events += 1
                else:
                    self._seen_meta_events[marker] = time.monotonic()
                    if len(self._seen_meta_events) > 120000:
                        self._seen_meta_events = dict(
                            list(self._seen_meta_events.items())[-120000:]
                        )
            if event_epoch is None:
                return
            lag_ms = max(0.0, (received_epoch - event_epoch) * 1000)
            self._last_event_lag_ms = lag_ms
            if lag_ms > threshold_ms:
                self._delayed_events += 1

    def invocation(self, payload):
        received = float(payload.get("_webhook_received_monotonic") or time.monotonic())
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                messages = change.get("value", {}).get("messages", [])
                if messages:
                    message = messages[0]
                    try:
                        event_epoch = float(message.get("timestamp"))
                    except (TypeError, ValueError):
                        event_epoch = None
                    return str(message.get("id") or "unknown"), received, event_epoch
        return None, received, None

    def processing(self, invocation_id, milliseconds):
        if not self._enabled or not invocation_id:
            return
        with self._lock:
            self._processing.append(milliseconds)
            self._sample(invocation_id, "processing", processing_ms=milliseconds)
            self._report_if_due()

    def reply(self, reply_id, invocation_id, received, event_epoch=None):
        if not self._enabled or not invocation_id:
            return
        with self._lock:
            self._prune_locked(time.monotonic())
            state = self._pending.setdefault(invocation_id, {
                "received": received,
                "event_epoch": event_epoch,
                "replies": set(),
                "delivery_ids": set(),
                "message_ids": set(),
                "successful_replies": 0,
                "delivered_replies": 0,
                "sending_complete": False,
                "last_delivery_epoch": None,
                "sent_at": None,
            })
            state["replies"].add(reply_id)
            self._reply_to_invocation[reply_id] = invocation_id

    def response(self, reply_id, status="", message_id=""):
        if not self._enabled:
            return
        now = time.monotonic()
        with self._lock:
            invocation_id = self._reply_to_invocation.pop(reply_id, None)
            if not invocation_id:
                return
            state = self._pending.get(invocation_id)
            if not state:
                return
            state["replies"].discard(reply_id)
            if status == "SENT" and message_id:
                state["successful_replies"] += 1
                state["message_ids"].add(message_id)
                if state["sent_at"] is None:
                    state["sent_at"] = now
                early = self._early_deliveries.pop(message_id, None)
                if early is None:
                    state["delivery_ids"].add(message_id)
                    self._message_to_invocation[message_id] = invocation_id
                else:
                    self._out_of_order_statuses += 1
                    state["delivered_replies"] += 1
                    state["last_delivery_epoch"] = early[1]
            if state["replies"]:
                return
            milliseconds = (now - state["received"]) * 1000
            self._responses.append(milliseconds)
            self._sample(invocation_id, "response", response_ms=milliseconds, status=status)
            state["sending_complete"] = True
            if state["successful_replies"] == 0:
                self._pending.pop(invocation_id, None)
            else:
                self._complete_delivery(invocation_id, state, now)
            self._report_if_due()

    def delivered(self, message_id, event_epoch=None):
        """Record receipt of Meta's delivered/read callback for an outbound reply."""
        if not self._enabled or not message_id:
            return
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            completed_at = self._completed_message_ids.get(message_id)
            if completed_at is not None and now - completed_at < 300:
                return
            invocation_id = self._message_to_invocation.pop(message_id, None)
            if invocation_id is None:
                # A callback can overtake the sender future. Keep a small,
                # short-lived correlation inbox; unrelated delivery callbacks
                # are bounded so metrics cannot consume unbounded memory.
                self._early_deliveries[message_id] = (now, event_epoch)
                cutoff = now - 300
                self._early_deliveries = {
                    key: value for key, value in list(self._early_deliveries.items())[-5000:]
                    if value[0] >= cutoff
                }
                return
            state = self._pending.get(invocation_id)
            if state is None or message_id not in state["delivery_ids"]:
                return
            state["delivery_ids"].discard(message_id)
            state["delivered_replies"] += 1
            state["last_delivery_epoch"] = event_epoch
            self._complete_delivery(invocation_id, state, now)

    def _complete_delivery(self, invocation_id, state, now):
        if not state["sending_complete"]:
            return
        if state["delivered_replies"] < state["successful_replies"]:
            return
        webhook_ms = (now - state["received"]) * 1000
        meta_ms = None
        if state["event_epoch"] is not None and state["last_delivery_epoch"] is not None:
            meta_ms = max(0.0, (state["last_delivery_epoch"] - state["event_epoch"]) * 1000)
        self._delivery_count += 1
        self._last_delivery = {
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reply_count": state["successful_replies"],
            "webhook_to_delivery_ms": round(webhook_ms, 3),
            "meta_event_e2e_ms": None if meta_ms is None else round(meta_ms, 3),
        }
        self._sample(invocation_id, "delivered", delivery_ms=webhook_ms)
        for message_id in state["message_ids"]:
            self._completed_message_ids[message_id] = now
        cutoff = now - 300
        self._completed_message_ids = {
            key: value for key, value in list(self._completed_message_ids.items())[-5000:]
            if value >= cutoff
        }
        self._pending.pop(invocation_id, None)

    def _prune_locked(self, now):
        expired = {
            invocation_id for invocation_id, state in self._pending.items()
            if now - state["received"] > 3600
        }
        if expired:
            self._pending = {
                key: value for key, value in self._pending.items() if key not in expired
            }
            self._reply_to_invocation = {
                key: value for key, value in self._reply_to_invocation.items()
                if value not in expired
            }
            self._message_to_invocation = {
                key: value for key, value in self._message_to_invocation.items()
                if value not in expired
            }

    def health(self) -> dict:
        """Return aggregate latency state without message or customer identifiers."""
        with self._lock:
            self._prune_locked(time.monotonic())
            return {
                "completed_invocations": self._delivery_count,
                "pending_invocations": sum(
                    1 for state in self._pending.values() if state["successful_replies"]
                ),
                "last_completed_invocation": (
                    None if self._last_delivery is None else dict(self._last_delivery)
                ),
            }

    def meta_health(self) -> dict:
        """Return provider-delay/retry signals without exposing event identifiers."""
        now = time.monotonic()
        with self._lock:
            self._prune_locked(now)
            pending_started = [
                state.get("sent_at") for state in self._pending.values()
                if state.get("sent_at") is not None and state["delivery_ids"]
            ]
            return {
                "last_event_lag_ms": round(self._last_event_lag_ms, 3),
                "delayed_events": self._delayed_events,
                "duplicate_events": self._duplicate_events,
                "out_of_order_statuses": self._out_of_order_statuses,
                "oldest_pending_delivery_seconds": round(
                    max(0.0, now - min(pending_started)), 3,
                ) if pending_started else 0.0,
            }

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
