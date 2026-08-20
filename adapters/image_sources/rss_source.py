"""RSS feed image source (priority 2)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from .http_source import HttpImageSource

_IMG_RE = re.compile(r'https?://\S+\.(?:jpg|jpeg|png)', re.IGNORECASE)


class RSSSource(HttpImageSource):
    name = "rss"

    def __init__(self, feed_url: str, **kwargs):
        super().__init__(**kwargs)
        self._feed_url = feed_url

    def resolve_url(self, on_date: date) -> str | None:
        resp = self._session.get(self._feed_url, timeout=self._timeout)
        if resp.status_code != 200 or not resp.text:
            return None
        # Prefer <enclosure url="..."> then media:content, then first image URL.
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            match = _IMG_RE.search(resp.text)
            return match.group(0) if match else None

        for tag in (".//enclosure", ".//{*}content"):
            for el in root.iterfind(tag):
                url = el.get("url")
                if url and _IMG_RE.match(url):
                    return url
        match = _IMG_RE.search(resp.text)
        return match.group(0) if match else None
