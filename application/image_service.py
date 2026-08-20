"""ImageService / ImageCollector + ImageValidator interface (section 10)."""
from __future__ import annotations

from datetime import date
from typing import Protocol

from domain.image import Image
from application.ports.repositories import LogRepositoryPort
from application.ports.storage import ImageSourcePort


class ImageValidatorProtocol(Protocol):
    def validate(self, image: Image) -> bool: ...


class AllSourcesFailed(Exception):
    pass


class ImageCollector:
    """Iterate sources in priority order; return first valid image (section 10)."""

    def __init__(
        self,
        sources: list[ImageSourcePort],
        validator: ImageValidatorProtocol,
        logs: LogRepositoryPort | None = None,
    ):
        self._sources = sources
        self._validator = validator
        self._logs = logs

    def collect(self, on_date: date | None = None) -> Image:
        on_date = on_date or date.today()
        for source in self._sources:
            try:
                candidate = source.fetch(on_date)
            except Exception as exc:  # transient source failure -> next source
                self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:{exc}")
                continue
            if candidate is None:
                self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:no-candidate")
                continue
            if self._validator.validate(candidate):
                self._log("IMAGE_FETCH_SUCCESS", details=f"{source.name}:{candidate.sha256()}")
                return candidate
            self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:invalid")
        raise AllSourcesFailed(f"All image sources failed for {on_date.isoformat()}")

    def _log(self, event: str, mobile: str = "", details: str = "") -> None:
        if self._logs:
            self._logs.log(event, mobile, details)


class ImageService:
    """Idempotent daily image collection + storage (sections 10, 11)."""

    def __init__(
        self,
        collector: ImageCollector,
        validator: ImageValidatorProtocol,
        images_dir: str = "images",
    ):
        self._collector = collector
        self._validator = validator
        self._images_dir = images_dir

    def ensure_daily_image(
        self, on_date: date, existing: bytes | None = None
    ) -> Image | None:
        """Return the image to store, or None if a valid one already exists.

        Idempotency (section 10): if an existing valid image is present, do
        not replace it.
        """
        if existing:
            current = Image(image_date=on_date, data=existing)
            if self._validator.validate(current):
                return None
        image = self._collector.collect(on_date)
        return image

    def canonical_path(self, on_date: date) -> str:
        return Image(image_date=on_date).canonical_path(self._images_dir)
