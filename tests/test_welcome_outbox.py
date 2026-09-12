from datetime import date
from types import SimpleNamespace
import pytest
import admin
from application.ports.whatsapp import WhatsAppResult
from application.welcome_service import drain_welcomes
from application.reply_outbox import QueuedReplies, drain_replies
from tests.test_admin import container


def activate(c, commit=False):
    c.subscriber_service.upsert_pending("9199", "monthly", "Nitin")
    c.subscriber_service.grant_opt_in("9199", "test")
    p = c.payment_service.create_payment("9199", "monthly")
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=commit)
    return args


def test_activation_queues_once_without_sending(container, monkeypatch):
    monkeypatch.setattr(container.delivery_service, "send_welcome", lambda s: pytest.fail("premature send"))
    args = activate(container)
    assert admin.cmd_verify(container, args) == 0
    assert admin.cmd_verify(container, args) == 0
    assert len(container.welcomes.all()) == 1
    assert container.welcomes.all()[0]["status"] == "QUEUED"


def test_commit_failure_does_not_send(container, monkeypatch):
    args = activate(container, True)
    monkeypatch.setattr(container.delivery_service, "send_welcome", lambda s: pytest.fail("premature send"))
    def fail(*args, **kwargs):
        raise RuntimeError("push failed")
    monkeypatch.setattr(admin, "_commit", fail)
    with pytest.raises(RuntimeError):
        admin.cmd_verify(container, args)


@pytest.mark.parametrize("published,consent,expected", [(False, True, "QUEUED"), (True, False, "CANCELLED")])
def test_welcome_requires_published_page_and_consent(container, monkeypatch, published, consent, expected):
    admin.cmd_verify(container, activate(container))
    if not consent:
        container.subscriber_service.revoke_opt_in("9199")
    monkeypatch.setattr(container.delivery_service, "send_welcome", lambda s: pytest.fail("must not send"))
    drain_welcomes(container, date.today(), lambda: None, lambda *a: published)
    assert container.welcomes.all()[0]["status"] == expected


def test_welcome_reserved_before_send_and_not_repeated(container, monkeypatch):
    admin.cmd_verify(container, activate(container))
    events = []
    def persist():
        events.append(container.welcomes.all()[0]["status"])
    def send(sub):
        assert events[-1] == "PENDING"
        events.append("send")
        return WhatsAppResult(ok=True, message_id="wamid.test")
    monkeypatch.setattr(container.delivery_service, "send_welcome", send)
    for _ in range(2):
        drain_welcomes(container, date.today(), persist, lambda *a: True)
    assert events.count("send") == 1
    container.message_statuses.record("wamid.test", "delivered")
    container.message_statuses.reconcile(container.welcomes)
    assert container.welcomes.all()[0]["status"] == "DELIVERED"


def test_unknown_welcome_is_not_retried(container, monkeypatch):
    admin.cmd_verify(container, activate(container))
    calls = []
    def send(sub):
        calls.append(sub.mobile)
        return WhatsAppResult(ok=False, unknown=True)
    monkeypatch.setattr(container.delivery_service, "send_welcome", send)
    for _ in range(2):
        assert drain_welcomes(container, date.today(), lambda: None, lambda *a: True) == 1
    assert len(calls) == 1


def test_reply_reservation_failure_prevents_network(container):
    QueuedReplies(container.reply_outbox).send_text("9199", "Payment reference")
    client = SimpleNamespace(send_text=lambda *a: pytest.fail("must not send"))
    def fail():
        raise RuntimeError("push failed")
    with pytest.raises(RuntimeError):
        drain_replies(container.reply_outbox, client, fail)


def test_reply_outbox_dedupes_after_success(container):
    QueuedReplies(container.reply_outbox).send_text("9199", "Payment reference")
    calls = []
    def send(*args):
        calls.append(args)
        return WhatsAppResult(ok=True, message_id="wamid.reply")
    for _ in range(2):
        assert not drain_replies(container.reply_outbox, SimpleNamespace(send_text=send), lambda: None)
    assert len(calls) == 1


def test_production_webhook_commit_failure_sends_nothing(container):
    import main
    container.config["persistence"] = {"mode": "github_api"}
    def fail(*args, **kwargs):
        raise RuntimeError("push failed")
    container.repo_sync = SimpleNamespace(enabled=True, in_quiet_window=lambda: False,
        pull=lambda **kw: None, push=fail, abort=lambda: None)
    container.whatsapp = SimpleNamespace(send_buttons=lambda *a: pytest.fail("premature reply"))
    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.incoming", "from": "9199", "type": "text", "text": {"body": "Radhe Radhe"}}
    ]}}]}]}
    with pytest.raises(RuntimeError, match="push failed"):
        main._process_payload(container, payload)
    assert container.reply_outbox.all() == []
