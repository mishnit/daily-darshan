"""ImageService / ImageCollector + ImageValidator interface (section 10)."""
from __future__ import annotations

from datetime import date
from typing import Protocol

from domain.image import Image
from application.ports.repositories import LogRepositoryPort
from application.ports.storage import ImageSourcePort


class ImageValidatorProtocol(Protocol):
    def validate(self, image: Image) -> bool: ...
    def validate_fallback(self, image: Image) -> bool: ...


class AllSourcesFailed(Exception):
    pass


class ImageCollector:
    """Choose the largest valid remote image from the configured source chain."""

    def __init__(
        self,
        sources: list[ImageSourcePort] | dict[str, ImageSourcePort],
        validator: ImageValidatorProtocol,
        logs: LogRepositoryPort | None = None,
        rotation: dict[str, list[str]] | None = None,
    ):
        self._sources = sources
        self._validator = validator
        self._logs = logs
        self._rotation = rotation or {}

    def source_names_for(self, on_date: date) -> list[str]:
        """Configured Monday–Sunday chain, with old list behaviour preserved."""
        return self._rotation.get(on_date.strftime("%A").lower(), [])

    def _sources_for(self, on_date: date) -> list[ImageSourcePort]:
        if isinstance(self._sources, dict):
            return [self._sources[name] for name in self.source_names_for(on_date) if name in self._sources]
        return self._sources

    def collect(self, on_date: date | None = None) -> Image:
        on_date = on_date or date.today()
        candidates: list[tuple[tuple[int, int, int], Image]] = []
        for source in self._sources_for(on_date):
            try:
                candidate = source.fetch(on_date)
            except Exception as exc:  # transient source failure -> next source
                self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:{exc}")
                continue
            if candidate is None:
                self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:no-candidate")
                continue
            if self._validator.validate(candidate):
                candidates.append((_resolution_rank(candidate), candidate))
                continue
            self._log("IMAGE_FETCH_FAILED", details=f"{source.name}:invalid")
        if candidates:
            # ``max`` preserves the configured order for an equal-resolution tie.
            _, image = max(candidates, key=lambda item: item[0])
            self._log("IMAGE_FETCH_SUCCESS", details=f"{image.source}:{image.sha256()}")
            return image
        raise AllSourcesFailed(f"All image sources failed for {on_date.isoformat()}")

    def _log(self, event: str, mobile: str = "", details: str = "") -> None:
        if self._logs:
            self._logs.log(event, mobile, details)


def _resolution_rank(image: Image) -> tuple[int, int, int]:
    """Rank images by pixels, then their shorter and longer sides.

    Pillow is a production dependency. The zero rank keeps lightweight test
    doubles backward-compatible and preserves source order when dimensions
    cannot be decoded.
    """
    try:
        import io
        from PIL import Image as PILImage

        with PILImage.open(io.BytesIO(image.data)) as decoded:
            width, height = decoded.size
    except Exception:
        return (0, 0, 0)
    return (width * height, min(width, height), max(width, height))


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
        self, on_date: date, existing: bytes | None = None, force_refresh: bool = False
    ) -> Image | None:
        """Return the image to store, or None if a valid one already exists.

        Idempotency (section 10): if an existing valid image is present, do
        not replace it unless ``force_refresh`` requests a new remote source
        selection for today's scheduled image.
        """
        if existing and not force_refresh:
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
