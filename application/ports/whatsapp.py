"""WhatsApp client port (Tech Doc section 13)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class WhatsAppResult:
    ok: bool
    message_id: str = ""
    error: str = ""


@dataclass
class MediaUploadResult:
    ok: bool
    media_id: str = ""
    error: str = ""


class WhatsAppClientPort(ABC):
    @abstractmethod
    def send_text(self, mobile: str, message: str) -> WhatsAppResult: ...

    @abstractmethod
    def send_image(
        self, mobile: str, image_url: str, caption: str | None = None
    ) -> WhatsAppResult: ...

    @abstractmethod
    def send_template(self, mobile: str, template: str) -> WhatsAppResult: ...

    # --- interactive CTAs (buttons / list). Default impls keep fakes simple. ---
    def send_buttons(
        self, mobile: str, body: str, buttons: list[tuple[str, str]]
    ) -> WhatsAppResult:
        """Send up to 3 reply buttons. buttons = [(id, title), ...].

        Default: not supported. The Meta adapter overrides this.
        """
        return WhatsAppResult(ok=False, error="send_buttons not supported")

    def send_list(
        self, mobile: str, body: str, button_text: str, rows: list[tuple[str, str, str]]
    ) -> WhatsAppResult:
        """Send a list message. rows = [(id, title, description), ...].

        Default: not supported. The Meta adapter overrides this.
        """
        return WhatsAppResult(ok=False, error="send_list not supported")

    def send_template_params(
        self,
        mobile: str,
        template_name: str,
        body_params: list[str],
        lang: str = "en",
    ) -> WhatsAppResult:
        """Send an approved template filling its body {{n}} variables.

        Default: not supported. The Meta adapter overrides this.
        """
        return WhatsAppResult(ok=False, error="send_template_params not supported")

    # --- optional media path (fix #6). Default impls keep test fakes simple. ---
    def upload_media(self, content: bytes, mime_type: str = "image/jpeg") -> MediaUploadResult:
        """Upload media bytes to the provider; return a reusable media id.

        Default: not supported. The Meta adapter overrides this.
        """
        return MediaUploadResult(ok=False, error="upload_media not supported")

    def send_image_by_id(
        self, mobile: str, media_id: str, caption: str | None = None
    ) -> WhatsAppResult:
        """Send an image by a previously uploaded media id (private-repo safe)."""
        return WhatsAppResult(ok=False, error="send_image_by_id not supported")
