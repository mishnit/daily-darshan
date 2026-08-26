"""Shared HTTP helper for image sources."""
from __future__ import annotations

from datetime import date

import requests

from domain.image import Image
from application.ports.storage import ImageSourcePort


class HttpImageSource(ImageSourcePort):
    """Base class that downloads bytes from a resolved URL."""

    name = "http"

    def __init__(self, timeout: float = 15.0, session: requests.Session | None = None):
        self._timeout = timeout
        self._session = session or requests.Session()

    def resolve_url(self, on_date: date) -> str | None:
        """Return the image URL for the date, or None if unavailable."""
        raise NotImplementedError

    def _download(self, url: str, on_date: date) -> Image | None:
        resp = self._session.get(url, timeout=self._timeout,
                                 headers={"User-Agent": "DailyDarshan/2.0"})
        if resp.status_code != 200 or not resp.content:
            return None
        return Image(image_date=on_date, data=resp.content, source=self.name)

    def fetch(self, on_date: date) -> Image | None:
        url = self.resolve_url(on_date)
        if not url:
            return None
        return self._download(url, on_date)
