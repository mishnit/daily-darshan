"""Meta WhatsApp Cloud API adapter (Tech Doc section 13).

Meta-specific HTTP/JSON details are isolated here. Secrets come from the
environment (section 19); never hard-code tokens.
"""
from __future__ import annotations

import os

import requests

from application.ports.whatsapp import MediaUploadResult, WhatsAppClientPort, WhatsAppResult

_GRAPH_BASE = "https://graph.facebook.com/v19.0"


class MetaWhatsAppClient(WhatsAppClientPort):
    def __init__(
        self,
        access_token: str | None = None,
        phone_number_id: str | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self._token = access_token or os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self._phone_id = phone_number_id or os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self._timeout = timeout
        self._session = session or requests.Session()

    @property
    def _url(self) -> str:
        return f"{_GRAPH_BASE}/{self._phone_id}/messages"

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def _post(self, payload: dict) -> WhatsAppResult:
        try:
            resp = self._session.post(
                self._url, json=payload, headers=self._headers, timeout=self._timeout
            )
        except requests.RequestException as exc:  # transient network error
            return WhatsAppResult(ok=False, error=f"network:{exc}")

        if resp.status_code >= 400:
            return WhatsAppResult(ok=False, error=f"http_{resp.status_code}:{resp.text[:200]}")

        try:
            data = resp.json()
            message_id = data["messages"][0]["id"]
        except (ValueError, KeyError, IndexError) as exc:
            return WhatsAppResult(ok=False, error=f"parse:{exc}")
        return WhatsAppResult(ok=True, message_id=message_id)

    def send_text(self, mobile: str, message: str) -> WhatsAppResult:
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "text",
            "text": {"body": message},
        })

    def send_image(self, mobile: str, image_url: str, caption: str | None = None) -> WhatsAppResult:
        image: dict = {"link": image_url}
        if caption:
            image["caption"] = caption
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "image",
            "image": image,
        })

    def send_template(self, mobile: str, template: str) -> WhatsAppResult:
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "template",
            "template": {"name": template, "language": {"code": "en"}},
        })

    def send_buttons(self, mobile, body, buttons):
        """Interactive reply buttons (max 3). buttons = [(id, title), ...]."""
        action_buttons = [
            {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
            for bid, title in buttons[:3]
        ]
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body[:1024]},
                "action": {"buttons": action_buttons},
            },
        })

    def send_list(self, mobile, body, button_text, rows):
        """Interactive list message. rows = [(id, title, description), ...]."""
        list_rows = [
            {"id": rid, "title": title[:24], "description": (desc or "")[:72]}
            for rid, title, desc in rows
        ]
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body[:1024]},
                "action": {
                    "button": button_text[:20],
                    "sections": [{"title": "Options", "rows": list_rows}],
                },
            },
        })

    def send_template_params(
        self,
        mobile: str,
        template_name: str,
        body_params: list[str],
        lang: str = "en",
    ) -> WhatsAppResult:
        """Send an approved template, filling its body {{1}}..{{n}} variables.

        Used for the utility-template daily delivery: {{1}} = name,
        {{2}} = per-subscriber page URL, etc.
        """
        components = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        }]
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": lang},
                "components": components,
            },
        })

    # ------------------------------------------------------------------ #
    # Media path (fix #6): upload bytes, then send by media id. Works for
    # private repos where a raw.githubusercontent.com link is not reachable.
    # ------------------------------------------------------------------ #
    def upload_media(self, content: bytes, mime_type: str = "image/jpeg") -> MediaUploadResult:
        url = f"{_GRAPH_BASE}/{self._phone_id}/media"
        try:
            resp = self._session.post(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": ("image.jpg", content, mime_type)},
                timeout=self._timeout,
            )
        except requests.RequestException as exc:
            return MediaUploadResult(ok=False, error=f"network:{exc}")
        if resp.status_code >= 400:
            return MediaUploadResult(ok=False, error=f"http_{resp.status_code}:{resp.text[:200]}")
        try:
            media_id = resp.json()["id"]
        except (ValueError, KeyError) as exc:
            return MediaUploadResult(ok=False, error=f"parse:{exc}")
        return MediaUploadResult(ok=True, media_id=media_id)

    def send_image_by_id(
        self, mobile: str, media_id: str, caption: str | None = None
    ) -> WhatsAppResult:
        image: dict = {"id": media_id}
        if caption:
            image["caption"] = caption
        return self._post({
            "messaging_product": "whatsapp",
            "to": mobile,
            "type": "image",
            "image": image,
        })
