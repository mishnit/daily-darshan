"""Shared pytest fixtures and in-memory fakes."""
from __future__ import annotations

from datetime import date

import pytest

from application.ports.whatsapp import MediaUploadResult, WhatsAppClientPort, WhatsAppResult
from application.ports.storage import ImageSourcePort
from domain.image import Image


class FakeWhatsApp(WhatsAppClientPort):
    """Records sends; configurable to fail N times then succeed."""

    def __init__(self, fail_times: int = 0, always_fail: bool = False, upload_ok: bool = True):
        self.sent: list[dict] = []
        self._fail_times = fail_times
        self._always_fail = always_fail
        self._upload_ok = upload_ok
        self._calls = 0
        self.uploads = 0

    def _result(self) -> WhatsAppResult:
        self._calls += 1
        if self._always_fail or self._calls <= self._fail_times:
            return WhatsAppResult(ok=False, error="simulated")
        return WhatsAppResult(ok=True, message_id=f"msg{self._calls}")

    def send_text(self, mobile, message):
        r = self._result()
        self.sent.append({"type": "text", "mobile": mobile, "message": message, "ok": r.ok})
        return r

    def send_image(self, mobile, image_url, caption=None):
        r = self._result()
        self.sent.append({"type": "image", "mobile": mobile, "url": image_url, "ok": r.ok})
        return r

    def send_image_by_id(self, mobile, media_id, caption=None):
        r = self._result()
        self.sent.append({"type": "image_id", "mobile": mobile, "media_id": media_id, "ok": r.ok})
        return r

    def upload_media(self, content, mime_type="image/jpeg"):
        self.uploads += 1
        if not self._upload_ok:
            return MediaUploadResult(ok=False, error="simulated-upload-fail")
        return MediaUploadResult(ok=True, media_id="MEDIA123")

    def send_template(self, mobile, template):
        r = self._result()
        self.sent.append({"type": "template", "mobile": mobile, "template": template, "ok": r.ok})
        return r

    def send_buttons(self, mobile, body, buttons):
        r = self._result()
        self.sent.append({"type": "buttons", "mobile": mobile, "body": body,
                          "buttons": [bid for bid, _ in buttons], "ok": r.ok})
        return r

    def send_list(self, mobile, body, button_text, rows):
        r = self._result()
        self.sent.append({"type": "list", "mobile": mobile, "body": body,
                          "rows": [rid for rid, *_ in rows], "ok": r.ok})
        return r

    def send_template_params(self, mobile, template_name, body_params, lang="en"):
        r = self._result()
        self.sent.append({
            "type": "template_params", "mobile": mobile, "template": template_name,
            "params": list(body_params), "lang": lang, "ok": r.ok,
        })
        return r


class FakeSource(ImageSourcePort):
    def __init__(self, name, image: Image | None = None, raises: bool = False):
        self.name = name
        self._image = image
        self._raises = raises

    def fetch(self, on_date):
        if self._raises:
            raise RuntimeError("source down")
        return self._image


@pytest.fixture
def plans():
    return {
        "monthly": {"amount": 49, "days": 30},
        "yearly": {"amount": 449, "days": 365},
    }


@pytest.fixture
def upi_config():
    return {"payee_vpa": "test@upi", "payee_name": "Test Payee", "currency": "INR"}


@pytest.fixture
def repos(tmp_path):
    """Real CSV repositories backed by a temp directory."""
    from repositories.subscriber_repository import CSVSubscriberRepository
    from repositories.payment_repository import CSVPaymentRepository
    from repositories.sentlog_repository import CSVSentLogRepository
    from repositories.renewal_repository import CSVRenewalRepository

    return {
        "subscribers": CSVSubscriberRepository(str(tmp_path / "subscribers.csv")),
        "payments": CSVPaymentRepository(str(tmp_path / "payments.csv")),
        "sentlog": CSVSentLogRepository(str(tmp_path / "sentlog.csv")),
        "renewals": CSVRenewalRepository(str(tmp_path / "renewals.csv")),
    }
