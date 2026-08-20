"""Section 30: renewal reminder selection, idempotency, retry, recording."""
from __future__ import annotations

from datetime import date, timedelta

from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from application.renewal_reminder_service import RenewalReminderService
from tests.conftest import FakeWhatsApp

TODAY = date(2026, 8, 17)


def _add(repos, mobile, days_to_expiry, status=SubscriberStatus.ACTIVE, opt_in=True):
    end = TODAY + timedelta(days=days_to_expiry)
    repos["subscribers"].append(Subscriber(
        mobile=mobile, plan="monthly", status=status,
        start_date=date(2026, 1, 1), end_date=end, opt_in=opt_in,
    ))


def _service(repos, wa=None):
    return RenewalReminderService(
        repos["subscribers"], repos["renewals"], wa or FakeWhatsApp(),
        reminder_days=[3, 1], max_retries=3, retry_sleep=0,
    )


def test_subscriber_exactly_3_days_selected(repos):
    _add(repos, "9199", 3)
    due = _service(repos).find_due_subscribers(TODAY)
    assert [s.mobile for s, _ in due] == ["9199"]


def test_subscriber_exactly_1_day_selected(repos):
    _add(repos, "9199", 1)
    due = _service(repos).find_due_subscribers(TODAY)
    assert [d for _, d in due] == [1]


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
    # the subscriber is no longer within [3,1] days of the NEW expiry.
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


def test_successful_send_is_recorded(repos):
    _add(repos, "9199", 3)
    svc = _service(repos)
    svc.run(TODAY)
    assert repos["renewals"].already_sent("9199", "3_DAY", date(2026, 8, 20)) is True
