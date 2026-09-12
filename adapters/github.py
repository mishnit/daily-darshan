"""GitHub persistence adapter (Tech Doc section 9).

Two implementations:
  - LocalGitRepository: operates on a local working copy using git CLI. Used
    inside GitHub Actions where the repo is already checked out.
  - GitHubApiRepository: uses the REST contents API (for serverless writers).

Recommended write sequence (section 9): read -> modify -> write temp ->
validate -> replace -> commit -> push. Never force-push.
"""
from __future__ import annotations

import base64
import os
import subprocess

import requests

from application.ports.storage import GitHubRepositoryPort


class LocalGitRepository(GitHubRepositoryPort):
    def __init__(
        self,
        root: str = ".",
        author_name: str = "Daily Darshan Automation",
        author_email: str = "geekymishnit@gmail.com",
    ):
        self._root = os.path.abspath(root)
        self._author_name = author_name
        self._author_email = author_email

    def _abs(self, path: str) -> str:
        return os.path.join(self._root, path)

    def read_file(self, path: str) -> bytes | None:
        full = self._abs(path)
        if not os.path.exists(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()

    def write_file(self, path: str, content: bytes, message: str) -> None:
        full = self._abs(path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(content)

    def commit(self, files: list[str], message: str) -> None:
        # Stage the specified files. GitHub Actions must force-add generated
        # documentation because /docs/ is intentionally ignored for local
        # contributors. Outside Actions, normal ignore rules remain in force.
        self._stage_files(files)
        # Do not mistake unrelated, unstaged changes (for example an audit-log
        # entry) for staged work. Git would reject a commit with no index diff.
        staged = self._git("diff", "--cached", "--name-only", "--", *files, capture=True)
        if not staged.strip():
            # Nothing staged -> no-op (keeps job idempotent)
            return
        
        # Signed commits are the default. A missing key is a deliberate hard
        # failure: silently producing an unsigned automation commit would
        # violate the repository's audit requirement.
        sign_commits = os.environ.get("GIT_COMMIT_GPG_SIGN", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }

        # Perform commit and handle the common "nothing to commit" outcome
        commit_cmd = [
            "git",
            "-c",
            f"user.name={self._author_name}",
            "-c",
            f"user.email={self._author_email}",
            "-c",
            f"commit.gpgsign={'true' if sign_commits else 'false'}",
            "commit",
            "-m",
            message,
        ]
        result = subprocess.run(
            commit_cmd,
            cwd=self._root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").lower()
            # Git returns exit code 1 with "nothing to commit" when no staged
            # changes are present. Treat this as a no-op rather than failing
            # the whole job.
            if "nothing to commit" in stderr or "no changes added to commit" in stderr:
                return
            # Otherwise re-raise with context so callers can handle it.
            raise subprocess.CalledProcessError(result.returncode, commit_cmd, output=result.stdout, stderr=result.stderr)

        # Retry once from latest state on push conflict; never force-push.
        try:
            self._git("push")
        except subprocess.CalledProcessError:
            self._git("pull", "--rebase")
            self._git("push")

    def _stage_files(self, files: list[str]) -> None:
        is_actions = os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"
        for path in files:
            if is_actions and (path == "docs" or path.startswith("docs/")):
                self._git("add", "-f", path)
            else:
                # ``add`` also stages deletions for tracked files.
                self._git("add", path)

    def _git(self, *args: str, capture: bool = False) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self._root,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout if capture else ""


class GitHubApiRepository(GitHubRepositoryPort):
    """Snapshot reads and atomic, non-force Git Data API transactions."""

    def __init__(
        self,
        repo: str | None = None,
        branch: str = "main",
        token: str | None = None,
        timeout: float = 15.0,
        session: requests.Session | None = None,
    ):
        self._repo = repo or os.environ.get("GITHUB_REPO", "")
        self._branch = branch
        self._token = token or os.environ.get("GITHUB_TOKEN", "")
        self._timeout = timeout
        self._session = session or requests.Session()
        # The serverless process has no checkout at ``owner/repo/<path>`` to
        # read from later, so retain the bytes passed by the caller.
        self._pending: list[tuple[str, bytes, str]] = []
        self._base_commit = None
        self._base_tree = None

    def _api(self, method, path, **kwargs):
        response = getattr(self._session, method)(
            f"https://api.github.com/repos/{self._repo}/{path}",
            headers=self._headers, timeout=self._timeout, **kwargs,
        )
        response.raise_for_status()
        return response.json()

    def begin_snapshot(self):
        """All reads and the eventual commit share one immutable parent."""
        if self._pending:
            raise RuntimeError("Uncommitted GitHub writes require reconciliation")
        head = self._api("get", f"git/ref/heads/{self._branch}")
        self._base_commit = head["object"]["sha"]
        commit = self._api("get", f"git/commits/{self._base_commit}")
        self._base_tree = commit["tree"]["sha"]

    def discard_pending(self):
        self._pending.clear()
        self._base_commit = self._base_tree = None

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }

    def read_file(self, path: str) -> bytes | None:
        """Fetch a file from the repository via the Contents API.
        
        Returns the file content as bytes, or None if the file does not exist.
        """
        url = f"https://api.github.com/repos/{self._repo}/contents/{path}"
        if self._base_commit is None:
            self.begin_snapshot()
        params = {"ref": self._base_commit}
        resp = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return base64.b64decode(resp.json()["content"])

    def write_file(self, path: str, content: bytes, message: str) -> None:
        """Buffer bytes for the next atomic Git tree commit."""
        self._pending.append((path, content, message))

    def commit(self, files: list[str], message: str) -> None:
        """Publish all files atomically; never transplant stale data onto a new head."""
        if not self._pending:
            return
        if self._base_commit is None:
            raise RuntimeError("Read a GitHub snapshot before writing")
        entries = {}
        for path, content, _ in self._pending:
            blob = self._api("post", "git/blobs", json={
                "content": base64.b64encode(content).decode(), "encoding": "base64",
            })
            entries[path] = {"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}
        tree = self._api("post", "git/trees", json={
            "base_tree": self._base_tree, "tree": list(entries.values()),
        })
        commit = self._api("post", "git/commits", json={
            "message": message, "tree": tree["sha"], "parents": [self._base_commit],
        })
        # A concurrent branch advance is not an ancestor of this commit, so a
        # non-force ref update fails rather than overwriting another writer.
        self._api("patch", f"git/refs/heads/{self._branch}", json={
            "sha": commit["sha"], "force": False,
        })
        self._pending.clear()
        self._base_commit, self._base_tree = commit["sha"], tree["sha"]
