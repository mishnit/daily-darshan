"""Generic website scrape image source (priority 3)."""
from __future__ import annotations

import re
from datetime import date

from .http_source import HttpImageSource

# Grab the first og:image or <img src> that points to an image file.
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
    re.IGNORECASE,
)
_IMG_SRC_RE = re.compile(
    r'<img[^>]+src=["\']([^"\']+\.(?:jpg|jpeg|png))["\']', re.IGNORECASE
)


class WebsiteSource(HttpImageSource):
    name = "website"

    def __init__(self, page_url: str, **kwargs):
        super().__init__(**kwargs)
        self._page_url = page_url

    def resolve_url(self, on_date: date) -> str | None:
        resp = self._session.get(self._page_url, timeout=self._timeout)
        if resp.status_code != 200 or not resp.text:
            return None
        og = _OG_IMAGE_RE.search(resp.text)
        if og:
            return og.group(1)
        img = _IMG_SRC_RE.search(resp.text)
        return img.group(1) if img else None
