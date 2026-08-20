"""Temple website image source (priority 1)."""
from __future__ import annotations

from datetime import date

from .http_source import HttpImageSource


class TempleSource(HttpImageSource):
    name = "temple"

    def __init__(self, base_url: str, **kwargs):
        super().__init__(**kwargs)
        self._base_url = base_url.rstrip("/")

    def resolve_url(self, on_date: date) -> str | None:
        # Convention: <base>/YYYY-MM-DD.jpg. Adjust per real temple site.
        return f"{self._base_url}/{on_date.isoformat()}.jpg"
