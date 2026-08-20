"""Tests for launch-blocker fixes: defensive CSV parsing, webhook idempotency,
signature verification, malformed-body handling, and media-based delivery."""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

import pytest

from domain.enums import PaymentStatus, SubscriberStatus
from domain.payment import Payment
from domain.subscriber import Subscriber


# --------------------------- #4 defensive CSV parsing --------------------------- #

def test_subscriber_from_row_unknown_status_is_inert():
    sub = Subscriber.from_row({
        "mobile": "9199", "plan": "monthly", "start_date": "2026-01-01",
        "end_date": "2026-12-31", "status": "Actve", "opt_in": "true",  # typo
    })
    # Unknown status -> CANCELLED, which is never deliverable.
    assert sub.status == SubscriberStatus.CANCELLED
    assert sub.is_deliverable(date(2026, 6, 1)) is False


def test_subscriber_from_row_bad_dates_do_not_crash():
    sub = Subscriber.from_row({
        "mobile": "9199", "plan": "monthly", "start_date": "not-a-date",
        "end_date": "2026-13-45", "status": "ACTIVE", "opt_in": "true",
    })
    assert sub.start_date is None and sub.end_date is None


def test_payment_from_row_unknown_status_defaults_pending_never_success():
    p = Payment.from_row({
        "reference_id": "DD2608190001", "mobile": "9199", "plan": "monthly",
        "amount": "oops", "status": "weird", "utr": "", "created_at": "", "verified_at": "",
    })
    assert p.status == PaymentStatus.PENDING  # corrupt row must not read as SUCCESS
    assert p.amount == 0.0                     # bad amount coerced, no crash


def test_repository_all_skips_corrupt_rows(repos):
    # Append a valid subscriber, then a corrupt row missing the key column.
    repos["subscribers"].append(Subscriber(mobile="9199", plan="monthly",
                                            status=SubscriberStatus.ACTIVE))
    # Write a raw broken row directly into the CSV file.
    with open(repos["subscribers"]._csv.path, "a", encoding="utf-8") as fh:
        fh.write(",,,,,\n")            # blank/garbage row
    subs = repos["subscribers"].all()  # must not raise
    assert [s.mobile for s in subs if s.mobile] == ["9199"]


# --------------------------- #1/#2/#3 webhook --------------------------- #
# These import main lazily with an isolated config via env var.

@pytest.fixture
def webhook(tmp_path, monkeypatch):
    """Import main.py with an isolated config + known app secret."""
    cfg = {
        "plans": {"monthly": {"amount": 49, "days": 30}, "yearly": {"amount": 449, "days": 365}},
        "upi": {"payee_vpa": "t@upi", "payee_name": "T", "currency": "INR"},
        "image_sources": [], "image_source_config": {},
        "image_validation": {"allowed_formats": ["JPEG"], "min_width": 0, "min_height": 0},
        "paths": {
            "images_dir": str(tmp_path / "images"),
            "subscribers_csv": str(tmp_path / "subscribers.csv"),
            "payments_csv": str(tmp_path / "payments.csv"),
            "sentlog_csv": str(tmp_path / "sentlog.csv"),
            "renewals_csv": str(tmp_path / "renewals.csv"),
            "logs_csv": str(tmp_path / "logs.csv"),
            "processed_csv": str(tmp_path / "processed.csv"),
        },
        "renewal": {"reminder_days": [3, 1]},
        "delivery": {"caption": "D - {date}", "max_send_retries": 1},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("DAILY_DARSHAN_CONFIG", str(cfg_path))
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "s3cret")

    import importlib
    import config as config_mod
    config_mod.load_config.cache_clear()
    import main
    main = importlib.reload(main)

    # Bind a fresh Container to THIS test's isolated config, so no state leaks
    # between tests via the module-global container.
    main.container = config_mod.Container(config=cfg, root=str(tmp_path))

    from fastapi.testclient import TestClient
    # Replace the WhatsApp client with a recording fake so no network is hit.
    from tests.conftest import FakeWhatsApp
    fake = FakeWhatsApp()
    main.container.whatsapp = fake
    return main, TestClient(main.app), fake


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _msg(mobile="9199", text="SUBSCRIBE monthly", mid="wamid.1", name=None):
    value = {"messages": [
        {"id": mid, "from": mobile, "type": "text", "text": {"body": text}}
    ]}
    if name is not None:
        value["contacts"] = [{"wa_id": mobile, "profile": {"name": name}}]
    return {"entry": [{"changes": [{"value": value}]}]}


def _tap(mobile, button_id, mid, name=None, kind="button_reply"):
    """An interactive button/list tap webhook (CTA)."""
    value = {"messages": [{
        "id": mid, "from": mobile, "type": "interactive",
        "interactive": {"type": "button" if kind == "button_reply" else "list",
                        kind: {"id": button_id, "title": button_id}},
    }]}
    if name is not None:
        value["contacts"] = [{"wa_id": mobile, "profile": {"name": name}}]
    return {"entry": [{"changes": [{"value": value}]}]}


def test_webhook_rejects_bad_signature(webhook):
    _, client, _ = webhook
    body = json.dumps(_msg()).encode()
    resp = client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert resp.status_code == 403


def test_webhook_accepts_valid_signature(webhook):
    _, client, fake = webhook
    # Any typed text now yields the CTA menu (buttons), not a subscription.
    body = json.dumps(_msg(text="hi")).encode()
    resp = client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": _sign("s3cret", body)})
    assert resp.status_code == 200
    assert any(s["type"] == "buttons" for s in fake.sent)


def test_webhook_malformed_body_no_500(webhook):
    _, client, _ = webhook
    body = b"this is not json"
    resp = client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": _sign("s3cret", body)})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ignored"


def test_typed_plan_word_does_not_subscribe(webhook):
    """Regression for bug #1: typing a plan word must NOT create a payment."""
    main, client, fake = webhook
    body = json.dumps(_msg(mobile="9199", text="how much is yearly?", mid="t1")).encode()
    client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign("s3cret", body)})
    assert len(main.container.payments.all()) == 0          # no accidental payment
    assert main.container.subscribers.find("9199") is None  # no accidental signup
    assert any(s["type"] == "buttons" for s in fake.sent)   # shown the menu instead


def test_webhook_idempotent_on_duplicate_message_id(webhook):
    main, client, fake = webhook
    # Set up an opted-in subscriber ready to pay, then tap "I agree" — the tap
    # is what creates the payment. A duplicate delivery of that tap must not
    # create a second payment.
    main.container.subscriber_service.upsert_pending("9199", "monthly", "Ravi")
    body = json.dumps(_tap("9199", "CTA_OPTIN_AGREE", "wamid.dup")).encode()
    headers = {"X-Hub-Signature-256": _sign("s3cret", body)}

    client.post("/webhook", content=body, headers=headers)
    payments_after_first = len(main.container.payments.all())

    client.post("/webhook", content=body, headers=headers)  # duplicate delivery
    payments_after_second = len(main.container.payments.all())

    assert payments_after_first == 1
    assert payments_after_second == 1


def test_webhook_captures_profile_name(webhook):
    """Tapping a plan with a WhatsApp profile name present stores that name."""
    main, client, _ = webhook
    body = json.dumps(_tap("9199", "PLAN_monthly", "wamid.name", name="Ravi Kumar")).encode()
    resp = client.post("/webhook", content=body,
                       headers={"X-Hub-Signature-256": _sign("s3cret", body)})
    assert resp.status_code == 200
    sub = main.container.subscribers.find("9199")
    assert sub is not None and sub.name == "Ravi Kumar"


def test_webhook_subscribe_cta_then_plan_then_name(webhook):
    """Full CTA flow: tap Subscribe -> plan list; tap plan (no name) -> asked
    for name; reply name -> stored + payment created."""
    main, client, fake = webhook

    # 1) Tap "Subscribe" -> a plan LIST is sent, no payment/subscriber yet.
    b0 = json.dumps(_tap("9111", "CTA_SUBSCRIBE", "m0")).encode()
    client.post("/webhook", content=b0, headers={"X-Hub-Signature-256": _sign("s3cret", b0)})
    assert any(s["type"] == "list" for s in fake.sent)
    assert len(main.container.payments.all()) == 0

    # 2) Tap a plan (no profile name) -> asked for name, still no payment.
    b1 = json.dumps(_tap("9111", "PLAN_monthly", "m1", kind="list_reply")).encode()
    client.post("/webhook", content=b1, headers={"X-Hub-Signature-256": _sign("s3cret", b1)})
    assert main.container.subscriber_service.is_awaiting_name("9111") is True
    assert len(main.container.payments.all()) == 0
    assert any("name" in s.get("message", "").lower() for s in fake.sent)

    # 3) Reply with a name (free text) -> opt-in disclosure shown, still no payment.
    b2 = json.dumps(_msg(mobile="9111", text="Sita Devi", mid="m2")).encode()
    client.post("/webhook", content=b2, headers={"X-Hub-Signature-256": _sign("s3cret", b2)})
    sub = main.container.subscribers.find("9111")
    assert sub.name == "Sita Devi"
    assert sub.awaiting_name is False
    assert sub.opt_in is False                       # consent not yet granted
    assert len(main.container.payments.all()) == 0   # gated on opt-in
    assert any(s["type"] == "buttons" and "CTA_OPTIN_AGREE" in s["buttons"]
               for s in fake.sent)

    # 4) Tap "I agree" -> opt-in recorded (with timestamp) + payment created.
    b3 = json.dumps(_tap("9111", "CTA_OPTIN_AGREE", "m3")).encode()
    client.post("/webhook", content=b3, headers={"X-Hub-Signature-256": _sign("s3cret", b3)})
    sub = main.container.subscribers.find("9111")
    assert sub.opt_in is True and sub.opt_in_at        # consent proof recorded
    assert len(main.container.payments.all()) == 1


def test_webhook_renew_cta_existing_subscriber_uses_plan_no_prompt(webhook):
    """Tapping Renew for a known subscriber uses their existing plan, greets by
    name, and does NOT re-prompt for a name."""
    from datetime import date
    from domain.enums import SubscriberStatus
    from domain.subscriber import Subscriber
    main, client, fake = webhook
    main.container.subscribers.append(Subscriber(
        mobile="9222", plan="yearly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="tok-9222", name="Meera",
    ))
    body = json.dumps(_tap("9222", "CTA_RENEW", "r1")).encode()
    client.post("/webhook", content=body, headers={"X-Hub-Signature-256": _sign("s3cret", body)})

    assert main.container.subscriber_service.is_awaiting_name("9222") is False
    pays = main.container.payments.all()
    assert len(pays) == 1 and pays[0].plan == "yearly"  # existing plan, not default
    reply = fake.sent[-1]["message"]
    assert "Meera" in reply and "Renewing" in reply
