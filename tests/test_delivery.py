"""Section 20: duplicate delivery prevention and retry logic."""
from __future__ import annotations

from datetime import date

from domain.enums import PaymentStatus, SubscriberStatus
from domain.payment import Payment
from domain.subscriber import Subscriber
from application.delivery_service import DeliveryService
from application.subscriber_service import SubscriberService
from tests.conftest import FakeWhatsApp


def _seed(repos, mobile="9199"):
    repos["subscribers"].append(Subscriber(
        mobile=mobile, plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 12, 31), opt_in=True,
    ))
    repos["payments"].append(Payment(
        reference_id="DD2601010001", mobile=mobile, plan="monthly",
        amount=49, status=PaymentStatus.SUCCESS,
    ))


def _delivery(repos, wa, plans):
    elig = SubscriberService(repos["subscribers"], repos["payments"], plans, repos["sentlog"])
    return DeliveryService(repos["subscribers"], repos["sentlog"], wa, elig,
                           max_retries=3, retry_sleep=0)


def test_delivery_sends_to_eligible(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    report = _delivery(repos, wa, plans).deliver(date(2026, 8, 19), "http://img/2026-08-19.jpg")
    assert report.sent == 1
    assert report.failed == 0
    assert repos["sentlog"].was_sent(date(2026, 8, 19), "9199") is True


def test_delivery_idempotent_second_run_skips(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    svc = _delivery(repos, wa, plans)
    svc.deliver(date(2026, 8, 19), "http://img/x.jpg")
    report2 = svc.deliver(date(2026, 8, 19), "http://img/x.jpg")
    assert report2.sent == 0
    assert report2.skipped == 1
    # Only one WhatsApp send happened across both runs.
    assert sum(1 for s in wa.sent if s["ok"]) == 1


def test_delivery_retries_transient_failure_then_succeeds(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp(fail_times=2)  # first two attempts fail, third succeeds
    report = _delivery(repos, wa, plans).deliver(date(2026, 8, 19), "http://img/x.jpg")
    assert report.sent == 1
    assert len(wa.sent) == 3  # 3 attempts for the single subscriber


def test_delivery_records_failure_after_exhausting_retries(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp(always_fail=True)
    report = _delivery(repos, wa, plans).deliver(date(2026, 8, 19), "http://img/x.jpg")
    assert report.failed == 1
    assert report.sent == 0
    # A FAILED row is recorded, but idempotency only skips SENT rows.
    assert repos["sentlog"].was_sent(date(2026, 8, 19), "9199") is False


def test_delivery_continues_when_one_subscriber_fails(repos, plans):
    _seed(repos, "9199")
    _seed(repos, "9200")
    # Fail the very first send only; the retry for 9199 will succeed, 9200 succeeds.
    wa = FakeWhatsApp(fail_times=1)
    report = _delivery(repos, wa, plans).deliver(date(2026, 8, 19), "http://img/x.jpg")
    assert report.sent == 2
    assert report.failed == 0


def test_delivery_uses_media_upload_when_bytes_provided(repos, plans):
    # fix #6: with image_bytes, upload once and send by media id (private-repo safe).
    _seed(repos)
    wa = FakeWhatsApp()
    report = _delivery(repos, wa, plans).deliver(
        date(2026, 8, 19), image_url="http://img/x.jpg", image_bytes=b"jpegbytes"
    )
    assert report.sent == 1
    assert wa.uploads == 1                                  # uploaded once
    assert all(s["type"] == "image_id" for s in wa.sent)   # sent by media id, not url


def test_delivery_falls_back_to_url_when_upload_fails(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp(upload_ok=False)
    report = _delivery(repos, wa, plans).deliver(
        date(2026, 8, 19), image_url="http://img/x.jpg", image_bytes=b"jpegbytes"
    )
    assert report.sent == 1
    assert any(s["type"] == "image" and s["url"] == "http://img/x.jpg" for s in wa.sent)


def test_delivery_aborts_when_no_url_and_no_bytes(repos, plans):
    _seed(repos)
    wa = FakeWhatsApp()
    report = _delivery(repos, wa, plans).deliver(date(2026, 8, 19))
    assert report.sent == 0 and len(wa.sent) == 0
