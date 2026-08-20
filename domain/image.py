"""Image domain object and canonical path rules (Tech Doc section 10)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date


@dataclass
class Image:
    """A candidate/stored darshan image for a given date."""

    image_date: date
    data: bytes = b""
    source: str = ""
    fmt: str | None = None  # e.g. "JPEG", "PNG"

    def canonical_path(self, images_dir: str = "images") -> str:
        """images/YYYY-MM-DD.jpg (section 10)."""
        return f"{images_dir}/{self.image_date.isoformat()}.jpg"

    @property
    def is_empty(self) -> bool:
        return not self.data

    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()
