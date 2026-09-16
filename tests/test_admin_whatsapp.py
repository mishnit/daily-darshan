"""Admin decisions must be authorized, current and durable before publication."""
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import admin
import main
import scheduler
from application.admin_alert import payment_counts
from application.image_approval import deployment_ready
from adapters.github import LocalGitRepository
from domain.clock import today_ist, INDIA_TZ
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from tests.test_admin import container
from tests.conftest import FakeWhatsApp

ADMIN = "919535507255"
CUSTOMER = "9199"


@pytest.fixture
def review(container, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ADMIN_NUMBERS", ADMIN)
    container.whatsapp = FakeWhatsApp()
    container.subscriber_service.upsert_pending(CUSTOMER, "monthly", "Nitin")
    container.subscriber_service.grant_opt_in(CUSTOMER, "test")
    return container


def confirmed(c):
    p = c.payment_service.create_payment(CUSTOMER, "monthly")
    main._handle_message(c, CUSTOMER, "text", f"UTR {p.reference_id} 123456789012")
    main._handle_message(c, CUSTOMER, "button", c.whatsapp.sent[-1]["buttons"][0])
    return c.payments.find(p.reference_id)


def open_payment(c, p):
    main._handle_message(c, ADMIN, "button", f"ADM_PAY_{p.reference_id}")
    return c.whatsapp.sent[-1]["buttons"]


def test_only_confirmed_utr_visible_to_admin(review):
    c = review
    p = c.payment_service.create_payment(CUSTOMER, "monthly")
    main._handle_message(c, CUSTOMER, "text", f"UTR {p.reference_id} 123456789012")
    main._handle_message(c, ADMIN, "button", "ADM_PAYMENTS_0")
    assert "No confirmed UTRs" in c.whatsapp.sent[-1]["message"]
    assert payment_counts([c.payments.find(p.reference_id).to_row()], today_ist()) == (0, 1)


def test_authorized_approval_applies_once_and_queues_publication(review):
    c = review
    p = confirmed(c)
    approve, _ = open_payment(c, p)
    main._handle_message(c, ADMIN, "button", approve)
    sub = c.subscribers.find(CUSTOMER)
    assert sub.status == SubscriberStatus.ACTIVE
    assert sub.start_date == today_ist(p.utr_confirmed_at)
    assert sub.end_date == sub.start_date + timedelta(days=30)
    assert c.welcomes.find(p.reference_id)["status"] == "QUEUED"
    assert c.pipeline_requests.find(f"payment-{p.reference_id}")
    assert not list(Path(c.root).glob("docs/*/index.html"))
    main._handle_message(c, ADMIN, "button", approve)
    assert c.subscribers.find(CUSTOMER).to_row() == sub.to_row()


def test_customer_cannot_use_admin_buttons(review):
    c = review
    p = confirmed(c)
    approve, _ = open_payment(c, p)
    main._handle_message(c, CUSTOMER, "button", approve)
    assert c.payments.find(p.reference_id).status.value == "PENDING"
    assert not c.pipeline_requests.all()


def test_admin_must_refresh_after_confirmed_utr_correction(review):
    c = review
    p = confirmed(c)
    approve, _ = open_payment(c, p)
    c.payment_service.record_utr(p.reference_id, "999999999999")
    main._handle_message(c, ADMIN, "button", approve)
    assert c.payments.find(p.reference_id).status.value == "PENDING"
    assert "details changed" in c.whatsapp.sent[-1]["message"]


def test_admin_rejection_does_not_activate(review):
    c = review
    p = confirmed(c)
    _, reject = open_payment(c, p)
    main._handle_message(c, ADMIN, "button", reject)
    assert c.payments.find(p.reference_id).status.value == "FAILED"
    assert c.subscribers.find(CUSTOMER).status == SubscriberStatus.PENDING
    assert not c.pipeline_requests.all()


@pytest.mark.parametrize("remaining", [-3, 0, 10])
def test_dates_use_confirmation_and_preserve_remaining_days(review, remaining):
    c = review
    day = date(2026, 9, 16)
    c.subscribers.update(Subscriber(CUSTOMER, "monthly", start_date=date(2026, 8, 1),
        end_date=day + timedelta(days=remaining), status=SubscriberStatus.ACTIVE, opt_in=True))
    p = confirmed(c)
    p.utr_confirmed_at = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc) # Sep 16 IST
    c.payments.update(p)
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False, skip_render=True)
    assert admin.cmd_verify(c, args) == 0
    first = c.subscribers.find(CUSTOMER)
    assert first.start_date == day
    assert first.end_date == day + timedelta(days=max(0, remaining) + 30)
    assert admin.cmd_verify(c, args) == 0
    assert c.subscribers.find(CUSTOMER).to_row() == first.to_row()


def seed_images(c):
    c.config["admin"] = {"require_image_approval": True, "image_preview_base": "https://raw.githubusercontent.com/test/repo/main"}
    for key in ("source_a", "source_b"):
        c.image_reviews.upsert(key, {"id": key, "date": today_ist().isoformat(), "generation": "batch",
            "source": key, "path": f"images/{key}.jpg", "sha256": hashlib.sha256(key.encode()).hexdigest(),
            "status": "PENDING", "approved_by": "", "approved_at": ""})


def test_visual_preview_approval_gates_pipeline(review):
    c = review
    seed_images(c)
    for fn in (scheduler.run_pages, scheduler.run_welcome, scheduler.run_renewal, scheduler.run_delivery):
        with pytest.raises(RuntimeError, match="Awaiting admin"):
            fn(c, None, today_ist())
    main._handle_message(c, ADMIN, "button", "ADM_IMG_source_a")
    assert c.whatsapp.sent[-2]["type"] == "image"
    approve = c.whatsapp.sent[-1]["buttons"][0]
    main._handle_message(c, ADMIN, "button", approve)
    assert c.image_reviews.find("source_a")["status"] == "APPROVED"
    assert c.image_reviews.find("source_b")["status"] == "SUPERSEDED"
    assert c.pipeline_requests.find("image-batch")


def test_image_preview_cannot_be_approved_after_refresh(review):
    c = review
    seed_images(c)
    main._handle_message(c, ADMIN, "button", "ADM_IMG_source_a")
    approve = c.whatsapp.sent[-1]["buttons"][0]
    row = c.image_reviews.find("source_a")
    row["status"] = "SUPERSEDED"
    c.image_reviews.upsert(row["id"], row)
    main._handle_message(c, ADMIN, "button", approve)
    assert not c.pipeline_requests.all()


def test_image_publication_checks_approved_bytes_and_stamp(review):
    from application.image_approval import materialize, require_published
    c = review
    seed_images(c)
    main._handle_message(c, ADMIN, "button", "ADM_IMG_source_a")
    main._handle_message(c, ADMIN, "button", c.whatsapp.sent[-1]["buttons"][0])
    git = LocalGitRepository(c.root)
    git.write_file("images/source_a.jpg", b"wrong", "test")
    with pytest.raises(RuntimeError, match="bytes changed"):
        materialize(c, git, today_ist())
    git.write_file("images/source_a.jpg", b"source_a", "test")
    path = materialize(c, git, today_ist())
    assert git.read_file(path) == b"source_a"
    Path(c.root, "config.json").write_text(json.dumps(c.config))
    assert not deployment_ready(c.root, today_ist())
    row = c.image_reviews.find("source_a")
    git.write_file("docs/image-approval.json", json.dumps({"id": row["id"], "date": row["date"], "sha256": row["sha256"]}).encode(), "test")
    assert deployment_ready(c.root, today_ist())
    c.config["delivery"] = {"page_base_url": "https://vipseva.com"}
    requested = {}
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"id": row["id"], "date": row["date"], "sha256": row["sha256"]}
    def get(url, **kwargs):
        requested.update(url=url, **kwargs)
        return Response()
    patch = pytest.MonkeyPatch()
    patch.setattr("requests.get", get)
    try:
        require_published(c, today_ist())
    finally:
        patch.undo()
    assert requested["url"] == "https://vipseva.com/image-approval.json"
    assert requested["params"] == {"approval": row["id"]}


def test_first_time_value_proposition_uses_welcome_history(review):
    c = review
    main._send_menu(c, CUSTOMER)
    assert "HD image" in c.whatsapp.sent[-1]["body"]
    c.welcomes.upsert("old", {"reference_id": "old", "mobile": CUSTOMER, "status": "DELIVERED"})
    main._send_menu(c, CUSTOMER)
    assert "HD image" not in c.whatsapp.sent[-1]["body"]


def test_bold_payment_example(review):
    c = review
    p = c.payment_service.create_payment(CUSTOMER, "monthly")
    main._send_payment_instructions(c, CUSTOMER, p)
    assert f"*UTR {p.reference_id} 123456789012*" in c.whatsapp.sent[-1]["message"]


def test_failed_admin_commit_restores_activation(review):
    c = review
    p = confirmed(c)
    approve, _ = open_payment(c, p)
    c.config["persistence"] = {"mode": "github_api"}
    def fail(*a, **k):
        raise RuntimeError("commit failed")
    c.repo_sync = SimpleNamespace(enabled=True, pull=lambda **k: None, push=fail, abort=lambda: None)
    payload = {"entry": [{"changes": [{"value": {"messages": [{"id": "approve-event", "from": ADMIN,
        "type": "interactive", "interactive": {"button_reply": {"id": approve}}}]}}]}]}
    with pytest.raises(RuntimeError, match="commit failed"):
        main._process_payload(c, payload)
    assert c.payments.find(p.reference_id).status.value == "PENDING"
    assert c.subscribers.find(CUSTOMER).status == SubscriberStatus.PENDING
    assert not c.pipeline_requests.all()
