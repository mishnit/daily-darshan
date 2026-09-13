"""Product-level invariants using real repositories and message handlers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.test_admin import container
from tests.conftest import FakeWhatsApp


@pytest.fixture(autouse=True)
def add_second_plan(container):
    container.config["plans"]["yearly"] = {"amount": 699, "days": 365}


def setup_sub(c, *, expired=False, opted_in=True):
    c.whatsapp = FakeWhatsApp()
    c.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    c.subscriber_service.activate("9199")
    if opted_in:
        c.subscriber_service.grant_opt_in("9199", "test")
    if expired:
        sub = c.subscribers.find("9199")
        sub.end_date = datetime.now(ZoneInfo("Asia/Kolkata")).date() - timedelta(days=1)
        c.subscribers.update(sub)
    return c.subscribers.find("9199")


@pytest.mark.parametrize("expired", [False, True])
def test_renewal_checkout_preserves_entitlement_and_reuses_name(container, expired):
    import main
    original = setup_sub(container, expired=expired).to_row()
    main._handle_message(container, "9199", "button", "CTA_RENEW")
    assert container.payments.all() == []
    for _ in range(2):
        main._handle_message(container, "9199", "button", "PLAN_yearly", "Different Profile")
    assert container.subscribers.find("9199").to_row() == original
    assert len(container.payments.all()) == 1
    assert container.payments.all()[0].plan == "yearly"
    assert "Renewing your yearly plan" in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("action", ["CTA_SUBSCRIBE", "CTA_RENEW", "PLAN_yearly", "CTA_BACK"])
def test_payment_review_cannot_be_replaced_by_stale_cta(container, action):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_monthly")
    main._handle_message(container, "9199", "text", "123456789012")
    before = container.payments.all()[0].to_row()
    main._handle_message(container, "9199", "button", action)
    main._handle_message(container, "9199", "text", "999999999999")
    assert [p.to_row() for p in container.payments.all()] == [before]
    assert "do not pay again" in container.whatsapp.sent[-1]["message"]


def test_active_continue_shows_renewal_checkout_not_only_subscription_status(container):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    payment = container.payments.all()[0]
    main._resume_conversation(container, "9199")
    assert payment.reference_id in container.whatsapp.sent[-1]["message"]
    main._send_subscription_status(container, "9199")
    assert "active" in container.whatsapp.sent[-1]["message"]
    assert payment.reference_id in container.whatsapp.sent[-1]["message"]


def test_stop_resume_keeps_paid_days_and_creates_no_payment(container):
    import main
    original = setup_sub(container)
    main._handle_message(container, "9199", "text", "STOP")
    main._send_menu(container, "9199")
    rows = container.whatsapp.sent[-1]["rows"]
    assert "CTA_RESUME_MESSAGES" in rows
    main._handle_message(container, "9199", "button", "CTA_RESUME_MESSAGES")
    assert not container.subscribers.find("9199").opt_in
    main._handle_message(container, "9199", "button", "CTA_OPTIN_AGREE")
    sub = container.subscribers.find("9199")
    assert sub.opt_in and sub.end_date == original.end_date and sub.plan == original.plan
    assert container.payments.all() == []


def test_expired_optout_renewal_requires_consent_and_uses_selected_plan(container):
    import main
    original = setup_sub(container, expired=True, opted_in=False)
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    assert container.whatsapp.sent[-1]["type"] == "buttons"
    assert not container.subscribers.find("9199").opt_in
    main._handle_message(container, "9199", "button", "CTA_OPTIN_AGREE")
    assert "Renewing your yearly plan" in container.whatsapp.sent[-1]["message"]
    assert container.subscribers.find("9199").end_date == original.end_date


def test_new_customer_can_change_unpaid_plan(container):
    import main
    container.whatsapp = FakeWhatsApp()
    main._handle_message(container, "9199", "button", "PLAN_monthly", "Nitin")
    main._handle_message(container, "9199", "button", "CTA_OPTIN_AGREE")
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    assert main._latest_pending_payment(container, "9199").plan == "yearly"
    assert "Plan: yearly" in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("expired", [False, True])
def test_approval_applies_selected_plan_once_and_queues_welcome(container, expired):
    import admin
    import main
    from types import SimpleNamespace
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    original = setup_sub(container, expired=expired)
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    main._handle_message(container, "9199", "text", "123456789012")
    payment = main._latest_pending_payment(container, "9199")
    args = SimpleNamespace(reference_id=payment.reference_id, activate=True, renew=False, commit=False)
    before_sends = len(container.whatsapp.sent)
    for _ in range(2):
        assert admin.cmd_verify(container, args) == 0
    sub = container.subscribers.find("9199")
    assert sub.plan == "yearly" and sub.status.value == "ACTIVE"
    assert sub.end_date == max(today, original.end_date) + timedelta(days=365)
    assert len(container.welcomes.all()) == 1
    assert container.welcomes.all()[0]["status"] == "QUEUED"
    assert len(container.whatsapp.sent) == before_sends
    assert container.sentlog.all() == []
