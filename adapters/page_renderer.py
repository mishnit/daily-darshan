"""Static per-subscriber page renderer (GitHub Pages target for the utility link).

Renders one HTML page per subscription at:
    <pages_dir>/<subscription_id>/index.html

The page shows today's date, delivery status, the HD darshan image, and basic
subscription info, plus Open Graph tags for a clean WhatsApp link preview. The
path uses the unguessable subscription_id; no PII (mobile number) is rendered.

This is an infrastructure adapter — it performs file I/O and HTML templating and
is deliberately kept out of the domain/application layers.
"""
from __future__ import annotations

import html
import os
from datetime import date

from domain.subscriber import Subscriber

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="robots" content="noindex, nofollow">
  <title>Daily Darshan — {date}</title>
  <meta property="og:type" content="website">
  <meta property="og:title" content="Daily Darshan — {date}">
  <meta property="og:description" content="{status_text}">
  <meta property="og:image" content="{image_url}">
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; background: #faf6ef; color: #2b2b2b; }}
    .wrap {{ max-width: 640px; margin: 0 auto; padding: 24px; text-align: center; }}
    h1 {{ font-size: 1.3rem; margin: 8px 0; }}
    .greeting {{ font-size: 1rem; color: #555; margin-bottom: 8px; }}
    .status {{ display: inline-block; padding: 4px 12px; border-radius: 999px;
               background: #e4f6e4; color: #1a7f37; font-size: 0.85rem; margin-bottom: 16px; }}
    img.darshan {{ width: 100%; height: auto; border-radius: 12px; box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
    .meta {{ margin-top: 16px; font-size: 0.9rem; color: #555; }}
    .meta div {{ margin: 2px 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🙏 Daily Darshan</h1>
    <div class="greeting">{greeting}</div>
    <div class="status">{status_text}</div>
    <img class="darshan" src="{image_url}" alt="Daily Darshan for {date}"
         onerror="this.onerror=null; this.src='{fallback_url}';">
    <div class="meta">
      <div>Date: {date}</div>
      <div>Plan: {plan}</div>
      <div>Subscription active until: {end_date}</div>
    </div>
  </div>
</body>
</html>
"""


class PageRenderer:
    def __init__(self, pages_dir: str = "docs", image_public_base: str = ""):
        """pages_dir: local dir committed to the repo (GitHub Pages source).
        image_public_base: absolute base URL where images are publicly served,
        e.g. https://user.github.io/daily-darshan . Used for the <img> src and
        og:image so the link preview and page both resolve the image.
        """
        self._pages_dir = pages_dir
        self._image_public_base = image_public_base.rstrip("/")

    def image_url(self, on_date: date, images_dir: str = "images") -> str:
        return f"{self._image_public_base}/{images_dir}/{on_date.isoformat()}.jpg"

    def fallback_url(self, images_dir: str = "images", fallback_name: str = "fallback.jpg") -> str:
        """Public URL of the safety-net image used when a dated image is gone
        (e.g. pruned by retention). Referenced by the page's <img onerror>."""
        return f"{self._image_public_base}/{images_dir}/{fallback_name}"

    def render_html(self, subscriber: Subscriber, on_date: date, delivered: bool,
                    images_dir: str = "images") -> str:
        status_text = "Delivered" if delivered else "Ready"
        from domain.subscriber import sanitize_display_name
        safe_name = sanitize_display_name(subscriber.name, "")
        greeting = f"Namaste, {safe_name}" if safe_name else "Namaste"
        return _TEMPLATE.format(
            date=html.escape(on_date.isoformat()),
            status_text=html.escape(f"{status_text} — {on_date.isoformat()}"),
            image_url=html.escape(self.image_url(on_date, images_dir)),
            fallback_url=html.escape(self.fallback_url(images_dir)),
            plan=html.escape(subscriber.plan or "—"),
            end_date=html.escape(subscriber.end_date.isoformat() if subscriber.end_date else "—"),
            greeting=html.escape(greeting),
        )

    def page_path(self, subscription_id: str) -> str:
        return os.path.join(self._pages_dir, subscription_id, "index.html")

    def write_page(self, subscriber: Subscriber, on_date: date, delivered: bool = True,
                   images_dir: str = "images", root: str = ".") -> str | None:
        """Write the per-subscriber page. Returns the relative path written,
        or None if the subscriber has no subscription_id."""
        if not subscriber.subscription_id:
            return None
        rel_path = self.page_path(subscriber.subscription_id)
        full = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(self.render_html(subscriber, on_date, delivered, images_dir))
        return rel_path

    def write_all(self, subscribers: list[Subscriber], on_date: date,
                  delivered: bool = True, images_dir: str = "images",
                  root: str = ".") -> list[str]:
        written = []
        for sub in subscribers:
            path = self.write_page(sub, on_date, delivered, images_dir, root)
            if path:
                written.append(path)
        return written

    def prune_pages(
        self,
        subscribers: list[Subscriber],
        on_date: date,
        grace_days: int = 7,
        root: str = ".",
    ) -> list[str]:
        """Remove per-subscriber pages for subscribers inactive beyond a grace period.

        Each page lives at ``<pages_dir>/<subscription_id>/index.html``. A page
        is **kept** if any of the following holds for its subscription_id:
          - the subscriber is currently ACTIVE, or
          - the subscriber's ``end_date`` is within ``grace_days`` of
            ``on_date`` (i.e. recently expired/cancelled — keep the link alive
            for late openers), or
          - the subscription_id is unknown to us (no matching subscriber row):
            we do not delete pages we cannot reason about.

        A page is **removed** only when its subscriber is non-active AND their
        ``end_date`` is more than ``grace_days`` in the past. This never touches
        a live subscriber's branded URL and gives recently-expired subscribers a
        grace window before their page disappears.

        Only immediate subdirectories of ``pages_dir`` are considered; loose
        files (e.g. a landing page or .gitkeep) are left untouched. Returns
        repo-relative directory paths removed. Idempotent.
        """
        import shutil
        from datetime import timedelta

        from domain.enums import SubscriberStatus

        abs_dir = os.path.join(root, self._pages_dir)
        if not os.path.isdir(abs_dir):
            return []

        # Index subscribers by their page id for O(1) lookup.
        by_id = {s.subscription_id: s for s in subscribers if s.subscription_id}
        cutoff = on_date - timedelta(days=grace_days)

        removed: list[str] = []
        for name in os.listdir(abs_dir):
            full = os.path.join(abs_dir, name)
            if not os.path.isdir(full):
                continue  # skip loose files

            sub = by_id.get(name)
            if sub is None:
                continue  # unknown id -> keep (don't delete what we can't reason about)
            if sub.status == SubscriberStatus.ACTIVE:
                continue  # live subscriber -> always keep
            # Non-active: keep during the grace window after end_date.
            if sub.end_date is None or sub.end_date >= cutoff:
                continue

            shutil.rmtree(full, ignore_errors=True)
            removed.append(os.path.join(self._pages_dir, name))
        return removed
