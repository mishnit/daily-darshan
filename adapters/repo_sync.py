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

Enabled only when a GitHub token + repo are configured (production webhook).
In local/dev and inside GitHub Actions (where the scheduler commits via git
directly), this is a no-op so existing behaviour and tests are unchanged.
"""
from __future__ import annotations

import os

from application.ports.storage import GitHubRepositoryPort


class RepoSync:
    """Pull-before / push-after sync of a fixed set of repo-relative files."""

    def __init__(
        self,
        github: GitHubRepositoryPort | None,
        root: str,
        tracked_files: list[str],
        enabled: bool,
    ):
        self._github = github
        self._root = root
        self._tracked = tracked_files
        self.enabled = enabled and github is not None

    def _abs(self, rel: str) -> str:
        return os.path.join(self._root, rel)

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
        """Push local tracked files back to the repo. Returns files pushed."""
        if not self.enabled:
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
