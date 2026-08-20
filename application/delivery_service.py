"""DeliveryService: daily image delivery with idempotency + retries (sections 12, 14)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from domain.enums import DeliveryStatus
from domain.subscriber import sanitize_display_name
from application.ports.repositories import (
    LogRepositoryPort,
    SentLogRepositoryPort,
    SubscriberRepositoryPort,
)
from application.ports.whatsapp import WhatsAppClientPort, WhatsAppResult


@dataclass
class DeliveryReport:
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    failures: list[str] = field(default_factory=list)


class DeliveryService:
    def __init__(
        self,
        subscribers: SubscriberRepositoryPort,
        sentlog: SentLogRepositoryPort,
        whatsapp: WhatsAppClientPort,
        eligibility,  # SubscriberService (duck-typed: is_eligible)
        caption_template: str = "Daily Darshan - {date}",
        max_retries: int = 3,
        retry_sleep: float = 0.0,
        logs: LogRepositoryPort | None = None,
        delivery_mode: str = "image",
        template_name: str = "",
        template_lang: str = "en",
        page_base_url: str = "",
    ):
        self._subscribers = subscribers
        self._sentlog = sentlog
        self._whatsapp = whatsapp
        self._eligibility = eligibility
        self._caption_template = caption_template
        self._max_retries = max_retries
        self._retry_sleep = retry_sleep
        self._logs = logs
        self._mode = delivery_mode
        self._template_name = template_name
        self._template_lang = template_lang
        self._page_base_url = page_base_url

    def _retry(self, send) -> WhatsAppResult:
        """Call a zero-arg send callable with bounded retries."""
        last: WhatsAppResult = WhatsAppResult(ok=False, error="not attempted")
        for attempt in range(1, self._max_retries + 1):
            last = send()
            if last.ok:
                return last
            if attempt < self._max_retries and self._retry_sleep:
                time.sleep(self._retry_sleep)
        return last

    def _page_url(self, sub) -> str:
        return f"{self._page_base_url.rstrip('/')}/{sub.subscription_id}"

    def deliver(
        self,
        on_date: date,
        image_url: str | None = None,
        image_bytes: bytes | None = None,
    ) -> DeliveryReport:
        """Deliver today's darshan to eligible subscribers.

        delivery_mode:
          - "utility_template": send an approved parameterized template whose
            {{2}} is the per-subscriber page URL (utility-priced). No image is
            attached to the WhatsApp message; the image lives on the page.
          - "image" (default): send the image inline. Prefers Meta media upload
            (private-repo safe), falls back to image_url.
        """
        if self._mode == "utility_template":
            return self._deliver_template(on_date)
        return self._deliver_image(on_date, image_url, image_bytes)

    # ------------------------------------------------------------------ #
    # Utility-template mode (per-subscriber page URL)
    # ------------------------------------------------------------------ #
    def _deliver_template(self, on_date: date) -> DeliveryReport:
        report = DeliveryReport()
        if not self._template_name or not self._page_base_url:
            self._log("DELIVERY_ABORTED", "", "template_name/page_base_url not configured")
            return report

        for sub in self._subscribers.all():
            mobile = sub.mobile
            if self._sentlog.was_sent(on_date, mobile):
                report.skipped += 1
                continue
            if not self._eligibility.is_eligible(mobile, on_date):
                report.skipped += 1
                continue
            if not sub.subscription_id:
                # Cannot build a per-subscriber URL; skip and log for backfill.
                report.skipped += 1
                self._log("DELIVERY_SKIPPED_NO_SUBID", mobile, "")
                continue

            page_url = self._page_url(sub)
            # (#7) Sanitize name for the WhatsApp template param; safe fallback.
            name = sanitize_display_name(sub.name, "devotee")
            result = self._retry(lambda: self._whatsapp.send_template_params(
                mobile, self._template_name, [name, page_url], self._template_lang,
            ))
            status = DeliveryStatus.SENT if result.ok else DeliveryStatus.FAILED
            self._sentlog.append({
                "date": on_date.isoformat(),
                "mobile": mobile,
                "image": page_url,  # record the page URL delivered
                "whatsapp_message_id": result.message_id,
                "status": status.value,
            })
            if result.ok:
                report.sent += 1
                self._log("WHATSAPP_SEND_SUCCESS", mobile, result.message_id)
            else:
                report.failed += 1
                report.failures.append(mobile)
                self._log("WHATSAPP_SEND_FAILED", mobile, result.error)
        return report

    # ------------------------------------------------------------------ #
    # Image mode (inline image; media upload or url)
    # ------------------------------------------------------------------ #
    def _deliver_image(
        self, on_date: date, image_url: str | None, image_bytes: bytes | None
    ) -> DeliveryReport:
        report = DeliveryReport()
        caption = self._caption_template.format(date=on_date.isoformat())
        image_name = f"{on_date.isoformat()}.jpg"

        media_id: str | None = None
        if image_bytes:
            upload = self._whatsapp.upload_media(image_bytes, "image/jpeg")
            if upload.ok:
                media_id = upload.media_id
            else:
                self._log("WHATSAPP_MEDIA_UPLOAD_FAILED", "", upload.error)
        if media_id is None and not image_url:
            self._log("DELIVERY_ABORTED", "", "no media_id and no image_url")
            return report

        for sub in self._subscribers.all():
            mobile = sub.mobile
            if self._sentlog.was_sent(on_date, mobile):
                report.skipped += 1
                continue
            if not self._eligibility.is_eligible(mobile, on_date):
                report.skipped += 1
                continue

            if media_id:
                result = self._retry(
                    lambda: self._whatsapp.send_image_by_id(mobile, media_id, caption)
                )
            else:
                result = self._retry(
                    lambda: self._whatsapp.send_image(mobile, image_url, caption)
                )
            status = DeliveryStatus.SENT if result.ok else DeliveryStatus.FAILED
            self._sentlog.append({
                "date": on_date.isoformat(),
                "mobile": mobile,
                "image": image_name,
                "whatsapp_message_id": result.message_id,
                "status": status.value,
            })
            if result.ok:
                report.sent += 1
                self._log("WHATSAPP_SEND_SUCCESS", mobile, result.message_id)
            else:
                report.failed += 1
                report.failures.append(mobile)
                self._log("WHATSAPP_SEND_FAILED", mobile, result.error)
        return report

    def _log(self, event: str, mobile: str, details: str) -> None:
        if self._logs:
            self._logs.log(event, mobile, details)
