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

import csv
import io
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
        preserve_remote_csv_keys: dict[str, tuple[str, ...]] | None = None,
        preserve_remote_csv_status_ranks: dict[str, dict[str, int]] | None = None,
    ):
        self._github = github
        self._root = root
        self._tracked = tracked_files
        self.enabled = enabled and github is not None
        # Deferred/failed writes must survive the next request's pull.
        self._dirty: set[str] = set()
        self._baseline: dict[str, bytes | None] = {}
        self._snapshot_ready = False
        # Some ledgers are append/update-only for this service. A periodic
        # snapshot must not interpret absence from stale process memory as
        # authority to delete rows at the accepted remote baseline. GitHub
        # Actions remains the retention authority: rows already pruned from
        # that baseline are not restored.
        self._preserve_remote_csv_keys = preserve_remote_csv_keys or {}
        self._preserve_remote_csv_status_ranks = preserve_remote_csv_status_ranks or {}

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
            content = self._restore_preserved_remote_rows(rel, content)
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

    def _restore_preserved_remote_rows(self, rel: str, content: bytes) -> bytes:
        """Restore remotely present keyed rows omitted by stale local memory.

        Semantic reconciliation normally updates the canonical in-memory
        repository before it is flushed.  This is the final persistence
        barrier: even if another stale cache rewrites the file between merge
        and push, an accepted remote review or contact-slot row cannot be
        deleted by a periodic Render snapshot.
        """
        key_fields = self._preserve_remote_csv_keys.get(rel)
        baseline = self._baseline.get(rel)
        if not key_fields or not baseline:
            return content
        try:
            remote_reader = csv.DictReader(io.StringIO(baseline.decode("utf-8")), escapechar="\\")
            local_reader = csv.DictReader(io.StringIO(content.decode("utf-8")), escapechar="\\")
            remote_rows = list(remote_reader)
            local_rows = list(local_reader)
            fieldnames = local_reader.fieldnames or remote_reader.fieldnames
            if not fieldnames or any(field not in fieldnames for field in key_fields):
                raise RuntimeError(f"Protected CSV has no usable key columns: {rel}")
            identity = lambda row: tuple(row.get(field, "") for field in key_fields)
            local_by_key = {identity(row): row for row in local_rows}
            ranks = self._preserve_remote_csv_status_ranks.get(rel, {})
            missing = []
            restored_status = False
            for remote_row in remote_rows:
                key = identity(remote_row)
                local_row = local_by_key.get(key)
                if local_row is None:
                    missing.append(remote_row)
                elif ranks.get(remote_row.get("status", ""), 0) > ranks.get(
                    local_row.get("status", ""), 0
                ):
                    # Keep the status and its provider/approval metadata together.
                    local_row.update(remote_row)
                    restored_status = True
        except (UnicodeDecodeError, csv.Error) as exc:
            # Fail closed: malformed local bytes must never bypass the row
            # preservation barrier and become an authoritative deletion.
            raise RuntimeError(f"Cannot validate protected CSV before push: {rel}") from exc
        if not missing and not restored_status:
            return content
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=fieldnames, escapechar="\\")
        writer.writeheader()
        writer.writerows(local_rows + missing)
        restored = output.getvalue().encode("utf-8")
        with open(self._abs(rel), "wb") as target:
            target.write(restored)
        return restored

    def read_latest(self, rel: str) -> tuple[bytes | None, bytes | None]:
        """Return the previous baseline and this file at the latest head."""
        if not self.enabled:
            return None, None
        previous = self._baseline.get(rel)
        if hasattr(self._github, "begin_snapshot"):
            self._github.begin_snapshot()
        remote = self._github.read_file(rel)
        self._snapshot_ready = True
        return previous, remote

    def accept_remote(self, rel: str, content: bytes | None) -> None:
        """Advance one baseline only after its semantic merge succeeded."""
        self._baseline[rel] = content

    def snapshot_transaction(self):
        """Capture reconciliation bookkeeping before an atomic webhook action."""
        return set(self._dirty), dict(self._baseline), self._snapshot_ready

    def restore_transaction(self, snapshot) -> None:
        """Restore reconciliation bookkeeping and discard an abandoned Git write."""
        dirty, baseline, snapshot_ready = snapshot
        self._dirty = set(dirty)
        self._baseline = dict(baseline)
        self._snapshot_ready = bool(snapshot_ready)
        if hasattr(self._github, "discard_pending"):
            self._github.discard_pending()

    def abort(self):
        """Caller restored its snapshot; discard the abandoned transaction."""
        self._dirty.clear()
        self._snapshot_ready = False
        if hasattr(self._github, "discard_pending"):
            self._github.discard_pending()
