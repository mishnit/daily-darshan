import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from tests.test_admin import container
from application.ports.whatsapp import WhatsAppResult
from application.reply_outbox import QueuedReplies, drain_replies


def prepare(c):
    c.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    c.subscriber_service.grant_opt_in("9199", "test")
    c.conversations.upsert("9199", {"mobile": "9199", "version": "1", "last_inbound": str(time.time()), "last_recovery": "0"})
    calls = []
    def send(*args, **kwargs):
        calls.append(args)
        return WhatsAppResult(ok=True, message_id="wamid.reply")
    c.whatsapp = SimpleNamespace(send_text=send, send_buttons=send, send_list=send)
    return calls


@pytest.mark.parametrize("choice", ["CONTINUE", "RESEND", "BACK"])
def test_menu_recovery_choices_route_to_existing_commands(container, choice):
    import main
    calls = prepare(container)
    payment = container.payment_service.create_payment("9199", "monthly")
    main._send_menu(container, "9199")
    assert [r[0] for r in calls[-1][3]] == [
        "CTA_SUBSCRIBE", "CTA_RENEW", "CTA_CONTINUE", "CTA_RESEND", "CTA_BACK"]
    main._handle_message(container, "9199", "button", "CTA_" + choice)
    assert len(container.payments.all()) == 1
    if choice != "BACK":
        assert payment.reference_id in calls[-1][1]


@pytest.mark.parametrize("command", ["CONTINUE", "STATUS", "RESEND"])
def test_recovery_reuses_reference_and_does_not_mutate_entitlement(container, command):
    import main
    calls = prepare(container)
    payment = container.payment_service.create_payment("9199", "monthly")
    before = container.subscribers.find("9199").to_row()
    main._handle_message(container, "9199", "text", command)
    assert payment.reference_id in calls[-1][1]
    assert len(container.payments.all()) == 1
    assert container.subscribers.find("9199").to_row() == before


def test_recovery_after_utr_does_not_request_payment_again(container):
    import main
    calls = prepare(container)
    payment = container.payment_service.create_payment("9199", "monthly")
    container.payment_service.record_utr(payment.reference_id, "123456789012")
    main._handle_message(container, "9199", "text", "CONTINUE")
    assert "verification pending" in calls[-1][1]
    assert "Pay via UPI" not in calls[-1][1]
    main._start_payment(container, "9199", "monthly")
    assert len(container.payments.all()) == 1


def test_recovery_command_never_becomes_name(container):
    import main
    calls = prepare(container)
    container.subscriber_service.set_awaiting_name("9199", True)
    main._handle_message(container, "9199", "text", "RESEND")
    assert container.subscribers.find("9199").name == "Nitin"
    assert "What name" in calls[-1][1]


def test_repeat_payment_cta_reuses_pending_reference(container):
    import main
    calls = prepare(container)
    main._start_payment(container, "9199", "monthly")
    main._start_payment(container, "9199", "monthly")
    assert len(container.payments.all()) == 1
    assert calls[0][1] == calls[1][1]


@pytest.mark.parametrize("change", ["version", "optout", "expired", "legacy"])
def test_worker_cancels_obsolete_replies(container, change):
    calls = prepare(container)
    QueuedReplies(container.reply_outbox, container).send_text("9199", "old instructions")
    row = container.reply_outbox.all()[0]
    if change == "version":
        state = container.conversations.find("9199")
        state["version"] = "2"
        container.conversations.upsert("9199", state)
    elif change == "optout":
        container.subscriber_service.revoke_opt_in("9199")
    else:
        row["expires_at" if change == "expired" else "version"] = "0" if change == "expired" else ""
        container.reply_outbox.upsert(row["id"], row)
    assert not drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container)
    assert not calls
    assert container.reply_outbox.all()[0]["status"] == "CANCELLED"


def test_worker_backoff_and_unknown_not_retried(container):
    calls = prepare(container)
    QueuedReplies(container.reply_outbox, container).send_text("9199", "instructions")
    now = time.time()
    def reject(*args):
        calls.append(args)
        return WhatsAppResult(ok=False, error="temporary")
    container.whatsapp.send_text = reject
    assert drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container, now)
    drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container, now + 1)
    assert len(calls) == 1
    drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container, now + 61)
    assert len(calls) == 2
    row = container.reply_outbox.all()[0]
    row["status"] = "UNKNOWN"
    container.reply_outbox.upsert(row["id"], row)
    drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container, now + 999)
    assert len(calls) == 2


def test_worker_endpoint_requires_signature_and_fresh_timestamp(container, monkeypatch):
    import main
    monkeypatch.setattr(main, "container", container)
    container.whatsapp_app_secret = "test-secret"
    container.config["persistence"] = {"mode": "github_api"}
    calls = []
    monkeypatch.setattr(main, "_process_payload", lambda *a: calls.append(a))
    client = TestClient(main.app)
    assert client.post("/internal/retry-replies", json={"timestamp": time.time()}).status_code == 403
    for timestamp, expected in [(time.time() - 600, 400), (time.time(), 200)]:
        raw = json.dumps({"timestamp": timestamp}).encode()
        signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        assert client.post("/internal/retry-replies", content=raw,
            headers={"X-Hub-Signature-256": "sha256=" + signature}).status_code == expected
    assert len(calls) == 1


def test_active_recovery_does_not_extend_subscription(container):
    import main
    calls = prepare(container)
    sub = container.subscriber_service.activate("9199")
    before = sub.to_row()
    main._handle_message(container, "9199", "text", "STATUS")
    assert "subscription is active" in calls[-1][1]
    assert container.subscribers.find("9199").to_row() == before
    assert container.payments.all() == []


def test_worker_retries_without_new_customer_message_and_cooldown_keeps_current_reply(container):
    import main
    calls = prepare(container)
    container.payment_service.create_payment("9199", "monthly")
    container.config["persistence"] = {"mode": "github_api"}
    container.repo_sync = SimpleNamespace(enabled=True, in_quiet_window=lambda: False,
        pull=lambda **kw: None, push=lambda *a, **kw: None, abort=lambda: None)
    def reject(*args):
        calls.append(args)
        return WhatsAppResult(ok=False, error="temporary")
    container.whatsapp.send_text = reject
    def payload(identifier):
        return {"entry": [{"changes": [{"value": {"messages": [{"id": identifier,
            "from": "9199", "type": "text", "text": {"body": "RESEND"}}]}}]}]}
    with pytest.raises(RuntimeError):
        main._process_payload(container, payload("recovery-1"))
    version = container.conversations.find("9199")["version"]
    main._process_payload(container, payload("recovery-2"))
    assert container.conversations.find("9199")["version"] == version
    assert len(container.reply_outbox.all()) == 1
    row = container.reply_outbox.all()[0]
    row["next_attempt"] = "0"
    container.reply_outbox.upsert(row["id"], row)
    container.whatsapp.send_text = lambda *a: WhatsAppResult(ok=True, message_id="wamid.recovered")
    main._process_payload(container, {})
    assert container.reply_outbox.all()[0]["status"] == "SENT"
