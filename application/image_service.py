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

    def prune_images(
        self,
        keep: int = 7,
        fallback_name: str = "fallback.jpg",
        root: str = ".",
    ) -> list[str]:
        """Delete old dated images, keeping the newest ``keep`` plus a fallback.

        Retention policy (section 10/11 hygiene):
          - Keep the ``keep`` most recent dated images named ``YYYY-MM-DD.jpg``.
          - Always keep ``fallback_name`` if present (the safety-net image).
          - Delete every other .jpg in the images dir.

        Only files matching the canonical dated name are considered for
        deletion; anything else (e.g. the fallback, a .gitkeep) is left alone
        unless it is an older dated image. Returns the repo-relative paths
        removed so the caller can commit the deletions.

        Idempotent: a second run with nothing to prune returns [].
        """
        import os
        import re

        dated_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jpg$")
        abs_dir = os.path.join(root, self._images_dir)
        if not os.path.isdir(abs_dir):
            return []

        dated: list[tuple[str, str]] = []  # (date_str, filename)
        for name in os.listdir(abs_dir):
            m = dated_re.match(name)
            if m:
                dated.append((m.group(1), name))

        # Newest first by ISO date string (lexicographic == chronological here).
        dated.sort(key=lambda t: t[0], reverse=True)
        to_delete = dated[keep:] if keep >= 0 else []

        removed: list[str] = []
        for _date_str, name in to_delete:
            if name == fallback_name:
                continue  # never delete the fallback, even if it looks dated
            full = os.path.join(abs_dir, name)
            try:
                os.remove(full)
            except FileNotFoundError:
                continue
            removed.append(os.path.join(self._images_dir, name))
        return removed
