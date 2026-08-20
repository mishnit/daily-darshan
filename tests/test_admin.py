"""Tests for the admin CLI (verify + activate in one step)."""
from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

import admin
from config import Container
from domain.enums import PaymentStatus, SubscriberStatus


@pytest.fixture
def container(tmp_path):
    """A Container wired to isolated CSV files in tmp_path (no git, no network)."""
    config = {
        "plans": {"monthly": {"amount": 49, "days": 30}},
        "upi": {"payee_vpa": "test@upi", "payee_name": "Test", "currency": "INR"},
        "image_sources": [],
        "image_source_config": {},
        "image_validation": {"allowed_formats": ["JPEG"], "min_width": 0, "min_height": 0},
        "paths": {
            "images_dir": "images",
            "subscribers_csv": "subscribers.csv",
            "payments_csv": "payments.csv",
            "sentlog_csv": "sentlog.csv",
            "renewals_csv": "renewals.csv",
            "logs_csv": "logs.csv",
        },
        "renewal": {"reminder_days": [3, 1]},
        "delivery": {"caption": "D - {date}", "max_send_retries": 1},
    }
    return Container(config=config, root=str(tmp_path))


def test_verify_only_sets_success(container):
    p = container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    args = SimpleNamespace(reference_id=p.reference_id, activate=False, renew=False, commit=False)
    rc = admin.cmd_verify(container, args)
    assert rc == 0
    stored = container.payments.find(p.reference_id)
    assert stored.status == PaymentStatus.SUCCESS
    # No subscriber should have been activated.
    assert container.subscribers.find("919999999999") is None


def test_verify_with_activate_creates_active_subscriber(container):
    p = container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False)
    rc = admin.cmd_verify(container, args)
    assert rc == 0

    stored = container.payments.find(p.reference_id)
    assert stored.status == PaymentStatus.SUCCESS

    sub = container.subscribers.find("919999999999")
    assert sub is not None
    assert sub.status == SubscriberStatus.ACTIVE
    assert sub.start_date is not None and sub.end_date is not None
    assert (sub.end_date - sub.start_date).days == 30  # monthly plan length


def test_verify_activate_makes_subscriber_eligible(container):
    p = container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    # Real flow captures opt-in before payment; reflect that here.
    container.subscriber_service.upsert_pending("919999999999", "monthly")
    container.subscriber_service.grant_opt_in("919999999999", "test")
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False)
    admin.cmd_verify(container, args)
    # Eligible on the activation start date (successful payment + ACTIVE + opt-in).
    sub = container.subscribers.find("919999999999")
    assert container.subscriber_service.is_eligible("919999999999", sub.start_date) is True


def test_verify_unknown_reference_returns_error(container, capsys):
    args = SimpleNamespace(reference_id="DD9999999999", activate=True, renew=False, commit=False)
    rc = admin.cmd_verify(container, args)
    assert rc == 1
    assert "not found" in capsys.readouterr().err.lower()


def test_verify_activate_renews_existing_active_subscriber(container):
    from datetime import timedelta
    from domain.subscriber import Subscriber
    # Existing ACTIVE subscriber expiring 2026-09-30.
    container.subscribers.append(Subscriber(
        mobile="919999999999", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 9, 30), opt_in=True,
        subscription_id="tok-x", name="Ravi",
    ))
    p = container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False)
    rc = admin.cmd_verify(container, args)
    assert rc == 0
    sub = container.subscribers.find("919999999999")
    # Renewal extends from existing expiry (2026-09-30 + 30d), not from today.
    assert sub.status == SubscriberStatus.ACTIVE
    assert sub.end_date == date(2026, 9, 30) + timedelta(days=30)


def test_reject_sets_failed(container):
    p = container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    args = SimpleNamespace(reference_id=p.reference_id, commit=False)
    rc = admin.cmd_reject(container, args)
    assert rc == 0
    assert container.payments.find(p.reference_id).status == PaymentStatus.FAILED


def test_list_pending_runs(container, capsys):
    container.payment_service.create_payment("919999999999", "monthly", date(2026, 8, 19))
    rc = admin.cmd_list_pending(container, SimpleNamespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "919999999999" in out


def test_parser_verify_flags():
    args = admin.build_parser().parse_args(["verify", "DD2608190001", "--activate", "--commit"])
    assert args.reference_id == "DD2608190001"
    assert args.activate is True
    assert args.commit is True
