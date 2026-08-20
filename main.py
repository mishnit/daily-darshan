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

from fastapi import BackgroundTasks, FastAPI, Query, Request, Response

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
    # Durability posture (informational, not fatal for liveness).
    checks["durable_persistence"] = "enabled" if getattr(c, "repo_sync", None) and c.repo_sync.enabled else "local-only"
    checks["signature_verification"] = "enabled" if c.whatsapp_app_secret else "disabled"
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
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    provided = header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


@app.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks) -> Response:
    raw_body = await request.body()

    # Reject forged/unsigned requests before doing any work (must be synchronous).
    if not _signature_valid(raw_body, request.headers.get("X-Hub-Signature-256")):
        return Response(content="invalid signature", status_code=403)

    c = _get_container()
    if c is None:
        # Cannot process without a container; ack so Meta doesn't hammer retries,
        # and rely on /health + logs to surface the outage.
        log.error("Webhook received but container is unavailable: %s", _container_error)
        return _json({"status": "unavailable"}, 200)

    # Never 500 on a malformed/non-JSON body; ack and ignore (synchronous).
    try:
        payload = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        return _json({"status": "ignored"})
    if not isinstance(payload, dict):
        return _json({"status": "ignored"})

    # (fix #2) Ack Meta immediately and do the slow work (repo pull/push +
    # WhatsApp calls) off the response path in a background task. Signature is
    # already verified and the payload parsed, so scheduling is safe. Meta only
    # needs a fast 200 to avoid retrying.
    background_tasks.add_task(_process_payload, c, payload)
    return _json({"status": "accepted"})


def _process_payload(c, payload: dict) -> None:
    """Process a verified webhook payload. Runs in a background task.

    Does the slow/blocking work: pull shared state, handle each message
    (idempotent, isolated), push changes back. Never raises to the caller.
    """
    # Pull latest shared state so this ephemeral webhook sees subscribers/
    # payments written elsewhere; push after (P0 fix #6).
    try:
        c.repo_sync.pull()
    except Exception:  # noqa: BLE001 - never block on sync
        log.exception("repo_sync.pull failed; proceeding with local state")

    processed_any = False
    for message, ctx in _iter_messages(payload):
        message_id = message.get("id", "")
        mobile = message.get("from", "")
        # Isolate each message: a failure must not abort the batch.
        try:
            # Dedupe on WhatsApp message id: skip a re-delivered message.
            if not c.processed.mark_if_new(message_id, mobile):
                continue
            kind, value = _extract_input(message)
            if mobile and value:
                name = _profile_name(ctx, mobile)
                _handle_message(c, mobile, kind, value, name)
                processed_any = True
        except Exception:  # noqa: BLE001 - log + continue
            log.exception("Failed handling message id=%s from=%s", message_id, mobile)

    # Push any local CSV changes back to the shared repo (P0 fix #6).
    if processed_any:
        try:
            c.repo_sync.push("Webhook update")
        except Exception:  # noqa: BLE001
            log.exception("repo_sync.push failed; local writes not yet shared")


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
    if mtype == "interactive":
        interactive = message.get("interactive", {})
        for key in ("button_reply", "list_reply"):
            if key in interactive:
                return "button", interactive[key].get("id", "")
    return "", ""


def _send_menu(c, mobile: str) -> None:
    """Entry CTA menu: Subscribe / Renew / Stop (buttons)."""
    c.whatsapp.send_buttons(
        mobile,
        "🙏 Welcome to Daily Darshan! What would you like to do?",
        [("CTA_SUBSCRIBE", "Subscribe"), ("CTA_RENEW", "Renew"), ("CTA_STOP", "Stop messages")],
    )


def _send_plan_list(c, mobile: str) -> None:
    """Send the plan catalog as a tappable list (ids = PLAN_<plan>)."""
    rows = []
    for plan, meta in c.config["plans"].items():
        amount = meta.get("amount")
        days = meta.get("days")
        rows.append((f"PLAN_{plan}", plan.capitalize(), f"₹{amount} · {days} days"))
    c.whatsapp.send_list(mobile, "Choose your Daily Darshan plan:", "View plans", rows)


def _request_opt_in(c, mobile: str) -> None:
    """Show the consent disclosure and an explicit Agree button (#9)."""
    c.whatsapp.send_buttons(
        mobile,
        "By continuing, you agree to receive a *daily darshan* image on WhatsApp "
        "and occasional subscription updates from Daily Darshan. You can stop "
        "anytime by replying STOP. Do you agree?",
        [("CTA_OPTIN_AGREE", "I agree"), ("CTA_STOP", "No thanks")],
    )


def _after_name_or_optin(c, mobile: str, returning: bool) -> None:
    """Gate on explicit opt-in: request consent if not yet granted, else pay."""
    sub = c.subscribers.find(mobile)
    if sub is None:
        _send_menu(c, mobile)
        return
    if not sub.opt_in:
        _request_opt_in(c, mobile)
        return
    _start_payment(c, mobile, sub.plan or _default_plan(c), returning=returning)


def _handle_message(c, mobile: str, kind: str, value: str, name: str = "") -> None:
    """CTA-driven conversation state machine with explicit opt-in/opt-out.

    Buttons/list ids: CTA_SUBSCRIBE -> plans; CTA_RENEW -> renew;
    PLAN_<plan> -> chosen plan; CTA_OPTIN_AGREE -> record consent + pay;
    CTA_STOP -> opt out. Free text ONLY for name (when awaiting) and 12-digit
    UTR (and STOP-family keywords). Phone is implicit.
    """
    svc = c.subscriber_service
    wa = c.whatsapp

    # ---------------- Button / list taps (CTAs) ---------------- #
    if kind == "button":
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
            # Returning = already had subscription dates before this checkout.
            returning = sub.end_date is not None
            _start_payment(c, mobile, sub.plan or _default_plan(c), returning=returning)
            return
        if value.startswith("PLAN_"):
            plan = value[len("PLAN_"):]
            if plan not in c.config["plans"]:
                _send_plan_list(c, mobile)
                return
            svc.upsert_pending(mobile, plan, name)
            sub = c.subscribers.find(mobile)
            if not sub.name:
                svc.set_awaiting_name(mobile, True)
                wa.send_text(mobile, "🙏 What name should we greet you by?")
                return
            _after_name_or_optin(c, mobile, returning=False)
            return
        # Unknown/expired button id -> re-show the menu.
        _send_menu(c, mobile)
        return

    # ---------------- Free text (name / UTR / STOP only) ---------------- #
    text = value.strip()

    # (0) Opt-out keywords (typed). Meta expects STOP to work as free text too.
    if text.upper() in ("STOP", "UNSUBSCRIBE", "CANCEL"):
        _handle_opt_out(c, mobile)
        return

    # (a) Awaiting the user's name -> capture it (a UTR is never a name).
    if svc.is_awaiting_name(mobile) and not _UTR_RE.match(text):
        if text:
            svc.set_name(mobile, text)
            svc.set_awaiting_name(mobile, False)
            _after_name_or_optin(c, mobile, returning=False)
        else:
            wa.send_text(mobile, "Please reply with the name we should greet you by 🙏")
        return

    # (b) UTR submission.
    if _UTR_RE.match(text):
        payment = _latest_pending_payment(c, mobile)
        if payment is None:
            _send_menu(c, mobile)
            return
        c.payment_service.record_utr(payment.reference_id, text)
        wa.send_text(
            mobile,
            "Thanks! We received your UTR. Your subscription activates once an "
            "admin verifies the payment.",
        )
        return

    # (c) Anything else typed -> present the CTA menu (no free-text commands).
    _send_menu(c, mobile)


def _handle_opt_out(c, mobile: str) -> None:
    """Honor STOP: revoke consent, confirm, stop business-initiated sends (#9)."""
    sub = c.subscriber_service.revoke_opt_in(mobile)
    if sub is None:
        c.whatsapp.send_text(mobile, "You're not subscribed. Reply to start anytime. 🙏")
        return
    c.whatsapp.send_text(
        mobile,
        "You've been opted out — you won't receive further Daily Darshan messages. "
        "Reply SUBSCRIBE anytime to resume. 🙏",
    )


def _handle_renew(c, mobile: str, name: str = "") -> None:
    svc = c.subscriber_service
    existing = c.subscribers.find(mobile)
    if existing is None:
        # Unknown mobile tapped Renew -> treat as a fresh subscribe.
        _send_plan_list(c, mobile)
        return
    renew_plan = existing.plan or _default_plan(c)
    if name.strip() and not existing.name:
        svc.set_name(mobile, name)
        existing = c.subscribers.find(mobile)
    if not existing.name:
        svc.upsert_pending(mobile, renew_plan, name)
        svc.set_awaiting_name(mobile, True)
        c.whatsapp.send_text(mobile, "🙏 What name should we greet you by?")
        return
    # Returning subscriber: opt-in gate still applies if they'd previously opted out.
    _after_name_or_optin(c, mobile, returning=True)


def _start_payment(c, mobile: str, plan: str, returning: bool = False) -> None:
    """Create the payment + UPI intent and message it to the user.

    `returning=True` uses renewal wording for an existing subscriber.
    """
    wa = c.whatsapp
    sub = c.subscribers.find(mobile)
    greeting = f"Namaste {sub.name}! " if sub and sub.name else ""
    payment = c.payment_service.create_payment(mobile, plan)
    intent = c.payment_service.generate_upi_intent(payment)
    if returning:
        header = f"{greeting}Renewing your {plan} plan.\nAmount: ₹{payment.amount:g}\n"
    else:
        header = f"{greeting}Plan: {plan}\nAmount: ₹{payment.amount:g}\n"
    wa.send_text(
        mobile,
        f"{header}"
        f"Pay via UPI:\n{intent}\n\n"
        f"Reference: {payment.reference_id}\n"
        f"After paying, reply with your 12-digit UTR.",
    )


def _default_plan(c) -> str:
    return next(iter(c.config["plans"]))


def _latest_pending_payment(c, mobile: str):
    from domain.enums import PaymentStatus

    pending = [
        p for p in c.payments.all()
        if p.mobile == mobile and p.status == PaymentStatus.PENDING
    ]
    if not pending:
        return None
    import datetime as _dt
    return sorted(pending, key=lambda p: p.created_at or _dt.datetime.min)[-1]
