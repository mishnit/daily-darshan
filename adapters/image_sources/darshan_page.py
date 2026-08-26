"""Defensive HTML-page adapter for official daily-darshan pages."""
from __future__ import annotations

import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin

from .http_source import HttpImageSource

_BAD = ("logo", "icon", "avatar", "banner", "placeholder", "pixel", "tracking", "qr", "thumb", "map")
_DATE_RE = re.compile(r"\b(\d{4})[-/](\d{2})[-/](\d{2})\b|\b(\d{2})[-/](\d{2})[-/](\d{4})\b")

class _Images(HTMLParser):
    def __init__(self): super().__init__(); self.items = []
    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property", "").lower() == "og:image":
            self.items.append((attrs.get("content", ""), "og:image", 0))
        elif tag == "img":
            try:
                pixels = int(attrs.get("width", 0)) * int(attrs.get("height", 0))
            except (TypeError, ValueError):
                pixels = 0
            self.items.append((
                attrs.get("src") or attrs.get("data-src") or attrs.get("data-lazy-src", ""),
                " ".join(attrs.get(k, "") for k in ("alt", "title", "class", "id")),
                pixels,
            ))

def _conflicting_date(html: str, on_date: date) -> bool:
    found = set()
    for m in _DATE_RE.finditer(html):
        try:
            found.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m.group(1) else date(int(m.group(6)), int(m.group(5)), int(m.group(4))))
        except ValueError: pass
    return bool(found) and on_date not in found

class DarshanPageSource(HttpImageSource):
    """Choose one relevant, non-navigation image from an official page."""
    name = "darshan_page"
    def __init__(self, page_url, keywords, *, date_parameter=False, check_page_date=False, **kwargs):
        super().__init__(**kwargs); self._page_url = page_url; self._keywords = tuple(k.lower() for k in keywords)
        self._date_parameter = date_parameter; self._check_page_date = check_page_date
        self.last_page_url = ""; self.last_image_url = ""
    def page_url(self, on_date):
        return f"{self._page_url}{'&' if '?' in self._page_url else '?'}date={on_date.isoformat()}" if self._date_parameter else self._page_url
    def resolve_url(self, on_date):
        page_url = self.page_url(on_date); self.last_page_url = page_url
        try: resp = self._session.get(page_url, timeout=self._timeout, headers={"User-Agent": "DailyDarshan/2.0"})
        except Exception: return None
        if resp.status_code != 200 or not resp.text or (self._check_page_date and _conflicting_date(resp.text, on_date)): return None
        parser = _Images(); parser.feed(resp.text); best = None
        for raw, attrs, pixels in parser.items:
            url = urljoin(page_url, raw); text = f"{url} {attrs}".lower()
            if not url or any(word in text for word in _BAD): continue
            score = sum(10 for word in self._keywords if word in text) + (8 if "darshan" in text else 0) + (2 if url.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp")) else 0)
            candidate = (score, pixels, url)
            if score and (best is None or candidate[:2] > best[:2]): best = candidate
        if best: self.last_image_url = best[2]; return best[2]
        return None
