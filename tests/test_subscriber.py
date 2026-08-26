"""Section 20: subscriber state transitions and eligibility."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain.enums import InvalidStateTransition, PaymentStatus, SubscriberStatus
from domain.payment import Payment
from domain.subscriber import Subscriber
from application.subscriber_service import SubscriberService


def _sub(status=SubscriberStatus.PENDING, **kw):
    return Subscriber(mobile="9199", plan="monthly", status=status, **kw)


def test_pending_to_active():
    s = _sub()
    s.activate(30, date(2026, 8, 1))
    assert s.status == SubscriberStatus.ACTIVE
    assert s.end_date == date(2026, 8, 31)


def test_active_pause_resume():
    s = _sub(SubscriberStatus.ACTIVE)
    s.pause()
    assert s.status == SubscriberStatus.PAUSED
    s.resume()
    assert s.status == SubscriberStatus.ACTIVE


def test_cancel_from_any_active_state():
    s = _sub(SubscriberStatus.ACTIVE)
    s.cancel()
    assert s.status == SubscriberStatus.CANCELLED


def test_invalid_transition_cancelled_is_terminal():
    s = _sub(SubscriberStatus.CANCELLED)
    with pytest.raises(InvalidStateTransition):
        s.resume()


def test_invalid_transition_pending_to_paused():
    s = _sub(SubscriberStatus.PENDING)
    with pytest.raises(InvalidStateTransition):
        s.pause()


def test_renew_extends_from_existing_expiry():
    # Section 29: renewal extends from current expiry, not payment date.
    s = _sub(SubscriberStatus.ACTIVE, start_date=date(2026, 7, 1), end_date=date(2026, 8, 20))
    s.renew(30, on_date=date(2026, 8, 18))
    assert s.end_date == date(2026, 9, 19)  # 2026-08-20 + 30d


def test_renew_from_today_when_expired():
    s = _sub(SubscriberStatus.EXPIRED, end_date=date(2026, 8, 1))
    s.renew(30, on_date=date(2026, 8, 18))
    assert s.end_date == date(2026, 9, 17)  # today + 30d
    assert s.status == SubscriberStatus.ACTIVE


# ------------------------- eligibility ------------------------- #

def _seed_active_with_payment(repos, mobile="9199", end=date(2026, 12, 31)):
    sub = Subscriber(mobile=mobile, plan="monthly", status=SubscriberStatus.ACTIVE,
                     start_date=date(2026, 1, 1), end_date=end, opt_in=True)
    repos["subscribers"].append(sub)
    repos["payments"].append(Payment(reference_id="DD2601010001", mobile=mobile,
                                      plan="monthly", amount=49, status=PaymentStatus.SUCCESS))


def test_eligible_active_optin_with_successful_payment(repos, plans):
    _seed_active_with_payment(repos)
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is True


def test_eligible_without_successful_payment(repos, plans):
    sub = Subscriber(mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
                     start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True)
    repos["subscribers"].append(sub)
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is True


def test_not_eligible_when_expired(repos, plans):
    _seed_active_with_payment(repos, end=date(2026, 8, 1))
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is False


def test_not_eligible_when_already_sent(repos, plans):
    _seed_active_with_payment(repos)
    repos["sentlog"].append({
        "date": "2026-08-19", "mobile": "9199", "image": "2026-08-19.jpg",
        "whatsapp_message_id": "m1", "status": "SENT",
    })
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is False


def test_not_eligible_when_opted_out(repos, plans):
    sub = Subscriber(mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
                     start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=False)
    repos["subscribers"].append(sub)
    repos["payments"].append(Payment(reference_id="DD2601010001", mobile="9199",
                                     plan="monthly", amount=49, status=PaymentStatus.SUCCESS))
    svc = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    assert svc.is_eligible("9199", date(2026, 8, 19)) is False
