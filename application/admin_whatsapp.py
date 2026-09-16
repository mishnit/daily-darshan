"""Sender-authorized admin review through normal durable webhook transactions."""
from datetime import datetime
import hashlib
import json
import os
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

from domain.clock import today_ist, INDIA_TZ
from application.image_approval import queue_request


def admin_numbers():
    return {n.strip().lstrip("+") for n in os.environ.get("WHATSAPP_ADMIN_NUMBERS", "").split(",")
            if n.strip().lstrip("+").isdigit()}


def fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()


def handle_admin(c, mobile, value):
    from main import _require_send
    def text(message):
        _require_send(c.whatsapp.send_text(mobile, message), "admin reply")
    if mobile not in admin_numbers():
        text("This action is available only to an authorized administrator.")
        return
    today = today_ist()
    if value.upper() == "ADMIN":
        _require_send(c.whatsapp.send_buttons(mobile,
            "Admin review: verify payments against your bank records before approving. Choose a task.",
            [("ADM_PAYMENTS_0", "Review payments"), ("ADM_IMAGES", "Select daily image")]), "admin menu")
        return
    if value.startswith("ADM_PAYMENTS_"):
        try:
            offset = max(0, int(value.removeprefix("ADM_PAYMENTS_")))
        except ValueError:
            text("Send ADMIN to reopen review.")
            return
        payments = sorted((p for p in c.payments.all() if p.status.value in {"PENDING", "SUPERSEDED"}
                           and p.utr), key=lambda p: p.reference_id)
        selected = payments[offset:offset + 9]
        if not selected:
            text("No confirmed UTRs await review. Unconfirmed drafts are not in this queue.")
            return
        rows = [(f"ADM_PAY_{p.reference_id}", p.reference_id, f"{p.mobile} · {p.plan} · ₹{p.amount:g}") for p in selected]
        if len(payments) > offset + 9:
            rows.append((f"ADM_PAYMENTS_{offset + 9}", "More payments", "Next page"))
        _require_send(c.whatsapp.send_list(mobile, "Select a confirmed payment to inspect its UTR.", "Review payments", rows), "admin payments")
        return
    if value == "ADM_IMAGES":
        rows = [r for r in c.image_reviews.all() if r["date"] == today.isoformat() and r["status"] == "PENDING"]
        if not rows:
            text("No image selection is pending for today. Run Daily Image to collect candidates if needed.")
            return
        _require_send(c.whatsapp.send_list(mobile, "Choose a source to preview today's image before approving it.",
            "Preview sources", [(f"ADM_IMG_{r['id']}", r["source"][:24], r["date"]) for r in rows[:10]]), "admin images")
        return
    state = c.conversations.find(mobile) or {"mobile": mobile, "version": "0"}
    if value.startswith("ADM_PAY_"):
        p = c.payments.find(value.removeprefix("ADM_PAY_"))
        if not p or p.status.value not in {"PENDING", "SUPERSEDED"} or not p.utr:
            text("This payment is no longer awaiting review. Send ADMIN to refresh.")
            return
        token = uuid4().hex
        state.update(admin_kind="payment", admin_reference=p.reference_id,
                     admin_fingerprint=fingerprint(p.to_row()), admin_token=token)
        c.conversations.upsert(mobile, state)
        _require_send(c.whatsapp.send_buttons(mobile,
            f"Payment {p.reference_id}\nCustomer: {p.mobile}\nPlan: {p.plan}\nAmount: ₹{p.amount:g}\n"
            f"UTR: *{p.utr}*\nCustomer confirmed: {p.utr_confirmed_at or 'legacy record'}\n"
            "Check the credited amount and UTR in your bank. Approve only if they match.",
            [(f"ADM_APPROVE_{token}", "Approve payment"), (f"ADM_REJECT_{token}", "Reject payment")]), "admin payment decision")
        return
    if value.startswith("ADM_IMG_"):
        row = c.image_reviews.find(value.removeprefix("ADM_IMG_"))
        if not row or row["date"] != today.isoformat() or row["status"] != "PENDING":
            text("This image preview is no longer current. Send ADMIN to refresh.")
            return
        base = c.config.get("admin", {}).get("image_preview_base", "").rstrip("/")
        if not base.startswith("https://"):
            text("Image preview hosting is not configured; approval remains blocked.")
            return
        token = uuid4().hex
        state.update(admin_kind="image", admin_reference=row["id"], admin_fingerprint=fingerprint(row), admin_token=token)
        c.conversations.upsert(mobile, state)
        _require_send(c.whatsapp.send_image(mobile, base + "/" + quote(row["path"], safe="/"),
            f"{row['date']} · {row['source']}"), "admin preview")
        _require_send(c.whatsapp.send_buttons(mobile, f"Use {row['source']} for {row['date']}?",
            [(f"ADM_APPROVE_{token}", "Approve image"), ("ADM_IMAGES", "Other sources")]), "admin image decision")
        return
    token = state.get("admin_token")
    if not token or value not in {f"ADM_APPROVE_{token}", f"ADM_REJECT_{token}"}:
        text("This approval is no longer current. Send ADMIN to refresh.")
        return
    if state.get("admin_kind") == "payment":
        p = c.payments.find(state["admin_reference"])
        if not p or fingerprint(p.to_row()) != state["admin_fingerprint"]:
            text("Payment details changed. Send ADMIN to review the latest UTR before approving.")
            return
        from admin import _verify_locked, cmd_reject
        args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False, skip_render=True)
        if value.startswith("ADM_APPROVE_"):
            if _verify_locked(c, args):
                raise RuntimeError("Admin activation failed; webhook transaction must roll back")
            queue_request(c, f"payment-{p.reference_id}", "Payment approved; regenerate, deploy then welcome")
            result = f"Approved {p.reference_id}. Subscription updated; page publication and welcome are queued."
        else:
            cmd_reject(c, args)
            result = f"Rejected {p.reference_id}. No entitlement was added."
    elif state.get("admin_kind") == "image":
        row = c.image_reviews.find(state["admin_reference"])
        if (not row or row["date"] != today.isoformat() or row["status"] != "PENDING"
                or fingerprint(row) != state["admin_fingerprint"]):
            text("Image selection changed or expired. Send ADMIN to refresh.")
            return
        if value.startswith("ADM_REJECT_"):
            text("Use Other sources to select a different image.")
            return
        for other in c.image_reviews.all():
            if other["date"] == row["date"] and other["status"] in {"PENDING", "APPROVED"}:
                other["status"] = "SUPERSEDED"
                c.image_reviews.upsert(other["id"], other)
        row.update(status="APPROVED", approved_by=mobile, approved_at=datetime.now(INDIA_TZ).isoformat())
        c.image_reviews.upsert(row["id"], row)
        queue_request(c, f"image-{row['generation']}", "Image approved; regenerate, deploy then deliver")
        result = f"Approved {row['source']} for {row['date']}. Page regeneration, deployment and delivery are queued."
    else:
        text("Send ADMIN to reopen review.")
        return
    state.update(admin_token="", admin_kind="", admin_reference="", admin_fingerprint="")
    c.conversations.upsert(mobile, state)
    text(result)
