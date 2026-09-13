from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from types import SimpleNamespace
import json

import pytest
from tests.test_admin import container
from tests.conftest import FakeWhatsApp
from domain.subscriber import Subscriber
from domain.enums import SubscriberStatus


@pytest.mark.parametrize("status,expiry,expected", [
    (None, None, "CTA_SUBSCRIBE"),
    ("PENDING", None, "CTA_SUBSCRIBE"),
    ("EXPIRED", -1, "CTA_RENEW"),
    ("PAUSED", 5, "CTA_RENEW"),
    ("ACTIVE", -1, "CTA_RENEW"),
    ("ACTIVE", 0, "CTA_RENEW"),
    ("ACTIVE", 5, "CTA_RENEW"),
])
def test_menu_matches_entitlement(container, status, expiry, expected):
    import main
    container.config["plans"]["yearly"] = {"amount": 449, "days": 365}
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    if status:
        container.subscribers.append(Subscriber("9199", "monthly", status=SubscriberStatus(status),
            end_date=today + timedelta(days=expiry) if expiry is not None else None))
    calls = []
    container.whatsapp = SimpleNamespace(send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True))
    main._send_menu(container, "9199")
    ids = [row[0] for row in calls[-1][3]]
    assert ids == (["CTA_STATUS"] if expiry is not None else []) + [expected]


@pytest.mark.parametrize("cta", ["CTA_SUBSCRIBE", "CTA_RENEW", "PLAN_monthly", "CTA_OPTIN_AGREE"])
def test_active_user_cannot_purchase_via_old_buttons(container, cta):
    """Legacy CTAs never grant paid days; an explicit plan can now open a renewal checkout."""
    import main
    container.config["plans"]["yearly"] = {"amount": 449, "days": 365}
    container.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    container.subscriber_service.grant_opt_in("9199", "test")
    container.subscriber_service.activate("9199")
    before = container.subscribers.find("9199").end_date
    container.whatsapp = FakeWhatsApp()
    main._handle_message(container, "9199", "button", cta)
    assert len(container.payments.all()) == 0
    assert container.subscribers.find("9199").end_date == before
    assert container.subscribers.find("9199").plan == "monthly"


def test_active_user_can_choose_strictly_larger_plan(container):
    import main
    container.config["plans"]["yearly"] = {"amount": 449, "days": 365}
    container.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    container.subscriber_service.grant_opt_in("9199", "test")
    container.subscriber_service.activate("9199")
    before = container.subscribers.find("9199").end_date
    container.whatsapp = FakeWhatsApp()
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    assert len(container.payments.all()) == 1
    assert container.payments.all()[0].plan == "yearly"
    assert container.subscribers.find("9199").end_date == before
    assert container.subscribers.find("9199").plan == "monthly"


def test_active_menu_labels_extension_and_hides_it_for_largest_plan(container):
    import main
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"]["yearly"] = {"amount": 449, "days": 365}
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )
    container.subscribers.append(Subscriber(
        "9199", "monthly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=5), opt_in=True,
    ))
    main._send_menu(container, "9199")
    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
        ("CTA_RENEW", "Extend plan", "Choose a larger plan"),
    ]

    sub = container.subscribers.find("9199")
    sub.plan = "yearly"
    container.subscribers.update(sub)
    main._send_menu(container, "9199")
    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
    ]


def test_yearly_payment_status_does_not_offer_extend_plan(container):
    """The largest active plan must not advertise an unavailable upgrade."""
    import main
    from domain.enums import PaymentStatus

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"]["yearly"] = {"amount": 699, "days": 365}
    container.subscribers.append(Subscriber(
        "9199", "yearly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True,
    ))
    payment = container.payment_service.create_payment("9199", "yearly", today)
    payment.utr = "123456789012"
    payment.status = PaymentStatus.PENDING
    container.payments.update(payment)

    message = main._payment_status_text(container, payment)
    assert "Extend plan" not in message
    assert "No larger plan is currently available" in message


def test_active_plan_list_contains_only_strictly_larger_plans(container):
    import main
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"].update({
        "weekly": {"amount": 29, "days": 7},
        "yearly": {"amount": 449, "days": 365},
    })
    container.subscribers.append(Subscriber(
        "9199", "monthly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=5), opt_in=True,
    ))
    container.whatsapp = FakeWhatsApp()
    main._send_plan_list(container, "9199")
    assert container.whatsapp.sent[-1]["rows"] == ["PLAN_yearly"]


def test_subscription_status_includes_current_plan(container):
    import main
    container.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    container.subscriber_service.activate("9199")
    container.whatsapp = FakeWhatsApp()
    main._send_subscription_status(container, "9199")
    assert "Current plan: Monthly." in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("text", ["123456789012", "UTR: 123456789012"])
def test_production_utr_ack_is_persisted_and_sent_once(container, text):
    import main
    container.config["persistence"] = {"mode": "github_api"}
    commits = []
    def persist(*args, **kwargs):
        commits.append([dict(row) for row in container.reply_outbox.all()])
    container.repo_sync = SimpleNamespace(enabled=True,
        pull=lambda **kw: None, push=persist, abort=lambda: None)
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "monthly")
    payload = {"entry": [{"changes": [{"value": {"messages": [{"id": "utr-event",
        "from": "9199", "type": "text", "text": {"body": text}}]}}]}]}
    main._process_payload(container, payload)
    main._process_payload(container, payload)
    assert container.payments.find(payment.reference_id).utr == "123456789012"
    assert len(container.whatsapp.sent) == 1
    message = container.whatsapp.sent[0]["message"]
    assert "Please allow us some time" in message
    assert "admin will review" in message
    assert payment.reference_id in message
    assert commits[0][0]["status"] == "QUEUED"
    assert "Please allow us some time" in json.loads(commits[0][0]["arguments"])[0][1]


def test_screenshot_requests_utr_text_without_recording_payment(container):
    import main
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "monthly")
    kind, value = main._extract_input({"type": "image", "image": {"id": "image-id"}})
    main._handle_message(container, "9199", kind, value)
    assert "12-digit UTR as text" in container.whatsapp.sent[-1]["message"]
    assert container.payments.find(payment.reference_id).utr == ""
