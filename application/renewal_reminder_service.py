"""RenewalReminderService (sections 25-30)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from domain.enums import DeliveryStatus, ReminderType, SubscriberStatus
from domain.subscriber import Subscriber, sanitize_display_name
from application.ports.repositories import (
    LogRepositoryPort,
    RenewalRepositoryPort,
    SentLogRepositoryPort,
    SubscriberRepositoryPort,
)
from application.ports.whatsapp import WhatsAppClientPort, WhatsAppResult


@dataclass
class ReminderReport:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


class RenewalReminderService:
    def __init__(
        self,
        subscribers: SubscriberRepositoryPort,
        renewals: RenewalRepositoryPort,
        whatsapp: WhatsAppClientPort,
        reminder_days: list[int],
        template_name: str = "",
        template_lang: str = "en",
        max_retries: int = 3,
        retry_sleep: float = 0.0,
        logs: LogRepositoryPort | None = None,
        sentlog: SentLogRepositoryPort | None = None,
    ):
        self._subscribers = subscribers
        self._renewals = renewals
        self._whatsapp = whatsapp
        self._reminder_days = sorted(set(reminder_days), reverse=True)
        self._template_name = template_name
        self._template_lang = template_lang
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep
        self._logs = logs
        self._sentlog = sentlog

    def find_due_subscribers(self, today: date | None = None) -> list[tuple[Subscriber, int]]:
        """Return (subscriber, days_remaining) for ACTIVE+opt_in subscribers
        whose days_remaining is in the configured reminder_days (section 27)."""
        today = today or date.today()
        due: list[tuple[Subscriber, int]] = []
        for sub in self._subscribers.all():
            if sub.status != SubscriberStatus.ACTIVE or not sub.opt_in:
                continue
            remaining = sub.days_remaining(today)
            if remaining is None or remaining < 0:  # expired excluded
                continue
            if remaining in self._reminder_days:
                due.append((sub, remaining))
        return due

    def already_sent(self, subscriber: Subscriber, reminder_type: ReminderType, expiry_date: date) -> bool:
        return self._renewals.already_sent(subscriber.mobile, reminder_type.value, expiry_date)

    def _send_with_retry(self, subscriber: Subscriber) -> WhatsAppResult:
        if not self._template_name:
            return WhatsAppResult(ok=False, error="config:renewal template_name is required")
        if not subscriber.subscription_id:
            return WhatsAppResult(ok=False, error="config:subscription_id is required")

        name = sanitize_display_name(subscriber.name, "Devotee").title()
        expiry = subscriber.end_date.isoformat() if subscriber.end_date else "soon"
        last = WhatsAppResult(ok=False, error="not attempted")
        for attempt in range(1, self._max_retries + 1):
            last = self._whatsapp.send_template_params(
                subscriber.mobile,
                self._template_name,
                [name],
                self._template_lang,
                url_button_param=subscriber.subscription_id,
            )
            if last.ok:
                return last
            if attempt < self._max_retries and self._retry_sleep:
                time.sleep(self._retry_sleep)
        return last

    def send_reminder(self, subscriber: Subscriber, days_remaining: int) -> WhatsAppResult:
        """Send an approved Utility template outside the 24-hour session window.

        Uses the delivery-status template: body {{1}} = display name and the
        dynamic URL-button {{1}} = subscription id. ``days_remaining`` remains part of
        the public method because it selects the 3-day/1-day idempotency key.
        """
        return self._send_with_retry(subscriber)

    def record_reminder(
        self,
        subscriber: Subscriber,
        reminder_type: ReminderType,
        expiry_date: date,
        result: WhatsAppResult,
    ) -> None:
        from datetime import datetime

        self._renewals.append({
            "mobile": subscriber.mobile,
            "reminder_type": reminder_type.value,
            "expiry_date": expiry_date.isoformat(),
            "sent_at": datetime.now().isoformat(),
            "whatsapp_message_id": result.message_id,
            "status": "SENT" if result.ok else "FAILED",
        })

    def run(self, today: date | None = None) -> ReminderReport:
        today = today or date.today()
        report = ReminderReport()
        for sub, remaining in self.find_due_subscribers(today):
            reminder_type = ReminderType.for_days_remaining(remaining)
            expiry = sub.end_date
            # Renewal and Daily Darshan delivery share one daily contact slot.
            # This protects scheduled/manual reruns and either execution order.
            if self._sentlog and self._sentlog.was_sent(today, sub.mobile):
                report.skipped += 1
                continue
            # Idempotency: mobile + reminder_type + expiry_date (section 28).
            if self.already_sent(sub, reminder_type, expiry):
                # Backfill successful reminders written before the shared daily
                # ledger was introduced, so rollout-day reruns stay deduplicated.
                if self._sentlog:
                    self._sentlog.append({
                        "date": today.isoformat(),
                        "mobile": sub.mobile,
                        "image": f"renewal:{reminder_type.value}",
                        "whatsapp_message_id": "",
                        "status": DeliveryStatus.SENT.value,
                    })
                report.skipped += 1
                continue
            result = self.send_reminder(sub, remaining)
            self.record_reminder(sub, reminder_type, expiry, result)
            if result.ok:
                if self._sentlog:
                    self._sentlog.append({
                        "date": today.isoformat(),
                        "mobile": sub.mobile,
                        "image": f"renewal:{reminder_type.value}",
                        "whatsapp_message_id": result.message_id,
                        "status": DeliveryStatus.SENT.value,
                    })
                report.sent += 1
                self._log("RENEWAL_REMINDER_SENT", sub.mobile, reminder_type.value)
            else:
                report.failed += 1
                report.failures.append(sub.mobile)
                self._log("RENEWAL_REMINDER_FAILED", sub.mobile, result.error)
        return report

    def _log(self, event: str, mobile: str, details: str) -> None:
        if self._logs:
            self._logs.log(event, mobile, details)
