"""WhatsApp webhook + conversation handling (Tech Doc sections 5, 13).

Serverless-friendly FastAPI app. Meta calls:
  GET  /webhook  -> verification handshake (hub.challenge)
  POST /webhook  -> inbound messages

Conversation flow (section 6, 23):
  plan selection -> reference id -> UPI intent -> pay -> submit UTR.
Admin verification happens out-of-band (payments.csv / admin UI).
"""
from __future__ import annotations
import traceback
import hashlib
import hmac
import json
import logging
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request, Response, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool
from domain.enums import PaymentStatus
from repositories.state_lock import state_lock
from repositories.state_lock import StateLockTimeout
from adapters.github import BranchAdvancedError
from application.webhook_metrics import WebhookMetrics
from adapters.payment_gateway import PaymentGatewayError
from application.events import (current_menu_event, daily_menu_shloka_available,
                                event_for_date, event_message, source_shloka_message)
from domain.clock import today_ist

app = FastAPI(title="Daily Darshan Webhook", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://vipseva.com"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["content-type"],
)
log = logging.getLogger("daily_darshan.webhook")
log.disabled = os.environ.get("WEBHOOK_LOGGING_ENABLED", "true").strip().lower() not in {
    "1", "true", "yes", "on",
}

# Composition root, guarded (P1a fix #1): a bad config / dependency must NOT
# crash import — otherwise the whole app (including /health) fails to start.
# We build the container lazily and record any failure so /health can report it.
container = None
_container_error: str | None = None
_webhook_actor = None
_webhook_actor_lock = threading.Lock()
_best_effort_initialized = False
_best_effort_sender_pool = None
_best_effort_sender_slots = None
_best_effort_sender_lock = threading.Lock()
_last_payment_refresh = 0.0
_best_effort_since_prune = 0
_webhook_metrics = WebhookMetrics(log)
_pending_karma_awards: set[str] = set()


def _get_container():
    global container, _container_error
    if container is None and _container_error is None:
        try:
            from config import Container
            container = Container()
        except Exception as exc:  # noqa: BLE001 - surface, don't crash import
            _container_error = f"{type(exc).__name__}: {exc}"
            log.exception("Container initialization failed")
    return container


# Attempt eager init at import, but never raise out of it.
_get_container()

_VERIFY_TOKEN = os.environ.get("WEBHOOK_VERIFY_TOKEN", "")
_UTR_RE = re.compile(r"^\d{12}$")
_REFERRAL_RE = re.compile(r"(?:^|[\s?&])ref(?:errer)?=([0-9]{7,15})\b", re.IGNORECASE)


@app.api_route("/health", methods=["GET", "HEAD"])
def health() -> Response:
    """Readiness probe (P1a). Reports degraded state instead of 500-ing.

    Supports GET and HEAD. HEAD returns the same status code (200/503) with no
    body — useful for lightweight liveness pings (e.g. the keepalive self-ping
    and uptime monitors) that only need the status line.

    Returns 200 only when the container built and core repositories are
    readable; 503 with a reason otherwise, so orchestrators can react.
    """
    c = _get_container()
    if c is None:
        return _json({"status": "unhealthy", "reason": _container_error or "init failed"}, 503)
    checks: dict = {}
    ok = True
    # Core store readability.
    try:
        c.subscribers.all()
        checks["subscribers_csv"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["subscribers_csv"] = f"error: {exc}"
        ok = False
    production = c.config.get("persistence", {}).get("mode") == "github_api"
    durable = bool(getattr(c, "repo_sync", None) and c.repo_sync.enabled)
    signed = bool(c.whatsapp_app_secret)
    whatsapp = bool(getattr(c.whatsapp, "is_configured", True))
    verified = bool(_VERIFY_TOKEN)
    checks["durable_persistence"] = "enabled" if durable else "local-only"
    checks["signature_verification"] = "enabled" if signed else "disabled"
    checks["whatsapp_delivery"] = "configured" if whatsapp else "missing credentials"
    checks["webhook_verification"] = "configured" if verified else "missing token"
    payment_gateway = getattr(c, "payment_gateway", None)
    if c.payment_service.payment_mode == "payment_gateway":
        gateway_ready = bool(payment_gateway and payment_gateway.is_configured)
        checks["payment_gateway"] = "configured" if gateway_ready else "missing credentials"
        ok = ok and gateway_ready
    # Local development intentionally supports CSV-only/no-secret operation.
    # A github_api deployment is production: accepting unsigned events or
    # acknowledging writes that cannot be persisted would lose user actions.
    best_effort = _best_effort_enabled()
    checks["webhook_mode"] = "best-effort-queue" if best_effort else "durable-synchronous"
    if production and not best_effort and not all((durable, signed, whatsapp, verified)):
        ok = False
    if production and best_effort and not all((signed, whatsapp, verified)):
        ok = False
    body = {"status": "ok" if ok else "degraded", "checks": checks}
    metrics_enabled = os.environ.get("WEBHOOK_METRICS_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }
    if metrics_enabled:
        actor = _webhook_actor
        body["webhook_metrics"] = (
            actor.metrics() if actor is not None
            else {
                "queue": {"worker_started": False, "depth": 0},
                "snapshot": {"last_snapshot_at": None, "next_snapshot_in_seconds": None},
            }
        )
    return _json(body, 200 if ok else 503)


def _json(payload: dict, status: int = 200) -> Response:
    return Response(content=json.dumps(payload), media_type="application/json", status_code=status)


def _best_effort_enabled() -> bool:
    return os.environ.get("WEBHOOK_BEST_EFFORT_QUEUE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _refresh_remote_payments(c, *, strict: bool) -> None:
    if not c.repo_sync.enabled:
        return
    payment_path = c.config["paths"]["payments_csv"]
    baseline, remote = c.repo_sync.read_latest(payment_path)
    conflicts = c.payments.merge_remote(baseline, remote, strict=strict)
    c.repo_sync.accept_remote(payment_path, remote)
    if conflicts:
        log.error("Payment refresh kept local conflicting fields conflicts=%s", conflicts)


def _maybe_refresh_remote_payments(c) -> None:
    global _last_payment_refresh
    interval = max(1.0, float(os.environ.get("WEBHOOK_PAYMENT_REFRESH_SECONDS", "15")))
    now = time.monotonic()
    if now - _last_payment_refresh >= interval:
        _refresh_remote_payments(c, strict=False)
        _last_payment_refresh = now


def _refresh_shared_csvs(c, *, strict: bool) -> None:
    """Merge every CSV concurrently written by Render and GitHub Actions."""
    if not c.repo_sync.enabled:
        return
    from application.semantic_csv_merge import merge_append_only, merge_keyed

    paths = c.config["paths"]
    specifications = [
        (paths["subscribers_csv"], c.subscribers._csv,
         dict(key_fields=("mobile",), union_fields=("applied_payment_refs",))),
        (paths["sentlog_csv"], c.sentlog._csv,
         dict(key_fields=("date", "mobile"), status_field="status")),
        (paths["renewals_csv"], c.renewals._csv,
         dict(key_fields=("mobile", "reminder_type", "expiry_date"), status_field="status")),
        (paths.get("welcomes_csv", "csv/welcomes.csv"), c.welcomes,
         dict(key_fields=("reference_id",), status_field="status")),
        (paths.get("image_reviews_csv", "csv/image_reviews.csv"), c.image_reviews,
         dict(key_fields=("id",), status_field="status",
              status_ranks={"PENDING": 0, "SUPERSEDED": 1, "APPROVED": 2})),
        (paths.get("pipeline_requests_csv", "csv/pipeline_requests.csv"), c.pipeline_requests,
         dict(key_fields=("id",))),
        (paths.get("karma_events_csv", "csv/karma_events.csv"), c.karma_events,
         dict(key_fields=("id",))),
    ]
    for path, repository, policy in specifications:
        baseline, remote = c.repo_sync.read_latest(path)
        conflicts = merge_keyed(repository, baseline, remote, strict=strict, **policy)
        c.repo_sync.accept_remote(path, remote)
        if conflicts:
            log.error("Semantic CSV merge used remote conflict values path=%s conflicts=%s", path, conflicts)

    log_path = paths["logs_csv"]
    baseline, remote = c.repo_sync.read_latest(log_path)
    merge_append_only(c.logs._csv, baseline, remote)
    c.repo_sync.accept_remote(log_path, remote)

    approved = {}
    for row in c.image_reviews.all():
        if row.get("status") == "APPROVED":
            approved.setdefault(row.get("date", ""), []).append(row.get("id", ""))
    duplicates = {day: ids for day, ids in approved.items() if day and len(ids) > 1}
    if duplicates:
        from application.semantic_csv_merge import SemanticMergeConflict
        raise SemanticMergeConflict(f"Multiple approved images after merge: {duplicates}")


def _is_critical_webhook(payload: dict) -> bool:
    """Financial/admin decisions and rejected-payment reviews commit immediately."""
    for message, _ctx in _iter_messages(payload):
        kind, value = _extract_input(message)
        value = (value or "").strip()
        if kind == "text" and value.upper() == "ADMIN":
            return True
        if kind == "button" and (
            value == "CTA_PAYMENT_REVIEW"
            or value.startswith(("ADM_", "UTR_CONFIRM_", "UTR_EDIT_"))
        ):
            return True
    return False


def _process_best_effort_batch(c, payloads, *, critical: bool = False):
    """Single-writer state transition plus bounded parallel reply transport."""
    global _best_effort_initialized
    from application.reply_outbox import (
        QueuedReplies, prepare_replies, snapshot_prepared_replies,
    )
    if not _best_effort_initialized and c.repo_sync.enabled:
        # Happens in the actor, never on the request path.
        c.repo_sync.pull(strict=False)
        from repositories.csv_repository import CSVRepository
        CSVRepository.reload_all_memory()
        _best_effort_initialized = True
    # Refresh the remotely mutable payment ledger at the latest Git head for
    # every actor batch. Critical commands reject a same-field conflict.
    if critical:
        _refresh_remote_payments(c, strict=True)
    else:
        _maybe_refresh_remote_payments(c)
    client = c.whatsapp
    existing = {row["id"] for row in c.reply_outbox.all()}
    current_metric = {"id": None, "received": 0.0}
    c.whatsapp = QueuedReplies(
        c.reply_outbox, c,
        on_enqueue=lambda reply_id: _webhook_metrics.reply(
            reply_id, current_metric["id"], current_metric["received"],
        ),
    )
    try:
        for payload in payloads:
            invocation_id, received = _webhook_metrics.invocation(payload)
            current_metric.update(id=invocation_id, received=received)
            processing_started = time.monotonic()
            if _process_messages(c, payload, restore_on_error=False):
                log.error("Best-effort message processing had isolated failures")
            _webhook_metrics.processing(
                invocation_id, (time.monotonic() - processing_started) * 1000,
            )
        reply_ids = {row["id"] for row in c.reply_outbox.all()} - existing
        prepared, _ = prepare_replies(
            c.reply_outbox, c, reply_ids=reply_ids, limit=max(1, len(reply_ids))
        )
        snapshots = snapshot_prepared_replies(c.reply_outbox, prepared)
    finally:
        c.whatsapp = client

    if critical:
        _flush_critical_snapshot(c)

    _submit_best_effort_replies(c, client, snapshots)
    _prune_best_effort_dedupe(c, len(payloads))


def _merge_best_effort_outcomes(c, outcome_batches):
    from application.reply_outbox import merge_reply_outcomes
    controls = [outcome for batch in outcome_batches for outcome in batch]
    karma = [item for item in controls if item.get("type") == "karma_share"]
    outcomes = [item for item in controls if item.get("type") != "karma_share"]
    for item in karma:
        if not c.karma_events.find(item["id"]):
            c.karma_events.upsert(item["id"], item)
        _pending_karma_awards.discard(item["id"])
    merge_reply_outcomes(c.reply_outbox, outcomes)
    c.message_statuses.reconcile(c.reply_outbox, consume=True)
    # This mode explicitly has no reply retry. Keeping full serialized reply
    # arguments for every successful transport until the 15-minute snapshot
    # can exhaust a free-tier instance under a sustained burst.
    completed_ids = {
        outcome["id"] for outcome in outcomes
        if outcome.get("status") in {"SENT", "DELIVERED", "CANCELLED"}
    }
    if completed_ids:
        c.reply_outbox.retain(lambda row: row.get("id") not in completed_ids)


def _prune_best_effort_dedupe(c, count):
    global _best_effort_since_prune
    _best_effort_since_prune += count
    interval = max(100, int(os.environ.get("WEBHOOK_DEDUPE_PRUNE_INTERVAL", "10000")))
    if _best_effort_since_prune < interval:
        return
    _best_effort_since_prune = 0
    limit = max(interval, int(os.environ.get("WEBHOOK_DEDUPE_MAX_ROWS", "120000")))
    removed = c.processed.prune_to_recent(limit)
    log.info("Best-effort dedupe compaction removed=%d retained_limit=%d", removed, limit)


def _get_sender_pool():
    global _best_effort_sender_pool, _best_effort_sender_slots
    if _best_effort_sender_pool is None:
        with _best_effort_sender_lock:
            if _best_effort_sender_pool is None:
                workers = max(1, int(os.environ.get("WEBHOOK_SENDER_WORKERS", "320")))
                capacity = max(workers, int(os.environ.get("WEBHOOK_SENDER_CAPACITY", "5000")))
                _best_effort_sender_pool = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="whatsapp-sender",
                )
                _best_effort_sender_slots = threading.BoundedSemaphore(capacity)
    return _best_effort_sender_pool, _best_effort_sender_slots


def _submit_best_effort_replies(c, client, snapshots):
    from application.reply_outbox import send_reply_snapshots
    pool, slots = _get_sender_pool()
    for snapshot in snapshots:
        if not slots.acquire(blocking=False):
            cancelled = dict(snapshot, status="CANCELLED", error="sender_overloaded")
            _webhook_metrics.response(snapshot["id"], "CANCELLED")
            _get_webhook_actor(c).enqueue_control([cancelled])
            continue
        started = time.monotonic()
        future = pool.submit(send_reply_snapshots, client, [snapshot])

        def completed(task, *, submitted_at=started):
            try:
                outcomes, _failed = task.result()
                for outcome in outcomes:
                    _webhook_metrics.response(outcome["id"], outcome.get("status", ""))
                _get_webhook_actor(c).enqueue_control(outcomes)
                log.debug("WhatsApp transport completed send_ms=%.1f", (time.monotonic() - submitted_at) * 1000)
            except Exception:
                log.exception("Unexpected asynchronous WhatsApp sender failure")
            finally:
                slots.release()

        future.add_done_callback(completed)


def _flush_best_effort_snapshot(c, *, strict=False, message="Batch webhook snapshot"):
    from repositories.csv_repository import CSVRepository
    # Rebase every shared business CSV semantically before serializing memory.
    _refresh_remote_payments(c, strict=strict)
    _refresh_shared_csvs(c, strict=strict)
    dirty_files = CSVRepository.flush_all_memory()
    if c.repo_sync.enabled:
        pushed = c.repo_sync.push(message, strict=strict)
        log.info("Best-effort Git snapshot dirty=%d files=%d", dirty_files, len(pushed))
    else:
        log.info("Best-effort local snapshot dirty=%d", dirty_files)


def _flush_critical_snapshot(c, message="Persist critical financial or admin webhook"):
    """Immediate admin/UTR durability barrier, serialized by the actor."""
    for attempt in range(3):
        try:
            _flush_best_effort_snapshot(
                c, strict=True, message=message,
            )
            return
        except BranchAdvancedError:
            c.repo_sync.abort()
            if attempt == 2:
                raise
            _refresh_remote_payments(c, strict=True)
            log.info("Critical webhook lost Git CAS race; retrying against latest head")


def _process_critical_webhook(c, payloads):
    try:
        _process_best_effort_batch(c, payloads, critical=True)
    except Exception:
        log.exception("Critical admin/UTR command was not durably committed")
        mobiles = {
            str(message.get("from", ""))
            for payload in payloads
            for message, _ctx in _iter_messages(payload)
            if message.get("from")
        }
        for mobile in mobiles:
            try:
                c.whatsapp.send_text(
                    mobile,
                    "This action could not be saved safely because repository state changed. "
                    "Please retry the command.",
                )
            except Exception:
                log.exception("Could not send critical-command failure response to %s", mobile)
        raise


def _get_webhook_actor(c):
    global _webhook_actor
    if _webhook_actor is None:
        with _webhook_actor_lock:
            if _webhook_actor is None:
                from application.webhook_actor import BestEffortWebhookActor
                _webhook_actor = BestEffortWebhookActor(
                    lambda payloads: _process_best_effort_batch(c, payloads),
                    lambda: _flush_best_effort_snapshot(c),
                    critical_processor=lambda payloads: _process_critical_webhook(c, payloads),
                    control_processor=lambda outcomes: _merge_best_effort_outcomes(c, outcomes),
                    capacity=int(os.environ.get("WEBHOOK_QUEUE_CAPACITY", "5000")),
                    batch_size=int(os.environ.get("WEBHOOK_BATCH_SIZE", "100")),
                    flush_seconds=float(os.environ.get("WEBHOOK_GIT_FLUSH_SECONDS", "900")),
                    logger=log,
                )
    return _webhook_actor


@app.post("/karma/share")
async def record_karma_share(request: Request) -> Response:
    """Queue one idempotent daily point after a subscription-page share handoff."""
    from domain.clock import today_ist, INDIA_TZ
    c = _get_container()
    if c is None:
        return _json({"status": "unavailable"}, 503)
    try:
        payload = await request.json()
        subscription_id = str(payload.get("subscription_id", "")).strip()
    except (ValueError, TypeError, AttributeError):
        return _json({"status": "invalid"}, 400)
    subscriber = next(
        (row for row in c.subscribers.all() if row.subscription_id == subscription_id),
        None,
    )
    if subscriber is None:
        return _json({"status": "not_found"}, 404)
    day = today_ist().isoformat()
    key = f"{subscription_id}:{day}"
    already = c.karma_events.find(key) is not None or key in _pending_karma_awards
    if not already:
        from datetime import datetime
        _pending_karma_awards.add(key)
        _get_webhook_actor(c).enqueue_control([{
            "type": "karma_share",
            "id": key,
            "subscription_id": subscription_id,
            "date": day,
            "points": "1",
            "recorded_at": datetime.now(INDIA_TZ).isoformat(),
        }])
    return _json({"status": "recorded", "awarded": not already, "date": day})


@app.get("/webhook")
def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> Response:
    """Meta webhook verification handshake (section 13)."""
    if mode == "subscribe" and token == _VERIFY_TOKEN and _VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="forbidden", status_code=403)


def _signature_valid(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 HMAC over the raw body (fix #2).

    If no app secret is configured, verification is skipped (returns True) so
    local/dev setups still work — but in production WHATSAPP_APP_SECRET should
    always be set. Uses constant-time comparison.
    """
    c = _get_container()
    secret = c.whatsapp_app_secret if c else ""
    if not secret:
        # Keep local development convenient, but fail closed whenever the app
        # is configured for its production GitHub-backed persistence mode.
        production = bool(c and c.config.get("persistence", {}).get("mode") == "github_api")
        return not production
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


@app.post("/internal/retry-replies")
async def retry_replies(request: Request) -> Response:
    """Authenticated scheduler wakeup; all writes share the webhook state lock."""
    c = _get_container()
    if _best_effort_enabled():
        return _json({"status": "disabled", "reason": "best-effort queue has no retry worker"})
    if c is None or not c.whatsapp_app_secret:
        return _json({"status": "unavailable"}, 503)
    raw = await request.body()
    if not _signature_valid(raw, request.headers.get("X-Hub-Signature-256")):
        return Response(content="invalid signature", status_code=403)
    try:
        timestamp = float(json.loads(raw)["timestamp"])
        if not abs(time.time() - timestamp) <= 300:
            raise ValueError("expired request")
    except (ValueError, KeyError, TypeError):
        return _json({"status": "invalid request"}, 400)
    if c.config.get("persistence", {}).get("mode") != "github_api":
        return _json({"status": "durable persistence required"}, 503)
    try:
        await run_in_threadpool(_process_payload_with_retries, c, {}, 0.0)
    except StateLockTimeout:
        # Normal backpressure: Meta will retry. Avoid an alarming traceback for
        # an expected overlap while another durable transaction is committing.
        log.warning("Webhook state is busy; returning 503 for provider retry")
        return _json({"status": "retry"}, 503)
    except Exception:
        log.exception("Reply retry requires attention")
        return _json({"status": "retry or reconciliation required"}, 503)
    return _json({"status": "processed"})


def _process_payment_gateway_webhook(c, raw_body: bytes, signature: str) -> dict:
    """Apply one signed, idempotent gateway decision and persist it immediately."""
    gateway = getattr(c, "payment_gateway", None)
    if c.payment_service.payment_mode != "payment_gateway" or gateway is None:
        raise PaymentGatewayError("Payment gateway mode is disabled")
    event = gateway.parse_webhook(raw_body, signature)
    if not event.reference_id or not event.terminal_status:
        return {"status": "ignored", "event": event.event_type}
    payment = c.payments.find(event.reference_id)
    if payment is None or payment.payment_provider != gateway.name:
        return {"status": "ignored", "event": event.event_type}

    # SUCCESS is monotonic: a late cancellation/expiry can never revoke an
    # entitlement already granted for a captured payment.
    if event.terminal_status == "SUCCESS":
        if event.external_payment_id:
            payment.gateway_payment_id = event.external_payment_id
            c.payments.update(payment)
        from admin import _verify_locked
        from types import SimpleNamespace
        result = _verify_locked(c, SimpleNamespace(
            reference_id=payment.reference_id,
            activate=True,
            renew=False,
            commit=False,
            skip_render=True,
        ))
        if result:
            raise RuntimeError(f"Automatic activation failed for {payment.reference_id}")
        # The signed gateway callback is the customer's authoritative payment
        # confirmation. Reuse the welcome ledger as the idempotency record so
        # duplicate paid/captured callbacks cannot send the approval twice,
        # and so the later welcome worker does not send a second activation
        # notification for the same payment.
        welcome = c.welcomes.find(payment.reference_id) or {}
        if welcome.get("status") in {"QUEUED", "FAILED", "CANCELLED", ""}:
            from domain.clock import today_ist
            from application.payment_messages import payment_approval_text
            subscriber = c.subscribers.find(payment.mobile)
            reservation = c.sentlog.reserve(
                today_ist(), payment.mobile, f"gateway-approval:{payment.reference_id}"
            )
            welcome.update({
                "reference_id": payment.reference_id,
                "mobile": payment.mobile,
                "status": "PENDING",
                "whatsapp_message_id": "",
                "error": "Automatic gateway approval notification pending",
            })
            c.welcomes.upsert(payment.reference_id, welcome)
            try:
                notification = c.whatsapp.send_text(
                    payment.mobile,
                    payment_approval_text(c.config, c.payments.find(payment.reference_id), subscriber),
                )
            except Exception:
                welcome.update(status="UNKNOWN", error="Approval notification transport raised")
                c.welcomes.upsert(payment.reference_id, welcome)
                raise
            welcome.update(
                status="SENT" if notification.ok else "UNKNOWN" if notification.unknown else "FAILED",
                whatsapp_message_id=notification.message_id or "",
                error=notification.error or "Automatic gateway approval; welcome suppressed",
            )
            c.welcomes.upsert(payment.reference_id, welcome)
            if reservation is not None:
                c.sentlog.complete(reservation, notification)
            _require_send(notification, "automatic gateway payment approval notification")
        c.logs.log("PAYMENT_GATEWAY_APPROVED", payment.mobile,
                   f"{payment.reference_id}:{event.event_type}")
    elif payment.status in {PaymentStatus.PENDING, PaymentStatus.SUPERSEDED}:
        payment.status = PaymentStatus.FAILED
        payment.rejected_at = datetime.now(ZoneInfo("Asia/Kolkata"))
        payment.superseded_at = None
        if event.external_payment_id:
            payment.gateway_payment_id = event.external_payment_id
        c.payments.update(payment)
        c.logs.log("PAYMENT_GATEWAY_REJECTED", payment.mobile,
                   f"{payment.reference_id}:{event.event_type}")
        from application.payment_messages import payment_rejection_text
        _require_send(c.whatsapp.send_text(
            payment.mobile, payment_rejection_text(c.config, payment)
        ), "gateway rejection notification")

    _flush_critical_snapshot(
        c, message=f"Persist critical gateway event for {payment.reference_id}"
    )
    return {"status": "processed", "event": event.event_type,
            "reference_id": payment.reference_id}


@app.post("/payments/webhook/razorpay")
async def receive_razorpay_webhook(request: Request) -> Response:
    """Receive Razorpay callbacks independently of Meta webhook signatures."""
    c = _get_container()
    if c is None:
        return _json({"status": "unavailable"}, 503)
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")
    try:
        outcome = await run_in_threadpool(
            _process_payment_gateway_webhook, c, raw_body, signature
        )
    except PaymentGatewayError as exc:
        log.warning("Rejected Razorpay webhook: %s", exc)
        return _json({"status": "rejected"}, 403)
    except Exception:
        # A non-2xx response asks Razorpay to redeliver; automatic approval
        # must not be acknowledged until the CSV/Git transaction completes.
        log.exception("Razorpay webhook processing failed")
        return _json({"status": "retry"}, 503)
    return _json(outcome)


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await request.body()

    # Reject forged/unsigned requests before doing any work (must be synchronous).
    if not _signature_valid(raw_body, request.headers.get("X-Hub-Signature-256")):
        return Response(content="invalid signature", status_code=403)

    c = _get_container()
    if c is None:
        if _best_effort_enabled():
            log.error("Dropping acknowledged webhook because container is unavailable: %s", _container_error)
            return _json({"status": "dropped"})
        log.error("Webhook received but container is unavailable: %s", _container_error)
        return _json({"status": "unavailable"}, 503)

    # Never 500 on a malformed/non-JSON body; ack and ignore (synchronous).
    try:
        payload = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        return _json({"status": "ignored"})
    if not isinstance(payload, dict):
        return _json({"status": "ignored"})
    payload["_webhook_received_monotonic"] = time.monotonic()

    if _best_effort_enabled():
        actor = _get_webhook_actor(c)
        critical = _is_critical_webhook(payload)
        accepted = actor.enqueue(payload, critical=True) if critical else actor.enqueue(payload)
        return _json({"status": "queued" if accepted else "dropped"})

    # Acknowledge only after synchronous durable processing.
    try:
        # Keep each lock attempt short on a small Render instance.  A bounded
        # retry is much less disruptive than making a second Meta delivery
        # wait behind a slow GitHub transaction for ten seconds.
        await run_in_threadpool(_process_payload_with_retries, c, payload, 2.0)
    except StateLockTimeout:
        log.warning("Webhook state is busy; returning 503 for provider retry")
        return _json({"status": "retry"}, 503)
    except Exception:
        log.exception("Webhook not completed; request must be retried")
        return _json({"status": "retry"}, 503)
    return _json({"status": "accepted"})

def _process_payload_with_retries(c, payload: dict, lock_timeout: float | None = None) -> None:
    """Retry contention-only webhook failures using a fresh durable snapshot.

    This never retries a generic processing failure.  In particular, a Meta
    response with an unknown outcome stays PENDING for reconciliation.  A
    branch-advance race happens before a send (or leaves a durable PENDING
    reservation), so repeating the transaction is safe and avoids pushing the
    work back to Meta merely because an Actions job committed at the same time.
    """
    attempts = 3
    for attempt in range(attempts):
        try:
            _process_payload(c, payload, lock_timeout)
            return
        except (StateLockTimeout, BranchAdvancedError) as exc:
            if attempt == attempts - 1:
                raise
            delay = 0.15 * (2 ** attempt)
            log.info(
                "Webhook transaction contention (%s); retrying in %.2fs (%d/%d)",
                type(exc).__name__, delay, attempt + 1, attempts,
            )
            time.sleep(delay)


def _process_payload(c, payload: dict, lock_timeout: float | None = None) -> None:
    """Persist intent, send outside the lock, then merge the provider outcome."""
    production = c.config.get("persistence", {}).get("mode") == "github_api"
    prepared_snapshots = []
    failed_phases = []
    # Background retries yield immediately to an occupied customer transaction.
    with state_lock(c.root, timeout=lock_timeout if payload else 0):
        # Capture only under the lock: another request temporarily installs
        # QueuedReplies on the shared container while handling its message.
        client = c.whatsapp
        if production and not c.repo_sync.enabled:
            raise RuntimeError("Durable persistence is unavailable")
        
        c.repo_sync.pull(strict=True)
        snapshot = _snapshot_webhook_state(c)
        prepared_replies = []
        
        try:
            if production:
                from application.reply_outbox import QueuedReplies, prepare_replies
                existing_reply_ids = {row['id'] for row in c.reply_outbox.all()}
                c.whatsapp = QueuedReplies(c.reply_outbox, c)
            
            # Phase 1: Message Processing
            if _process_messages(c, payload):
                failed_phases.append("_process_messages")

            if production:
                mobiles = {m.get('from', '') for m, _ in _iter_messages(payload)} if payload else None
                reply_ids = ({row['id'] for row in c.reply_outbox.all()} - existing_reply_ids) if payload else None
                prepared_replies, preparation_failed = prepare_replies(
                    c.reply_outbox, c, mobiles=mobiles, reply_ids=reply_ids,
                    limit=5)
                if not payload and preparation_failed:
                    failed_phases.append('prepare_replies')
            # In production this atomically persists both the inbound state and
            # each PENDING outbound reservation before Meta is contacted.
            c.repo_sync.push("Webhook update", strict=True)

            if production:
                from application.reply_outbox import snapshot_prepared_replies
                prepared_snapshots = snapshot_prepared_replies(c.reply_outbox, prepared_replies)
                if len(prepared_snapshots) != len(prepared_replies):
                    raise RuntimeError("Durable reply reservation disappeared before send")

        except Exception as e:
            log.exception(f"Exception raised inside primary state lock: {e}")
            _restore_webhook_state(snapshot)
            c.repo_sync.abort()
            raise
        finally:
            c.whatsapp = client

    # Phase 3: Outbound WhatsApp Send (Outside Lock)
    if production and prepared_snapshots:
        from application.reply_outbox import send_reply_snapshots, merge_reply_outcomes
        outcomes, send_failed = send_reply_snapshots(client, prepared_snapshots)
        # Persisted outbound failures belong to the independent retry worker.
        # Redelivering an already saved inbound event cannot repair them.
        if not payload and send_failed:
            failed_phases.append('send_reply_snapshots')
        # Reacquire only for the small fresh-read/merge/commit transaction.
        # Other webhook requests can progress while the Meta call is in flight.
        with state_lock(c.root, timeout=lock_timeout):
            c.repo_sync.pull(strict=True)
            snapshot = _snapshot_webhook_state(c)
            try:
                if merge_reply_outcomes(c.reply_outbox, outcomes):
                    failed_phases.append("merge_reply_outcomes")
                # A receipt may arrive while transport is outside the lock,
                # before its message ID has been attached to the outbox row.
                c.message_statuses.reconcile(c.reply_outbox)
                c.repo_sync.push("Persist webhook reply outbox", strict=True)
            except Exception as e:
                log.exception(f"Exception raised inside merge state lock: {e}")
                _restore_webhook_state(snapshot)
                c.repo_sync.abort()
                raise

    # Clear error reporting
    if failed_phases:
        log.error("Webhook processing failed in phases: %s", failed_phases)
        raise RuntimeError(f"One or more webhook responses need retry (Failures in: {', '.join(failed_phases)})")


def _process_messages(c, payload: dict, restore_on_error: bool = True) -> bool:
    """Handle a batch under the caller's state lock; report retriable failures."""
    failed = False
    for message, ctx in _iter_messages(payload):
        message_id = message.get("id", "")
        mobile = message.get("from", "")
        snapshot = {}
        # Isolate each message: a failure must not abort the batch.
        try:
            snapshot = _snapshot_webhook_state(c) if restore_on_error else {}
            # Dedupe on WhatsApp message id: skip a re-delivered message.
            if not c.processed.mark_if_new(message_id, mobile):
                continue
            kind, value = _extract_input(message)
            if mobile and value:
                state = c.conversations.find(mobile) or {"mobile": mobile, "version": "0", "last_recovery": "0"}
                # Meta can redeliver distinct inbound events hours later and
                # out of order.  A unique message ID is not sufficient to make
                # an older command safe: replaying it can rewind the
                # conversation and send an obsolete menu/payment response.
                # Claim the message ID above (so Meta may be acknowledged with
                # HTTP 200), but do not mutate business state or reply when a
                # newer event from this user has already been processed.
                now = time.time()
                try:
                    incoming_at = min(now, float(message.get("timestamp", now)))
                except (ValueError, TypeError):
                    incoming_at = now
                try:
                    last_inbound = float(state.get("last_inbound") or 0)
                except (ValueError, TypeError):
                    last_inbound = 0
                if last_inbound > 0 and incoming_at < last_inbound:
                    c.logs.log(
                        "STALE_WEBHOOK_IGNORED",
                        mobile,
                        f"incoming_at={incoming_at};last_inbound={last_inbound}",
                    )
                    continue
                recovery = ((kind == "text" and value.strip().upper() in {"CONTINUE", "RESEND"})
                            or (kind == "button" and value in {"CTA_CONTINUE", "CTA_RESEND"}))
                if (recovery
                        and time.time() - float(state.get("last_recovery") or 0) < 30):
                    continue
                state.update(version=str(int(state["version"]) + 1), last_inbound=str(incoming_at))
                c.conversations.upsert(mobile, state)
            referral = _REFERRAL_RE.search(value or "")
            if referral:
                c.referrals.upsert(message_id, {
                    "message_id": message_id,
                    "visitor_mobile": mobile,
                    "referrer_mobile": referral.group(1),
                    "recorded_at": datetime.now().isoformat(),
                })
            if mobile and value:
                name = _profile_name(ctx, mobile)
                _handle_message(c, mobile, kind, value, name)
        except CustomerIntentReplyFailed as exc:
            # STOP and received UTR are facts, independent of reply transport.
            # Preserve the fact and queue its acknowledgement in the sole outbox.
            from application.reply_outbox import QueuedReplies
            QueuedReplies(c.reply_outbox, c).send_text(exc.mobile, exc.text)
            failed = True
        except Exception:  # noqa: BLE001 - log + continue
            # The id is claimed first to prevent concurrent duplicate sends.
            # Release it after a failure so Meta can retry the user action.
            if restore_on_error:
                _restore_webhook_state(snapshot)
            failed = True
            log.exception("Failed handling message id=%s from=%s", message_id, mobile)

    for status in _iter_statuses(payload):
        message_id = str(status.get("id", ""))
        if not message_id:
            continue
        c.message_statuses.record(message_id, status.get("status"))
    c.message_statuses.reconcile(
        c.sentlog, c.renewals, c.welcomes, c.reply_outbox,
        consume=not restore_on_error,
    )

    return failed


def _iter_messages(payload: dict):
    """Yield (message, value) from Meta's nested webhook structure.

    `value` is the enclosing object that also carries `contacts[]` (profile
    names), so the handler can resolve the sender's display name.
    """
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                yield message, value


def _iter_statuses(payload: dict):
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            yield from change.get("value", {}).get("statuses", [])


def _webhook_paths(c) -> list[str]:
    paths = c.config["paths"]
    return [
        paths.get("image_reviews_csv", "csv/image_reviews.csv"),
        paths.get("pipeline_requests_csv", "csv/pipeline_requests.csv"),
        paths["subscribers_csv"], paths["payments_csv"],
        paths.get("processed_csv", "csv/processed.csv"), paths["logs_csv"],
        paths["sentlog_csv"], paths["renewals_csv"],
        paths.get("message_statuses_csv", "csv/message_statuses.csv"),
        paths.get("welcomes_csv", "csv/welcomes.csv"),
        paths.get("reply_outbox_csv", "csv/reply_outbox.csv"),
        paths.get("conversations_csv", "csv/conversations.csv"),
        paths.get("referrals_csv", "csv/referrals.csv"),
        paths.get("karma_events_csv", "csv/karma_events.csv"),
    ]


def _snapshot_webhook_state(c) -> dict[str, tuple[str, bytes | None]]:
    snapshot = {}
    for configured in _webhook_paths(c):
        full = configured if os.path.isabs(configured) else os.path.join(c.root, configured)
        try:
            with open(full, "rb") as source:
                content = source.read()
        except FileNotFoundError:
            content = None
        snapshot[configured] = (full, content)
    return snapshot


def _restore_webhook_state(snapshot: dict[str, tuple[str, bytes | None]]) -> None:
    for full, content in snapshot.values():
        if content is None:
            try:
                os.remove(full)
            except FileNotFoundError:
                pass
        else:
            with open(full, "wb") as output:
                output.write(content)


class CustomerIntentReplyFailed(RuntimeError):
    """A durable customer instruction succeeded, but its reply did not."""
    def __init__(self, mobile, text):
        super().__init__("Customer instruction saved; acknowledgement needs retry")
        self.mobile, self.text = mobile, text


def _send_intent_reply(c, mobile, text):
    try:
        result = c.whatsapp.send_text(mobile, text)
    except Exception:
        raise CustomerIntentReplyFailed(mobile, text) from None
    if not result.ok:
        raise CustomerIntentReplyFailed(mobile, text)


def _require_send(result, purpose: str) -> None:
    if not result.ok:
        raise RuntimeError(f"WhatsApp {purpose} failed: {result.error}")


def _profile_name(value: dict, wa_id: str) -> str:
    """Resolve the sender's WhatsApp profile name from value.contacts[].

    Matches the contact whose wa_id equals the message sender; falls back to the
    first contact's profile name, else empty.
    """
    contacts = value.get("contacts", []) or []
    for contact in contacts:
        if str(contact.get("wa_id", "")) == str(wa_id):
            return str(contact.get("profile", {}).get("name", "")).strip()
    if contacts:
        return str(contacts[0].get("profile", {}).get("name", "")).strip()
    return ""


def _extract_input(message: dict) -> tuple[str, str]:
    """Return (kind, value) for an inbound message.

    kind == "button"  -> value is the tapped reply/list id (a CTA id).
    kind == "text"     -> value is the typed text body.
    kind == ""         -> unsupported/empty.
    """
    mtype = message.get("type")
    if mtype == "text":
        return "text", message.get("text", {}).get("body", "")
    if mtype in {"image", "document"}:
        caption = message.get(mtype, {}).get("caption", "").strip()
        if re.fullmatch(r"(?:UTR\s*[:#-]?\s*)?\d{12}", caption, re.IGNORECASE):
            return "text", caption
        return "media", mtype
    if mtype == "interactive":
        interactive = message.get("interactive", {})
        for key in ("button_reply", "list_reply"):
            if key in interactive:
                return "button", interactive[key].get("id", "")
    return "", ""


def _has_active_subscription(c, mobile):
    sub = c.subscribers.find(mobile)
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return bool(sub and sub.status.value == "ACTIVE" and sub.end_date
                and sub.end_date >= today)


def _larger_plan_names(c, mobile: str) -> list[str]:
    """Return configured plans strictly larger than an active subscriber's plan."""
    sub = c.subscribers.find(mobile)
    if not _has_active_subscription(c, mobile) or not sub:
        return list(c.config["plans"])
    current_rank = _plan_rank(c, _effective_plan_name(c, sub))
    if current_rank is None:
        return []
    return [
        plan
        for plan in c.config["plans"]
        if (_plan_rank(c, plan) or (-1, -1)) > current_rank
    ]


def _is_expiring_soon(c, mobile: str) -> bool:
    """Renewal opens three IST calendar days before expiry, including expiry day."""
    sub = c.subscribers.find(mobile)
    if not _has_active_subscription(c, mobile) or not sub or not sub.end_date:
        return False
    remaining = (sub.end_date - datetime.now(ZoneInfo("Asia/Kolkata")).date()).days
    return 0 <= remaining <= 3


def _shows_subscription_status(c, subscriber) -> bool:
    """Only active or date-expired subscribers get a status menu row."""
    if not subscriber or not subscriber.end_date:
        return False
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    return subscriber.status.value == "ACTIVE" or subscriber.end_date < today


def _eligible_plan_names(c, mobile: str) -> list[str]:
    """Offer upgrades anytime; include same-plan renewal only near expiry."""
    sub = c.subscribers.find(mobile)
    if not sub or not _has_active_subscription(c, mobile):
        return list(c.config["plans"])
    larger = _larger_plan_names(c, mobile)
    current_plan = _effective_plan_name(c, sub)
    return [plan for plan in c.config["plans"]
            if plan in larger or (plan == current_plan and _is_expiring_soon(c, mobile))]


def _plan_rank(c, plan: str) -> tuple[int, float] | None:
    """Rank configured plans consistently by entitlement duration then price."""
    meta = c.config["plans"].get(plan)
    if not meta:
        return None
    return int(meta.get("days", 0)), float(meta.get("amount", 0))


def _supersede_lower_unpaid_checkouts(c, mobile: str) -> None:
    """Retire unpaid checkouts no longer eligible for the active subscriber.

    A UTR is financial evidence and must remain reviewable even when its plan is
    lower. Only untouched PENDING instructions are made obsolete here.
    """
    sub = c.subscribers.find(mobile)
    if not sub or not _has_active_subscription(c, mobile):
        return
    current_rank = _plan_rank(c, _effective_plan_name(c, sub))
    if current_rank is None:
        return
    eligible = _eligible_plan_names(c, mobile)
    for payment in c.payments.all():
        if (payment.mobile != mobile or payment.status.value != "PENDING" or payment.utr):
            continue
        payment_rank = _plan_rank(c, payment.plan)
        if payment.plan in eligible:
            continue
        payment.mark_superseded()
        c.payments.update(payment)
        event = ("PAYMENT_SUPERSEDED_LOWER_PLAN" if payment_rank is None or payment_rank < current_rank
                 else "PAYMENT_SUPERSEDED_OUTSIDE_RENEWAL_WINDOW")
        c.logs.log(event, mobile, payment.reference_id)


def _applied_payment_refs(sub) -> set[str]:
    from application.payment_references import parse_applied_payment_refs
    return parse_applied_payment_refs(sub.applied_payment_refs)


def _effective_plan_name(c, sub) -> str:
    """Resolve entitlement from the subscriber and its applied payment markers.

    Manual recovery can leave ``sub.plan`` stale while atomically recording an
    applied payment reference.  Applied references are authoritative admin
    state, so menu eligibility uses the largest applied entitlement and never
    offers an accidental downgrade.
    """
    candidates = [sub.plan] if sub.plan in c.config["plans"] else []
    for reference in _applied_payment_refs(sub):
        payment = c.payments.find(reference)
        if payment and payment.mobile == sub.mobile and payment.plan in c.config["plans"]:
            candidates.append(payment.plan)
    if not candidates:
        return sub.plan
    return max(candidates, key=lambda plan: (
        int(c.config["plans"][plan].get("days", 0)),
        float(c.config["plans"][plan].get("amount", 0)),
    ))


def _approved_source_for_date(c, on_date) -> str:
    """Read the one administrator-approved source for a business date."""
    rows = [row for row in c.image_reviews.all()
            if row.get("date") == on_date.isoformat() and row.get("status") == "APPROVED"]
    return rows[0].get("source", "") if len(rows) == 1 else ""


def _approved_source_for_today(c) -> str:
    return _approved_source_for_date(c, today_ist())


def _send_menu(c, mobile: str) -> None:
    """Show actions appropriate to entitlement and the current checkout."""
    sub = c.subscribers.find(mobile)
    payment = _checkout_payment(c, mobile)
    superseded_review = _latest_superseded_utr(c, mobile) if payment is None else None
    # An abandoned unpaid checkout may outlive a deleted subscriber row.
    # Restart signup, but retain payment evidence and reviewed/approved states.
    if not sub and payment and payment.status.value == "PENDING" and not payment.utr:
        payment = None
    active = _has_active_subscription(c, mobile)
    larger_plans = _larger_plan_names(c, mobile) if active else []
    expiring_soon = _is_expiring_soon(c, mobile)
    eligible_plans = _eligible_plan_names(c, mobile)
    rows = [("CTA_STATUS", "Subscription status", "Check your subscription")] if _shows_subscription_status(c, sub) else []
    festival = current_menu_event(c.config.get("events"))
    body = "🙏 Radhe Radhe! Choose an option below."
    locked_payment = bool(
        payment and (payment.status.value != "PENDING" or payment.utr)
    )
    if sub and (not sub.end_date or payment) and not locked_payment and (sub.awaiting_name or not sub.name):
        c.subscriber_service.set_awaiting_name(mobile, True)
        body = "🙏 What name should we greet you by? Reply with your name, or choose another plan."
        rows.append(("CTA_SUBSCRIBE", "Change plan", "Choose a different plan"))
    elif sub and not sub.opt_in and not sub.end_date and not locked_payment:
        _request_opt_in(c, mobile)
        return
    elif payment:
        reviewing = payment.utr or payment.status.value != "PENDING"
        rows.append(("CTA_PAYMENT", "Payment status" if reviewing else "Payment instructions",
                     "View your payment details"))
        if payment.status.value == "PENDING" and not payment.utr and (not active or eligible_plans):
            if active:
                label = "Renew" if expiring_soon else "Upgrade"
            else:
                label = "Renew" if (sub and sub.end_date) else "Change plan"
            if active and expiring_soon:
                description = "Renew or choose a larger plan" if larger_plans else "Renew your current plan"
            else:
                description = "Choose a larger plan" if active else "Choose a different plan"
            rows.append(("CTA_RENEW", label, description))
        # Keep financial details behind the explicit Payment status list
        # option. The menu body stays generic and safe to glance at/share.
    elif active:
        if not sub.opt_in:
            rows.append(("CTA_RESUME_MESSAGES", "Resume messages", "Restore consent without paying"))
        if eligible_plans:
            if expiring_soon:
                description = "Renew or choose a larger plan" if larger_plans else "Renew your current plan"
                rows.append(("CTA_RENEW", "Renew", description))
            else:
                rows.append(("CTA_RENEW", "Upgrade", "Choose a larger plan"))
    else:
        rows.append(("CTA_RENEW", "Renew", "Renew your subscription") if sub and sub.end_date
                    else ("CTA_SUBSCRIBE", "View plans", "Choose a plan"))
        if sub and sub.is_expired(datetime.now(ZoneInfo("Asia/Kolkata")).date()):
            body = f"Your subscription expired on {sub.end_date}. Choose a renewal plan."
    if superseded_review:
        rows.append((
            "CTA_PAYMENT_REVIEW",
            "Review past UTR",
            f"Ask admin to recheck {superseded_review.reference_id}",
        ))
    if _has_active_subscription(c, mobile) and not sub.opt_in and not any(r[0] == "CTA_RESUME_MESSAGES" for r in rows):
        rows.append(("CTA_RESUME_MESSAGES", "Resume messages", "Restore consent without paying"))
    if festival:
        body = f"{event_message(festival)}\n\n{body}"
    elif daily_menu_shloka_available(c.config.get("daily_shloka_menu")):
        # At 06:00 IST every normal day starts with the neutral fallback. Once
        # today's approval lands, this switches to the actual selected source.
        approved_source = _approved_source_for_today(c) or "fallback"
        if approved_source:
            source_content = source_shloka_message(
                approved_source, c.config.get("daily_shlokas", {})
            )
            if source_content:
                body = f"{source_content}\n\n{body}"
    else:
        # Until the 06:00 IST reset, retain yesterday's devotional context.
        # This avoids an empty transition window after midnight.
        previous_day = today_ist() - timedelta(days=1)
        previous_event = event_for_date(c.config.get("events"), previous_day)
        if previous_event:
            body = f"{event_message(previous_event)}\n\n{body}"
        else:
            previous_source = _approved_source_for_date(c, previous_day) or "fallback"
            source_content = source_shloka_message(
                previous_source, c.config.get("daily_shlokas", {}), day_label="Yesterday's"
            )
            if source_content:
                body = f"{source_content}\n\n{body}"
    result = c.whatsapp.send_list(
        mobile,
        (("🙏 Welcome to Daily Darshan! Receive daily new temple darshan on WhatsApp, enjoy an HD image "
          "on your personal page, and share the image with family and friends. "
          "Choose a plan, make payment and confirm your UTR to request activation.\n\n")
         if not any(row.get("mobile") == mobile for row in c.welcomes.all()) else "") + body,
        "Open menu",
        rows,
    )
    _require_send(result, "menu")


def _send_plan_list(c, mobile: str) -> None:
    """Send the plan catalog as a tappable list (ids = PLAN_<plan>)."""
    payment = _checkout_payment(c, mobile)
    if payment and (payment.status.value != "PENDING" or payment.utr):
        _resume_conversation(c, mobile)
        return
    plan_names = _eligible_plan_names(c, mobile)
    if _has_active_subscription(c, mobile) and not plan_names:
        sub = c.subscribers.find(mobile)
        _require_send(c.whatsapp.send_text(
            mobile,
            f"You already have the largest available plan: {_effective_plan_name(c, sub).capitalize()}. "
            "Same-plan renewal opens three days before expiry. "
            "Send MENU to check your subscription status.",
        ), "plan list")
        return
    rows = []
    for plan in plan_names:
        meta = c.config["plans"][plan]
        amount = meta.get("amount")
        days = meta.get("days")
        rows.append((f"PLAN_{plan}", plan.capitalize(), f"₹{amount} · {days} days"))
    if _is_expiring_soon(c, mobile):
        prompt = "Choose a renewal plan:"
    elif _has_active_subscription(c, mobile):
        prompt = "Choose a larger Daily Darshan plan to upgrade:"
    else:
        prompt = "Choose your Daily Darshan plan:"
    result = c.whatsapp.send_list(mobile, prompt, "View plans", rows)
    _require_send(result, "plan list")


def _request_opt_in(c, mobile: str) -> None:
    """Show the consent disclosure and an explicit Agree button (#9)."""
    result = c.whatsapp.send_buttons(
        mobile,
        "By continuing, you agree to receive a *daily darshan* image on WhatsApp "
        "and occasional subscription updates from Daily Darshan. You can stop "
        "anytime by replying STOP. Do you agree?",
        [("CTA_OPTIN_AGREE", "I agree"), ("CTA_STOP", "No thanks")],
    )
    _require_send(result, "consent request")


def _after_name_or_optin(c, mobile: str, returning: bool) -> None:
    """Gate on explicit opt-in: request consent if not yet granted, else pay."""
    sub = c.subscribers.find(mobile)
    if sub is None:
        _send_menu(c, mobile)
        return
    if not sub.name:
        c.subscriber_service.set_awaiting_name(mobile, True)
        _require_send(c.whatsapp.send_text(mobile, "🙏 What name should we greet you by?"), "name request")
        return
    if not sub.opt_in:
        _request_opt_in(c, mobile)
        return
    payment = _latest_pending_payment(c, mobile)
    _start_payment(c, mobile, payment.plan if payment else sub.plan or _default_plan(c),
                   returning=sub.end_date is not None)


def _handle_message(c, mobile: str, kind: str, value: str, name: str = "") -> None:
    """CTA-driven conversation state machine with explicit opt-in/opt-out.

    Buttons/list ids: CTA_SUBSCRIBE/CTA_RENEW -> plans;
    PLAN_<plan> -> chosen plan; CTA_OPTIN_AGREE -> record consent + pay;
    CTA_STOP -> opt out. Free text ONLY for name (when awaiting) and 12-digit
    UTR (and STOP-family keywords). Phone is implicit.
    """
    svc = c.subscriber_service
    wa = c.whatsapp
    if (kind == "text" and value.strip().upper() == "ADMIN") or (kind == "button" and value.startswith("ADM_")):
        from application.admin_whatsapp import handle_admin
        handle_admin(c, mobile, value.strip())
        return

    if kind == "media":
        _require_send(wa.send_text(mobile,
            "Thanks for sharing. Please send your payment reference and 12-digit UTR as text "
            "in the format *UTR Txn_Ref_ID UTR_ID*. Example: *UTR DD2609130001 123456789012*. "
            "A screenshot alone cannot be recorded for payment review."), "UTR text request")
        return

    # ---------------- Button / list taps (CTAs) ---------------- #
    if kind == "button":
        if value.startswith(("UTR_CONFIRM_", "UTR_EDIT_")):
            _handle_utr_confirmation(c, mobile, value)
            return
        payment = _checkout_payment(c, mobile)
        if (payment and (payment.status.value != "PENDING" or payment.utr)
                and (value in {"CTA_SUBSCRIBE", "CTA_RENEW"} or value.startswith("PLAN_"))):
            _resume_conversation(c, mobile)
            return
        if value == "CTA_STATUS":
            _send_subscription_status(c, mobile)
            return
        if value == "CTA_PAYMENT":
            _resume_conversation(c, mobile)
            return
        if value == "CTA_PAYMENT_REVIEW":
            review_payment = _latest_superseded_utr(c, mobile) if payment is None else None
            if review_payment:
                from domain.enums import PaymentStatus
                review_payment.status = PaymentStatus.PENDING
                review_payment.rejected_at = None
                review_payment.superseded_at = None
                c.payments.update(review_payment)
                c.logs.log("PAYMENT_REVIEW_REQUESTED", mobile, review_payment.reference_id)
                _require_send(wa.send_text(
                    mobile,
                    f"Your review request for {review_payment.reference_id} is pending administrator verification. "
                    "Please do not pay again while it is under review.",
                ), "review request")
            else:
                _send_menu(c, mobile)
            return
        if value == "CTA_HELP":
            _send_menu(c, mobile)
            return
        if value == "CTA_RESUME_MESSAGES":
            if _has_active_subscription(c, mobile):
                result = wa.send_buttons(mobile,
                    "Do you agree to receive Daily Darshan and subscription updates again? Reply STOP anytime to opt out. Your paid dates will not change.",
                    [("CTA_RESUME_AGREE", "I agree"), ("CTA_STOP", "No thanks")])
                _require_send(result, "resume consent")
            else:
                _send_menu(c, mobile)
            return
        if value == "CTA_RESUME_AGREE":
            if _has_active_subscription(c, mobile):
                svc.grant_opt_in(mobile, "whatsapp_resume")
                _require_send(wa.send_text(mobile, "Daily Darshan messages are enabled. Your paid subscription and any payment under review are unchanged. 🙏"), "consent restored")
            else:
                _send_menu(c, mobile)
            return
        if value in {"CTA_CONTINUE", "CTA_RESEND", "CTA_BACK"}:
            _handle_message(c, mobile, "text", value.removeprefix("CTA_"), name)
            return
        if value == "CTA_SUBSCRIBE":
            _send_plan_list(c, mobile)
            return
        if value == "CTA_RENEW":
            _handle_renew(c, mobile, name)
            return
        if value == "CTA_STOP":
            _handle_opt_out(c, mobile)
            return
        if value == "CTA_OPTIN_AGREE":
            sub = c.subscribers.find(mobile)
            if sub is None:
                _send_menu(c, mobile)
                return
            svc.grant_opt_in(mobile, "whatsapp_cta")
            if _has_active_subscription(c, mobile) and not _latest_pending_payment(c, mobile):
                _require_send(wa.send_text(mobile,
                    "Daily Darshan messages are enabled. Your paid subscription is unchanged. 🙏"), "consent restored")
                return
            # Returning = already had subscription dates before this checkout.
            returning = sub.end_date is not None
            _after_name_or_optin(c, mobile, returning=returning)
            return
        if value.startswith("PLAN_"):
            plan = value[len("PLAN_"):]
            if plan not in c.config["plans"]:
                _send_plan_list(c, mobile)
                return
            sub = c.subscribers.find(mobile)
            if _has_active_subscription(c, mobile) and plan not in _eligible_plan_names(c, mobile):
                _send_plan_list(c, mobile)
                return
            if sub and sub.end_date:
                # A checkout must not alter the paid plan or entitlement before approval.
                if payment is None or payment.plan != plan:
                    c.payment_service.create_payment(mobile, plan)
            else:
                svc.upsert_pending(mobile, plan, name if not sub or not sub.name else "")
                if payment and payment.plan != plan:
                    c.payment_service.create_payment(mobile, plan)
            sub = c.subscribers.find(mobile)
            if not sub.name:
                svc.set_awaiting_name(mobile, True)
                _require_send(wa.send_text(mobile, "🙏 What name should we greet you by?"), "name request")
                return
            _after_name_or_optin(c, mobile, returning=False)
            return
        # Unknown/expired button id -> re-show the menu.
        _send_menu(c, mobile)
        return

    # ---------------- Free text (name / UTR / STOP only) ---------------- #
    text = value.strip()
    referenced_utr = re.fullmatch(r"UTR\s+(DD\d{10})\s+(\d{12})", text, re.IGNORECASE)
    utr_text = re.fullmatch(r"UTR\s*[:#-]?\s*(\d{12})", text, re.IGNORECASE)
    if utr_text:
        text = utr_text.group(1)

    if text.upper() == "STATUS":
        _send_subscription_status(c, mobile)
        return
    if text.upper() in {"CONTINUE", "RESEND"}:
        state = c.conversations.find(mobile) or {"mobile": mobile, "version": "0"}
        if time.time() - float(state.get("last_recovery") or 0) < 30:
            return
        _resume_conversation(c, mobile)
        state["last_recovery"] = str(time.time())
        c.conversations.upsert(mobile, state)
        return

    # (0) Opt-out keywords (typed). Meta expects STOP to work as free text too.
    if text.upper() in ("STOP", "UNSUBSCRIBE", "CANCEL"):
        _handle_opt_out(c, mobile)
        return

    if (text.upper() in {
            "HI", "HELLO", "RADHE RADHE", "RENEW", "SUBSCRIBE", "MENU", "START",
            "PAYMENT", "PAY", "PAYMENT STATUS", "PAYMENT INSTRUCTIONS",
        }
            or text.upper().startswith("RADHE RADHE ")):
        _send_menu(c, mobile)
        return

    # Backtracking is deliberately safe: it only changes conversational state,
    # never payment status or consent. A stale CTA cannot apply a new plan.
    if text.upper() in {"BACK", "GO BACK", "PREVIOUS"}:
        sub = c.subscribers.find(mobile)
        if sub is not None and sub.awaiting_name:
            svc.set_awaiting_name(mobile, False)
            _send_plan_list(c, mobile)
            return
        _send_menu(c, mobile)
        return

    # (a) Awaiting the user's name -> capture it (a UTR is never a name).
    if svc.is_awaiting_name(mobile) and not _UTR_RE.match(text) and not referenced_utr:
        if (not any(ch.isalpha() for ch in text) or any(ch.isdigit() for ch in text) or "?" in text
                or re.match(r"^(?:UTR\b|PAYMENT\b|HOW\s|WHAT\s|HELP\b)", text, re.IGNORECASE)):
            _require_send(wa.send_text(mobile, "Please reply with your name using letters, not a payment reference or question. Send MENU for your options. 🙏"), "name request")
        else:
            svc.set_name(mobile, text)
            svc.set_awaiting_name(mobile, False)
            _after_name_or_optin(c, mobile, returning=False)
        return

    # (b) UTR submission.
    if _UTR_RE.match(text) or referenced_utr:
        if c.payment_service.payment_mode == "payment_gateway":
            _require_send(c.whatsapp.send_text(
                mobile,
                "UTR submission is not required for this checkout. Send MENU and open Payment instructions; the gateway confirms payment automatically.",
            ), "gateway payment guidance")
            return
        payment = _latest_pending_payment(c, mobile)
        if referenced_utr:
            reference, text = referenced_utr.groups()
            payment = c.payments.find(reference.upper())
            if (
                not payment
                or payment.mobile != mobile
                or payment.status.value not in {"PENDING", "SUPERSEDED"}
                or (payment.status.value == "SUPERSEDED" and not _superseded_review_open(c, payment))
            ):
                _require_send(wa.send_text(mobile, "That reference is not an open payment for your account. Send MENU to check payment status."), "payment reference")
                return
            other_review = any(p.mobile == mobile and p.reference_id != payment.reference_id
                               and p.status.value == "PENDING" and p.utr for p in c.payments.all())
            if other_review:
                _require_send(wa.send_text(mobile, "Another payment is under review. Please wait for administrator verification and do not pay again."), "payment review")
                return
        elif payment and payment.utr:
            _resume_conversation(c, mobile)
            return
        elif any(p.mobile == mobile and p.status.value == "SUPERSEDED" for p in c.payments.all()):
            _require_send(wa.send_text(mobile, "You have changed checkout plans. To match your payment correctly, send UTR followed by the reference from the instructions you paid against and your 12-digit UTR, for example: *UTR DD2609130001 123456789012*. Do not pay again."), "payment reference")
            return
        if payment is None:
            _send_menu(c, mobile)
            return
        if payment.utr and not referenced_utr:
            _resume_conversation(c, mobile)
            return
        state = c.conversations.find(mobile) or {"mobile": mobile, "version": "0"}
        token = uuid4().hex
        state.update(utr_draft=text, utr_reference=payment.reference_id, utr_confirmation=token)
        c.conversations.upsert(mobile, state)
        _send_utr_confirmation(c, mobile, state)
        return

    # (c) Anything else typed -> present the CTA menu (no free-text commands).
    _send_menu(c, mobile)


def _send_utr_confirmation(c, mobile: str, state: dict) -> None:
    token = state["utr_confirmation"]
    _require_send(c.whatsapp.send_buttons(mobile,
        f"Please check your UTR {state['utr_draft']} for payment {state['utr_reference']}.\n"
        "Is this correct? Confirm to submit it for admin verification, or change it. "
        "This UTR has not been submitted for review yet.",
        [(f"UTR_CONFIRM_{token}", "Confirm UTR"), (f"UTR_EDIT_{token}", "Change UTR")],
    ), "UTR confirmation")


def _handle_utr_confirmation(c, mobile: str, value: str) -> None:
    """Only the current, sender-bound draft can become payment evidence."""
    state = c.conversations.find(mobile) or {}
    token = state.get("utr_confirmation")
    if not token or value not in {f"UTR_CONFIRM_{token}", f"UTR_EDIT_{token}"}:
        _require_send(c.whatsapp.send_text(mobile,
            "This confirmation is no longer current. Send your payment reference and UTR again to check it."), "stale UTR confirmation")
        return
    reference = state.get("utr_reference", "")
    payment = c.payments.find(reference)
    if (
        not payment
        or payment.mobile != mobile
        or payment.status.value not in {"PENDING", "SUPERSEDED"}
        or (payment.status.value == "SUPERSEDED" and not _superseded_review_open(c, payment))
    ):
        _send_menu(c, mobile)
        return
    if value.startswith("UTR_EDIT_"):
        state.update(utr_draft="", utr_reference="", utr_confirmation="")
        c.conversations.upsert(mobile, state)
        _require_send(c.whatsapp.send_text(mobile,
            f"Please send the correct UTR, for example: *UTR {reference} 123456789012*. "
            "You will be asked to confirm it before submission. Do not pay again."), "change UTR")
        return
    if any(p.mobile == mobile and p.reference_id != reference
           and p.status.value == "PENDING" and p.utr for p in c.payments.all()):
        _require_send(c.whatsapp.send_text(mobile,
            "Another payment is under review. Please wait for administrator verification and do not pay again."), "payment review")
        return
    utr = state.get("utr_draft", "")
    c.payment_service.record_utr(reference, utr, reconcile_checkout=True)
    state.update(utr_draft="", utr_reference="", utr_confirmation="")
    c.conversations.upsert(mobile, state)
    _send_intent_reply(c, mobile,
        f"Your latest UTR {utr} for payment {reference} has been recorded.\n"
        "It replaced the previous UTR (if any) and is now awaiting admin verification.\n"
        "We aim to review it within 24 hours. You do not need to pay again.\n"
        "Please send MENU to check payment status.")


def _handle_opt_out(c, mobile: str) -> None:
    """Honor STOP: revoke consent, confirm, stop business-initiated sends (#9)."""
    sub = c.subscriber_service.revoke_opt_in(mobile)
    if sub is None:
        _send_intent_reply(c, mobile, "You're not subscribed. Send Radhe Radhe anytime to see the menu. 🙏")
        return
    _send_intent_reply(c,
        mobile,
        "You've been opted out — you won't receive further Daily Darshan messages. "
        "Your paid subscription dates are unchanged. Send Radhe Radhe anytime, then choose "
        "Resume messages if available, or Payment instructions for an existing checkout. If expired, choose Renew. 🙏",
    )


def _handle_renew(c, mobile: str, name: str = "") -> None:
    """Offer same/larger renewal near expiry, otherwise strictly larger upgrades."""
    _send_plan_list(c, mobile)


def _start_payment(c, mobile: str, plan: str, returning: bool = False) -> None:
    """Create the payment + UPI intent and message it to the user.

    `returning=True` uses renewal wording for an existing subscriber.
    """
    payment = _checkout_payment(c, mobile)
    if payment and (payment.status.value != "PENDING" or payment.utr):
        _require_send(c.whatsapp.send_text(mobile, _payment_status_text(c, payment)), "payment status")
        return
    if _has_active_subscription(c, mobile) and plan not in _eligible_plan_names(c, mobile):
        _send_plan_list(c, mobile)
        return
    if payment is None or payment.plan != plan:
        payment = c.payment_service.create_payment(mobile, plan)
    _send_payment_instructions(c, mobile, payment, returning)


def _send_payment_instructions(c, mobile, payment, returning=False):
    if payment.utr or payment.status.value not in {"PENDING", "SUPERSEDED"}:
        _require_send(c.whatsapp.send_text(mobile, _payment_status_text(c, payment)), "payment status")
        return
    if payment.status.value == "SUPERSEDED":
        _send_plan_list(c, mobile)
        return
    if _has_active_subscription(c, mobile) and payment.plan not in _eligible_plan_names(c, mobile):
        _supersede_lower_unpaid_checkouts(c, mobile)
        _send_plan_list(c, mobile)
        return
    wa = c.whatsapp
    sub = c.subscribers.find(mobile)
    greeting = f"Radhe Radhe {sub.name} Ji! " if sub and sub.name else ""
    plan = payment.plan
    if returning:
        header = f"{greeting}Renewing your {plan} plan.\nAmount: ₹{payment.amount:g}\n"
    else:
        header = f"{greeting}Plan: {plan}\nAmount: ₹{payment.amount:g}\n"
    if c.payment_service.payment_mode == "payment_gateway":
        try:
            payment = c.payment_service.ensure_gateway_checkout(payment)
        except Exception:
            log.exception("Payment gateway checkout creation failed for %s", payment.reference_id)
            _require_send(wa.send_text(
                mobile,
                "Payment checkout is temporarily unavailable. Please send MENU and try again shortly; no payment was taken.",
            ), "payment gateway unavailable")
            return
        action = "Renew" if returning else "Pay"
        _require_send(wa.send_text(
            mobile,
            f"{header}{action} securely using this payment link:\n{payment.checkout_url}\n\n"
            f"Reference: {payment.reference_id}\n"
            "Your subscription will be updated automatically after the gateway confirms payment. "
            "You do not need to send a UTR.",
        ), "payment gateway instruction")
        return
    intent = c.payment_service.generate_upi_intent(payment)
    result = wa.send_text(
        mobile,
        f"{header}"
        f"Pay via UPI:\n{intent}\n\n"
        f"Reference: {payment.reference_id}\n"
        f"After paying, reply with your payment reference and 12-digit UTR.\n"
        f"Format: *UTR Txn_Ref_ID UTR_ID*\nExample: *UTR {payment.reference_id} 123456789012*\n"
        "We will ask you to confirm the UTR before submitting it for admin verification.\n"
        f"If you changed plans, use the reference from the instructions you paid against. "
        "If you already paid against older instructions, use that older reference; do not pay again.",
    )
    _require_send(result, "payment instruction")


def _resume_conversation(c, mobile):
    state = c.conversations.find(mobile) or {}
    draft_payment = c.payments.find(state.get("utr_reference", "")) if state.get("utr_confirmation") else None
    if (
        draft_payment
        and draft_payment.mobile == mobile
        and (
            draft_payment.status.value == "PENDING"
            or (
                draft_payment.status.value == "SUPERSEDED"
                and _superseded_review_open(c, draft_payment)
            )
        )
    ):
        _send_utr_confirmation(c, mobile, state)
        return
    sub = c.subscribers.find(mobile)
    payment = _checkout_payment(c, mobile)
    if payment and (payment.utr or payment.status.value != "PENDING"):
        _require_send(c.whatsapp.send_text(mobile, _payment_status_text(c, payment)), "payment status")
    elif not sub:
        _send_plan_list(c, mobile)
    elif sub.awaiting_name or not sub.name:
        c.subscriber_service.set_awaiting_name(mobile, True)
        _require_send(c.whatsapp.send_text(mobile, "🙏 What name should we greet you by?"), "name request")
    elif not sub.opt_in:
        _request_opt_in(c, mobile)
    else:
        if payment:
            _send_payment_instructions(c, mobile, payment, sub.end_date is not None)
        elif sub.is_deliverable(datetime.now(ZoneInfo('Asia/Kolkata')).date()):
            _require_send(c.whatsapp.send_text(mobile,
                f"Your Daily Darshan subscription is active until {sub.end_date}. Send MENU for options."), "active status")
        else:
            _send_plan_list(c, mobile)


def _send_subscription_status(c, mobile):
    sub = c.subscribers.find(mobile)
    if not sub:
        message = "You do not have a subscription yet. Send MENU to view plans."
    else:
        status = "expired" if sub.is_expired(datetime.now(ZoneInfo("Asia/Kolkata")).date()) else sub.status.value.lower()
        message = (f"Your Daily Darshan subscription is {status}. Current plan: {_effective_plan_name(c, sub).capitalize()}. "
                   f"Expiry: {sub.end_date or 'not activated'}. "
                   f"Messages: {'enabled' if sub.opt_in else 'stopped'}. Send MENU for options.")
    payment = _checkout_payment(c, mobile)
    if payment:
        message += " " + _payment_status_text(c, payment)
    _require_send(c.whatsapp.send_text(mobile, message), "subscription status")


def _default_plan(c) -> str:
    return next(iter(c.config["plans"]))


def _payment_status_text(c, payment):
    ref = payment.reference_id
    if payment.status.value == "FAILED":
        from application.payment_messages import payment_rejection_text
        return payment_rejection_text(c.config, payment)
    if payment.status.value == "SUCCESS":
        if payment.activation_state != "APPLIED":
            return f"Payment {ref} is approved. Subscription activation is being completed; please do not pay again."
        from application.payment_messages import payment_approval_text
        return payment_approval_text(c.config, payment, c.subscribers.find(payment.mobile))
    if payment.utr:
        return (f"Payment verification pending for {ref}. Please allow the admin time to verify it. "
                f"If you have already made payment, please confirm your UTR in this format: "
                f"*UTR {ref} 123456789012* (replace the last 12 digits with your UTR). "
                "Plan changes and replacement payments remain locked until the review completes. "
                "Do not pay again if this payment is already complete.")
    return f"Payment {ref} is awaiting payment. Send MENU and select Payment instructions."


def _checkout_payment(c, mobile):
    """Latest non-superseded checkout, including unresolved approval/rejection."""
    _supersede_lower_unpaid_checkouts(c, mobile)
    sub = c.subscribers.find(mobile)
    applied = _applied_payment_refs(sub) if sub else set()
    payments = [p for p in c.payments.all()
                if p.mobile == mobile and p.status.value != "SUPERSEDED"
                and (p.reference_id not in applied or p.status.value == "SUCCESS")]
    if not payments:
        return None
    payment = max(payments, key=lambda p: (p.created_at.isoformat() if p.created_at else "", p.reference_id))
    if payment.status.value == "SUCCESS" and payment.activation_state == "APPLIED":
        sub = c.subscribers.find(mobile)
        if sub and (
            sub.status.value != "ACTIVE"
            or sub.is_expired(datetime.now(ZoneInfo("Asia/Kolkata")).date())
        ):
            return None
        welcome = c.welcomes.find(payment.reference_id)
        if welcome and (welcome.get("publication_verified") == "true" or welcome.get("status") in {"PENDING", "UNKNOWN", "SENT", "DELIVERED", "READ"}):
            return None
    return payment


def _latest_superseded_utr(c, mobile):
    """Latest archived UTR that the customer may explicitly reopen for review."""
    sub = c.subscribers.find(mobile)
    applied = _applied_payment_refs(sub) if sub else set()
    payments = [
        payment for payment in c.payments.all()
        if payment.mobile == mobile
        and payment.status.value == "SUPERSEDED"
        and payment.utr
        and _superseded_review_open(c, payment)
        and payment.reference_id not in applied
    ]
    if not payments:
        return None
    return max(
        payments,
        key=lambda payment: (
            payment.created_at.isoformat() if payment.created_at else "",
            payment.reference_id,
        ),
    )


def _superseded_review_open(c, payment) -> bool:
    """Allow unresolved superseded UTR recovery only during its visibility window."""
    if payment.status.value != "SUPERSEDED" or payment.rejected_at:
        return False
    anchor = payment.superseded_at or payment.utr_confirmed_at or payment.created_at
    if anchor is None:
        return False
    days = max(1, int(c.config.get("delivery", {}).get("failed_payment_release_days", 3)))
    return 0 <= (today_ist() - today_ist(anchor)).days < days


def _latest_pending_payment(c, mobile: str):
    from domain.enums import PaymentStatus

    _supersede_lower_unpaid_checkouts(c, mobile)
    sub = c.subscribers.find(mobile)
    applied = _applied_payment_refs(sub) if sub else set()
    pending = [
        p for p in c.payments.all()
        if p.mobile == mobile and p.status == PaymentStatus.PENDING and p.reference_id not in applied
    ]
    if not pending:
        return None
    import datetime as _dt
    return sorted(pending, key=lambda p: p.created_at or _dt.datetime.min)[-1]
