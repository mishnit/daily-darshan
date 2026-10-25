"""The 13:00 IST watchdog alerts on a complete delivery outage, not send acceptance."""
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from application import admin_alert
from domain.clock import INDIA_TZ
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from tests.test_admin import container  # noqa: F401 - isolated Container fixture


DAY = date(2026, 10, 25)


def active(container, mobile="9199", *, opted_in=True):
    container.subscribers.append(Subscriber(
        mobile=mobile, plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=DAY - timedelta(days=1), end_date=DAY + timedelta(days=5),
        opt_in=opted_in, subscription_id=f"sub-{mobile}",
    ))


def contact(container, *, status, on_date=DAY, mobile="9199"):
    container.sentlog.append({
        "date": on_date.isoformat(), "mobile": mobile, "image": "page",
        "whatsapp_message_id": "wamid.test", "status": status,
    })


def at(hour, minute=0):
    return datetime(2026, 10, 25, hour, minute, tzinfo=INDIA_TZ)


def test_alert_only_after_one_pm_for_eligible_active_subscriber(container):
    active(container)
    assert admin_alert.delivery_missing_status(container, at(12, 59)) is None
    assert "1 eligible subscriber" in admin_alert.delivery_missing_status(container, at(13))


def test_no_alert_without_eligible_active_subscribers(container):
    active(container, opted_in=False)
    assert admin_alert.delivery_missing_status(container, at(13)) is None


def test_pending_sent_and_old_day_do_not_count_as_confirmed_delivery(container):
    active(container)
    contact(container, status="READ", on_date=DAY - timedelta(days=1))
    contact(container, status="SENT")
    assert admin_alert.delivery_missing_status(container, at(13)) is not None


def test_delivered_or_read_for_eligible_user_suppresses_zero_delivery_alert(container):
    active(container)
    active(container, mobile="9188")
    contact(container, status="DELIVERED")
    assert admin_alert.delivery_confirmation_counts(
        container.subscribers.all(), container.sentlog.all(), DAY,
    ) == (2, 1)
    assert admin_alert.delivery_missing_status(container, at(13)) is None


def test_watchdog_sends_existing_approved_ops_template(container, monkeypatch):
    calls = []
    active(container)
    monkeypatch.setattr("config.Container", lambda: container)
    monkeypatch.setattr(admin_alert, "admin_numbers", lambda: {"919535507255"})
    monkeypatch.setattr(admin_alert, "delivery_missing_status", lambda *_: "No confirmed delivery")
    monkeypatch.setattr(admin_alert, "MetaWhatsAppClient", lambda: SimpleNamespace(
        send_template_params=lambda *args, **kwargs: calls.append((args, kwargs))
        or SimpleNamespace(ok=True),
    ))
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    assert admin_alert.main(["delivery_missing"]) == 0
    assert calls == [(('919535507255', 'daily_darshan_ops_alert',
                       ['Daily delivery', 'No confirmed delivery']),
                      {'lang': 'en', 'url_button_param': '12345'})]


def test_watchdog_workflow_runs_at_one_pm_ist_and_reads_main():
    workflow = Path(".github/workflows/delivery-deadline-alert.yml").read_text()
    assert 'cron: "30 7 * * *"' in workflow
    assert "ref: main" in workflow
    assert "python -m application.admin_alert delivery_missing" in workflow
