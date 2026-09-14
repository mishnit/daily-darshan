"""Approved template payloads and the shared daily contact limit."""
from datetime import date, timedelta
from itertools import permutations
import json
from pathlib import Path

import pytest

from adapters.whatsapp import MetaWhatsAppClient
from application.ports.whatsapp import WhatsAppResult
from application.subscription_template import subscription_status
from application.welcome_service import drain_welcomes, queue_missing_welcomes
from config import Container
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from tests.conftest import FakeWhatsApp
from tests.test_admin import container


DAY = date(2030, 9, 14)  # Must use scheduled date, not wall-clock date.
IMAGE = "https://vipseva.com/images/today.jpg"
TEMPLATE = "dailydarshan_subscription_status"


@pytest.fixture
def configured(container, tmp_path):
    production = json.loads(Path("config.json").read_text())
    cfg = container.config
    cfg["delivery"] = production["delivery"] | {"max_send_retries": 1}
    cfg["renewal"] = production["renewal"]
    c = Container(config=cfg, root=str(tmp_path))
    c.whatsapp = FakeWhatsApp()
    c.delivery_service._whatsapp = c.whatsapp
    c.renewal_service._whatsapp = c.whatsapp
    c.renewal_service._max_retries = 1
    return c


def add_sub(c, remaining=3, *, opted_in=True, name="Nitin Mishra"):
    sub = Subscriber(
        "9199", "monthly", name=name, status=SubscriberStatus.ACTIVE,
        start_date=DAY - timedelta(days=27), end_date=DAY + timedelta(days=remaining),
        opt_in=opted_in, subscription_id="abc123", applied_payment_refs="DD3009140001",
    )
    c.subscribers.append(sub)
    queue_missing_welcomes(c)
    return sub


def run(c, kind, day=DAY, image=IMAGE):
    if kind == "welcome":
        return drain_welcomes(c, day, lambda: None, lambda *_: True, header_image_url=image)
    if kind == "renewal":
        return c.renewal_service.run(day, header_image_url=image)
    return c.delivery_service.deliver(day, image_url=image)


@pytest.mark.parametrize("kind", ["welcome", "renewal", "delivery"])
@pytest.mark.parametrize("remaining,status", [(30, "Active"), (4, "Active"), (3, "Expiring in 3 days"),
                                          (2, "Expiring in 2 days"), (1, "Expiring in 1 day"), (0, "Expiring today")])
def test_all_senders_use_approved_components_and_business_date(configured, monkeypatch, kind, remaining, status):
    add_sub(configured, remaining)
    payloads = []
    client = MetaWhatsAppClient(access_token="test", phone_number_id="test")
    monkeypatch.setattr(client, "_post", lambda payload: payloads.append(payload) or WhatsAppResult(ok=True, message_id="wamid.test"))
    configured.delivery_service._whatsapp = client
    configured.renewal_service._whatsapp = client
    run(configured, kind)
    if kind == "renewal" and remaining not in {1, 2, 3}:
        assert payloads == []
        return
    assert len(payloads) == 1
    assert payloads[0]["to"] == "9199"
    assert payloads[0]["template"] == {
        "name": TEMPLATE, "language": {"code": "en"},
        "components": [
            {"type": "header", "parameters": [{"type": "image", "image": {"link": IMAGE}}]},
            {"type": "body", "parameters": [
                {"type": "text", "text": "Nitin Mishra"},
                {"type": "text", "text": "Activated" if kind == "welcome" else status},
            ]},
            {"type": "button", "sub_type": "url", "index": "0",
             "parameters": [{"type": "text", "text": "abc123"}]},
        ],
    }


@pytest.mark.parametrize("order", list(permutations(["welcome", "renewal", "delivery"])))
def test_only_one_daily_message_in_any_execution_order_and_on_rerun(configured, order):
    add_sub(configured)
    for _ in range(2):
        for kind in order:
            run(configured, kind)
    assert len(configured.whatsapp.sent) == 1
    assert configured.sentlog.was_sent(DAY, "9199")
    expected = "Activated" if order[0] == "welcome" else "Expiring in 3 days"
    assert configured.whatsapp.sent[0]["params"] == ["Nitin Mishra", expected]
    for kind in order:
        run(configured, kind, DAY + timedelta(days=1))
    assert len(configured.whatsapp.sent) == 2


@pytest.mark.parametrize("first", ["welcome", "renewal", "delivery"])
def test_uncertain_send_blocks_every_other_sender_and_reruns(configured, first, monkeypatch):
    add_sub(configured)
    monkeypatch.setattr(configured.whatsapp, "_result", lambda: WhatsAppResult(ok=False, unknown=True, error="timeout"))
    run(configured, first)
    for kind in ["welcome", "renewal", "delivery", first]:
        run(configured, kind)
    assert len(configured.whatsapp.sent) == 1
    assert configured.sentlog.was_sent(DAY, "9199")


@pytest.mark.parametrize("first", ["welcome", "renewal", "delivery"])
def test_confirmed_failure_allows_one_successful_fallback(configured, first):
    add_sub(configured)
    configured.whatsapp._fail_times = 1
    run(configured, first)
    for kind in ["welcome", "renewal", "delivery", first]:
        run(configured, kind)
    assert len(configured.whatsapp.sent) == 2
    assert sum(m["ok"] for m in configured.whatsapp.sent) == 1


@pytest.mark.parametrize("kind", ["welcome", "renewal", "delivery"])
def test_missing_required_header_does_not_send(configured, kind):
    add_sub(configured)
    run(configured, kind, image=None)
    assert configured.whatsapp.sent == []
    assert not configured.sentlog.was_sent(DAY, "9199")


@pytest.mark.parametrize("kind", ["welcome", "renewal", "delivery"])
@pytest.mark.parametrize("name,expected", [("", "devotee"), ("Nitin\n\tMishra", "Nitin Mishra")])
def test_name_is_sanitized_consistently(configured, kind, name, expected):
    add_sub(configured, name=name)
    run(configured, kind)
    assert configured.whatsapp.sent[0]["params"][0] == expected


@pytest.mark.parametrize("expired,opted_in", [(True, True), (False, False)])
def test_expired_and_opted_out_subscribers_get_no_automatic_message(configured, expired, opted_in):
    sub = add_sub(configured, -1 if expired else 3, opted_in=opted_in)
    for kind in ["welcome", "renewal", "delivery"]:
        run(configured, kind)
    assert configured.whatsapp.sent == []
    if expired:
        assert subscription_status(sub, DAY) == "Expired"


@pytest.mark.parametrize("kind", ["welcome", "renewal", "delivery"])
def test_daily_slot_is_reserved_before_provider_call(configured, monkeypatch, kind):
    add_sub(configured)
    original = configured.whatsapp._result
    def send():
        assert configured.sentlog.was_sent(DAY, "9199")
        return original()
    monkeypatch.setattr(configured.whatsapp, "_result", send)
    run(configured, kind)
    assert len(configured.whatsapp.sent) == 1
