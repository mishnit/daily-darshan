"""Payment-gateway boundary used by checkout and signed callbacks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from domain.payment import Payment


@dataclass(frozen=True)
class GatewayCheckout:
    provider: str
    external_id: str
    url: str


@dataclass(frozen=True)
class GatewayEvent:
    event_id: str
    event_type: str
    reference_id: str
    external_payment_id: str = ""
    terminal_status: str = ""


class PaymentGatewayPort(Protocol):
    name: str

    def create_checkout(self, payment: Payment) -> GatewayCheckout: ...

    def parse_webhook(self, body: bytes, signature: str) -> GatewayEvent: ...
