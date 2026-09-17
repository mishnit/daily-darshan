"""WhatsApp webhook + conversation handling (Tech Doc sections 5, 13).

Serverless-friendly FastAPI app. Meta calls:
  GET  /webhook  -> verification handshake (hub.challenge)
  POST /webhook  -> inbound messages

Conversation flow (section 6, 23):
  plan selection -> reference id -> UPI intent -> pay -> submit UTR.
Admin verification happens out-of-band (payments.csv / admin UI).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query, Request, Response
from starlette.concurrency import run_in_threadpool
from domain.enums import PaymentStatus
from repositories.state_lock import state_lock
from repositories.state_lock import StateLockTimeout

app = FastAPI(title="Daily Darshan Webhook", version="2.0.0")
log = logging.getLogger("daily_darshan.webhook")

# Composition root, guarded (P1a fix #1): a bad config / dependency must NOT
# crash import — otherwise the whole app (including /health) fails to start.
# We build the container lazily and record any failure so /health can report it.
container = None
_container_error: str | None = None


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
    # Local development intentionally supports CSV-only/no-secret operation.
    # A github_api deployment is production: accepting unsigned events or
    # acknowledging writes that cannot be persisted would lose user actions.
    if production and not all((durable, signed, whatsapp, verified)):
        ok = False
    return _json({"status": "ok" if ok else "degraded", "checks": checks}, 200 if ok else 503)


def _json(payload: dict, status: int = 200) -> Response:
    return Response(content=json.dumps(payload), media_type="application/json", status_code=status)


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
        await run_in_threadpool(_process_payload, c, {}, 5.0)
    except StateLockTimeout:
        # Normal backpressure: Meta will retry. Avoid an alarming traceback for
        # an expected overlap while another durable transaction is committing.
        log.warning("Webhook state is busy; returning 503 for provider retry")
        return _json({"status": "retry"}, 503)
    except Exception:
        log.exception("Reply retry requires attention")
        return _json({"status": "retry or reconciliation required"}, 503)
    return _json({"status": "processed"})


@app.post("/webhook")
async def receive_webhook(request: Request) -> Response:
    raw_body = await request.body()

    # Reject forged/unsigned requests before doing any work (must be synchronous).
    if not _signature_valid(raw_body, request.headers.get("X-Hub-Signature-256")):
        return Response(content="invalid signature", status_code=403)

    c = _get_container()
    if c is None:
        # Do not acknowledge an event that cannot be processed.
        log.error("Webhook received but container is unavailable: %s", _container_error)
        return _json({"status": "unavailable"}, 503)

    # Never 500 on a malformed/non-JSON body; ack and ignore (synchronous).
    try:
        payload = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        return _json({"status": "ignored"})
    if not isinstance(payload, dict):
        return _json({"status": "ignored"})

    # Keep the event loop free, but do not acknowledge before durable commit.
    try:
        await run_in_threadpool(_process_payload, c, payload, 10.0)
    except StateLockTimeout:
        # Expected backpressure if a short Git commit phase overlaps. Meta will
        # retry; log one line rather than an alarming stack trace.
        log.warning("Webhook state is busy; returning 503 for provider retry")
        return _json({"status": "retry"}, 503)
    except Exception:
        log.exception("Webhook not completed; request must be retried")
        return _json({"status": "retry"}, 503)
    return _json({"status": "accepted"})


def _process_payload(c, payload: dict, lock_timeout: float | None = None) -> None:
    """Persist intent, send outside the lock, then merge the provider outcome."""
    production = c.config.get("persistence", {}).get("mode") == "github_api"
    prepared_snapshots = []
    failed = False
    with state_lock(c.root, timeout=lock_timeout):
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
            failed = _process_messages(c, payload)
            if production:
                # Internal worker (empty payload) drains a bounded global batch.
                # Customer events never inherit another customer's stuck state.
                mobiles = {m.get('from', '') for m, _ in _iter_messages(payload)} if payload else None
                reply_ids = ({row['id'] for row in c.reply_outbox.all()} - existing_reply_ids) if payload else None
                prepared_replies, preparation_failed = prepare_replies(
                    c.reply_outbox, c, mobiles=mobiles, reply_ids=reply_ids)
                if not payload:
                    failed |= preparation_failed
            # In production this atomically persists both the inbound state and
            # each PENDING outbound reservation before Meta is contacted.
            c.repo_sync.push("Webhook update", strict=True)
            if production:
                from application.reply_outbox import snapshot_prepared_replies
                prepared_snapshots = snapshot_prepared_replies(c.reply_outbox, prepared_replies)
                if len(prepared_snapshots) != len(prepared_replies):
                    raise RuntimeError("Durable reply reservation disappeared before send")
        except Exception:
            _restore_webhook_state(snapshot)
            c.repo_sync.abort()
            raise
        finally:
            c.whatsapp = client
    if production and prepared_snapshots:
        from application.reply_outbox import send_reply_snapshots, merge_reply_outcomes
        outcomes, send_failed = send_reply_snapshots(client, prepared_snapshots)
        # Persisted outbound failures belong to the independent retry worker.
        # Redelivering an already saved inbound event cannot repair them.
        if not payload:
            failed |= send_failed
        # Reacquire only for the small fresh-read/merge/commit transaction.
        # Other webhook requests can progress while the Meta call is in flight.
        with state_lock(c.root, timeout=lock_timeout):
            c.repo_sync.pull(strict=True)
            snapshot = _snapshot_webhook_state(c)
            try:
                failed |= merge_reply_outcomes(c.reply_outbox, outcomes)
                c.repo_sync.push("Persist webhook reply outbox", strict=True)
            except Exception:
                _restore_webhook_state(snapshot)
                c.repo_sync.abort()
                raise
    if failed:
        raise RuntimeError("One or more webhook responses need retry")


def _process_messages(c, payload: dict) -> bool:
    """Handle a batch under the caller's state lock; report retriable failures."""
    failed = False
    for message, ctx in _iter_messages(payload):
        message_id = message.get("id", "")
        mobile = message.get("from", "")
        snapshot = {}
        # Isolate each message: a failure must not abort the batch.
        try:
            snapshot = _snapshot_webhook_state(c)
            pending_reply = c.reply_retries.find(message_id)
            if pending_reply:
                result = c.whatsapp.send_text(pending_reply["mobile"], pending_reply["text"])
                if not result.ok:
                    failed = True
                    continue
                c.reply_retries.delete(message_id)
                continue
            # Dedupe on WhatsApp message id: skip a re-delivered message.
            if not c.processed.mark_if_new(message_id, mobile):
                continue
            kind, value = _extract_input(message)
            if mobile and value:
                state = c.conversations.find(mobile) or {"mobile": mobile, "version": "0", "last_recovery": "0"}
                recovery = ((kind == "text" and value.strip().upper() in {"CONTINUE", "RESEND"})
                            or (kind == "button" and value in {"CTA_CONTINUE", "CTA_RESEND"}))
                if (recovery
                        and time.time() - float(state.get("last_recovery") or 0) < 30):
                    continue
                # Bound delayed events to their original reply window.
                try:
                    incoming_at = min(time.time(), float(message.get("timestamp", time.time())))
                except (ValueError, TypeError):
                    incoming_at = 0
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
            # Keep them and retry only the acknowledgement on redelivery.
            c.reply_retries.upsert(message_id, {
                "message_id": message_id, "mobile": exc.mobile, "text": exc.text,
            })
            failed = True
        except Exception:  # noqa: BLE001 - log + continue
            # The id is claimed first to prevent concurrent duplicate sends.
            # Release it after a failure so Meta can retry the user action.
            _restore_webhook_state(snapshot)
            failed = True
            log.exception("Failed handling message id=%s from=%s", message_id, mobile)

    for status in _iter_statuses(payload):
        message_id = str(status.get("id", ""))
        if not message_id:
            continue
        c.message_statuses.record(message_id, status.get("status"))
    c.message_statuses.reconcile(c.sentlog, c.renewals, c.welcomes, c.reply_outbox)

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
        paths.get("reply_retries_csv", "csv/reply_retries.csv"),
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
        payment.status = PaymentStatus.SUPERSEDED
        c.payments.update(payment)
        event = ("PAYMENT_SUPERSEDED_LOWER_PLAN" if payment_rank is None or payment_rank < current_rank
                 else "PAYMENT_SUPERSEDED_OUTSIDE_RENEWAL_WINDOW")
        c.logs.log(event, mobile, payment.reference_id)


def _applied_payment_refs(sub) -> set[str]:
    return {ref.strip() for ref in (sub.applied_payment_refs or "").split(";") if ref.strip()}


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


def _send_menu(c, mobile: str) -> None:
    """Show actions appropriate to entitlement and the current checkout."""
    sub = c.subscribers.find(mobile)
    payment = _checkout_payment(c, mobile)
    # An abandoned unpaid checkout may outlive a deleted subscriber row.
    # Restart signup, but retain payment evidence and reviewed/approved states.
    if not sub and payment and payment.status.value == "PENDING" and not payment.utr:
        payment = None
    active = _has_active_subscription(c, mobile)
    larger_plans = _larger_plan_names(c, mobile) if active else []
    expiring_soon = _is_expiring_soon(c, mobile)
    eligible_plans = _eligible_plan_names(c, mobile)
    rows = [("CTA_STATUS", "Subscription status", "Check your subscription")] if _shows_subscription_status(c, sub) else []
    body = "🙏 Radhe Radhe! Choose an option below."
    locked_payment = bool(payment and payment.status.value != "PENDING")
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
        if payment.status.value == "PENDING" and (not active or eligible_plans):
            if active:
                label = "Renew" if expiring_soon else "Upgrade"
            else:
                label = "Renew" if (sub and sub.end_date) else "Change plan"
            if active and expiring_soon:
                description = "Renew or choose a larger plan" if larger_plans else "Renew your current plan"
            else:
                description = "Choose a larger plan" if active else "Choose a different plan"
            rows.append(("CTA_RENEW", label, description))
        if reviewing:
            body = _payment_status_text(c, payment)
            if payment.status.value == "FAILED":
                rows.append(("CTA_PAYMENT_REVIEW", "Request review", "Ask the administrator to recheck payment"))
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
    if _has_active_subscription(c, mobile) and not sub.opt_in and not any(r[0] == "CTA_RESUME_MESSAGES" for r in rows):
        rows.append(("CTA_RESUME_MESSAGES", "Resume messages", "Restore consent without paying"))
    result = c.whatsapp.send_list(
        mobile,
        (("🙏 Welcome to Daily Darshan! Receive temple darshan on WhatsApp, enjoy an HD image "
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
    if payment and payment.status.value != "PENDING":
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
        if (payment and payment.status.value != "PENDING"
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
            if payment and payment.status.value == "FAILED":
                c.logs.log("PAYMENT_REVIEW_REQUESTED", mobile, payment.reference_id)
                _require_send(wa.send_text(mobile, f"Your review request for {payment.reference_id} has been recorded for the administrator. Please allow time for verification and do not pay again."), "review request")
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
        payment = _latest_pending_payment(c, mobile)
        if referenced_utr:
            reference, text = referenced_utr.groups()
            payment = c.payments.find(reference.upper())
            if not payment or payment.mobile != mobile or payment.status.value not in {"PENDING", "SUPERSEDED"}:
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
    if (not payment or payment.mobile != mobile
            or payment.status.value not in {"PENDING", "SUPERSEDED"}):
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
    if payment and payment.status.value != "PENDING":
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
    intent = c.payment_service.generate_upi_intent(payment)
    if returning:
        header = f"{greeting}Renewing your {plan} plan.\nAmount: ₹{payment.amount:g}\n"
    else:
        header = f"{greeting}Plan: {plan}\nAmount: ₹{payment.amount:g}\n"
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
    if (draft_payment and draft_payment.mobile == mobile
            and draft_payment.status.value in {"PENDING", "SUPERSEDED"}):
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
        return (f"Payment {ref} was rejected. Send MENU and select Request review to ask the administrator to recheck it. "
                "Keep your payment proof and do not pay again until the payment is resolved.")
    if payment.status.value == "SUCCESS":
        if payment.activation_state != "APPLIED":
            return f"Payment {ref} is approved. Subscription activation is being completed; please do not pay again."
        return f"Payment {ref} is approved. Your Darshan page is being prepared; publication is awaiting confirmation."
    if payment.utr:
        active = _has_active_subscription(c, payment.mobile)
        if active:
            larger_plans = _larger_plan_names(c, payment.mobile)
            expiring_soon = _is_expiring_soon(c, payment.mobile)
            plan_action = (
                "You may choose Renew to keep your current plan or choose a larger plan."
                if expiring_soon
                else "You may choose Upgrade to choose a larger plan."
                if larger_plans
                else "Same-plan renewal opens three days before expiry."
            )
        else:
            plan_action = "You may choose Change plan."
        return (f"Payment verification pending for {ref}. Please allow the admin time to verify it. "
                f"If you have already made payment, please confirm your UTR in this format: "
                f"*UTR {ref} 123456789012* (replace the last 12 digits with your UTR). "
                f"{plan_action} Do not pay again if this payment is already complete.")
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
