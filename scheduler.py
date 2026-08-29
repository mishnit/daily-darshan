"""Scheduler orchestration entrypoint (Tech Doc sections 11, 12, 26, 31).

Jobs (invoked by GitHub Actions):
  - image     : fetch + validate + store today's canonical image + pages, commit
  - pages     : regenerate pages from today's stored image only, commit
  - expiry    : flip ACTIVE subscribers past end_date to EXPIRED, commit
  - renewal   : send renewal reminders, then commit renewals log
  - delivery  : send today's darshan to eligible subscribers, then commit sentlog
  - keepalive : self-ping RENDER_HEALTHCHECK_URL every 5 min (free-plan warm-up)

Usage:
    python scheduler.py image
    python scheduler.py pages
    python scheduler.py expiry
    python scheduler.py delivery
    python scheduler.py renewal
    python scheduler.py keepalive
    python scheduler.py all
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from adapters.github import LocalGitRepository
from application.image_service import AllSourcesFailed
from config import Container
from domain.image import Image


def _image_public_url(config: dict, on_date: date) -> str:
    """Public raw URL for the committed image (WhatsApp needs a URL)."""
    repo = config.get("github", {}).get("repo") or __import__("os").environ.get("GITHUB_REPO", "")
    branch = config.get("github", {}).get("branch", "main")
    images_dir = config["paths"]["images_dir"]
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{images_dir}/{on_date.isoformat()}.jpg"


def _fallback_public_url(config: dict) -> str:
    repo = config.get("github", {}).get("repo") or __import__("os").environ.get("GITHUB_REPO", "")
    branch = config.get("github", {}).get("branch", "main")
    images_dir = config["paths"]["images_dir"]
    fallback = config.get("delivery", {}).get("fallback_image", "fallback.jpg")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{images_dir}/{fallback}"


def _render_pages(container: Container, on_date: date) -> list[str]:
    """Generate per-subscriber static pages for on_date. Returns paths written."""
    return container.page_renderer.write_all(
        container.subscribers.all(), on_date, delivered=True,
        images_dir=container.config["paths"]["images_dir"], root=container.root,
    )


def _canonical_jpeg(data: bytes) -> bytes:
    """Normalize and brand decoded remote images before storage/page rendering."""
    try:
        import io
        from PIL import Image as PILImage, ImageDraw, ImageFont
        with PILImage.open(io.BytesIO(data)) as source:
            image = source.convert("RGB")
            width, height = image.size
            footer_top = height - max(1, round(height * 0.18))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, footer_top, width, height), fill=(96, 96, 96))

            def font(size: int, *, bold: bool = False):
                face = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
                candidates = (
                    face,
                    f"/usr/share/fonts/truetype/dejavu/{face}",
                    "/System/Library/Fonts/Supplemental/Verdana Bold.ttf"
                    if bold else "/System/Library/Fonts/Supplemental/Verdana.ttf",
                )
                for candidate in candidates:
                    try:
                        return ImageFont.truetype(candidate, size)
                    except OSError:
                        continue
                return ImageFont.load_default(size=size)

            def centered(text: str, y: int, text_font) -> None:
                box = draw.textbbox((0, 0), text, font=text_font)
                draw.text(((width - (box[2] - box[0])) // 2, y), text, fill="white", font=text_font)

            title_font = font(max(12, round(height * 0.06)), bold=True)
            site_font = font(max(10, round(height * 0.035)))
            title_box = draw.textbbox((0, 0), "VIP Seva", font=title_font)
            site_box = draw.textbbox((0, 0), "www.vipseva.com", font=site_font)
            footer_height = height - footer_top
            content_height = (title_box[3] - title_box[1]) + (site_box[3] - site_box[1]) + max(4, height // 100)
            first_y = footer_top + max(0, (footer_height - content_height) // 2)
            centered("VIP Seva", first_y, title_font)
            centered("www.vipseva.com", first_y + (title_box[3] - title_box[1]) + max(4, height // 100), site_font)

            out = io.BytesIO()
            image.save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue()
    except Exception:
        # Validation already protects production inputs. Keep test doubles and
        # environments without Pillow backward compatible.
        return data


def run_image(
    container: Container, git: LocalGitRepository, on_date: date, *, render_pages: bool = True
) -> int:
    """Fetch/store a day's images, optionally regenerating subscriber pages."""
    path = container.image_service.canonical_path(on_date)
    candidates: list[Image] = []
    try:
        # Each image workflow run asks its remote source chain for a fresh
        # image set. A dated asset is never fabricated from the fallback.
        candidates = container.image_service.collect_daily_images(on_date)
        image = container.image_service.select_largest(candidates)
    except AllSourcesFailed as exc:
        fallback = container.config.get("delivery", {}).get("fallback_image", "fallback.jpg")
        fallback_path = f"{container.config['paths']['images_dir']}/{fallback}"
        fallback_bytes = git.read_file(fallback_path)
        candidate = Image(on_date, fallback_bytes or b"", source="local_fallback")
        if not fallback_bytes or not container.image_validator.validate_fallback(candidate):
            container.logs.log("IMAGE_FETCH_FAILED", details=str(exc))
            print(f"[image] FAILED: {exc}; local fallback invalid or missing", file=sys.stderr)
            return 1
        container.logs.log("IMAGE_FALLBACK_AVAILABLE", details=f"{fallback_path}:{exc}")
        print(f"[image] remote sources failed; no dated image written; fallback remains {fallback_path}")
        image = None

    committed: list[str] = []
    if image is not None:
        for candidate in candidates:
            candidate_path = container.image_service.candidate_path(on_date, candidate.source)
            git.write_file(candidate_path, _canonical_jpeg(candidate.data),
                           f"Add {candidate.source} darshan image {on_date.isoformat()}")
            committed.append(candidate_path)
        git.write_file(path, _canonical_jpeg(image.data), f"Add daily darshan image {on_date.isoformat()}")
        committed.append(path)
        print(f"[image] stored {len(candidates)} candidate(s); {path} uses source={image.source}")
    else:
        page_action = "refreshing pages only" if render_pages else "leaving subscriber pages unchanged"
        print(f"[image] no remote image stored at {path}; {page_action}.")

    # Normal daily runs always regenerate pages. Backfill uses image-only mode
    # for historic dates, avoiding six needless rewrites of the same pages.
    pages = _render_pages(container, on_date) if render_pages else []
    committed.extend(pages)

    # Retention: keep only the newest N dated images plus the fallback, and
    # delete the rest so the repo does not grow unbounded (section 10/11).
    delivery_cfg = container.config.get("delivery", {})
    keep = int(delivery_cfg.get("image_retention_days", 7))
    fallback_name = delivery_cfg.get("fallback_image", "fallback.jpg")
    removed = container.image_service.prune_images(
        keep=keep, fallback_name=fallback_name, root=container.root
    )
    if removed:
        # git add on a deleted path stages the deletion; commit() picks it up.
        committed.extend(removed)
        container.logs.log("IMAGES_PRUNED", details=f"removed={len(removed)} kept<= {keep}")
        print(f"[image] pruned {len(removed)} old image(s), keeping newest {keep} + fallback")

    # Persist collection success/failure and fallback availability, even when
    # the page and image bytes themselves were unchanged.
    logs_path = container.config["paths"].get("logs_csv")
    if logs_path:
        committed.append(logs_path)
    if committed:
        git.commit(committed, f"Daily darshan image + pages {on_date.isoformat()}")
    print(f"[image] pages={len(pages)}")
    return 0


def run_pages(container: Container, git: LocalGitRepository, on_date: date) -> int:
    """Regenerate subscriber pages from an existing canonical image only.

    This intentionally does not construct an image collector or fetch any
    remote source. A manual Actions run can therefore repair pages without
    changing today's selected darshan image.
    """
    image_path = container.image_service.canonical_path(on_date)
    image_bytes = git.read_file(image_path)
    image = Image(on_date, image_bytes or b"", source="stored_canonical")
    if not image_bytes or not container.image_validator.validate(image):
        print(f"[pages] FAILED: no valid stored image at {image_path}", file=sys.stderr)
        return 1

    pages = _render_pages(container, on_date)
    container.logs.log("PAGES_REGENERATED", details=f"{on_date.isoformat()}:count={len(pages)}")
    committed = list(pages)
    logs_path = container.config["paths"].get("logs_csv")
    if logs_path:
        committed.append(logs_path)
    if committed:
        git.commit(committed, f"Regenerate daily pages {on_date.isoformat()}")
    print(f"[pages] regenerated={len(pages)} image={image_path}")
    return 0


def run_delivery(container: Container, git: LocalGitRepository, on_date: date) -> int:
    mode = container.config.get("delivery", {}).get("mode", "image")
    if mode == "utility_template":
        # Template mode: the image lives on the per-subscriber page; the WhatsApp
        # message is a utility template carrying that page's URL. No bytes needed.
        report = container.delivery_service.deliver(on_date)
    else:
        image_url = _image_public_url(container.config, on_date)
        # Prefer sending the actual image bytes via Meta media upload so delivery
        # works even for a private repo (fix #6); fall back to the public URL.
        image_path = container.image_service.canonical_path(on_date)
        image_bytes = git.read_file(image_path)
        if not image_bytes:
            fallback = container.config.get("delivery", {}).get("fallback_image", "fallback.jpg")
            fallback_path = f"{container.config['paths']['images_dir']}/{fallback}"
            fallback_bytes = git.read_file(fallback_path)
            fallback_image = Image(on_date, fallback_bytes or b"", source="local_fallback")
            if not fallback_bytes or not container.image_validator.validate_fallback(fallback_image):
                print(f"[delivery] FAILED: local fallback invalid or missing at {fallback_path}", file=sys.stderr)
                return 1
            image_bytes = fallback_bytes
            image_url = _fallback_public_url(container.config)
            container.logs.log("DELIVERY_FALLBACK_USED", details=fallback_path)
        report = container.delivery_service.deliver(on_date, image_url, image_bytes)
    sentlog_path = container.config["paths"]["sentlog_csv"]
    git.commit([sentlog_path, container.config["paths"]["logs_csv"]],
               f"Daily delivery {on_date.isoformat()}")
    print(f"[delivery] sent={report.sent} skipped={report.skipped} failed={report.failed}")
    return 1 if report.failed and not report.sent else 0


def run_renewal(container: Container, git: LocalGitRepository, on_date: date) -> int:
    report = container.renewal_service.run(on_date)
    git.commit([container.config["paths"]["renewals_csv"], container.config["paths"]["logs_csv"]],
               f"Renewal reminders {on_date.isoformat()}")
    print(f"[renewal] sent={report.sent} skipped={report.skipped} failed={report.failed}")
    return 0


def run_expiry_sweep(container: Container, git: LocalGitRepository, on_date: date) -> int:
    """Flip ACTIVE subscribers past their end_date to EXPIRED in storage, then
    prune per-subscriber pages for subscribers inactive beyond a grace period."""
    expired = container.subscriber_service.sweep_expired(on_date)

    commit_paths = [
        container.config["paths"]["subscribers_csv"],
        container.config["paths"]["logs_csv"],
    ]

    # Page retention with grace: keep pages for ACTIVE subscribers and for
    # subscribers whose end_date is within `page_retention_grace_days` of today
    # (recently expired/cancelled -> keep the link alive for late openers).
    # Prune only pages for subscribers inactive beyond that window. Runs after
    # the sweep so today's expirations are evaluated against the grace period.
    grace = int(container.config.get("delivery", {}).get("page_retention_grace_days", 7))
    removed = container.page_renderer.prune_pages(
        container.subscribers.all(), on_date, grace_days=grace, root=container.root
    )
    if removed:
        commit_paths.extend(removed)
        container.logs.log("PAGES_PRUNED", details=f"removed={len(removed)} grace_days={grace}")
        print(f"[expiry] pruned {len(removed)} inactive page(s) (grace {grace}d)")

    git.commit(commit_paths, f"Expiry sweep {on_date.isoformat()}")
    print(f"[expiry] expired={len(expired)}")
    return 0


def run_keepalive(container: Container, interval_seconds: int = 300) -> int:
    """Self-ping the public health endpoint to prevent free-plan spin-down.

    Render free web services sleep after ~15 min idle and can be swept. Pinging
    /health every 5 min keeps the instance warm. Intended to run as a long-lived
    process (an always-on worker or cron runner), NOT as part of ``all`` which
    must exit.

    Reads RENDER_HEALTHCHECK_URL, e.g.
    https://daily-darshan-webhook.onrender.com/health
    """
    import os
    import time
    import urllib.request

    url = os.environ.get("RENDER_HEALTHCHECK_URL", "").strip()
    if not url:
        print("[keepalive] RENDER_HEALTHCHECK_URL not set; nothing to ping", file=sys.stderr)
        return 1

    print(f"[keepalive] pinging {url} every {interval_seconds}s")
    while True:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 - fixed https URL from env
                status = resp.getcode()
            container.logs.log("KEEPALIVE_PING", details=f"{url} -> {status}")
            print(f"[keepalive] {url} -> {status}")
        except Exception as exc:  # noqa: BLE001 - keep looping through transient errors
            container.logs.log("KEEPALIVE_FAILED", details=str(exc))
            print(f"[keepalive] FAILED: {exc}", file=sys.stderr)
        time.sleep(interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Daily Darshan scheduler")
    parser.add_argument("job", choices=["image", "image-only", "pages", "delivery", "renewal", "expiry", "keepalive", "all"])
    parser.add_argument("--date", help="ISO date override (YYYY-MM-DD)", default=None)
    args = parser.parse_args(argv)

    on_date = date.fromisoformat(args.date) if args.date else datetime.now(ZoneInfo("Asia/Kolkata")).date()
    container = Container()
    git = LocalGitRepository(root=container.root)

    # keepalive is a long-lived loop, not a one-shot batch step; handle and return.
    if args.job == "keepalive":
        return run_keepalive(container)

    rc = 0
    if args.job in ("image", "all"):
        rc |= run_image(container, git, on_date)
    if args.job == "image-only":
        rc |= run_image(container, git, on_date, render_pages=False)
    if args.job == "pages":
        rc |= run_pages(container, git, on_date)
    # Expiry sweep runs before renewal/delivery so downstream steps see accurate
    # EXPIRED status (eligibility is date-gated regardless, but this keeps the
    # stored status truthful for reminders, reports and admin views).
    if args.job in ("expiry", "all"):
        rc |= run_expiry_sweep(container, git, on_date)
    if args.job in ("renewal", "all"):
        rc |= run_renewal(container, git, on_date)
    if args.job in ("delivery", "all"):
        rc |= run_delivery(container, git, on_date)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
