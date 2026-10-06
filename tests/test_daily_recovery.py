from datetime import date, datetime, timedelta

from application.daily_recovery import recovery_action
from domain.clock import INDIA_TZ
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from tests.test_admin import container  # noqa: F401 - shared isolated Container fixture


DAY = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 13, 0, tzinfo=INDIA_TZ)


def _eligible(container):
    container.subscribers.append(Subscriber(
        mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        opt_in=True, subscription_id="sub-1",
    ))
    container.config["admin"] = {
        "require_image_approval": True,
        "image_auto_approval_minutes": 30,
    }


def _candidate(container, status="PENDING", queued_at=None):
    container.image_reviews.upsert("candidate", {
        "id": "candidate", "date": DAY.isoformat(), "generation": "generation",
        "source": "salangpur", "path": "images/candidate.jpg", "sha256": "hash",
        "width": "960", "height": "1280", "status": status,
        "queued_at": (queued_at or NOW).isoformat(), "approved_by": "",
        "approved_at": "", "approval_mode": "",
    })


def test_recovery_is_complete_when_every_eligible_subscriber_has_daily_slot(container):
    _eligible(container)
    container.sentlog.append({
        "date": DAY.isoformat(), "mobile": "9199", "image": "delivery",
        "whatsapp_message_id": "wamid.1", "status": "READ",
    })

    assert recovery_action(container, DAY)[0] == "complete"


def test_recovery_recollects_only_when_no_candidate_exists(container):
    _eligible(container)

    assert recovery_action(container, DAY)[0] == "image"


def test_recovery_waits_for_manual_review_window(container, monkeypatch):
    _eligible(container)
    _candidate(container, queued_at=NOW)
    monkeypatch.setattr(
        "application.daily_recovery.auto_approval_wait_seconds", lambda *_args: 120,
    )

    assert recovery_action(container, DAY)[0] == "wait"


def test_recovery_auto_approves_overdue_pending_candidate(container, monkeypatch):
    _eligible(container)
    _candidate(container, queued_at=NOW - timedelta(hours=1))
    monkeypatch.setattr(
        "application.daily_recovery.auto_approval_wait_seconds", lambda *_args: 0,
    )

    assert recovery_action(container, DAY)[0] == "auto_approve"


def test_recovery_regenerates_pages_after_approval(container, monkeypatch):
    _eligible(container)
    _candidate(container, status="APPROVED")
    monkeypatch.setattr("application.daily_recovery.deployment_ready", lambda *_args: False)

    assert recovery_action(container, DAY)[0] == "pages"


def test_recovery_retries_delivery_after_current_pages_are_rendered(container, monkeypatch):
    _eligible(container)
    _candidate(container, status="APPROVED")
    monkeypatch.setattr("application.daily_recovery.deployment_ready", lambda *_args: True)

    assert recovery_action(container, DAY)[0] == "delivery"


def test_pending_or_unknown_contact_is_not_blindly_retried(container):
    _eligible(container)
    container.sentlog.append({
        "date": DAY.isoformat(), "mobile": "9199", "image": "delivery",
        "whatsapp_message_id": "reservation:1", "status": "UNKNOWN",
    })

    assert recovery_action(container, DAY)[0] == "complete"
