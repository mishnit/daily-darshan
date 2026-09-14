"""Renewal/upgrade boundaries and recovery through real checkout handlers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import main
from domain.enums import PaymentStatus, SubscriberStatus
from domain.subscriber import Subscriber
from tests.conftest import FakeWhatsApp
from tests.test_admin import container


PLANS = ("starter", "weekly", "monthly", "yearly")
TODAY = datetime(2026, 9, 14, 12, tzinfo=ZoneInfo("Asia/Kolkata"))


@pytest.fixture
def journey(container, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return TODAY.astimezone(tz) if tz else TODAY.replace(tzinfo=None)

    monkeypatch.setattr(main, "datetime", Clock)
    container.config["plans"].clear()
    container.config["plans"].update({
        "starter": {"days": 3, "amount": 9},
        "weekly": {"days": 30, "amount": 69},
        "monthly": {"days": 90, "amount": 199},
        "yearly": {"days": 365, "amount": 699},
    })
    container.whatsapp = FakeWhatsApp()
    return container


def subscriber(c, plan, remaining, opt_in=True):
    c.subscribers.append(Subscriber(
        "9199", plan, name="Nitin", status=SubscriberStatus.ACTIVE,
        start_date=TODAY.date() - timedelta(days=10),
        end_date=TODAY.date() + timedelta(days=remaining), opt_in=opt_in,
    ))
    return c.subscribers.find("9199")


@pytest.mark.parametrize("current", PLANS)
@pytest.mark.parametrize("remaining", [-1, 0, 1, 2, 3, 4, 30])
@pytest.mark.parametrize("target", PLANS)
def test_checkout_policy_for_every_plan_and_expiry_boundary(journey, current, remaining, target):
    before = subscriber(journey, current, remaining).to_row()
    expected = [p for p in PLANS if remaining < 0 or PLANS.index(p) > PLANS.index(current)
                or (p == current and remaining <= 3)]
    assert main._eligible_plan_names(journey, "9199") == expected
    main._send_plan_list(journey, "9199")
    if expected:
        assert journey.whatsapp.sent[-1]["rows"] == [f"PLAN_{p}" for p in expected]
    else:
        assert "renewal opens three days" in journey.whatsapp.sent[-1]["message"]

    # Old WhatsApp plan buttons must obey exactly the same policy as the list.
    main._handle_message(journey, "9199", "button", f"PLAN_{target}")
    payments = journey.payments.all()
    assert [p.plan for p in payments] == ([target] if target in expected else [])
    assert journey.subscribers.find("9199").to_row() == before
    if target in expected:
        assert "Pay via UPI" in journey.whatsapp.sent[-1]["message"]
        main._handle_message(journey, "9199", "button", f"PLAN_{target}")
        assert len(journey.payments.all()) == 1  # Repeated selection reuses checkout.


@pytest.mark.parametrize("reminders", [[7, 3, 1], [1], [], [30]])
@pytest.mark.parametrize("remaining,allowed", [(0, True), (3, True), (4, False)])
def test_reminder_schedule_cannot_change_renewal_eligibility(journey, reminders, remaining, allowed):
    subscriber(journey, "monthly", remaining)
    journey.config["renewal"]["reminder_days"] = reminders
    assert ("monthly" in main._eligible_plan_names(journey, "9199")) is allowed


@pytest.mark.parametrize("entry", ["MENU", "PAYMENT", "CONTINUE", "CTA_PAYMENT", "CTA_OPTIN_AGREE", "direct"])
def test_stale_same_plan_unpaid_checkout_cannot_bypass_window(journey, entry):
    before = subscriber(journey, "monthly", 4).to_row()
    payment = journey.payment_service.create_payment("9199", "monthly")
    if entry == "direct":
        main._send_payment_instructions(journey, "9199", payment, True)
    else:
        main._handle_message(journey, "9199", "button" if entry.startswith("CTA_") else "text", entry)
    assert journey.payments.find(payment.reference_id).status == PaymentStatus.SUPERSEDED
    assert len(journey.payments.all()) == 1
    assert not any("Pay via UPI" in m.get("message", "") for m in journey.whatsapp.sent)
    sub = journey.subscribers.find("9199")
    assert (sub.plan, sub.end_date) == (before["plan"], TODAY.date() + timedelta(days=4))


@pytest.mark.parametrize("remaining", [0, 3, 4, 30])
@pytest.mark.parametrize("has_utr", [False, True])
def test_existing_same_plan_checkout_preserves_payment_evidence(journey, remaining, has_utr):
    subscriber(journey, "monthly", remaining)
    payment = journey.payment_service.create_payment("9199", "monthly")
    if has_utr:
        payment.utr = "123456789012"
        journey.payments.update(payment)
    main._handle_message(journey, "9199", "button", "CTA_PAYMENT")
    stored = journey.payments.find(payment.reference_id)
    assert stored.status == (PaymentStatus.PENDING if has_utr or remaining <= 3 else PaymentStatus.SUPERSEDED)
    messages = " ".join(m.get("message", "") for m in journey.whatsapp.sent)
    if has_utr:
        assert stored.utr == "123456789012"
        assert "verification pending" in messages
        assert "Pay via UPI" not in messages
    elif remaining <= 3:
        assert "Pay via UPI" in messages
    else:
        assert "Pay via UPI" not in messages


def test_direct_payment_start_cannot_create_early_renewal(journey):
    subscriber(journey, "monthly", 4)
    main._start_payment(journey, "9199", "monthly", True)
    assert journey.payments.all() == []
    assert journey.whatsapp.sent[-1]["rows"] == ["PLAN_yearly"]


def test_window_is_rechecked_after_subscription_extension(journey):
    sub = subscriber(journey, "monthly", 3)
    main._handle_message(journey, "9199", "button", "PLAN_monthly")
    old = journey.payments.all()[0]
    sub.end_date += timedelta(days=90)
    journey.subscribers.update(sub)
    main._handle_message(journey, "9199", "button", "CTA_PAYMENT")
    assert journey.payments.find(old.reference_id).status == PaymentStatus.SUPERSEDED
    main._handle_message(journey, "9199", "text", f"UTR {old.reference_id} 123456789012")
    restored = journey.payments.find(old.reference_id)
    assert restored.utr == "123456789012"
    assert restored.status == PaymentStatus.PENDING
    assert journey.subscribers.find("9199").end_date == sub.end_date


@pytest.mark.parametrize("remaining", [3, 4])
def test_consent_recovery_preserves_upgrade_and_renewal_policy(journey, remaining):
    before = subscriber(journey, "monthly", remaining, opt_in=False)
    target = "monthly" if remaining == 3 else "yearly"
    main._handle_message(journey, "9199", "button", f"PLAN_{target}")
    assert not any("Pay via UPI" in m.get("message", "") for m in journey.whatsapp.sent)
    main._handle_message(journey, "9199", "button", "CTA_OPTIN_AGREE")
    assert "Pay via UPI" in journey.whatsapp.sent[-1]["message"]
    after = journey.subscribers.find("9199")
    assert after.opt_in
    assert (after.plan, after.end_date) == (before.plan, before.end_date)


@pytest.mark.parametrize("reminders", [[7, 3, 1], [1], []])
@pytest.mark.parametrize("remaining", [0, 3, 4, 7])
def test_configured_page_renewal_cta_matches_checkout_window(journey, tmp_path, reminders, remaining):
    from config import Container

    sub = subscriber(journey, "monthly", remaining)
    journey.config["renewal"].update({"reminder_days": reminders, "whatsapp_number": "9199"})
    configured = Container(config=journey.config, root=str(tmp_path))
    html = configured.page_renderer.render_html(sub, TODAY.date(), delivered=True)
    assert ("Renew on WhatsApp" in html) is (remaining <= 3)
