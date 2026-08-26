"""Scheduler orchestration entrypoint (Tech Doc sections 11, 12, 26, 31).

Jobs (invoked by GitHub Actions):
  - image     : fetch + validate + store today's canonical image + pages, commit
  - expiry    : flip ACTIVE subscribers past end_date to EXPIRED, commit
  - renewal   : send renewal reminders, then commit renewals log
  - delivery  : send today's darshan to eligible subscribers, then commit sentlog
  - keepalive : self-ping RENDER_HEALTHCHECK_URL every 5 min (free-plan warm-up)

Usage:
    python scheduler.py image
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


def _render_pages(container: Container, on_date: date) -> list[str]:
    """Generate per-subscriber static pages for on_date. Returns paths written."""
    return container.page_renderer.write_all(
        container.subscribers.all(), on_date, delivered=True,
        images_dir=container.config["paths"]["images_dir"], root=container.root,
    )


def _canonical_jpeg(data: bytes) -> bytes:
    """Normalize decoded remote images to the .jpg canonical storage format."""
    try:
        import io
        from PIL import Image as PILImage
        with PILImage.open(io.BytesIO(data)) as source:
            out = io.BytesIO()
            source.convert("RGB").save(out, format="JPEG", quality=90, optimize=True)
            return out.getvalue()
    except Exception:
        # Validation already protects production inputs. Keep test doubles and
        # environments without Pillow backward compatible.
        return data


def run_image(container: Container, git: LocalGitRepository, on_date: date) -> int:
    path = container.image_service.canonical_path(on_date)
    existing = git.read_file(path)
    try:
        image = container.image_service.ensure_daily_image(on_date, existing)
    except AllSourcesFailed as exc:
        fallback = container.config.get("delivery", {}).get("fallback_image", "fallback.jpg")
        fallback_path = f"{container.config['paths']['images_dir']}/{fallback}"
        fallback_bytes = git.read_file(fallback_path)
        candidate = Image(on_date, fallback_bytes or b"", source="local_fallback")
        if not fallback_bytes or not container.image_validator.validate_fallback(candidate):
            container.logs.log("IMAGE_FETCH_FAILED", details=str(exc))
            print(f"[image] FAILED: {exc}; local fallback invalid or missing", file=sys.stderr)
            return 1
        container.logs.log("IMAGE_FALLBACK_USED", details=f"{fallback_path}:{exc}")
        print(f"[image] remote sources failed; using local fallback {fallback_path}")
        image = candidate

    committed: list[str] = []
    if image is not None:
        git.write_file(path, _canonical_jpeg(image.data), f"Add daily darshan image {on_date.isoformat()}")
        committed.append(path)
        print(f"[image] stored {path} from source={image.source}")
    else:
        print(f"[image] valid image already exists at {path}; refreshing pages only.")

    # Always (re)generate per-subscriber pages so subscribers who signed up since
    # the last run get a page even when today's image already exists.
    pages = _render_pages(container, on_date)
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

    if committed:
        git.commit(committed, f"Daily darshan image + pages {on_date.isoformat()}")
    print(f"[image] pages={len(pages)}")
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
    parser.add_argument("job", choices=["image", "delivery", "renewal", "expiry", "keepalive", "all"])
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
