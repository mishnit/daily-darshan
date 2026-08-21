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
        author_name: str = "daily-darshan-bot",
        author_email: str = "bot@users.noreply.github.com",
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
        # Check if there are any changes before attempting to stage files
        status = self._git("status", "--porcelain", capture=True)
        if not status.strip():
            # No changes in the working directory; nothing to commit
            return
        
        self._git("add", *files)
        # Re-check after staging to ensure changes were actually staged
        status = self._git("status", "--porcelain", capture=True)
        if not status.strip():
            # Nothing staged -> no-op (keeps job idempotent)
            return
        
        self._git("-c", f"user.name={self._author_name}",
                  "-c", f"user.email={self._author_email}",
                  "commit", "-m", message)
        # Retry once from latest state on push conflict; never force-push.
        try:
            self._git("push")
        except subprocess.CalledProcessError:
            self._git("pull", "--rebase")
            self._git("push")

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
    """Contents-API writer for serverless environments."""

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
        self._pending: list[str] = []

    @property
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
        }
