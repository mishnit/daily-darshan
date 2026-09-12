"""Fail closed when a personalized page is missing or stale on the public site."""
from html.parser import HTMLParser
import requests


class _Metadata(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            self.values[attrs.get("name")] = attrs.get("content")


class PublishedPageChecker:
    def __init__(self, base_url, session=None):
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()

    def __call__(self, subscriber, on_date):
        if not self.base.startswith("https://") or not subscriber.subscription_id:
            return False
        try:
            response = self.session.get(
                f"{self.base}/{subscriber.subscription_id}/", timeout=15,
                headers={"Cache-Control": "no-cache"}, allow_redirects=False,
            )
            if response.status_code != 200:
                return False
            parser = _Metadata()
            parser.feed(response.text)
            return all(parser.values.get(key) == value for key, value in {
                "darshan-subscription": subscriber.subscription_id,
                "darshan-date": on_date.isoformat(),
                "darshan-expiry": subscriber.end_date.isoformat() if subscriber.end_date else "",
            }.items())
        except (requests.RequestException, ValueError):
            return False
