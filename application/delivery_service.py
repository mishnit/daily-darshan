"""DeliveryService: daily image delivery with idempotency + retries (sections 12, 14)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from domain.enums import DeliveryStatus
from domain.clock import today_ist
from application.subscription_template import SUBSCRIPTION_STATUS_TEMPLATE, template_body
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
        welcome_template_name: str = "",
        welcome_template_lang: str = "en",
        template_header: str = "none",
        welcome_template_header: str = "none",
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
        self.publication_check = None
        self._welcome_template_name = welcome_template_name
        self._welcome_template_lang = welcome_template_lang
        self._template_header = self._validate_template_header(template_header)
        self._welcome_template_header = self._validate_template_header(welcome_template_header)
        if template_name == SUBSCRIPTION_STATUS_TEMPLATE:
            self._template_header = "image"
        if welcome_template_name == SUBSCRIPTION_STATUS_TEMPLATE:
            self._welcome_template_header = "image"

    @staticmethod
    def _validate_template_header(value: str) -> str:
        normalized = str(value or "none").strip().lower()
        if normalized not in {"none", "image"}:
            raise ValueError("template header must be 'none' or 'image'")
        return normalized

    @property
    def welcome_requires_image_header(self) -> bool:
        return self._welcome_template_header == "image"

    @property
    def requires_image_header(self) -> bool:
        return self._template_header == "image"

    def send_welcome(self, subscriber, header_image_url: str | None = None, *, on_date: date | None = None) -> WhatsAppResult:
        """Send the activation confirmation, independent of daily delivery.

        The welcome worker owns the shared daily ``sentlog`` reservation; this
        transport primitive only calls Meta. Call it only after activation,
        publication and daily-slot checks have succeeded.
        """
        if not subscriber.opt_in:
            return WhatsAppResult(ok=False, error="consent:subscriber opted out")
        if not self._welcome_template_name:
            return WhatsAppResult(ok=False, error="config:welcome_template_name is required")
        if not subscriber.subscription_id:
            return WhatsAppResult(ok=False, error="config:subscription_id is required")
        if self.welcome_requires_image_header and not header_image_url:
            return WhatsAppResult(ok=False, error="config:welcome template image header requires today's image URL")
        return self._whatsapp.send_template_params(
            subscriber.mobile, self._welcome_template_name,
            template_body(self._welcome_template_name, subscriber, on_date or today_ist(), activated=True),
            self._welcome_template_lang, url_button_param=subscriber.subscription_id,
            header_image_url=header_image_url if self.welcome_requires_image_header else None,
        )

    def _retry(self, send) -> WhatsAppResult:
        """Call a zero-arg send callable with bounded retries."""
        last: WhatsAppResult = WhatsAppResult(ok=False, error="not attempted")
        for attempt in range(1, self._max_retries + 1):
            last = send()
            if last.ok or last.unknown:
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
            dynamic URL button receives the subscription id. An image header
            is included only when that template's config selects ``image``.
          - "image" (default): send the image inline. Prefers Meta media upload
            (private-repo safe), falls back to image_url.
        """
        if self._mode == "utility_template":
            return self._deliver_template(on_date, image_url)
        return self._deliver_image(on_date, image_url, image_bytes)

    # ------------------------------------------------------------------ #
    # Utility-template mode (per-subscriber page URL)
    # ------------------------------------------------------------------ #
    def _deliver_template(self, on_date: date, image_url: str | None = None) -> DeliveryReport:
        report = DeliveryReport()
        if not self._template_name or not self._page_base_url:
            self._log("DELIVERY_ABORTED", "", "template_name/page_base_url not configured")
            return report
        if self._template_header == "image" and not image_url:
            self._log("DELIVERY_ABORTED", "", "template image header requires today's image URL")
            report.failed = 1
            report.failures.append("configuration")
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
            if self.publication_check and not self.publication_check(sub, on_date):
                report.failed += 1
                report.failures.append(mobile)
                self._log("DELIVERY_PAGE_NOT_PUBLISHED", mobile, "")
                continue
            # (#7) Sanitize name for the WhatsApp template param; safe fallback.
            body = template_body(self._template_name, sub, on_date)
            reservation = self._sentlog.reserve(on_date, mobile, page_url)
            if reservation is None:
                report.skipped += 1
                continue
            result = self._retry(lambda: self._whatsapp.send_template_params(
                mobile,
                self._template_name,
                body,
                self._template_lang,
                url_button_param=sub.subscription_id,
                header_image_url=image_url if self._template_header == "image" else None,
            ))
            self._sentlog.complete(reservation, result)
            if result.ok:
                report.sent += 1
                self._log("WHATSAPP_SEND_ACCEPTED", mobile, result.message_id)
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

            reservation = self._sentlog.reserve(on_date, mobile, image_name)
            if reservation is None:
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
            self._sentlog.complete(reservation, result)
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
