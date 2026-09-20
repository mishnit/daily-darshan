"""Hosted Razorpay Payment Links adapter.

The adapter deliberately uses the REST API directly so the application does
not need a provider SDK. Credentials and webhook secrets are environment-only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

import requests

from application.ports.payment_gateway import GatewayCheckout, GatewayEvent
from domain.payment import Payment


class PaymentGatewayError(RuntimeError):
    pass


class RazorpayPaymentGateway:
    name = "razorpay"

    def __init__(self, config: dict | None = None, session=None):
        config = config or {}
        self._key_id = os.environ.get("RAZORPAY_KEY_ID", "")
        self._key_secret = os.environ.get("RAZORPAY_KEY_SECRET", "")
        self._webhook_secret = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
        self._base_url = config.get("base_url", "https://api.razorpay.com/v1").rstrip("/")
        self._callback_url = config.get("callback_url", "")
        self._session = session or requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self._key_id and self._key_secret and self._webhook_secret)

    def create_checkout(self, payment: Payment) -> GatewayCheckout:
        if not self._key_id or not self._key_secret:
            raise PaymentGatewayError("Razorpay credentials are not configured")
        payload = {
            "amount": int(round(payment.amount * 100)),
            "currency": "INR",
            "accept_partial": False,
            "description": f"Daily Darshan {payment.plan} plan",
            "reference_id": payment.reference_id,
            "customer": {"contact": f"+{payment.mobile.lstrip('+')}"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": True,
            "notes": {
                "reference_id": payment.reference_id,
                "mobile": payment.mobile,
                "plan": payment.plan,
            },
        }
        if self._callback_url:
            payload.update(callback_url=self._callback_url, callback_method="get")
        try:
            response = self._session.post(
                f"{self._base_url}/payment_links",
                auth=(self._key_id, self._key_secret),
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise PaymentGatewayError(f"Could not create Razorpay checkout: {exc}") from exc
        external_id, url = str(data.get("id", "")), str(data.get("short_url", ""))
        if not external_id or not url:
            raise PaymentGatewayError("Razorpay checkout response omitted id or short_url")
        return GatewayCheckout(self.name, external_id, url)

    def parse_webhook(self, body: bytes, signature: str) -> GatewayEvent:
        if not self._webhook_secret:
            raise PaymentGatewayError("Razorpay webhook secret is not configured")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(expected, signature):
            raise PaymentGatewayError("Invalid Razorpay webhook signature")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise PaymentGatewayError("Invalid Razorpay webhook JSON") from exc
        event_type = str(payload.get("event", ""))
        entities = payload.get("payload", {})
        link = entities.get("payment_link", {}).get("entity", {})
        payment = entities.get("payment", {}).get("entity", {})
        notes = link.get("notes") or payment.get("notes") or {}
        reference = str(link.get("reference_id") or notes.get("reference_id") or "")
        external_payment_id = str(payment.get("id", ""))
        terminal = {
            "payment_link.paid": "SUCCESS",
            "payment.captured": "SUCCESS",
            "payment_link.cancelled": "FAILED",
            "payment_link.expired": "FAILED",
        }.get(event_type, "")
        return GatewayEvent(
            event_id=str(payload.get("id") or external_payment_id or link.get("id") or ""),
            event_type=event_type,
            reference_id=reference,
            external_payment_id=external_payment_id,
            terminal_status=terminal,
        )
