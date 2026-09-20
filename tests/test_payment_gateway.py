from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date

import pytest

import main
from adapters.payment_gateway import PaymentGatewayError, RazorpayPaymentGateway
from application.ports.payment_gateway import GatewayCheckout, GatewayEvent
from application.payment_service import PaymentService
from domain.enums import PaymentStatus
from domain.clock import today_ist
from config import Container
from tests.conftest import FakeWhatsApp


@pytest.fixture
def container(tmp_path):
    config = {
        "plans": {"monthly": {"amount": 100, "days": 30}},
        "upi": {"payee_vpa": "test@upi", "payee_name": "Test", "currency": "INR"},
        "payments": {"mode": "manual_utr"},
        "image_sources": [],
        "image_source_config": {},
        "image_validation": {},
        "paths": {
            "images_dir": "images", "subscribers_csv": "subscribers.csv",
            "payments_csv": "payments.csv", "sentlog_csv": "sentlog.csv",
            "renewals_csv": "renewals.csv", "logs_csv": "logs.csv",
        },
        "delivery": {}, "renewal": {}, "persistence": {"mode": "local"},
    }
    built = Container(config, str(tmp_path))
    built.whatsapp = FakeWhatsApp()
    return built


class FakeGateway:
    name = "razorpay"

    def __init__(self, terminal_status="SUCCESS"):
        self.terminal_status = terminal_status
        self.created = 0

    def create_checkout(self, payment):
        self.created += 1
        return GatewayCheckout(self.name, "plink_1", "https://rzp.io/i/test")

    def parse_webhook(self, body, signature):
        data = json.loads(body)
        return GatewayEvent(
            event_id=data.get("id", "evt_1"),
            event_type=data["event"],
            reference_id=data["reference_id"],
            external_payment_id="pay_1",
            terminal_status=self.terminal_status,
        )


def enable_gateway(container, gateway):
    container.payment_gateway = gateway
    container.payment_service = PaymentService(
        container.payments,
        container.config["plans"],
        container.config["upi"],
        container.logs,
        gateway=gateway,
        payment_mode="payment_gateway",
    )


def test_gateway_checkout_is_created_once(container):
    gateway = FakeGateway()
    enable_gateway(container, gateway)
    payment = container.payment_service.create_payment("9199", "monthly", date(2026, 9, 20))

    first = container.payment_service.ensure_gateway_checkout(payment)
    second = container.payment_service.ensure_gateway_checkout(first)

    assert gateway.created == 1
    assert second.payment_provider == "razorpay"
    assert second.gateway_checkout_id == "plink_1"
    assert second.checkout_url == "https://rzp.io/i/test"


def test_gateway_payment_instruction_uses_hosted_link_without_requesting_utr(container):
    gateway = FakeGateway()
    enable_gateway(container, gateway)
    payment = container.payment_service.create_payment("9199", "monthly", date(2026, 9, 20))

    main._send_payment_instructions(container, "9199", payment)

    message = container.whatsapp.sent[-1]["message"]
    assert "https://rzp.io/i/test" in message
    assert "You do not need to send a UTR" in message
    assert "Amount: ₹100" in message


def test_paid_gateway_webhook_automatically_activates_and_is_idempotent(container, monkeypatch):
    gateway = FakeGateway("SUCCESS")
    enable_gateway(container, gateway)
    commits = []
    monkeypatch.setattr(
        main, "_flush_critical_snapshot",
        lambda c, message="": commits.append(message),
    )
    payment = container.payment_service.create_payment("9199", "monthly", date(2026, 9, 20))
    payment = container.payment_service.ensure_gateway_checkout(payment)
    body = json.dumps({"event": "payment_link.paid", "reference_id": payment.reference_id}).encode()

    assert main._process_payment_gateway_webhook(container, body, "test")["status"] == "processed"
    first_expiry = container.subscribers.find("9199").end_date
    assert container.payments.find(payment.reference_id).status == PaymentStatus.SUCCESS
    assert container.payments.find(payment.reference_id).activation_state == "APPLIED"
    approval_messages = [
        item for item in container.whatsapp.sent
        if item.get("mobile") == "9199" and item.get("type") == "text"
    ]
    assert len(approval_messages) == 1
    assert "approved and applied" in approval_messages[0]["message"]
    assert "earn 1 Karma point daily" in approval_messages[0]["message"]
    assert container.welcomes.find(payment.reference_id)["status"] == "SENT"
    assert container.sentlog.was_sent(today_ist(), "9199")
    assert commits == [f"Persist critical gateway event for {payment.reference_id}"]

    main._process_payment_gateway_webhook(container, body, "test")
    assert container.subscribers.find("9199").end_date == first_expiry
    assert len([
        item for item in container.whatsapp.sent
        if item.get("mobile") == "9199" and item.get("type") == "text"
    ]) == 1
    assert commits == [
        f"Persist critical gateway event for {payment.reference_id}",
        f"Persist critical gateway event for {payment.reference_id}",
    ]


def test_terminal_gateway_rejection_fails_pending_but_cannot_revoke_success(container):
    gateway = FakeGateway("FAILED")
    enable_gateway(container, gateway)
    payment = container.payment_service.create_payment("9199", "monthly", date(2026, 9, 20))
    payment = container.payment_service.ensure_gateway_checkout(payment)
    body = json.dumps({"event": "payment_link.expired", "reference_id": payment.reference_id}).encode()

    main._process_payment_gateway_webhook(container, body, "test")
    assert container.payments.find(payment.reference_id).status == PaymentStatus.FAILED
    rejection_messages = len(container.whatsapp.sent)
    main._process_payment_gateway_webhook(container, body, "test")
    assert len(container.whatsapp.sent) == rejection_messages

    successful = container.payment_service.create_payment("9200", "monthly", date(2026, 9, 20))
    successful = container.payment_service.ensure_gateway_checkout(successful)
    container.payment_service.verify_payment(successful.reference_id)
    body = json.dumps({"event": "payment_link.expired", "reference_id": successful.reference_id}).encode()
    main._process_payment_gateway_webhook(container, body, "test")
    assert container.payments.find(successful.reference_id).status == PaymentStatus.SUCCESS


def test_razorpay_webhook_signature_and_mapping(monkeypatch):
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "secret")
    gateway = RazorpayPaymentGateway()
    body = json.dumps({
        "event": "payment_link.paid",
        "payload": {
            "payment_link": {"entity": {"reference_id": "DD2609200001"}},
            "payment": {"entity": {"id": "pay_123"}},
        },
    }).encode()
    signature = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    event = gateway.parse_webhook(body, signature)
    assert event.reference_id == "DD2609200001"
    assert event.external_payment_id == "pay_123"
    assert event.terminal_status == "SUCCESS"
    with pytest.raises(PaymentGatewayError):
        gateway.parse_webhook(body, "bad")
