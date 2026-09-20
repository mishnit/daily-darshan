from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import admin
import main
from application.payment_cleanup import release_stale_failed_payments
from config import Container
from domain.enums import PaymentStatus
from scheduler import run_log_cleanup
from tests.conftest import FakeWhatsApp


def _container(tmp_path):
    config = {
        "plans": {
            "monthly": {"amount": 49, "days": 30},
            "yearly": {"amount": 449, "days": 365},
        },
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
        "renewal": {"reminder_days": [3, 2, 1]},
        "delivery": {
            "caption": "D - {date}",
            "max_send_retries": 1,
            "log_retention_days": 30,
            "failed_payment_release_days": 3,
        },
    }
    return Container(config=config, root=str(tmp_path))


def _failed_payment(container, rejected_at):
    payment = container.payment_service.create_payment("9199", "monthly", date(2026, 9, 1))
    payment.status = PaymentStatus.FAILED
    payment.rejected_at = rejected_at
    payment.utr = "123456789012"
    container.payments.update(payment)
    return payment


def test_failed_payment_is_released_after_full_three_day_boundary(tmp_path):
    container = _container(tmp_path)
    rejected = datetime(2026, 9, 17, 23, 59, tzinfo=ZoneInfo("Asia/Kolkata"))
    payment = _failed_payment(container, rejected)

    assert release_stale_failed_payments(container, date(2026, 9, 19), after_days=3) == []
    assert container.payments.find(payment.reference_id).status == PaymentStatus.FAILED

    released = release_stale_failed_payments(container, date(2026, 9, 20), after_days=3)
    stored = container.payments.find(payment.reference_id)
    assert [row.reference_id for row in released] == [payment.reference_id]
    assert stored.status == PaymentStatus.SUPERSEDED
    assert stored.utr == "123456789012"
    assert stored.rejected_at == rejected


def test_legacy_failed_payment_uses_rejection_audit_timestamp(tmp_path):
    container = _container(tmp_path)
    payment = _failed_payment(container, None)
    container.logs._csv.append({
        "timestamp": "2026-09-17T12:00:00+05:30",
        "event": "PAYMENT_REJECTED",
        "mobile": payment.mobile,
        "details": payment.reference_id,
    })

    assert len(release_stale_failed_payments(container, date(2026, 9, 20))) == 1
    assert container.payments.find(payment.reference_id).status == PaymentStatus.SUPERSEDED


def test_cleanup_job_releases_checkout_and_customer_can_choose_plan_again(tmp_path):
    container = _container(tmp_path)
    payment = _failed_payment(
        container, datetime(2026, 9, 17, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    )
    commits = []
    git = SimpleNamespace(commit=lambda paths, message: commits.append((paths, message)))

    assert run_log_cleanup(container, git, date(2026, 9, 20)) == 0
    assert container.payments.find(payment.reference_id).status == PaymentStatus.SUPERSEDED
    assert commits and "payments.csv" in commits[0][0]
    container.whatsapp = FakeWhatsApp()
    main._send_menu(container, payment.mobile)
    assert "CTA_SUBSCRIBE" in container.whatsapp.sent[-1]["rows"]
    assert "CTA_PAYMENT" not in container.whatsapp.sent[-1]["rows"]


def test_admin_rejection_records_explicit_rejection_timestamp(tmp_path):
    container = _container(tmp_path)
    payment = container.payment_service.create_payment("9199", "monthly")
    assert admin.cmd_reject(
        container, SimpleNamespace(reference_id=payment.reference_id, commit=False)
    ) == 0
    assert container.payments.find(payment.reference_id).rejected_at is not None
