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
    shows_status = expiry is not None and status in {"ACTIVE", "EXPIRED"}
    assert ids == (["CTA_STATUS"] if shows_status else []) + [expected]


@pytest.mark.parametrize("cta", ["CTA_SUBSCRIBE", "CTA_RENEW", "CTA_OPTIN_AGREE"])
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


def test_active_user_can_extend_same_plan(container):
    import main

    container.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    container.subscriber_service.grant_opt_in("9199", "test")
    container.subscriber_service.activate("9199")
    sub = container.subscribers.find("9199")
    sub.end_date = datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=3)
    container.subscribers.update(sub)
    before = container.subscribers.find("9199").end_date
    container.whatsapp = FakeWhatsApp()

    main._handle_message(container, "9199", "button", "PLAN_monthly")

    assert len(container.payments.all()) == 1
    assert container.payments.all()[0].plan == "monthly"
    assert container.subscribers.find("9199").end_date == before


def test_active_user_cannot_choose_lower_plan_from_stale_button(container):
    import main

    container.config["plans"].update({
        "weekly": {"amount": 29, "days": 7},
        "yearly": {"amount": 449, "days": 365},
    })
    container.subscriber_service.upsert_pending("9199", "yearly", "Nitin")
    container.subscriber_service.grant_opt_in("9199", "test")
    container.subscriber_service.activate("9199")
    container.whatsapp = FakeWhatsApp()

    main._handle_message(container, "9199", "button", "PLAN_weekly")

    assert container.payments.all() == []
    assert "largest available plan" in container.whatsapp.sent[-1]["message"]


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
        ("CTA_RENEW", "Upgrade", "Choose a larger plan"),
    ]

    sub = container.subscribers.find("9199")
    sub.plan = "yearly"
    container.subscribers.update(sub)
    main._send_menu(container, "9199")
    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
    ]


def test_pending_checkout_uses_extend_outside_renewal_window(container):
    import main
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"].clear()
    container.config["plans"].update({
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    })
    container.subscribers.append(Subscriber(
        "9199", "monthly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True, name="Nitin",
    ))
    container.payment_service.create_payment("9199", "yearly", today)
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )

    main._send_menu(container, "9199")

    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
        ("CTA_PAYMENT", "Payment instructions", "View your payment details"),
        ("CTA_RENEW", "Upgrade", "Choose a larger plan"),
    ]


def test_applied_yearly_payment_repairs_stale_menu_entitlement(container):
    """Applied admin evidence wins over a stale subscriber plan label."""
    import main
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"].clear()
    container.config["plans"].update({
        "starter": {"amount": 9, "days": 3},
        "weekly": {"amount": 69, "days": 30},
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    })
    sub = Subscriber(
        "9199", "starter", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True,
    )
    container.subscribers.append(sub)
    payment = container.payment_service.create_payment("9199", "yearly", today)
    sub = container.subscribers.find("9199")
    sub.applied_payment_refs = payment.reference_id
    container.subscribers.update(sub)
    container.whatsapp = FakeWhatsApp()

    main._send_menu(container, "9199")

    assert container.whatsapp.sent[-1]["rows"] == ["CTA_STATUS"]
    assert "Payment instructions" not in container.whatsapp.sent[-1]["body"]
    main._send_subscription_status(container, "9199")
    assert "Current plan: Yearly." in container.whatsapp.sent[-1]["message"]

    # A button from an older WhatsApp message is revalidated against current
    # state and cannot reopen obsolete starter-plan choices.
    main._handle_message(container, "9199", "button", "CTA_RENEW")
    assert "largest available plan" in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("plan,days,expected_label", [
    ("starter", 30, "Upgrade"),
    ("weekly", 30, "Upgrade"),
    ("monthly", 3, "Renew"),
    ("yearly", 3, "Renew"),
])
def test_menu_labels_extension_or_renewal_by_expiry_window(container, plan, days, expected_label):
    import main

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"] = {
        "starter": {"amount": 9, "days": 3},
        "weekly": {"amount": 69, "days": 30},
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )
    container.subscribers.append(Subscriber(
        "9199", plan, status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=days), opt_in=True,
    ))

    main._send_menu(container, "9199")

    assert calls[-1][3][0][0] == "CTA_STATUS"
    assert calls[-1][3][1][0] == "CTA_RENEW"
    assert calls[-1][3][1][1] == expected_label


def test_expired_subscriber_menu_offers_renew(container):
    import main

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.subscribers.append(Subscriber(
        "9199", "yearly", status=SubscriberStatus.ACTIVE,
        end_date=today - timedelta(days=1), opt_in=True,
    ))
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )

    main._send_menu(container, "9199")

    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
        ("CTA_RENEW", "Renew", "Renew your subscription"),
    ]


def test_yearly_expiring_menu_does_not_suggest_larger_plan(container):
    import main

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"] = {
        "starter": {"amount": 9, "days": 3},
        "weekly": {"amount": 69, "days": 30},
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    container.subscribers.append(Subscriber(
        "9199", "yearly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=2), opt_in=True,
    ))
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )

    main._send_menu(container, "9199")

    assert calls[-1][3][1] == ("CTA_RENEW", "Renew", "Renew your current plan")


def test_yearly_payment_status_does_not_offer_extend_plan(container):
    """Payment review must not suggest early renewal for the largest plan."""
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
    assert "Same-plan renewal opens three days before expiry" in message
    assert "choose Upgrade" not in message


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


@pytest.mark.parametrize("current,expected", [
    ("starter", ["PLAN_weekly", "PLAN_monthly", "PLAN_yearly"]),
    ("weekly", ["PLAN_monthly", "PLAN_yearly"]),
    ("monthly", ["PLAN_yearly"]),
    ("yearly", []),
])
def test_each_active_plan_only_offers_strictly_larger_plans(container, current, expected):
    """Plan navigation must never offer a smaller plan."""
    import main

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"] = {
        "starter": {"amount": 9, "days": 3},
        "weekly": {"amount": 69, "days": 30},
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    container.subscribers.append(Subscriber(
        "9199", current, status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True,
    ))
    container.whatsapp = FakeWhatsApp()

    main._send_plan_list(container, "9199")

    if expected:
        assert container.whatsapp.sent[-1]["rows"] == expected
    else:
        assert "largest available plan" in container.whatsapp.sent[-1]["message"]


def test_active_higher_plan_supersedes_unpaid_lower_checkout_and_hides_instructions(container):
    import main
    from domain.enums import PaymentStatus

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"] = {
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    container.subscribers.append(Subscriber(
        "9199", "yearly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True, name="Nitin",
    ))
    lower = container.payment_service.create_payment("9199", "monthly", today)
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )

    main._send_menu(container, "9199")

    assert container.payments.find(lower.reference_id).status == PaymentStatus.SUPERSEDED
    assert calls[-1][3] == [
        ("CTA_STATUS", "Subscription status", "Check your subscription"),
    ]


def test_active_higher_plan_keeps_lower_checkout_with_utr_as_review_only(container):
    import main
    from domain.enums import PaymentStatus

    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container.config["plans"] = {
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    container.subscribers.append(Subscriber(
        "9199", "yearly", status=SubscriberStatus.ACTIVE,
        end_date=today + timedelta(days=30), opt_in=True, name="Nitin",
    ))
    lower = container.payment_service.create_payment("9199", "monthly", today)
    lower.utr = "123456789012"
    container.payments.update(lower)
    calls = []
    container.whatsapp = SimpleNamespace(
        send_list=lambda *args: calls.append(args) or SimpleNamespace(ok=True),
    )

    main._send_menu(container, "9199")

    assert container.payments.find(lower.reference_id).status == PaymentStatus.PENDING
    assert calls[-1][3][1] == ("CTA_PAYMENT", "Payment status", "View your payment details")
    assert "verification pending" in calls[-1][1]
    assert "Payment instructions" not in calls[-1][1]


def test_new_user_plan_list_offers_all_configured_plans(container):
    import main

    container.config["plans"] = {
        "starter": {"amount": 9, "days": 3},
        "weekly": {"amount": 69, "days": 30},
        "monthly": {"amount": 199, "days": 90},
        "yearly": {"amount": 699, "days": 365},
    }
    container.whatsapp = FakeWhatsApp()
    main._send_plan_list(container, "9199")

    assert container.whatsapp.sent[-1]["rows"] == [
        "PLAN_starter", "PLAN_weekly", "PLAN_monthly", "PLAN_yearly",
    ]


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
    assert "within 24 hours" in message
    assert "awaiting admin verification" in message
    assert "within 24 hours" in message
    assert payment.reference_id in message
    # The inbound state and send reservation are committed atomically before
    # Meta is contacted; there is no redundant QUEUED-only GitHub commit.
    assert commits[0][0]["status"] == "PENDING"
    assert commits[1][0]["status"] == "SENT"
    assert "within 24 hours" in json.loads(commits[0][0]["arguments"])[0][1]


def test_screenshot_requests_utr_text_without_recording_payment(container):
    import main
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "monthly")
    kind, value = main._extract_input({"type": "image", "image": {"id": "image-id"}})
    main._handle_message(container, "9199", kind, value)
    assert "12-digit UTR as text" in container.whatsapp.sent[-1]["message"]
    assert container.payments.find(payment.reference_id).utr == ""
