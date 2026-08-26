"""ImageValidator (Tech Doc section 10).

Checks: exists/non-empty, supported format, valid image data, optional
minimum dimensions. SHA-256 is available via Image.sha256().
"""
from __future__ import annotations

import io

try:
    from PIL import Image as PILImage
    _PIL_AVAILABLE = True
except Exception:  # pragma: no cover - Pillow optional at runtime
    _PIL_AVAILABLE = False

from domain.image import Image


class ImageValidator:
    def __init__(
        self,
        allowed_formats: list[str] | None = None,
        min_width: int = 0,
        min_height: int = 0,
    ):
        self._allowed = {f.upper() for f in (allowed_formats or ["JPEG", "PNG", "JPG"])}
        self._min_width = min_width
        self._min_height = min_height

    def validate(self, image: Image) -> bool:
        return self._validate(image, enforce_minimum_dimensions=True)

    def validate_fallback(self, image: Image) -> bool:
        """Validate the emergency local fallback without the HD-size policy."""
        return self._validate(image, enforce_minimum_dimensions=False)

    def _validate(self, image: Image, enforce_minimum_dimensions: bool) -> bool:
        if image is None or image.is_empty:
            return False
        if not _PIL_AVAILABLE:
            # Without Pillow we can only assert non-empty bytes.
            return True
        try:
            with PILImage.open(io.BytesIO(image.data)) as im:
                fmt = (im.format or "").upper()
                if fmt not in self._allowed:
                    return False
                im.verify()  # validate image data integrity
            # verify() invalidates the object; reopen for dimensions.
            with PILImage.open(io.BytesIO(image.data)) as im2:
                width, height = im2.size
                if enforce_minimum_dimensions and (
                    width < self._min_width or height < self._min_height
                ):
                    return False
        except Exception:
            return False
        return True
