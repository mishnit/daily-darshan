"""Tests for #7 (name sanitization), #4 (supersede pending), #9 (opt-in/opt-out)."""
from __future__ import annotations

from datetime import date

from domain.enums import PaymentStatus, SubscriberStatus
from domain.subscriber import Subscriber, sanitize_display_name
from application.payment_service import PaymentService
from application.subscriber_service import SubscriberService


# ------------------------------- #7 sanitize ------------------------------- #

def test_sanitize_strips_newlines_tabs_and_control_chars():
    assert sanitize_display_name("Anita\nSharma") == "Anita Sharma"
    assert sanitize_display_name("A\tB\r\nC") == "A B C"
    assert sanitize_display_name("Anita\x00\x1fSharma") == "Anita Sharma"


def test_sanitize_collapses_whitespace_and_caps_length():
    assert sanitize_display_name("Anita     Sharma") == "Anita Sharma"
    long = "x" * 200
    assert len(sanitize_display_name(long)) == 60


def test_sanitize_falls_back_when_empty_or_only_control():
    assert sanitize_display_name("") == "devotee"
    assert sanitize_display_name("   ") == "devotee"
    assert sanitize_display_name("\n\t\x00") == "devotee"
    assert sanitize_display_name("", fallback="") == ""


def test_delivery_template_param_is_sanitized(repos, plans):
    from application.delivery_service import DeliveryService
    from domain.payment import Payment
    from tests.conftest import FakeWhatsApp
    repos["subscribers"].append(Subscriber(
        mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="tok-9199", name="Bad\nName\twith\x00ctrl",
    ))
    repos["payments"].append(Payment(
        reference_id="DD2601019199", mobile="9199", plan="monthly", amount=49,
        status=PaymentStatus.SUCCESS))
    elig = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    wa = FakeWhatsApp()
    svc = DeliveryService(repos["subscribers"], repos["sentlog"], wa, elig,
                          delivery_mode="utility_template", template_name="t",
                          page_base_url="https://d/x", max_retries=1)
    svc.deliver(date(2026, 8, 19))
    name_param = wa.sent[0]["params"][0]
    assert "\n" not in name_param and "\t" not in name_param and "\x00" not in name_param
    assert name_param == "Bad Name with ctrl"


# ------------------------------- #4 supersede ------------------------------- #

def test_new_payment_supersedes_prior_pending(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    p1 = svc.create_payment("9199", "monthly", date(2026, 8, 19))
    p2 = svc.create_payment("9199", "monthly", date(2026, 8, 19))
    stored1 = repos["payments"].find(p1.reference_id)
    stored2 = repos["payments"].find(p2.reference_id)
    assert stored1.status == PaymentStatus.SUPERSEDED   # older pending superseded
    assert stored2.status == PaymentStatus.PENDING       # latest stays actionable


def test_supersede_only_affects_same_mobile_pending(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    other = svc.create_payment("9200", "monthly", date(2026, 8, 19))
    svc.create_payment("9199", "monthly", date(2026, 8, 19))
    svc.create_payment("9199", "monthly", date(2026, 8, 19))
    # The other mobile's pending payment is untouched.
    assert repos["payments"].find(other.reference_id).status == PaymentStatus.PENDING
    # 9199 has exactly one PENDING left.
    pend = [p for p in repos["payments"].all()
            if p.mobile == "9199" and p.status == PaymentStatus.PENDING]
    assert len(pend) == 1


# ------------------------------- #9 opt-in/out ------------------------------- #

def test_upsert_pending_new_subscriber_starts_opted_out(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    sub = svc.upsert_pending("9199", "monthly", "Ravi")
    assert sub.opt_in is False  # consent not implied by starting signup


def test_grant_opt_in_records_timestamp_and_source(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    svc.upsert_pending("9199", "monthly", "Ravi")
    sub = svc.grant_opt_in("9199", "whatsapp_cta")
    assert sub.opt_in is True
    assert sub.opt_in_at  # ISO timestamp recorded (consent proof)
    assert sub.opt_in_source == "whatsapp_cta"


def test_revoke_opt_in_sets_false_with_timestamp(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    svc.upsert_pending("9199", "monthly", "Ravi")
    svc.grant_opt_in("9199", "whatsapp_cta")
    sub = svc.revoke_opt_in("9199")
    assert sub.opt_in is False
    assert sub.opt_in_at and sub.opt_in_source == "opt_out"


def test_revoke_opt_in_unknown_mobile_returns_none(repos, plans):
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans)
    assert svc.revoke_opt_in("nope") is None


def test_opted_out_subscriber_not_eligible(repos, plans):
    from domain.payment import Payment
    repos["subscribers"].append(Subscriber(
        mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
        subscription_id="tok", name="Ravi",
    ))
    repos["payments"].append(Payment(reference_id="DD1", mobile="9199", plan="monthly",
                                     amount=49, status=PaymentStatus.SUCCESS))
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is True
    svc.revoke_opt_in("9199")  # STOP
    assert svc.is_eligible("9199", date(2026, 8, 19)) is False
