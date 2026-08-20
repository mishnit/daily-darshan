"""Tests for retention (image + page pruning) and the graceful image fallback.

Covers:
  - ImageService.prune_images: keep newest N dated images + fallback, delete rest.
  - PageRenderer.prune_pages: keep only active subscription_ids, remove the rest.
  - PageRenderer template: <img onerror> falls back to the fallback image URL.
"""
from __future__ import annotations

import os
from datetime import date

from adapters.page_renderer import PageRenderer
from application.image_service import ImageService
from domain.subscriber import Subscriber
from domain.enums import SubscriberStatus


# --------------------------- image pruning --------------------------- #

def _image_service(images_dir="images"):
    # Collector/validator are unused by prune_images; pass None-safe stubs.
    return ImageService(collector=None, validator=None, images_dir=images_dir)


def _touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"x")


def test_prune_images_keeps_newest_n_and_fallback(tmp_path):
    imgs = tmp_path / "images"
    dates = ["2026-08-14", "2026-08-15", "2026-08-16", "2026-08-17",
             "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"]
    for d in dates:
        _touch(str(imgs / f"{d}.jpg"))
    _touch(str(imgs / "fallback.jpg"))

    svc = _image_service()
    removed = svc.prune_images(keep=7, fallback_name="fallback.jpg", root=str(tmp_path))

    # 8 dated images, keep 7 -> exactly the oldest (2026-08-14) removed.
    assert removed == [os.path.join("images", "2026-08-14.jpg")]
    remaining = sorted(os.listdir(imgs))
    assert "2026-08-14.jpg" not in remaining
    assert "fallback.jpg" in remaining          # fallback preserved
    assert len([n for n in remaining if n[0].isdigit()]) == 7


def test_prune_images_noop_when_within_limit(tmp_path):
    imgs = tmp_path / "images"
    for d in ["2026-08-19", "2026-08-20"]:
        _touch(str(imgs / f"{d}.jpg"))
    svc = _image_service()
    assert svc.prune_images(keep=7, root=str(tmp_path)) == []
    assert len(os.listdir(imgs)) == 2


def test_prune_images_is_idempotent(tmp_path):
    imgs = tmp_path / "images"
    for d in ["2026-08-01", "2026-08-02", "2026-08-03"]:
        _touch(str(imgs / f"{d}.jpg"))
    svc = _image_service()
    first = svc.prune_images(keep=2, root=str(tmp_path))
    assert first == [os.path.join("images", "2026-08-01.jpg")]
    assert svc.prune_images(keep=2, root=str(tmp_path)) == []  # nothing left to prune


def test_prune_images_ignores_non_dated_files(tmp_path):
    imgs = tmp_path / "images"
    _touch(str(imgs / "2026-08-01.jpg"))
    _touch(str(imgs / "2026-08-02.jpg"))
    _touch(str(imgs / ".gitkeep"))
    _touch(str(imgs / "notes.txt"))
    svc = _image_service()
    removed = svc.prune_images(keep=1, root=str(tmp_path))
    assert removed == [os.path.join("images", "2026-08-01.jpg")]
    remaining = set(os.listdir(imgs))
    assert {".gitkeep", "notes.txt", "2026-08-02.jpg"} <= remaining


def test_prune_images_missing_dir_returns_empty(tmp_path):
    svc = _image_service()
    assert svc.prune_images(keep=7, root=str(tmp_path)) == []


# --------------------------- page pruning --------------------------- #

def _renderer(tmp_path, pages_dir="docs"):
    return PageRenderer(pages_dir=pages_dir, image_public_base="https://x.example/base")


def _make_page(root, pages_dir, sub_id):
    d = os.path.join(root, pages_dir, sub_id)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<html></html>")


TODAY = date(2026, 8, 20)


def _sub(sub_id, status, end_date):
    return Subscriber(mobile=f"m-{sub_id}", plan="monthly", status=status,
                      end_date=end_date, subscription_id=sub_id)


def test_prune_pages_keeps_active_removes_long_expired(tmp_path):
    r = _renderer(tmp_path)
    for sid in ["active1", "active2", "expired_old", "cancelled_old"]:
        _make_page(str(tmp_path), "docs", sid)

    subs = [
        _sub("active1", SubscriberStatus.ACTIVE, date(2026, 12, 31)),
        _sub("active2", SubscriberStatus.ACTIVE, date(2026, 12, 31)),
        _sub("expired_old", SubscriberStatus.EXPIRED, date(2026, 8, 1)),      # 19d ago
        _sub("cancelled_old", SubscriberStatus.CANCELLED, date(2026, 8, 5)),  # 15d ago
    ]
    removed = r.prune_pages(subs, TODAY, grace_days=7, root=str(tmp_path))

    assert sorted(removed) == [os.path.join("docs", "cancelled_old"),
                               os.path.join("docs", "expired_old")]
    remaining = sorted(os.listdir(tmp_path / "docs"))
    assert remaining == ["active1", "active2"]


def test_prune_pages_keeps_recently_expired_within_grace(tmp_path):
    r = _renderer(tmp_path)
    _make_page(str(tmp_path), "docs", "just_expired")
    _make_page(str(tmp_path), "docs", "long_expired")

    subs = [
        # end_date within 7 days of today -> kept (grace window).
        _sub("just_expired", SubscriberStatus.EXPIRED, date(2026, 8, 16)),  # 4d ago
        # end_date well past the grace window -> removed.
        _sub("long_expired", SubscriberStatus.EXPIRED, date(2026, 7, 1)),
    ]
    removed = r.prune_pages(subs, TODAY, grace_days=7, root=str(tmp_path))

    assert removed == [os.path.join("docs", "long_expired")]
    remaining = sorted(os.listdir(tmp_path / "docs"))
    assert remaining == ["just_expired"]


def test_prune_pages_grace_boundary_is_inclusive(tmp_path):
    r = _renderer(tmp_path)
    _make_page(str(tmp_path), "docs", "edge")
    # end_date exactly == cutoff (today - grace) is kept (>= cutoff).
    subs = [_sub("edge", SubscriberStatus.EXPIRED, date(2026, 8, 13))]  # exactly 7d ago
    removed = r.prune_pages(subs, TODAY, grace_days=7, root=str(tmp_path))
    assert removed == []
    assert os.listdir(tmp_path / "docs") == ["edge"]


def test_prune_pages_unknown_id_is_kept(tmp_path):
    r = _renderer(tmp_path)
    _make_page(str(tmp_path), "docs", "orphan")  # no matching subscriber row
    removed = r.prune_pages([], TODAY, grace_days=7, root=str(tmp_path))
    assert removed == []
    assert os.listdir(tmp_path / "docs") == ["orphan"]


def test_prune_pages_expired_without_end_date_is_kept(tmp_path):
    r = _renderer(tmp_path)
    _make_page(str(tmp_path), "docs", "no_end")
    subs = [_sub("no_end", SubscriberStatus.EXPIRED, None)]
    removed = r.prune_pages(subs, TODAY, grace_days=7, root=str(tmp_path))
    assert removed == []  # no end_date -> cannot age out -> kept


def test_prune_pages_leaves_loose_files_untouched(tmp_path):
    r = _renderer(tmp_path)
    _make_page(str(tmp_path), "docs", "active1")
    _make_page(str(tmp_path), "docs", "old1")
    with open(tmp_path / "docs" / ".gitkeep", "w") as fh:
        fh.write("")

    subs = [
        _sub("active1", SubscriberStatus.ACTIVE, date(2026, 12, 31)),
        _sub("old1", SubscriberStatus.EXPIRED, date(2026, 1, 1)),
    ]
    removed = r.prune_pages(subs, TODAY, grace_days=7, root=str(tmp_path))
    assert removed == [os.path.join("docs", "old1")]
    remaining = set(os.listdir(tmp_path / "docs"))
    assert ".gitkeep" in remaining and "active1" in remaining and "old1" not in remaining


def test_prune_pages_missing_dir_returns_empty(tmp_path):
    r = _renderer(tmp_path)
    assert r.prune_pages([], TODAY, grace_days=7, root=str(tmp_path)) == []


# --------------------------- graceful fallback --------------------------- #

def test_page_html_has_onerror_fallback_to_fallback_image(tmp_path):
    r = _renderer(tmp_path)
    sub = Subscriber(mobile="9199", plan="monthly", status=SubscriberStatus.ACTIVE,
                     end_date=date(2026, 12, 31), subscription_id="tok",
                     name="Ravi")
    htmltext = r.render_html(sub, date(2026, 8, 20), delivered=True)

    # The dated image is the primary src; fallback.jpg is the onerror target.
    assert "https://x.example/base/images/2026-08-20.jpg" in htmltext
    assert "onerror=" in htmltext
    assert "https://x.example/base/images/fallback.jpg" in htmltext


def test_fallback_url_uses_public_base(tmp_path):
    r = _renderer(tmp_path)
    assert r.fallback_url() == "https://x.example/base/images/fallback.jpg"
    assert r.fallback_url(fallback_name="safety.jpg") == "https://x.example/base/images/safety.jpg"


# --------- public URL path decoupled from on-disk images dir --------- #

def test_image_url_uses_public_path_not_disk_dir(tmp_path):
    """Images stored on disk under docs/images must still resolve at
    <base>/images/... because GitHub Pages serves docs/ as the web root."""
    r = PageRenderer(pages_dir="docs", image_public_base="https://vipseva.com",
                     image_url_path="images")
    # Even if a caller passes the on-disk dir "docs/images", the public URL
    # uses the configured public segment "images".
    url = r.image_url(date(2026, 8, 20), images_dir="docs/images")
    assert url == "https://vipseva.com/images/2026-08-20.jpg"
    assert r.fallback_url(images_dir="docs/images") == "https://vipseva.com/images/fallback.jpg"


def test_custom_image_url_path(tmp_path):
    r = PageRenderer(pages_dir="docs", image_public_base="https://vipseva.com",
                     image_url_path="darshan-images")
    assert r.image_url(date(2026, 8, 20)) == "https://vipseva.com/darshan-images/2026-08-20.jpg"
