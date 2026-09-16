from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import time

import pytest
import main
import admin
from tests.test_admin import container
from tests.conftest import FakeWhatsApp
from domain.enums import SubscriberStatus, PaymentStatus
from application.welcome_service import drain_welcomes


def prepare(c):
    c.config['plans']['yearly'] = {'amount': 699, 'days': 365}
    c.whatsapp = FakeWhatsApp()
    c.subscriber_service.upsert_pending('9199', 'monthly', 'Nitin')
    c.subscriber_service.grant_opt_in('9199', 'test')


@pytest.mark.parametrize('greeting', ['Hi', 'hello', 'Radhe Radhe', 'MENU'])
@pytest.mark.parametrize('stage', ['name', 'consent', 'utr', 'review'])
def test_greeting_reopens_relevant_step_without_reset(container, greeting, stage):
    prepare(container)
    sub = container.subscribers.find('9199')
    if stage == 'name':
        sub.name = ''
        sub.awaiting_name = True
    if stage in {'name', 'consent'}:
        sub.opt_in = False
    container.subscribers.update(sub)
    p = container.payment_service.create_payment('9199', 'monthly')
    if stage == 'review':
        container.payment_service.record_utr(p.reference_id, '123456789012')
    before_sub = container.subscribers.find('9199').to_row()
    before_payments = [p.to_row() for p in container.payments.all()]
    main._handle_message(container, '9199', 'text', greeting)
    assert container.subscribers.find('9199').to_row() == before_sub
    assert [p.to_row() for p in container.payments.all()] == before_payments
    reply = container.whatsapp.sent[-1]
    if stage == 'name':
        assert 'What name' in reply['body']
    elif stage == 'consent':
        assert reply['buttons'] == ['CTA_OPTIN_AGREE', 'CTA_STOP']
    else:
        assert 'CTA_PAYMENT' in reply['rows']
        if stage == 'utr':
            main._handle_message(container, '9199', 'text', '123456789012')
            main._handle_message(container, '9199', 'button', container.whatsapp.sent[-1]['buttons'][0])
            assert container.payments.find(p.reference_id).utr == '123456789012'


def test_payment_keyword_reopens_menu_while_name_is_requested(container):
    prepare(container)
    sub = container.subscribers.find('9199')
    sub.name = ''
    sub.awaiting_name = True
    container.subscribers.update(sub)
    container.whatsapp = FakeWhatsApp()

    main._handle_message(container, '9199', 'text', 'payment')

    assert container.whatsapp.sent[-1]['type'] == 'list'
    assert container.whatsapp.sent[-1]['rows'] == ['CTA_SUBSCRIBE']


def test_original_payment_reference_required_after_plan_change(container):
    prepare(container)
    old = container.payment_service.create_payment('9199', 'monthly')
    main._handle_message(container, '9199', 'button', 'PLAN_yearly')
    new = main._latest_pending_payment(container, '9199')
    main._handle_message(container, '9199', 'text', '123456789012')
    assert not any(p.utr for p in container.payments.all())
    main._handle_message(container, '9199', 'text', f'UTR {old.reference_id} 123456789012')
    main._handle_message(container, '9199', 'button', container.whatsapp.sent[-1]['buttons'][0])
    assert container.payments.find(old.reference_id).utr == '123456789012'
    assert container.payments.find(old.reference_id).plan == 'monthly'
    assert container.payments.find(new.reference_id).status == PaymentStatus.SUPERSEDED


def test_reference_from_another_customer_is_rejected(container):
    prepare(container)
    p = container.payment_service.create_payment('9188', 'monthly')
    main._handle_message(container, '9199', 'text', f'UTR {p.reference_id} 123456789012')
    assert not container.payments.find(p.reference_id).utr


def test_cancelled_subscriber_reactivates_once_without_restoring_consent(container):
    prepare(container)
    container.subscriber_service.activate('9199')
    container.subscriber_service.cancel('9199')
    container.subscriber_service.revoke_opt_in('9199')
    p = container.payment_service.create_payment('9199', 'yearly')
    args = SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False)
    assert admin.cmd_verify(container, args) == 0
    first = container.subscribers.find('9199').to_row()
    assert admin.cmd_verify(container, args) == 0
    assert container.subscribers.find('9199').to_row() == first
    assert first['status'] == 'ACTIVE' and first['opt_in'] == 'false'


@pytest.mark.parametrize('review', [False, True])
def test_resume_consent_independent_of_renewal_checkout(container, review):
    prepare(container)
    container.subscriber_service.activate('9199')
    p = container.payment_service.create_payment('9199', 'yearly')
    if review:
        container.payment_service.record_utr(p.reference_id, '123456789012')
    before = [p.to_row() for p in container.payments.all()]
    container.subscriber_service.revoke_opt_in('9199')
    main._send_menu(container, '9199')
    assert 'CTA_RESUME_MESSAGES' in container.whatsapp.sent[-1]['rows']
    main._handle_message(container, '9199', 'button', 'CTA_RESUME_MESSAGES')
    main._handle_message(container, '9199', 'button', 'CTA_RESUME_AGREE')
    assert container.subscribers.find('9199').opt_in
    assert [p.to_row() for p in container.payments.all()] == before


@pytest.mark.parametrize('status', ['EXPIRED', 'CANCELLED', 'PAUSED'])
def test_obsolete_welcome_never_sent(container, monkeypatch, status):
    prepare(container)
    p = container.payment_service.create_payment('9199', 'monthly')
    admin.cmd_verify(container, SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False))
    sub = container.subscribers.find('9199')
    sub.status = SubscriberStatus(status)
    container.subscribers.update(sub)
    monkeypatch.setattr(container.delivery_service, 'send_welcome', lambda *a: pytest.fail('obsolete welcome'))
    drain_welcomes(container, datetime.now().date(), lambda: None, lambda *a: True)
    assert container.welcomes.find(p.reference_id)['status'] == 'CANCELLED'
    assert main._checkout_payment(container, '9199') is None
    main._send_menu(container, '9199')
    assert 'CTA_RENEW' in container.whatsapp.sent[-1]['rows']


def test_unswept_expired_welcome_is_not_sent_or_blocking_renewal(container, monkeypatch):
    prepare(container)
    p = container.payment_service.create_payment('9199', 'monthly')
    admin.cmd_verify(container, SimpleNamespace(reference_id=p.reference_id, activate=True, renew=False, commit=False))
    sub = container.subscribers.find('9199')
    sub.end_date = datetime.now(ZoneInfo('Asia/Kolkata')).date() - timedelta(days=1)
    container.subscribers.update(sub)
    monkeypatch.setattr(container.delivery_service, 'send_welcome', lambda *a: pytest.fail('expired welcome'))
    drain_welcomes(container, datetime.now(ZoneInfo('Asia/Kolkata')).date(), lambda: None, lambda *a: True)
    assert container.welcomes.find(p.reference_id)['status'] == 'CANCELLED'
    assert main._checkout_payment(container, '9199') is None


@pytest.mark.parametrize('text', ['12345678901', 'UTR: 123', 'How do I pay?', 'Help'])
def test_invalid_name_does_not_advance_signup(container, text):
    container.whatsapp = FakeWhatsApp()
    main._handle_message(container, '9199', 'button', 'PLAN_monthly')
    main._handle_message(container, '9199', 'text', text)
    sub = container.subscribers.find('9199')
    assert sub.awaiting_name and not sub.name
    assert not container.payments.all()


def test_rejected_payment_review_and_explicit_admin_resolution(container):
    prepare(container)
    p = container.payment_service.create_payment('9199', 'monthly')
    admin.cmd_reject(container, SimpleNamespace(reference_id=p.reference_id, commit=False))
    main._handle_message(container, '9199', 'button', 'CTA_PAYMENT_REVIEW')
    assert 'recorded' in container.whatsapp.sent[-1]['message']
    args = SimpleNamespace(reference_id=p.reference_id, no_payment_confirmed=False, commit=False)
    assert admin.cmd_reopen_payment(container, args) == 1
    args.no_payment_confirmed = True
    assert admin.cmd_reopen_payment(container, args) == 0
    assert container.payments.find(p.reference_id).status == PaymentStatus.SUPERSEDED


def test_publication_change_invalidates_queued_status(container):
    from application.reply_outbox import QueuedReplies, drain_replies
    prepare(container)
    container.conversations.upsert('9199', {'mobile': '9199', 'version': '1', 'last_inbound': str(time.time())})
    container.welcomes.upsert('ref', {'reference_id': 'ref', 'mobile': '9199', 'status': 'QUEUED'})
    QueuedReplies(container.reply_outbox, container).send_text('9199', 'Page being prepared')
    container.welcomes.upsert('ref', {'reference_id': 'ref', 'mobile': '9199', 'status': 'QUEUED', 'publication_verified': 'true'})
    drain_replies(container.reply_outbox, container.whatsapp, lambda: None, container)
    assert container.reply_outbox.all()[0]['status'] == 'CANCELLED'
    assert not container.whatsapp.sent


@pytest.mark.parametrize('image_date,expected', [('2026-09-12', False), ('2026-09-13', True)])
def test_daily_publication_requires_actual_current_image(container, image_date, expected):
    from adapters.published_page import PublishedPageChecker
    prepare(container)
    container.subscriber_service.activate('9199')
    sub = container.subscribers.find('9199')
    day = datetime(2026, 9, 13).date()
    html = container.page_renderer.render_html(sub, day, delivered=True, image_name=f'{image_date}.jpg')
    session = SimpleNamespace(get=lambda *a, **kw: SimpleNamespace(status_code=200, text=html))
    assert PublishedPageChecker('https://example.com', session, require_current_image=True)(sub, day) == expected
    assert PublishedPageChecker('https://example.com', session)(sub, day)
