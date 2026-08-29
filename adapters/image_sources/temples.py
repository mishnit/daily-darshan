"""Small official temple adapters used by the weekday rotation registry."""
from __future__ import annotations

import base64
import binascii
import io
import json
import re
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlunsplit, urlsplit

try:
    from PIL import Image as PILImage
except Exception:  # pragma: no cover - Pillow is a project dependency
    PILImage = None

from .darshan_page import DarshanPageSource
from .http_source import HttpImageSource
from domain.image import Image


_WORDPRESS_SIZE_SUFFIX = re.compile(r"-\d{1,5}x\d{1,5}(?=\.[^.]+$)", re.IGNORECASE)
_VRINDAVAN_GALLERY_IMAGE = re.compile(r"static/static-_[a-z0-9]+\.jpg", re.IGNORECASE)
_VRINDAVAN_CDN = "https://cdn.iskconvrindavan.com/"
_SWAMINARAYAN_FEED = "https://dailydarshanserver.nnd.media/api/iframe/content?mode=dark"
_SWAMINARAYAN_CARD = re.compile(
    r'<a\b[^>]*\bhref=["\'](?P<detail>[^"\']+)["\'][^>]*>.*?'
    r'<div[^>]*class="[^"]*header-container[^"]*"[^>]*>\s*(?P<date>[^<]+?)\s*</div>'
    r'.*?<img(?P<image>[^>]+)>.*?<div[^>]*class="[^"]*temple-name[^"]*"[^>]*>'
    r'\s*(?P<temple>[^<]+?)\s*</div>',
    re.IGNORECASE | re.DOTALL,
)
_MAYAPUR_ALBUM = re.compile(
    r'<p>\s*(?P<date>\d{2}/\d{2}/\d{4})\s*</p>\s*(?:<p>\s*)?'
    r'<a\b[^>]*\bhref=["\'](?P<album>(?:https?://[^"\']+)?/media/album/\d+)["\']',
    re.IGNORECASE,
)
_MAYAPUR_ORIGINAL = re.compile(r'images\[\d+\]\s*=\s*["\'](?P<image>/storage/albums/[^"\']+_image\.jpg)["\']', re.IGNORECASE)
_MUMBAI_SRI_LINK = re.compile(r'href=["\'](?P<detail>/sringar/sringar-darshan-\d+)["\']', re.IGNORECASE)
_MUMBAI_DATE = re.compile(r'<span[^>]*class=["\'][^"\']*(?:s_date|change_date)[^"\']*["\'][^>]*>\s*(?P<date>[^<]+)', re.IGNORECASE)
_IMG_TAG = re.compile(r'<img\b(?P<attrs>[^>]*)>', re.IGNORECASE | re.DOTALL)
_HTML_ATTR = re.compile(r'(?P<name>[\w-]+)=["\'](?P<value>.*?)["\']', re.DOTALL)

class _TemplePageSource(DarshanPageSource):
    keywords = []
    page_is_dated = False
    date_parameter = False
    def __init__(self, page_url, **kwargs):
        super().__init__(page_url, self.keywords, check_page_date=self.page_is_dated,
                         date_parameter=self.date_parameter, **kwargs)


class ConfiguredTempleSource(DarshanPageSource):
    """Safe generic adapter for an enabled temple configured at runtime.

    Named adapters below retain their site-specific parsing. This adapter keeps
    a newly configured source from being silently omitted while still requiring
    a darshan-relevant, ordinary image URL before anything is stored.
    """

    def __init__(self, name: str, page_url: str, **kwargs):
        self.name = name
        keywords = [part for part in name.lower().split("_") if part]
        super().__init__(page_url, keywords + ["darshan"], **kwargs)


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
            # The official API orders a day's original darshans.  Keep the
            # first valid full-size image instead of downloading every item.
            self.last_image_url = url
            return image
        return None


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


class IskconMumbaiSource(HttpImageSource):
    """Decode date-matched Sringar Darshan images embedded by ISKCON Mumbai."""

    name = "iskcon_mumbai"
    max_detail_pages = 14

    def __init__(self, page_url: str, **kwargs):
        super().__init__(**kwargs)
        self._page_url = page_url
        self.last_page_url = ""
        self.last_image_url = ""

    @staticmethod
    def _page_date(html: str) -> date | None:
        match = _MUMBAI_DATE.search(html)
        if not match:
            return None
        raw = unescape(match.group("date")).strip()
        for fmt in ("%b %d, %Y", "%d %b %Y"):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
        return None

    @staticmethod
    def _embedded_images(html: str) -> list[bytes]:
        images = []
        for tag in _IMG_TAG.finditer(html):
            attrs = {m.group("name").lower(): unescape(m.group("value")) for m in _HTML_ATTR.finditer(tag.group("attrs"))}
            if "darshan-detail-images" not in attrs.get("class", ""):
                continue
            value = attrs.get("src", "")
            if not value.startswith("data:image/") or "," not in value:
                continue
            try:
                images.append(base64.b64decode(value.split(",", 1)[1], validate=True))
            except (ValueError, binascii.Error):
                continue
        return images

    def fetch(self, on_date: date):
        self.last_page_url = self._page_url
        try:
            listing = self._session.get(self._page_url, timeout=self._timeout, headers={"User-Agent": "DailyDarshan/2.0"})
        except Exception:
            return None
        if listing.status_code != 200 or not listing.text:
            return None

        seen = set()
        for match in _MUMBAI_SRI_LINK.finditer(listing.text):
            detail_url = urljoin(self._page_url, unescape(match.group("detail")))
            if detail_url in seen:
                continue
            seen.add(detail_url)
            if len(seen) > self.max_detail_pages:
                break
            try:
                detail = self._session.get(detail_url, timeout=self._timeout, headers={"User-Agent": "DailyDarshan/2.0"})
            except Exception:
                continue
            if detail.status_code != 200 or self._page_date(detail.text) != on_date:
                continue

            for raw in self._embedded_images(detail.text):
                dimensions = MahakalSource._dimensions(raw)
                if dimensions is None:
                    continue
                # The detail page supplies full-size embedded darshans in
                # display order; stop at its first decodable image.
                self.last_page_url = detail_url
                self.last_image_url = f"{detail_url}#embedded-darshan"
                return Image(image_date=on_date, data=raw, source=self.name)
        return None


class SwaminarayanSource(HttpImageSource):
    """Extract the largest dated HD darshan, not the Kalupur card cover."""

    name = "swaminarayan"

    def __init__(self, page_url: str, **kwargs):
        super().__init__(**kwargs)
        self._page_url = page_url
        self.last_page_url = ""
        self.last_image_url = ""

    def fetch(self, on_date: date):
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

            detail_url = urljoin(_SWAMINARAYAN_FEED, unescape(card.group("detail")))
            temple_id = parse_qs(urlsplit(detail_url).query).get("id", [None])[0]
            if not temple_id:
                continue
            hd_url = urljoin(detail_url, f"/temple/dailydarshan/hd/{temple_id}/{on_date.isoformat()}")
            try:
                hd_response = self._session.get(
                    hd_url,
                    timeout=self._timeout,
                    headers={"User-Agent": "DailyDarshan/2.0"},
                )
                images = json.loads(hd_response.text) if hd_response.status_code == 200 else []
            except Exception:
                continue

            for raw_url in images if isinstance(images, list) else []:
                if not isinstance(raw_url, str) or not raw_url.startswith(("https://", "http://")):
                    continue
                try:
                    image = self._download(raw_url, on_date)
                except Exception:
                    continue
                if image is None:
                    continue
                dimensions = MahakalSource._dimensions(image.data)
                if dimensions is None:
                    continue
                # The endpoint itself returns HD images in display order.
                # Fetching the first valid one bounds this source's runtime.
                self.last_image_url = raw_url
                return image
        return None
class MayapurSource(HttpImageSource):
    """Extract the largest original from Mayapur's dated daily-darshan album."""

    name = "mayapur"
    # Mayapur's host is slow under automated clients. The first album original
    # is already full-resolution; probing one keeps daily collection reliable.
    max_originals_to_probe = 1

    def __init__(self, page_url: str, **kwargs):
        super().__init__(**kwargs)
        self._page_url = page_url
        self.last_page_url = ""
        self.last_image_url = ""

    def _get(self, url: str):
        return self._session.get(url, timeout=self._timeout, headers={"User-Agent": "DailyDarshan/2.0"})

    def _viewer_url(self, on_date: date) -> str | None:
        self.last_page_url = self._page_url
        try:
            gallery = self._get(self._page_url)
        except Exception:
            return None
        if gallery.status_code != 200 or not gallery.text:
            return None
        requested = on_date.strftime("%d/%m/%Y")
        album_path = next(
            (match.group("album") for match in _MAYAPUR_ALBUM.finditer(gallery.text) if match.group("date") == requested),
            None,
        )
        if not album_path:
            return None
        # The gallery's album ID maps directly to the public first-image viewer;
        # avoid loading the very large album page only to discover that link.
        album_id = album_path.rsplit("/", 1)[-1]
        return urljoin(self._page_url, f"/imageviewer/show-album-pictures/{album_id}/0")

    @staticmethod
    def _declared_dimensions(data: bytes) -> tuple[int, int] | None:
        """Read JPEG dimensions from a short range response without decoding it."""
        if PILImage is None:
            return None
        try:
            with PILImage.open(io.BytesIO(data)) as image:
                return image.size
        except Exception:
            return None

    def _probe_original(self, url: str) -> tuple[tuple[int, int], bytes | None] | None:
        """Probe original dimensions, retaining bytes only if the server sent all."""
        try:
            response = self._session.get(
                url,
                timeout=self._timeout,
                headers={"User-Agent": "DailyDarshan/2.0", "Range": "bytes=0-65535"},
            )
        except Exception:
            return None
        if response.status_code not in (200, 206) or not response.content:
            return None
        dimensions = self._declared_dimensions(response.content)
        if dimensions is None:
            return None
        headers = getattr(response, "headers", {}) or {}
        # A 206 response is deliberately partial and must be downloaded again.
        return dimensions, (None if response.status_code == 206 or headers.get("Content-Range") else response.content)

    def fetch(self, on_date: date):
        viewer_url = self._viewer_url(on_date)
        if not viewer_url:
            return None
        try:
            viewer = self._get(viewer_url)
        except Exception:
            return None
        if viewer.status_code != 200 or not viewer.text:
            return None

        originals = []
        for index, match in enumerate(_MAYAPUR_ORIGINAL.finditer(viewer.text)):
            if index >= self.max_originals_to_probe:
                break
            url = urljoin(viewer_url, match.group("image"))
            probe = self._probe_original(url)
            if probe is None:
                continue
            (width, height), cached = probe
            originals.append((width * height, width, height, url, cached))

        for _, _, _, url, cached in sorted(originals, reverse=True):
            image = Image(image_date=on_date, data=cached, source=self.name) if cached else self._download(url, on_date)
            if image is not None and MahakalSource._dimensions(image.data) is not None:
                self.last_image_url = url
                return image
        return None
