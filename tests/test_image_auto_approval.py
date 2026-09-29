from datetime import date, datetime, timedelta
from pathlib import Path

from domain.clock import INDIA_TZ
from application.image_approval import (
    auto_approve_due,
    auto_approval_wait_seconds,
)
from tests.test_admin import container  # noqa: F401 - shared isolated Container fixture


DAY = date(2026, 9, 29)
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=INDIA_TZ)


def _candidate(c, key, source, size, queued_at):
    width, height = size
    c.image_reviews.upsert(key, {
        "id": key,
        "date": DAY.isoformat(),
        "generation": "generation-1",
        "source": source,
        "path": f"images/{source}.jpg",
        "sha256": key,
        "width": str(width),
        "height": str(height),
        "status": "PENDING",
        "queued_at": queued_at.isoformat(),
        "approved_by": "",
        "approved_at": "",
        "approval_mode": "",
    })


def test_pending_images_wait_for_full_configured_admin_window(container):
    container.config["admin"] = {
        "require_image_approval": True,
        "image_auto_approval_minutes": 30,
    }
    _candidate(container, "small", "small", (800, 800), NOW - timedelta(minutes=29))

    assert auto_approval_wait_seconds(container, DAY, now=NOW) == 60
    assert auto_approve_due(container, DAY, now=NOW) is None
    assert container.image_reviews.find("small")["status"] == "PENDING"


def test_timeout_auto_approves_highest_resolution_and_queues_pipeline(container):
    container.config["admin"] = {
        "require_image_approval": True,
        "image_auto_approval_minutes": 30,
    }
    queued = NOW - timedelta(minutes=30)
    _candidate(container, "small", "small", (800, 1000), queued)
    _candidate(container, "wide", "wide", (1600, 700), queued)
    _candidate(container, "best", "best", (1200, 1200), queued)

    selected = auto_approve_due(container, DAY, now=NOW)

    assert selected["id"] == "best"
    assert selected["status"] == "APPROVED"
    assert selected["approved_by"] == "system:image_auto_approved"
    assert selected["approval_mode"] == "AUTO_TIMEOUT"
    assert container.image_reviews.find("small")["status"] == "SUPERSEDED"
    assert container.image_reviews.find("wide")["status"] == "SUPERSEDED"
    request = container.pipeline_requests.find("image-generation-1")
    assert request and "auto-approved" in request["reason"]


def test_existing_manual_approval_is_never_replaced(container):
    container.config["admin"] = {
        "require_image_approval": True,
        "image_auto_approval_minutes": 30,
    }
    queued = NOW - timedelta(hours=1)
    _candidate(container, "manual", "manual", (800, 800), queued)
    manual = container.image_reviews.find("manual")
    manual.update(status="APPROVED", approved_by="9199", approval_mode="MANUAL")
    container.image_reviews.upsert("manual", manual)
    _candidate(container, "larger", "larger", (2000, 2000), queued)

    assert auto_approve_due(container, DAY, now=NOW) is None
    assert container.image_reviews.find("manual")["status"] == "APPROVED"
    assert container.image_reviews.find("larger")["status"] == "PENDING"


def test_daily_image_workflow_waits_then_runs_auto_approval_gate():
    workflow = Path(".github/workflows/image.yml").read_text(encoding="utf-8")

    assert "approval-wait:" in workflow
    assert "python3 -m application.image_approval --wait-seconds" in workflow
    assert 'sleep "$WAIT_SECONDS"' in workflow
    assert "python -m application.image_approval --auto-approve-due" in workflow
    assert "python -m application.admin_alert image_auto_approved" in workflow

