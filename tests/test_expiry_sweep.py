"""Tests for the expiry sweep (ACTIVE past end_date -> EXPIRED)."""
from __future__ import annotations

from datetime import date

from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from application.subscriber_service import SubscriberService

TODAY = date(2026, 8, 19)


def _svc(repos, plans):
    return SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])


def _add(repos, mobile, status, end):
    repos["subscribers"].append(Subscriber(
        mobile=mobile, plan="monthly", status=status,
        start_date=date(2026, 1, 1), end_date=end, opt_in=True,
        subscription_id=f"tok-{mobile}",
    ))


def test_active_past_expiry_becomes_expired(repos, plans):
    _add(repos, "9199", SubscriberStatus.ACTIVE, date(2026, 8, 1))  # past
    expired = _svc(repos, plans).sweep_expired(TODAY)
    assert expired == ["9199"]
    assert repos["subscribers"].find("9199").status == SubscriberStatus.EXPIRED


def test_active_not_yet_expired_untouched(repos, plans):
    _add(repos, "9199", SubscriberStatus.ACTIVE, date(2026, 12, 31))
    assert _svc(repos, plans).sweep_expired(TODAY) == []
    assert repos["subscribers"].find("9199").status == SubscriberStatus.ACTIVE


def test_end_date_equal_today_is_not_expired(repos, plans):
    # is_expired uses end_date < on_date, so end_date == today is still active.
    _add(repos, "9199", SubscriberStatus.ACTIVE, TODAY)
    assert _svc(repos, plans).sweep_expired(TODAY) == []
    assert repos["subscribers"].find("9199").status == SubscriberStatus.ACTIVE


def test_paused_is_not_auto_expired(repos, plans):
    _add(repos, "9199", SubscriberStatus.PAUSED, date(2026, 8, 1))  # past but paused
    assert _svc(repos, plans).sweep_expired(TODAY) == []
    assert repos["subscribers"].find("9199").status == SubscriberStatus.PAUSED


def test_cancelled_is_not_touched(repos, plans):
    _add(repos, "9199", SubscriberStatus.CANCELLED, date(2026, 8, 1))
    assert _svc(repos, plans).sweep_expired(TODAY) == []
    assert repos["subscribers"].find("9199").status == SubscriberStatus.CANCELLED


def test_sweep_is_idempotent(repos, plans):
    _add(repos, "9199", SubscriberStatus.ACTIVE, date(2026, 8, 1))
    svc = _svc(repos, plans)
    assert svc.sweep_expired(TODAY) == ["9199"]
    assert svc.sweep_expired(TODAY) == []  # already EXPIRED, not swept again


def test_sweep_only_affects_expired_ones(repos, plans):
    _add(repos, "9111", SubscriberStatus.ACTIVE, date(2026, 8, 1))   # expired
    _add(repos, "9222", SubscriberStatus.ACTIVE, date(2026, 12, 31)) # valid
    expired = _svc(repos, plans).sweep_expired(TODAY)
    assert expired == ["9111"]
    assert repos["subscribers"].find("9222").status == SubscriberStatus.ACTIVE
