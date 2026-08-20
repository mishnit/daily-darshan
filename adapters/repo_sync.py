"""Durable persistence sync for the webhook (P0 fix #6).

Problem: the webhook (`main.py`) runs on an ephemeral, single-instance host
(Render/Fly) and writes CSVs to the container's local disk. Those writes are
never pushed to the shared GitHub repo, so:
  - the scheduler/admin (which read the repo) never see new subscribers/UTRs, and
  - the local writes are lost on the next restart/redeploy/cold-start.

Fix: back the webhook's CSV files with the GitHub repo as the source of truth.
Before handling a message we PULL the latest CSVs from the repo into local
disk; after handling we PUSH the changed CSVs back via the Contents API. This
keeps the webhook and scheduler on one shared store.

Concurrency (see README "Coordination"): the scheduler (GitHub Actions) and the
webhook (Render) both write CSVs on `main`. To avoid the webhook pushing on top
of a nightly job mid-run, ``push`` observes a configurable *quiet window* (UTC)
that brackets the image/expiry/renewal/delivery jobs. During that window pushes
are deferred (buffered on local disk) and flushed on the next push after the
window closes. Pulls are always allowed so the webhook keeps reading fresh state.

Enabled only when a GitHub token + repo are configured (production webhook).
In local/dev and inside GitHub Actions (where the scheduler commits via git
directly), this is a no-op so existing behaviour and tests are unchanged.
"""
from __future__ import annotations

import os
from datetime import datetime, time, timezone

from application.ports.storage import GitHubRepositoryPort


def _parse_hhmm(value: str) -> time | None:
    """Parse a 'HH:MM' UTC string into a time; return None if empty/invalid."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        hh, mm = value.split(":", 1)
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, TypeError):
        return None


def _in_window(now: time, start: time, end: time) -> bool:
    """True if `now` falls in [start, end], supporting windows crossing midnight."""
    if start <= end:
        return start <= now <= end
    # Window wraps past midnight (e.g. 23:50 -> 00:10).
    return now >= start or now <= end


class RepoSync:
    """Pull-before / push-after sync of a fixed set of repo-relative files.

    A push during the configured quiet window is deferred: the caller's local
    writes are already on disk, so the next post-window push flushes them.
    """

    def __init__(
        self,
        github: GitHubRepositoryPort | None,
        root: str,
        tracked_files: list[str],
        enabled: bool,
        quiet_window: tuple[str, str] | None = None,
        clock=None,
    ):
        self._github = github
        self._root = root
        self._tracked = tracked_files
        self.enabled = enabled and github is not None
        # Quiet window (UTC 'HH:MM' strings). When both bounds parse, pushes are
        # deferred while now is inside [start, end].
        start, end = quiet_window or ("", "")
        self._quiet_start = _parse_hhmm(start)
        self._quiet_end = _parse_hhmm(end)
        # Injectable clock for tests; must return a tz-aware or UTC datetime.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _abs(self, rel: str) -> str:
        return os.path.join(self._root, rel)

    def in_quiet_window(self) -> bool:
        """True if the current UTC time is inside the configured quiet window."""
        if self._quiet_start is None or self._quiet_end is None:
            return False
        now = self._clock()
        now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        return _in_window(now_utc.timetz().replace(tzinfo=None), self._quiet_start, self._quiet_end)

    def pull(self) -> None:
        """Overwrite local tracked files with the repo's latest content.

        A file missing in the repo is left as-is locally (the local header-only
        file from CSVRepository init is a valid empty state).
        """
        if not self.enabled:
            return
        for rel in self._tracked:
            try:
                content = self._github.read_file(rel)
            except Exception:
                # Never let a transient read failure break request handling;
                # fall back to whatever is on local disk.
                continue
            if content is None:
                continue
            full = self._abs(rel)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(content)

    def push(self, message: str) -> list[str]:
        """Push local tracked files back to the repo. Returns files pushed.

        Deferred (returns []) while inside the quiet window, so the webhook does
        not write on top of an in-flight scheduler job. The local writes remain
        on disk and are flushed by the next push after the window closes.
        """
        if not self.enabled:
            return []
        if self.in_quiet_window():
            # Defer: local disk already has the latest rows; a later push (or the
            # next request outside the window) will flush them to the repo.
            return []
        pushed: list[str] = []
        for rel in self._tracked:
            full = self._abs(rel)
            if not os.path.exists(full):
                continue
            with open(full, "rb") as fh:
                content = fh.read()
            try:
                self._github.write_file(rel, content, message)
                pushed.append(rel)
            except Exception:
                # Best-effort per file; a failure here is logged by the caller.
                # The local write already succeeded, so we don't lose the row
                # within this process's lifetime; a later push retries it.
                continue
        return pushed
