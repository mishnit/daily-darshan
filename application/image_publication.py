"""Publish images and freshly rendered pages using bounded optimistic retries."""
from __future__ import annotations

import os
from pathlib import Path
import random
import subprocess
import tempfile
import time

from adapters.github import LocalGitRepository
from config import Container, load_config


class PublicationFiles(LocalGitRepository):
    """Accumulate one transaction, excluding the separate runner audit log."""

    def __init__(self, root):
        super().__init__(root)
        self.files = set()

    def commit(self, files, message):
        self.files.update(path for path in files if not os.path.isabs(path))


def publish_image(root, on_date, *, render_pages=True, attempts=5, regenerate_only=False,
                  image_source="canonical", force_recollect=False):
    from scheduler import run_image, run_pages, run_expiry_sweep
    from application.image_approval import ready, required, approved

    if not 1 <= attempts <= 5:
        raise ValueError("Image publication allows one to five attempts")
    repository = LocalGitRepository(root)
    # Audit output lives outside the transaction and survives failed attempts.
    audit_dir = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "darshan-image-audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    audit_path = audit_dir / f"{on_date.isoformat()}.csv"
    cached = None
    for attempt in range(attempts):
        repository._git("fetch", "origin", "main")
        base = repository._git("rev-parse", "FETCH_HEAD", capture=True).strip()
        with tempfile.TemporaryDirectory(prefix="darshan-publication-") as temp:
            checkout = os.path.join(temp, "checkout")
            repository._git("worktree", "add", "--detach", checkout, base)
            try:
                config = load_config(os.path.join(checkout, "config.json"))
                config["paths"]["logs_csv"] = str(audit_path)
                container = Container(config=config, root=checkout)
                already_approved = required(config) and approved(container, on_date) and not force_recollect
                if cached is None and not regenerate_only and not already_approved:
                    # Network collection happens once, before any publication retry.
                    cached = container.image_service.collect_daily_images(on_date)
                container.image_service.collect_daily_images = lambda date: cached
                transaction = PublicationFiles(checkout)
                result = (run_pages(container, transaction, on_date, image_source=image_source)
                          if regenerate_only else run_image(
                              container, transaction, on_date, render_pages=render_pages,
                              force_recollect=force_recollect,
                          ))
                if result:
                    return result
                # A manual historical Pages refresh must be presentation-only:
                # evaluating expiry against yesterday (or an older selected
                # image date) can postpone real expirations and prune the
                # wrong subscriber pages.  Today's normal render retains the
                # pre-publication expiry sweep.
                from domain.clock import today_ist
                if (render_pages and ready(container, on_date)
                        and (not regenerate_only or on_date == today_ist())):
                    run_expiry_sweep(container, transaction, on_date)
                transaction._stage_files(sorted(transaction.files))
                if not transaction._git("diff", "--cached", "--name-only", capture=True).strip():
                    repository._git("fetch", "origin", "main")
                    if repository._git("rev-parse", "FETCH_HEAD", capture=True).strip() == base:
                        return 0
                    if attempt == attempts - 1:
                        raise RuntimeError("main kept advancing during no-op image publication")
                    time.sleep(random.uniform(0.2, 0.5))
                    continue
                transaction._git("commit", "-m", f"Daily darshan image + pages {on_date.isoformat()}")
                try:
                    transaction._git("push", "origin", "HEAD:refs/heads/main")
                    return 0
                except subprocess.CalledProcessError as exc:
                    reason = (exc.stderr or "").lower()
                    if attempt == attempts - 1 or not any(
                        marker in reason for marker in ("fetch first", "non-fast-forward")
                    ):
                        raise
                    print(f"[image] main advanced; rebuilding from fresh CSVs (attempt {attempt + 2}/{attempts})")
            finally:
                # Only this disposable checkout is removed. Original checkout and
                # all remote commits remain untouched, including on failure.
                repository._git("worktree", "remove", "--force", checkout)
        time.sleep(random.uniform(0.2, min(2.0, 0.5 * (attempt + 1))))
    raise RuntimeError("Image publication exhausted retries")
