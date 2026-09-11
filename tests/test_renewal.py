"""Section 30: renewal reminder selection, idempotency, retry, recording."""
from __future__ import annotations

from datetime import date, timedelta

from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from application.delivery_service import DeliveryService
from application.renewal_reminder_service import RenewalReminderService
from tests.conftest import FakeWhatsApp

TODAY = date(2026, 8, 17)


def _add(repos, mobile, days_to_expiry, status=SubscriberStatus.ACTIVE, opt_in=True):
    end = TODAY + timedelta(days=days_to_expiry)
    repos["subscribers"].append(Subscriber(
        mobile=mobile, plan="monthly", status=status,
        start_date=date(2026, 1, 1), end_date=end, opt_in=opt_in,
        subscription_id=f"sub-{mobile}",
    ))


def _service(repos, wa=None):
    return RenewalReminderService(
        repos["subscribers"], repos["renewals"], wa or FakeWhatsApp(),
        reminder_days=[3, 2, 1], template_name="daily_darshan_delivery_update",
        template_lang="en", max_retries=3, retry_sleep=0,
        sentlog=repos["sentlog"],
    )


def test_subscriber_exactly_3_days_selected(repos):
    _add(repos, "9199", 3)
    due = _service(repos).find_due_subscribers(TODAY)
    assert [s.mobile for s, _ in due] == ["9199"]


def test_subscriber_exactly_1_day_selected(repos):
    _add(repos, "9199", 1)
    due = _service(repos).find_due_subscribers(TODAY)
    assert [d for _, d in due] == [1]


def test_subscriber_exactly_2_days_selected(repos):
    _add(repos, "9199", 2)
    due = _service(repos).find_due_subscribers(TODAY)
    assert [d for _, d in due] == [2]


def test_successful_2_day_reminder_is_recorded_idempotently(repos):
    _add(repos, "9199", 2)
    service = _service(repos)

    first = service.run(TODAY)
    second = service.run(TODAY)

    assert first.sent == 1
    assert second.sent == 0 and second.skipped == 1
    assert repos["renewals"].already_sent(
        "9199", "2_DAY", date(2026, 8, 19)
    ) is True
    assert repos["sentlog"].was_sent(TODAY, "9199") is True


def test_subscriber_4_days_not_selected(repos):
    _add(repos, "9199", 4)
    assert _service(repos).find_due_subscribers(TODAY) == []


def test_expired_subscriber_not_selected(repos):
    _add(repos, "9199", -1)
    assert _service(repos).find_due_subscribers(TODAY) == []


def test_cancelled_subscriber_not_selected(repos):
    _add(repos, "9199", 3, status=SubscriberStatus.CANCELLED)
    assert _service(repos).find_due_subscribers(TODAY) == []


def test_opted_out_subscriber_not_selected(repos):
    _add(repos, "9199", 3, opt_in=False)
    assert _service(repos).find_due_subscribers(TODAY) == []


def test_duplicate_3_day_reminder_prevented(repos):
    _add(repos, "9199", 3)
    svc = _service(repos)
    r1 = svc.run(TODAY)
    r2 = svc.run(TODAY)
    assert r1.sent == 1
    assert r2.sent == 0 and r2.skipped == 1


def test_duplicate_1_day_reminder_prevented(repos):
    _add(repos, "9199", 1)
    svc = _service(repos)
    svc.run(TODAY)
    r2 = svc.run(TODAY)
    assert r2.skipped == 1


def test_renewal_before_final_reminder_suppresses_old(repos):
    # Subscriber expiring 2026-08-20 got 3-day reminder on 2026-08-17.
    _add(repos, "9199", 3)  # end = 2026-08-20
    svc = _service(repos)
    svc.run(TODAY)  # sends 3-day for expiry 2026-08-20

    # Renew: new expiry moves out to 2026-09-19. On the old 1-day date (08-19),
    # the subscriber is no longer within [3,2,1] days of the NEW expiry.
    sub = repos["subscribers"].find("9199")
    sub.renew(30, on_date=date(2026, 8, 18))  # 08-20 + 30 = 09-19
    repos["subscribers"].update(sub)

    due = svc.find_due_subscribers(date(2026, 8, 19))
    assert due == []  # old pending 1-day reminder no longer applies


def test_failed_send_is_recorded_and_retryable(repos):
    _add(repos, "9199", 3)
    wa = FakeWhatsApp(always_fail=True)
    svc = _service(repos, wa)
    report = svc.run(TODAY)
    assert report.failed == 1
    assert len(wa.sent) == 3  # retried up to max_retries
    # Recorded as FAILED, so a later run can retry (already_sent only matches SENT).
    assert repos["renewals"].already_sent("9199", "3_DAY", date(2026, 8, 20)) is False
    assert repos["sentlog"].was_sent(TODAY, "9199") is False


def test_failed_renewal_does_not_block_same_day_delivery(repos):
    _add(repos, "9199", 2)
    reminder = _service(repos, FakeWhatsApp(always_fail=True)).run(TODAY)
    delivery = DeliveryService(
        repos["subscribers"], repos["sentlog"], FakeWhatsApp(),
        eligibility=type("Eligible", (), {"is_eligible": lambda self, mobile, day: True})(),
        max_retries=1,
    ).deliver(TODAY, image_url="https://vipseva.com/image.jpg")

    assert reminder.failed == 1
    assert delivery.sent == 1


def test_successful_renewal_blocks_delivery_for_same_subscriber_and_day(repos):
    _add(repos, "9199", 2)
    wa = FakeWhatsApp()

    reminder = _service(repos, wa).run(TODAY)
    delivery = DeliveryService(
        repos["subscribers"], repos["sentlog"], wa,
        eligibility=type("Eligible", (), {"is_eligible": lambda self, mobile, day: True})(),
        max_retries=1,
    ).deliver(TODAY, image_url="https://vipseva.com/image.jpg")

    assert reminder.sent == 1
    assert delivery.sent == 0 and delivery.skipped == 1
    assert sum(1 for item in wa.sent if item["ok"]) == 1


def test_successful_delivery_blocks_renewal_for_same_subscriber_and_day(repos):
    _add(repos, "9199", 2)
    wa = FakeWhatsApp()
    delivery = DeliveryService(
        repos["subscribers"], repos["sentlog"], wa,
        eligibility=type("Eligible", (), {"is_eligible": lambda self, mobile, day: True})(),
        max_retries=1,
    ).deliver(TODAY, image_url="https://vipseva.com/image.jpg")

    reminder = _service(repos, wa).run(TODAY)

    assert delivery.sent == 1
    assert reminder.sent == 0 and reminder.skipped == 1
    assert sum(1 for item in wa.sent if item["ok"]) == 1


def test_existing_successful_reminder_backfills_daily_ledger(repos):
    _add(repos, "9199", 2)
    repos["renewals"].append({
        "mobile": "9199",
        "reminder_type": "2_DAY",
        "expiry_date": "2026-08-19",
        "sent_at": "2026-08-17T08:00:00",
        "whatsapp_message_id": "old-message",
        "status": "SENT",
    })
    wa = FakeWhatsApp()

    reminder = _service(repos, wa).run(TODAY)

    assert reminder.sent == 0 and reminder.skipped == 1
    assert wa.sent == []
    assert repos["sentlog"].was_sent(TODAY, "9199") is True


def test_successful_send_is_recorded(repos):
    _add(repos, "9199", 3)
    svc = _service(repos)
    svc.run(TODAY)
    assert repos["renewals"].already_sent("9199", "3_DAY", date(2026, 8, 20)) is True


def test_renewal_uses_approved_utility_template(repos):
    _add(repos, "9199", 3)
    sub = repos["subscribers"].find("9199")
    sub.name = "nitin mishra"
    repos["subscribers"].update(sub)
    wa = FakeWhatsApp()

    _service(repos, wa).run(TODAY)

    assert wa.sent == [{
        "type": "template_params",
        "mobile": "9199",
        "template": "daily_darshan_delivery_update",
        "params": ["Nitin Mishra"],
        "lang": "en",
        "url_button_param": "sub-9199",
        "ok": True,
    }]


def test_renewal_fails_closed_without_template_name(repos):
    _add(repos, "9199", 3)
    wa = FakeWhatsApp()
    svc = RenewalReminderService(
        repos["subscribers"], repos["renewals"], wa,
        reminder_days=[3, 1], max_retries=3,
    )

    report = svc.run(TODAY)

    assert report.failed == 1
    assert wa.sent == []


def test_renewal_fails_closed_without_subscription_id(repos):
    _add(repos, "9299", 2)
    sub = repos["subscribers"].find("9299")
    sub.subscription_id = ""
    repos["subscribers"].update(sub)

    report = _service(repos).run(TODAY)

    assert report.failed == 1
    assert repos["sentlog"].was_sent(TODAY, "9299") is False
