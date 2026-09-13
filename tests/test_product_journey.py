"""Product-level invariants using real repositories and message handlers."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.test_admin import container
from tests.conftest import FakeWhatsApp


@pytest.fixture(autouse=True)
def add_second_plan(container):
    container.config["plans"]["quarterly"] = {"amount": 199, "days": 90}
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
    """Plan navigation cannot destroy the reviewed payment or misapply entitlement."""
    import main
    from domain.enums import PaymentStatus
    setup_sub(container)
    entitlement_before = container.subscribers.find("9199").to_row()
    main._handle_message(container, "9199", "button", "PLAN_quarterly")
    paid = container.payments.all()[0]
    main._handle_message(container, "9199", "text", f"UTR {paid.reference_id} 123456789012")
    main._send_menu(container, "9199")
    assert "CTA_PAYMENT" in container.whatsapp.sent[-1]["rows"]
    assert "CTA_RENEW" in container.whatsapp.sent[-1]["rows"]
    assert f"UTR {paid.reference_id} 123456789012" in container.whatsapp.sent[-1]["body"]

    main._handle_message(container, "9199", "button", action)
    if action == "CTA_BACK":
        assert "CTA_RENEW" in container.whatsapp.sent[-1]["rows"]
        main._handle_message(container, "9199", "button", "CTA_RENEW")
    if action != "PLAN_yearly":
        assert "PLAN_yearly" in container.whatsapp.sent[-1]["rows"]
        main._handle_message(container, "9199", "button", "PLAN_yearly")
    replacement = main._latest_pending_payment(container, "9199")
    assert replacement.reference_id != paid.reference_id
    preserved = container.payments.find(paid.reference_id)
    assert preserved.status == PaymentStatus.SUPERSEDED
    assert preserved.utr == "123456789012"
    assert container.subscribers.find("9199").to_row() == entitlement_before
    assert f"Example: UTR {replacement.reference_id} 123456789012" in container.whatsapp.sent[-1]["message"]

    main._handle_message(container, "9199", "text", f"UTR {paid.reference_id} 123456789012")
    assert container.payments.find(paid.reference_id).status == PaymentStatus.PENDING
    assert container.payments.find(replacement.reference_id).status == PaymentStatus.SUPERSEDED
    assert "awaiting admin verification" in container.whatsapp.sent[-1]["message"]
    assert "within 24 hours" in container.whatsapp.sent[-1]["message"]


def test_payment_status_during_review_explains_reference_qualified_utr(container):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_quarterly")
    payment = container.payments.all()[0]
    main._handle_message(container, "9199", "text", "123456789012")
    main._handle_message(container, "9199", "button", "CTA_PAYMENT")
    message = container.whatsapp.sent[-1]["message"]
    assert "verification pending" in message
    assert f"UTR {payment.reference_id} 123456789012" in message
    assert "Extend plan" in message


def test_reference_qualified_utr_correction_overwrites_previous_value(container):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_quarterly")
    payment = container.payments.all()[0]
    main._handle_message(container, "9199", "text", f"UTR {payment.reference_id} 123456789012")
    main._handle_message(container, "9199", "text", f"UTR {payment.reference_id} 999999999999")
    assert container.payments.find(payment.reference_id).utr == "999999999999"
    assert "Your latest UTR 999999999999 for payment" in container.whatsapp.sent[-1]["message"]
    assert "It replaced the previous UTR (if any)" in container.whatsapp.sent[-1]["message"]
    assert "awaiting admin verification" in container.whatsapp.sent[-1]["message"]
    assert "within 24 hours" in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("action", ["CTA_SUBSCRIBE", "CTA_RENEW", "CTA_BACK"])
def test_review_state_legacy_navigation_is_not_blocked(container, action):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_quarterly")
    payment = container.payments.all()[0]
    main._handle_message(container, "9199", "text", f"UTR {payment.reference_id} 123456789012")
    main._handle_message(container, "9199", "button", action)
    reply = container.whatsapp.sent[-1]
    if action == "CTA_BACK":
        assert "CTA_RENEW" in reply["rows"]
    else:
        assert "PLAN_yearly" in reply["rows"]


def test_active_continue_shows_renewal_checkout_not_only_subscription_status(container):
    import main
    setup_sub(container)
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    payment = container.payments.all()[0]
    main._resume_conversation(container, "9199")
    payment_message = container.whatsapp.sent[-1]["message"]
    assert payment.reference_id in payment_message
    assert f"Example: UTR {payment.reference_id} 123456789012" in payment_message
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


@pytest.mark.parametrize("stage", ["new", "active", "expired", "unpaid", "review", "optout"])
def test_messages_do_not_advertise_obsolete_navigation(container, stage):
    import main
    import re
    container.whatsapp = FakeWhatsApp()
    if stage != "new":
        setup_sub(container, expired=stage == "expired", opted_in=stage != "optout")
    if stage in {"unpaid", "review"}:
        main._handle_message(container, "9199", "button", "PLAN_quarterly")
        if stage == "review":
            main._handle_message(container, "9199", "text", "123456789012")
    container.whatsapp.sent.clear()
    main._send_menu(container, "9199")
    rows = container.whatsapp.sent[-1]["rows"]
    assert not set(rows) & {"CTA_CONTINUE", "CTA_RESEND", "CTA_BACK", "CTA_HELP", "CTA_STOP"}
    if stage in {"unpaid", "review"}:
        assert "CTA_PAYMENT" in rows
        assert "CTA_RENEW" in rows
        main._handle_message(container, "9199", "button", "CTA_PAYMENT")
        assert main._latest_pending_payment(container, "9199").reference_id in container.whatsapp.sent[-1]["message"]
    main._handle_message(container, "9199", "button", "CTA_HELP")
    main._send_subscription_status(container, "9199")
    main._handle_opt_out(container, "9199")
    for sent in container.whatsapp.sent:
        assert not re.search(r"\b(continue|resend|back)\b", sent.get("body", "") + sent.get("message", ""), re.I)


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


def test_new_user_menu_and_incomplete_signup(container):
    import main
    container.whatsapp = FakeWhatsApp()
    main._send_menu(container, "9199")
    assert container.whatsapp.sent[-1]["rows"] == ["CTA_SUBSCRIBE"]
    main._handle_message(container, "9199", "button", "CTA_SUBSCRIBE")
    assert "PLAN_monthly" in container.whatsapp.sent[-1]["rows"]
    main._handle_message(container, "9199", "button", "PLAN_monthly")
    main._send_menu(container, "9199")
    assert "What name" in container.whatsapp.sent[-1]["body"]
    assert container.whatsapp.sent[-1]["rows"] == ["CTA_SUBSCRIBE"]
    container.subscriber_service.set_name("9199", "nitin")
    container.subscriber_service.set_awaiting_name("9199", False)
    main._send_menu(container, "9199")
    assert container.whatsapp.sent[-1]["buttons"] == ["CTA_OPTIN_AGREE", "CTA_STOP"]
    assert container.payments.all() == []


def test_orphaned_unpaid_checkout_restarts_signup_without_deleting_payment(container):
    import main
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "yearly")
    before = payment.to_row()
    main._send_menu(container, "9199")
    assert container.whatsapp.sent[-1]["rows"] == ["CTA_SUBSCRIBE"]
    assert "Payment instructions" not in container.whatsapp.sent[-1]["body"]
    assert container.payments.find(payment.reference_id).to_row() == before
    # An old Payment instructions button must also lead to plan selection.
    main._handle_message(container, "9199", "button", "CTA_PAYMENT")
    assert "PLAN_yearly" in container.whatsapp.sent[-1]["rows"]
    main._handle_message(container, "9199", "button", "PLAN_yearly")
    assert container.subscribers.find("9199").awaiting_name
    main._handle_message(container, "9199", "text", "Nitin")
    assert container.whatsapp.sent[-1]["buttons"] == ["CTA_OPTIN_AGREE", "CTA_STOP"]
    main._handle_message(container, "9199", "button", "CTA_OPTIN_AGREE")
    assert payment.reference_id in container.whatsapp.sent[-1]["message"]
    assert len(container.payments.all()) == 1


@pytest.mark.parametrize("status,utr", [("FAILED", ""), ("SUCCESS", "")])
def test_orphaned_payment_evidence_keeps_status_and_blocks_purchase(container, status, utr):
    import main
    from domain.enums import PaymentStatus
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "yearly")
    payment.status = PaymentStatus(status)
    payment.utr = utr
    container.payments.update(payment)
    before = payment.to_row()
    main._send_menu(container, "9199")
    assert container.whatsapp.sent[-1]["rows"] == ["CTA_PAYMENT"] + (["CTA_PAYMENT_REVIEW"] if status == "FAILED" else [])
    for cta in ["CTA_SUBSCRIBE", "CTA_RENEW", "PLAN_monthly"]:
        main._handle_message(container, "9199", "button", cta)
        assert payment.reference_id in container.whatsapp.sent[-1]["message"]
    assert container.subscribers.find("9199") is None
    assert [p.to_row() for p in container.payments.all()] == [before]


@pytest.mark.parametrize("cta", ["PLAN_yearly", "CTA_SUBSCRIBE", "CTA_RENEW", "CTA_OPTIN_AGREE", "CTA_PAYMENT"])
def test_rejected_payment_requires_admin_resolution(container, cta):
    import main
    import admin
    from types import SimpleNamespace
    setup_sub(container)
    p = container.payment_service.create_payment("9199", "monthly")
    admin.cmd_reject(container, SimpleNamespace(reference_id=p.reference_id, commit=False))
    main._handle_message(container, "9199", "button", cta)
    main._handle_message(container, "9199", "text", "123456789012")
    assert len(container.payments.all()) == 1
    assert container.payments.find(p.reference_id).status.value == "FAILED"
    main._handle_message(container, "9199", "button", "CTA_PAYMENT")
    assert "rejected" in container.whatsapp.sent[-1]["message"]


@pytest.mark.parametrize("consent", [True, False])
def test_approved_payment_waits_for_publication_not_welcome_delivery(container, monkeypatch, consent):
    import main
    import admin
    from types import SimpleNamespace
    from application.welcome_service import drain_welcomes
    from application.ports.whatsapp import WhatsAppResult
    setup_sub(container, opted_in=consent)
    p = container.payment_service.create_payment("9199", "monthly")
    admin.cmd_verify(container, SimpleNamespace(reference_id=p.reference_id, activate=True, renew=True, commit=False))
    main._send_menu(container, "9199")
    assert "publication is awaiting confirmation" in container.whatsapp.sent[-1]["body"]
    drain_welcomes(container, datetime.now().date(), lambda: None, lambda *a: False)
    assert main._checkout_payment(container, "9199") is not None
    monkeypatch.setattr(container.delivery_service, "send_welcome", lambda *a: WhatsAppResult(ok=False, error="template unavailable"))
    drain_welcomes(container, datetime.now().date(), lambda: None, lambda *a: True)
    assert container.welcomes.find(p.reference_id)["publication_verified"] == "true"
    assert main._checkout_payment(container, "9199") is None
    main._send_menu(container, "9199")
    assert "CTA_RENEW" in container.whatsapp.sent[-1]["rows"]
