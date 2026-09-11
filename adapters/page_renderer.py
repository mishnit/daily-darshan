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
from urllib.parse import quote

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
    html, body {{ height: 100%; }}
    body {{ font-family: system-ui, sans-serif; margin: 0; overflow: hidden;
            background: #faf6ef; color: #2b2b2b; }}
    .wrap {{ box-sizing: border-box; display: flex; flex-direction: column;
             width: 100%; max-width: 640px; height: 100svh;
             margin: 0 auto; padding: 6px 0 0; text-align: center; }}
    h1 {{ flex: 0 0 auto; margin: 0 8px 4px; color: #34291f;
          font-size: 1.05rem; line-height: 1.2; }}
    .greeting {{ flex: 0 0 auto; margin: 0 8px; color: #53483c;
                 font-size: .86rem; font-weight: 600; }}
    .image-frame {{ display: flex; flex: 0 1 auto; align-items: flex-start;
                    justify-content: center; min-height: 0; margin: 0 8px;
                    overflow: hidden; }}
    img.darshan {{ display: block; width: auto; height: auto;
                   max-width: 100%; max-height: 100%; border-radius: 12px 12px 0 0;
                   box-shadow: 0 4px 16px rgba(0,0,0,.12); }}
    .renewal, .share {{ flex: 0 0 auto; padding: 9px 10px;
                       background: #fff3cd; color: #664d03;
                       font-size: .8rem; line-height: 1.35; }}
    .renewal {{ margin: 6px 8px 0; border-radius: 10px 10px 0 0; }}
    .share {{ margin: 0 8px 6px; border-radius: 0 0 10px 10px; }}
    .renewal p, .share p {{ margin: 0 0 7px; }}
    .renewal a, .share a {{ display: inline-block; padding: 7px 12px; border-radius: 7px;
                           color: #fff; text-decoration: none; font-weight: 650; }}
    .renewal a {{ background: #c62828; }}
    .share a {{ background: #198754; }}
    .renewal + .image-frame img {{ border-radius: 0; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1><strong>🕉&#xA0;</strong>&#x20;Daily Darshan</h1>
    <div class="greeting">{greeting}</div>
    {renewal_reminder}
    <div class="image-frame">
      <img class="darshan" src="{image_url}" alt="Daily Darshan for {date}"
           onerror="this.onerror=null; this.src='{fallback_url}';">
    </div>
    <div class="share">
      <p>Share this HD Daily Darshan image with friends and family on WhatsApp.</p>
      <a href="{share_url}">Share on WhatsApp</a>
    </div>
  </div>
</body>
</html>
"""


class PageRenderer:
    def __init__(self, pages_dir: str = "docs", image_public_base: str = "",
                 image_url_path: str = "images", renewal_whatsapp_number: str = "",
                 renewal_window_days: int = 3):
        """pages_dir: local dir committed to the repo (GitHub Pages source).
        image_public_base: absolute base URL where images are publicly served,
        e.g. https://vipseva.com . Used for the <img> src and og:image so the
        link preview and page both resolve the image.
        image_url_path: the PUBLIC path segment under image_public_base where
        images are served (e.g. "images"). This is decoupled from the on-disk
        images directory (paths.images_dir), because GitHub Pages serves from
        `pages_dir` (docs/) as its web root — so an image stored on disk at
        `docs/images/<date>.jpg` is served at `<base>/images/<date>.jpg`.
        """
        self._pages_dir = pages_dir
        self._image_public_base = image_public_base.rstrip("/")
        self._image_url_path = image_url_path.strip("/")
        self._renewal_whatsapp_number = "".join(
            char for char in renewal_whatsapp_number if char.isdigit()
        )
        self._renewal_window_days = max(0, int(renewal_window_days))

    def image_url(self, on_date: date, images_dir: str | None = None,
                  image_name: str | None = None) -> str:
        # images_dir is accepted for backward-compat but the public URL uses the
        # configured public path segment, not the on-disk directory.
        seg = self._image_url_path
        name = image_name or f"{on_date.isoformat()}.jpg"
        return f"{self._image_public_base}/{seg}/{name}"

    def fallback_url(self, images_dir: str | None = None, fallback_name: str = "fallback.jpg") -> str:
        """Public URL of the safety-net image used when a dated image is gone
        (e.g. pruned by retention). Referenced by the page's <img onerror>."""
        seg = self._image_url_path
        return f"{self._image_public_base}/{seg}/{fallback_name}"

    @staticmethod
    def source_display_name(source: str) -> str:
        """Turn an internal source key into a friendly temple name."""
        words = source.strip().replace("-", "_").split("_")
        return " ".join(word.upper() if word.lower() == "iskcon" else word.title() for word in words if word)

    def render_html(self, subscriber: Subscriber, on_date: date, delivered: bool,
                    images_dir: str | None = None, source: str = "",
                    image_name: str | None = None) -> str:
        status_text = "Delivered" if delivered else "Ready"
        from domain.subscriber import sanitize_display_name
        safe_name = sanitize_display_name(subscriber.name, "")
        greeting = (
            f"Radhe Radhe {safe_name.title()} Ji 🙏"
            if safe_name
            else "Radhe Radhe Ji 🙏"
        )
        renewal_reminder = self._renewal_reminder(subscriber, on_date)
        image_url = self.image_url(on_date, images_dir, image_name)
        share_text = (
            f"Radhe Radhe 🙏\n\nToday's HD Daily Darshan:\n{image_url}"
            "\n\nVisit VIP Seva for daily darshan:\nhttps://vipseva.com/"
        )
        return _TEMPLATE.format(
            date=html.escape(on_date.isoformat()),
            status_text=html.escape(f"{status_text} — {on_date.isoformat()}"),
            greeting=html.escape(greeting),
            image_url=html.escape(image_url),
            fallback_url=html.escape(self.fallback_url(images_dir)),
            renewal_reminder=renewal_reminder,
            share_url=html.escape(
                f"https://wa.me/?text={quote(share_text, safe='')}", quote=True
            ),
        )

    def _renewal_reminder(self, subscriber: Subscriber, on_date: date) -> str:
        if not subscriber.end_date or not self._renewal_whatsapp_number:
            return ""
        days_remaining = (subscriber.end_date - on_date).days
        if days_remaining > self._renewal_window_days:
            return ""
        if days_remaining > 1:
            message = f"Your subscription expires in {days_remaining} days on {subscriber.end_date.isoformat()}."
        elif days_remaining == 1:
            message = f"Your subscription expires tomorrow, on {subscriber.end_date.isoformat()}."
        elif days_remaining == 0:
            message = f"Your subscription expires today, on {subscriber.end_date.isoformat()}."
        else:
            message = f"Your subscription expired on {subscriber.end_date.isoformat()}."
        renew_url = (
            f"https://wa.me/{self._renewal_whatsapp_number}"
            f"?text={quote('RENEW', safe='')}"
        )
        return (
            '<div class="renewal" role="status">'
            f"<p>{html.escape(message)} Renew now to continue receiving Daily Darshan.</p>"
            f'<a href="{html.escape(renew_url, quote=True)}">Renew on WhatsApp</a>'
            "</div>"
        )

    def page_path(self, subscription_id: str) -> str:
        return os.path.join(self._pages_dir, subscription_id, "index.html")

    def write_page(self, subscriber: Subscriber, on_date: date, delivered: bool = True,
                   images_dir: str = "images", root: str = ".", source: str = "",
                   image_name: str | None = None) -> str | None:
        """Write the per-subscriber page. Returns the relative path written,
        or None if the subscriber has no subscription_id."""
        if not subscriber.subscription_id:
            return None
        rel_path = self.page_path(subscriber.subscription_id)
        full = os.path.join(root, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(self.render_html(
                subscriber, on_date, delivered, images_dir, source, image_name
            ))
        return rel_path

    def write_all(self, subscribers: list[Subscriber], on_date: date,
                  delivered: bool = True, images_dir: str = "images",
                  root: str = ".", source: str = "",
                  image_name: str | None = None) -> list[str]:
        written = []
        for sub in subscribers:
            path = self.write_page(
                sub, on_date, delivered, images_dir, root, source, image_name
            )
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
