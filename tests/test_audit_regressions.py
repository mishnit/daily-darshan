"""Failure-injection coverage for the customer-journey audit."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from types import SimpleNamespace
import json
import threading

import pytest
import requests

from tests.test_webhook_durability import app_client, _tap_payload, FakeGitHub
from tests.test_admin import container
from tests.test_delivery import _seed, _delivery
from tests.conftest import FakeWhatsApp
from application.ports.whatsapp import WhatsAppResult
from adapters.repo_sync import RepoSync
from application.delivery_service import DeliveryService
from application.subscriber_service import SubscriberService
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber

TODAY = date(2026, 8, 19)


def text_payload(text, mid="incoming", mobile="9199"):
    return {"entry": [{"changes": [{"value": {"messages": [{
        "id": mid, "from": mobile, "type": "text", "text": {"body": text},
    }]}}]}]}


def test_failed_stop_confirmation_preserves_optout_and_retries_reply_only(app_client):
    main, client = app_client
    c = main.container
    c.subscriber_service.upsert_pending("9199", "monthly")
    c.subscriber_service.grant_opt_in("9199", "test")
    c.whatsapp = FakeWhatsApp(always_fail=True)
    payload = text_payload("STOP")
    assert client.post("/webhook", json=payload).status_code == 503
    assert c.subscribers.find("9199").opt_in is False
    assert c.processed.was_processed("incoming")
    assert c.reply_retries.find("incoming")
    # A newer explicit opt-in must not be revoked by retrying an old STOP reply.
    c.subscriber_service.grant_opt_in("9199", "new-consent")
    c.whatsapp = FakeWhatsApp()
    assert client.post("/webhook", json=payload).status_code == 200
    assert c.subscribers.find("9199").opt_in is True
    assert c.reply_retries.find("incoming") is None


def test_failed_utr_ack_does_not_lose_or_reassign_payment(app_client):
    main, client = app_client
    c = main.container
    payment = c.payment_service.create_payment("9199", "monthly", TODAY)
    c.whatsapp = FakeWhatsApp(always_fail=True)
    payload = text_payload("123456789012")
    assert client.post("/webhook", json=payload).status_code == 503
    assert c.payments.find(payment.reference_id).utr == "123456789012"
    next_payment = c.payment_service.create_payment("9199", "monthly", TODAY)
    c.whatsapp = FakeWhatsApp()
    assert client.post("/webhook", json=payload).status_code == 200
    assert c.payments.find(next_payment.reference_id).utr == ""


@pytest.mark.parametrize("command", ["Hi", "Radhe Radhe", "RENEW", "SUBSCRIBE", "MENU"])
def test_restart_command_is_not_saved_as_name(app_client, command):
    main, client = app_client
    c = main.container
    c.subscriber_service.upsert_pending("9199", "monthly")
    c.subscriber_service.set_awaiting_name("9199", True)
    assert client.post("/webhook", json=text_payload(command)).status_code == 200
    sub = c.subscribers.find("9199")
    assert sub.name == "" and sub.awaiting_name


def test_ack_waits_for_persistence_and_failed_push_is_retryable(app_client, monkeypatch):
    main, client = app_client
    c = main.container
    seen = []
    def fail_push(message, strict=False):
        assert strict
        assert c.subscribers.find("9199") is not None
        seen.append("push")
        raise RuntimeError("offline")
    monkeypatch.setattr(c.repo_sync, "push", fail_push)
    payload = _tap_payload("9199", "PLAN_monthly", "durable", name="Nitin")
    assert client.post("/webhook", json=payload).status_code == 503
    assert seen == ["push"]
    assert not c.processed.was_processed("durable")
    assert c.subscribers.find("9199") is None
    monkeypatch.setattr(c.repo_sync, "push", lambda *a, **k: [])
    assert client.post("/webhook", json=payload).status_code == 200


def test_concurrent_failed_request_does_not_erase_success(app_client, monkeypatch):
    main, _ = app_client
    original = main._handle_message
    inside, release = threading.Event(), threading.Event()
    def handler(c, mobile, *args):
        if mobile == "bad":
            c.subscriber_service.upsert_pending("bad", "monthly")
            inside.set()
            assert release.wait(3)
            raise RuntimeError("failed reply")
        return original(c, mobile, *args)
    monkeypatch.setattr(main, "_handle_message", handler)
    with ThreadPoolExecutor(2) as pool:
        bad = pool.submit(main._process_payload, main.container, text_payload("x", "bad-id", "bad"))
        assert inside.wait(3)
        good = pool.submit(main._process_payload, main.container,
                           _tap_payload("good", "PLAN_monthly", "good-id", name="Nitin"))
        release.set()
        with pytest.raises(RuntimeError):
            bad.result()
        good.result()
    assert main.container.subscribers.find("good") is not None
    assert main.container.subscribers.find("bad") is None


def test_failed_write_is_not_overwritten_by_next_pull(tmp_path):
    path = tmp_path / "state.csv"
    path.write_text("local")
    gh = FakeGitHub({"state.csv": b"remote"}, fail_write=True)
    sync = RepoSync(gh, str(tmp_path), ["state.csv"], enabled=True)
    assert sync.push("save") == []
    sync.pull()
    assert path.read_text() == "local"


def test_callback_before_ledger_is_retained_and_reconciled(app_client):
    main, client = app_client
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "early", "status": "failed", "recipient_id": "9199"},
    ]}}]}]}
    assert client.post("/webhook", json=payload).status_code == 200
    main.container.sentlog.append({"date": TODAY.isoformat(), "mobile": "9199",
                                   "whatsapp_message_id": "early", "status": "SENT"})
    main.container.message_statuses.reconcile(main.container.sentlog)
    assert not main.container.sentlog.was_sent(TODAY, "9199")
    # Positive delivery evidence wins over a late/out-of-order failure.
    main.container.message_statuses.record("early", "delivered")
    main.container.message_statuses.record("early", "failed")
    main.container.message_statuses.reconcile(main.container.sentlog)
    assert main.container.sentlog.was_sent(TODAY, "9199")


def test_repeated_payment_activation_is_idempotent_even_after_commit_failure(container, monkeypatch):
    import admin
    payment = container.payment_service.create_payment("9199", "monthly", TODAY)
    args = SimpleNamespace(reference_id=payment.reference_id, activate=True, renew=False, commit=True)
    monkeypatch.setattr(admin, "_commit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("push failed")))
    with pytest.raises(RuntimeError):
        admin.cmd_verify(container, args)
    first = container.subscribers.find("9199").end_date
    verified_at = container.payments.find(payment.reference_id).verified_at
    args.commit = False
    assert admin.cmd_verify(container, args) == 0
    assert container.subscribers.find("9199").end_date == first
    assert container.payments.find(payment.reference_id).verified_at == verified_at


def test_reservation_must_be_persisted_before_send(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    def fail():
        assert not wa.sent
        raise RuntimeError("push failed")
    repos["sentlog"].persist = fail
    with pytest.raises(RuntimeError):
        _delivery(repos, wa, plans).deliver(TODAY, "https://image")
    assert not wa.sent


def test_crash_after_provider_acceptance_blocks_next_run(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    commits = []
    def persist():
        commits.append(repos["sentlog"].all()[0]["status"])
        if len(commits) == 2:
            raise RuntimeError("runner stopped after send")
    repos["sentlog"].persist = persist
    with pytest.raises(RuntimeError):
        _delivery(repos, wa, plans).deliver(TODAY, "https://image")
    assert commits == ["PENDING", "SENT"]
    # Model a fresh runner seeing only the last successfully committed row.
    repos["sentlog"]._csv.update_where(lambda row: True, {"status": "PENDING"})
    repos["sentlog"].persist = lambda: None
    report = _delivery(repos, wa, plans).deliver(TODAY, "https://image")
    assert report.skipped == 1 and len(wa.sent) == 1


def test_unknown_send_is_not_retried_or_released(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    wa._result = lambda: WhatsAppResult(ok=False, unknown=True, error="timeout")
    service = _delivery(repos, wa, plans)
    assert service.deliver(TODAY, "https://image").failed == 1
    assert service.deliver(TODAY, "https://image").skipped == 1
    assert len(wa.sent) == 1
    assert repos["sentlog"].all()[0]["status"] == "UNKNOWN"


def test_network_timeout_is_an_unknown_outcome():
    from adapters.whatsapp import MetaWhatsAppClient
    class Session:
        def post(self, *args, **kwargs):
            raise requests.Timeout()
    result = MetaWhatsAppClient("token", "phone-id", session=Session()).send_text("9199", "test")
    assert not result.ok and result.unknown


def test_welcome_uses_distinct_template_and_does_not_consume_daily_slot(repos, plans):
    sub = Subscriber("9199", "monthly", status=SubscriberStatus.ACTIVE,
                     start_date=TODAY, end_date=date(2026, 9, 18), opt_in=True,
                     subscription_id="opaque", name="Nitin")
    repos["subscribers"].append(sub)
    wa = FakeWhatsApp()
    eligibility = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    service = DeliveryService(
        repos["subscribers"], repos["sentlog"], wa, eligibility,
        delivery_mode="utility_template", template_name="daily_darshan_delivery_update",
        page_base_url="https://example.com", welcome_template_name="daily_darshan_welcome",
        welcome_template_lang="en",
    )
    welcome = service.send_welcome(sub)
    assert welcome.ok
    assert wa.sent[0]["template"] == "daily_darshan_welcome"
    assert repos["sentlog"].all() == []
    service._eligibility = eligibility
    service.publication_check = lambda *_: True
    report = service.deliver(TODAY)
    assert report.sent == 1
    assert [s["template"] for s in wa.sent] == [
        "daily_darshan_welcome", "daily_darshan_delivery_update"
    ]


def test_backtracking_does_not_save_navigation_as_name(app_client):
    main, client = app_client
    c = main.container
    c.subscriber_service.upsert_pending("9199", "monthly")
    c.subscriber_service.set_awaiting_name("9199", True)
    assert client.post("/webhook", json=text_payload("BACK", "back-1")).status_code == 200
    sub = c.subscribers.find("9199")
    assert sub.name == "" and sub.awaiting_name is False
    # A new navigation action remains possible after going back and does not
    # re-enter name capture. The menu response is the same handler used by the
    # CTA flow.
    assert client.post("/webhook", json=_tap_payload("9199", "CTA_SUBSCRIBE", "menu-2")).status_code == 200
    assert c.subscribers.find("9199").awaiting_name is False


@pytest.mark.parametrize("mode", ["delivery", "renewal"])
def test_missing_publication_blocks_both_message_types(container, mode):
    from domain.subscriber import Subscriber
    from domain.enums import SubscriberStatus
    c = container
    c.subscribers.append(Subscriber("9199", "monthly", status=SubscriberStatus.ACTIVE,
                                   end_date=date(2026, 8, 20), subscription_id="opaque"))
    wa = FakeWhatsApp()
    service = c.delivery_service if mode == "delivery" else c.renewal_service
    service._whatsapp = wa
    service._template_name = "daily_darshan_delivery_update"
    service.publication_check = lambda *a: False
    if mode == "delivery":
        service._mode = "utility_template"
        service._page_base_url = "https://example.com"
        report = service.deliver(TODAY)
    else:
        report = service.run(TODAY)
    assert report.failed == 1 and not wa.sent and not c.sentlog.all()


def test_page_metadata_and_actual_activation_confirmation(container):
    from adapters.published_page import PublishedPageChecker
    from domain.subscriber import Subscriber
    from domain.enums import SubscriberStatus
    sub = Subscriber("9199", "monthly", status=SubscriberStatus.ACTIVE,
                     subscription_id="opaque", end_date=date(2026, 8, 20))
    page = container.page_renderer.render_html(sub, TODAY, delivered=False)
    assert "Your Daily Darshan subscription is active. Welcome!" in page
    session = SimpleNamespace(get=lambda *a, **k: SimpleNamespace(status_code=200, text=page))
    check = PublishedPageChecker("https://example.com", session)
    assert check(sub, TODAY)
    assert not check(sub, date(2026, 8, 20))
    sub.subscription_id = "different"
    assert not check(sub, TODAY)
