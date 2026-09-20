"""Durable persistence sync for the webhook (P0 fix #6).

Problem: the webhook (`main.py`) runs on an ephemeral, single-instance host
(Render/Fly) and writes CSVs to the container's local disk. Those writes are
never pushed to the shared GitHub repo, so:
  - the scheduler/admin (which read the repo) never see new subscribers/UTRs, and
  - the local writes are lost on the next restart/redeploy/cold-start.

Fix: back the webhook's CSV files with the GitHub repo as the source of truth.
Before handling a message we PULL the latest CSVs from the repo into local
disk; after handling we PUSH changed CSVs in an atomic Git Data API commit. This
keeps the webhook and scheduler on one shared store.

Strict webhook operations read one immutable snapshot and fail on any read/write
error. The caller responds 503 after restoring its local snapshot. Writes are
never delayed by a time window; conflicts fail and are retried safely.

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
        # Deferred/failed writes must survive the next request's pull.
        self._dirty: set[str] = set()
        self._baseline: dict[str, bytes | None] = {}
        self._snapshot_ready = False

    def _abs(self, rel: str) -> str:
        return os.path.join(self._root, rel)

    def pull(self, strict: bool = False) -> None:
        """Overwrite local tracked files with the repo's latest content.

        A file missing in the repo is left as-is locally (the local header-only
        file from CSVRepository init is a valid empty state).
        """
        if not self.enabled:
            return
        if strict and self._dirty:
            raise RuntimeError("Unpersisted local state requires recovery")
        if hasattr(self._github, "begin_snapshot"):
            self._github.begin_snapshot()
        readable = [rel for rel in self._tracked if rel not in self._dirty]
        if (self._snapshot_ready and getattr(self._github, "snapshot_unchanged", False)
                and all(rel in self._baseline for rel in readable)):
            # This process already has the exact immutable branch snapshot.
            # Avoid re-downloading every CSV after our own previous commit.
            for rel in readable:
                content = self._baseline[rel]
                if content is not None:
                    with open(self._abs(rel), "wb") as output:
                        output.write(content)
            return
        self._snapshot_ready = False
        contents = None
        if hasattr(self._github, "read_files"):
            try:
                contents = self._github.read_files(readable)
            except Exception:
                if strict:
                    raise
                contents = None
        if strict and contents is None:
            # Finish every remote read before changing any local file.
            contents = {rel: self._github.read_file(rel) for rel in readable}
        for rel in readable:
            if rel in self._dirty:
                continue
            try:
                content = contents[rel] if contents is not None else self._github.read_file(rel)
            except Exception:
                if strict:
                    raise
                # Never let a transient read failure break request handling;
                # fall back to whatever is on local disk.
                continue
            if content is None:
                if strict:
                    full = self._abs(rel)
                    # A missing remote file must not resurrect stale local rows.
                    if os.path.exists(full):
                        with open(full, "rb") as source:
                            header = source.readline()
                        with open(full, "wb") as output:
                            output.write(header)
                        self._baseline[rel] = header
                else:
                    self._baseline[rel] = None
                continue
            full = self._abs(rel)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "wb") as fh:
                fh.write(content)
            self._baseline[rel] = content
        self._snapshot_ready = True

    def push(self, message: str, strict: bool = False) -> list[str]:
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
            if rel in self._baseline and content == self._baseline[rel] and rel not in self._dirty:
                continue
            try:
                self._github.write_file(rel, content, message)
                pushed.append(rel)
            except Exception:
                self._dirty.add(rel)
                if strict:
                    raise
                # Best-effort per file; a failure here is logged by the caller.
                # The local write already succeeded, so we don't lose the row
                # within this process's lifetime; a later push retries it.
                continue
        if not pushed:
            return []
        try:
            self._github.commit(pushed, message)
        except Exception:
            self._dirty.update(pushed)
            if strict:
                raise
            # Retain local dirty files, but discard the failed Git transaction
            # so the next payment refresh can start from a fresh branch head.
            if hasattr(self._github, "discard_pending"):
                self._github.discard_pending()
            return []
        self._dirty.difference_update(pushed)
        for rel in pushed:
            with open(self._abs(rel), "rb") as source:
                self._baseline[rel] = source.read()
        return pushed

    def read_latest(self, rel: str) -> tuple[bytes | None, bytes | None]:
        """Return the previous baseline and this file at the latest head."""
        if not self.enabled:
            return None, None
        previous = self._baseline.get(rel)
        if hasattr(self._github, "begin_snapshot"):
            self._github.begin_snapshot()
        remote = self._github.read_file(rel)
        self._baseline[rel] = remote
        self._snapshot_ready = True
        return previous, remote

    def abort(self):
        """Caller restored its snapshot; discard the abandoned transaction."""
        self._dirty.clear()
        self._snapshot_ready = False
        if hasattr(self._github, "discard_pending"):
            self._github.discard_pending()
