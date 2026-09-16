"""Unconfirmed customer input must never become payment review evidence."""
import pytest

import main
from tests.test_admin import container
from tests.conftest import FakeWhatsApp


@pytest.fixture
def checkout(container):
    container.whatsapp = FakeWhatsApp()
    payment = container.payment_service.create_payment("9199", "monthly")
    return container, payment


def draft(c, reference, utr="123456789012"):
    main._handle_message(c, "9199", "text", f"UTR {reference} {utr}")
    return c.whatsapp.sent[-1]["buttons"]


def test_draft_does_not_change_payment_or_subscription(checkout):
    c, p = checkout
    before = p.to_row()
    subscribers = [s.to_row() for s in c.subscribers.all()]
    buttons = draft(c, p.reference_id)
    assert c.payments.find(p.reference_id).to_row() == before
    assert [s.to_row() for s in c.subscribers.all()] == subscribers
    assert "not been submitted" in c.whatsapp.sent[-1]["body"]
    main._handle_message(c, "9199", "button", buttons[0])
    assert c.payments.find(p.reference_id).utr == "123456789012"
    assert "awaiting admin verification" in c.whatsapp.sent[-1]["message"]


def test_change_invalidates_old_confirmation(checkout):
    c, p = checkout
    confirm, change = draft(c, p.reference_id)
    main._handle_message(c, "9199", "button", change)
    main._handle_message(c, "9199", "button", confirm)
    assert not c.payments.find(p.reference_id).utr
    buttons = draft(c, p.reference_id, "999999999999")
    main._handle_message(c, "9199", "button", buttons[0])
    assert c.payments.find(p.reference_id).utr == "999999999999"


def test_new_draft_replaces_old_confirmation(checkout):
    c, p = checkout
    old = draft(c, p.reference_id)[0]
    new = draft(c, p.reference_id, "999999999999")[0]
    main._handle_message(c, "9199", "button", old)
    assert not c.payments.find(p.reference_id).utr
    main._handle_message(c, "9199", "button", new)
    assert c.payments.find(p.reference_id).utr == "999999999999"
    main._handle_message(c, "9199", "button", new)
    assert "no longer current" in c.whatsapp.sent[-1]["message"]


def test_confirmation_is_bound_to_sender(checkout):
    c, p = checkout
    button = draft(c, p.reference_id)[0]
    main._handle_message(c, "9188", "button", button)
    assert not c.payments.find(p.reference_id).utr


def test_draft_reloads_from_csv_and_payment_action_resumes(checkout):
    from repositories.csv_repository import CSVRepository
    c, p = checkout
    button = draft(c, p.reference_id)[0]
    old = c.conversations
    c.conversations = CSVRepository(old.path, old.fieldnames, "mobile")
    main._handle_message(c, "9199", "button", "CTA_PAYMENT")
    assert c.whatsapp.sent[-1]["buttons"][0] == button
    main._handle_message(c, "9199", "button", button)
    assert c.payments.find(p.reference_id).utr == "123456789012"


def test_confirmation_rechecks_other_review(checkout):
    c, p = checkout
    button = draft(c, p.reference_id)[0]
    other = c.payment_service.create_payment("9199", "monthly")
    c.payment_service.record_utr(other.reference_id, "999999999999")
    main._handle_message(c, "9199", "button", button)
    assert not c.payments.find(p.reference_id).utr
    assert "Another payment is under review" in c.whatsapp.sent[-1]["message"]


def test_confirmation_cannot_reopen_verified_payment(checkout):
    c, p = checkout
    button = draft(c, p.reference_id)[0]
    c.payment_service.verify_payment(p.reference_id)
    main._handle_message(c, "9199", "button", button)
    assert c.payments.find(p.reference_id).status.value == "SUCCESS"
    assert not c.payments.find(p.reference_id).utr
