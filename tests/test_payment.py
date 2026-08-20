"""Section 20: reference id, UPI intent, UTR validation, payment flow."""
from __future__ import annotations

from datetime import date

import pytest

from domain.enums import PaymentStatus
from domain.payment import Payment, build_reference_id, is_valid_reference_id, is_valid_utr
from application.payment_service import PaymentService, PaymentError


def test_reference_id_format():
    ref = build_reference_id(date(2026, 8, 17), 1)
    assert ref == "DD2608170001"
    assert is_valid_reference_id(ref)


def test_reference_id_sequence_padding():
    assert build_reference_id(date(2026, 8, 17), 42) == "DD2608170042"
    assert build_reference_id(date(2026, 8, 17), 9999) == "DD2608179999"


def test_reference_id_rejects_bad_values():
    assert not is_valid_reference_id("XX2608170001")
    assert not is_valid_reference_id("DD26081701")  # too short
    with pytest.raises(ValueError):
        build_reference_id(date(2026, 8, 17), 10000)


def test_upi_intent_generation():
    p = Payment(reference_id="DD2608170001", mobile="9199", plan="monthly", amount=49)
    intent = p.upi_intent("test@upi", "Test Payee", "INR")
    assert intent.startswith("upi://pay?")
    assert "pa=test%40upi" in intent
    assert "am=49" in intent
    assert "cu=INR" in intent
    assert "tn=DD2608170001" in intent


def test_utr_validation():
    assert is_valid_utr("123456789012")
    assert not is_valid_utr("12345")          # too short
    assert not is_valid_utr("12345678901a")   # non-numeric
    assert not is_valid_utr("")


def test_payment_service_create_and_sequence(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    p1 = svc.create_payment("9199", "monthly", date(2026, 8, 17))
    p2 = svc.create_payment("9200", "monthly", date(2026, 8, 17))
    assert p1.reference_id == "DD2608170001"
    assert p2.reference_id == "DD2608170002"  # sequence increments per day


def test_payment_service_unknown_plan(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    with pytest.raises(PaymentError):
        svc.create_payment("9199", "does-not-exist")


def test_record_utr_keeps_pending_until_admin_verify(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    p = svc.create_payment("9199", "monthly", date(2026, 8, 17))
    svc.record_utr(p.reference_id, "123456789012")
    stored = repos["payments"].find(p.reference_id)
    assert stored.utr == "123456789012"
    assert stored.status == PaymentStatus.PENDING  # UTR is not proof (section 6)


def test_verify_payment_sets_success(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    p = svc.create_payment("9199", "monthly", date(2026, 8, 17))
    svc.verify_payment(p.reference_id)
    stored = repos["payments"].find(p.reference_id)
    assert stored.status == PaymentStatus.SUCCESS
    assert stored.verified_at is not None
