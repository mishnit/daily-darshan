"""Small official temple adapters used by the weekday rotation registry."""
from __future__ import annotations

import io
import json
import re
from datetime import date, datetime
from html import unescape
from urllib.parse import urlunsplit, urlsplit

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - Pillow is a project dependency
    PILImage = None

from .darshan_page import DarshanPageSource
from .http_source import HttpImageSource


_WORDPRESS_SIZE_SUFFIX = re.compile(r"-\d{1,5}x\d{1,5}(?=\.[^.]+$)", re.IGNORECASE)
_VRINDAVAN_GALLERY_IMAGE = re.compile(r"static/static-_[a-z0-9]+\.jpg", re.IGNORECASE)
_VRINDAVAN_CDN = "https://cdn.iskconvrindavan.com/"
_SWAMINARAYAN_FEED = "https://dailydarshanserver.nnd.media/api/iframe/content?mode=dark"
_SWAMINARAYAN_CARD = re.compile(
    r'<div[^>]*class="[^"]*header-container[^"]*"[^>]*>\s*(?P<date>[^<]+?)\s*</div>'
    r'.*?<img(?P<image>[^>]+)>.*?<div[^>]*class="[^"]*temple-name[^"]*"[^>]*>'
    r'\s*(?P<temple>[^<]+?)\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
_HTML_ATTR = re.compile(r'(?P<name>[\w-]+)=["\'](?P<value>.*?)["\']', re.DOTALL)

class _TemplePageSource(DarshanPageSource):
    keywords = []
    page_is_dated = False
    date_parameter = False
    def __init__(self, page_url, **kwargs):
        super().__init__(page_url, self.keywords, check_page_date=self.page_is_dated,
                         date_parameter=self.date_parameter, **kwargs)

class MahakalSource(HttpImageSource):
    """Read dated, full-resolution darshan images from Mahakal's official API.

    ``/live-darshan`` is a JavaScript application shell.  Its page assets are
    static and cannot prove that an image belongs to the requested day, whereas
    the public media API supplies a date per item.
    """

    name = "mahakal"
    api_url = "https://prod-api.mahakal.brainabove.net/public/api/v1/media"

    def __init__(self, page_url: str, **kwargs):
        # Keep page_url in the config/constructor contract used by all temple
        # adapters, while deliberately querying the official dated API instead.
        super().__init__(**kwargs)
        self._page_url = page_url
        self.last_image_url = ""

    @staticmethod
    def _items(payload: object, on_date: date) -> list[str]:
        if not isinstance(payload, dict) or not payload.get("success"):
            return []
        groups = payload.get("data")
        if not isinstance(groups, dict):
            return []
        requested = on_date.isoformat()
        urls = []
        for media in groups.values():
            if not isinstance(media, list):
                continue
            for item in media:
                if not isinstance(item, dict):
                    continue
                if str(item.get("media_type", "")).lower() != "image":
                    continue
                if not str(item.get("date", "")).startswith(requested):
                    continue
                # actual_media_url is the original asset; thumbnail_url is not
                # acceptable for delivery or HD selection.
                url = item.get("actual_media_url") or item.get("media_url")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    urls.append(url)
        return list(dict.fromkeys(urls))

    @staticmethod
    def _dimensions(data: bytes) -> tuple[int, int] | None:
        if PILImage is None:
            return None
        try:
            with PILImage.open(io.BytesIO(data)) as image:
                image.verify()
            with PILImage.open(io.BytesIO(data)) as image:
                return image.size
        except Exception:
            return None

    def fetch(self, on_date: date):
        try:
            response = self._session.get(
                self.api_url,
                params={"date": on_date.isoformat()},
                timeout=self._timeout,
                headers={"User-Agent": "DailyDarshan/2.0"},
            )
            if response.status_code != 200:
                return None
            payload = json.loads(response.text)
        except Exception:
            return None

        best = None
        for url in self._items(payload, on_date):
            try:
                image = self._download(url, on_date)
            except Exception:
                continue
            if image is None:
                continue
            dimensions = self._dimensions(image.data)
            if dimensions is None:
                continue
            width, height = dimensions
            candidate = (width * height, width, height, url, image)
            if best is None or candidate[:4] > best[:4]:
                best = candidate
        if best is None:
            return None
        self.last_image_url = best[3]
        return best[4]


class SalangpurSource(_TemplePageSource):
    name, keywords, date_parameter = "salangpur", ["salangpur", "kashtbhanjan", "hanuman", "darshan"], True
class IskconBangaloreSource(_TemplePageSource):
    name, keywords, page_is_dated = "iskcon_bangalore", ["iskcon", "bangalore", "krishna", "radha", "darshan"], True

    def resolve_url(self, on_date):
        """Use the WordPress original instead of its generated thumbnail."""
        url = super().resolve_url(on_date)
        if not url:
            return None
        parts = urlsplit(url)
        original = urlunsplit(parts._replace(path=_WORDPRESS_SIZE_SUFFIX.sub("", parts.path)))
        self.last_image_url = original
        return original


class IskconVrindavanSource(_TemplePageSource):
    name, keywords = "iskcon_vrindavan", ["vrindavan", "krishna", "radha", "darshan"]

    def page_url(self, on_date):
        """The official gallery uses a dated route, not a query parameter."""
        return f"{self._page_url.rstrip('/')}/{on_date.isoformat()}/2/sringar-darshan"

    def resolve_url(self, on_date):
        """Extract a dated Sringar image from the Remix hydration payload.

        The page's Open Graph image is a site-wide share thumbnail, so relying on
        its regular ``<img>``/metadata parser would not retrieve that day's darshan.
        """
        page_url = self.page_url(on_date)
        self.last_page_url = page_url
        try:
            response = self._session.get(
                page_url,
                timeout=self._timeout,
                headers={"User-Agent": "DailyDarshan/2.0"},
            )
        except Exception:
            return None
        if response.status_code != 200 or not response.text:
            return None
        match = _VRINDAVAN_GALLERY_IMAGE.search(response.text)
        if not match:
            return None
        self.last_image_url = f"{_VRINDAVAN_CDN}{match.group(0)}"
        return self.last_image_url


class IskconTirupatiSource(_TemplePageSource):
    name, keywords = "iskcon_tirupati", ["tirupati", "krishna", "darshan"]
class SwaminarayanSource(_TemplePageSource):
    name, keywords = "swaminarayan", ["swaminarayan", "darshan", "vishnu"]

    def resolve_url(self, on_date):
        """Read Ahmedabad (Kalupur)'s dated card from the official iframe feed."""
        self.last_page_url = _SWAMINARAYAN_FEED
        try:
            response = self._session.get(
                _SWAMINARAYAN_FEED,
                timeout=self._timeout,
                headers={"User-Agent": "DailyDarshan/2.0"},
            )
        except Exception:
            return None
        if response.status_code != 200 or not response.text:
            return None
        for card in _SWAMINARAYAN_CARD.finditer(response.text):
            try:
                card_date = datetime.strptime(unescape(card.group("date")).strip(), "%a, %d %B %Y").date()
            except ValueError:
                continue
            if card_date != on_date or "kalupur" not in unescape(card.group("temple")).lower():
                continue
            attrs = {m.group("name").lower(): unescape(m.group("value")) for m in _HTML_ATTR.finditer(card.group("image"))}
            url = attrs.get("data-src") or attrs.get("src")
            if url and not url.startswith("data:"):
                self.last_image_url = url
                return url
        return None
class MayapurSource(_TemplePageSource):
    name, keywords = "mayapur", ["mayapur", "krishna", "radha", "darshan"]
