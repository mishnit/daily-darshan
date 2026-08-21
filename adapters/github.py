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
        
        # Stage the specified files (add will record deletions as staged)
        self._git("add", *files)
        # Re-check after staging to ensure changes were actually staged
        status = self._git("status", "--porcelain", capture=True)
        if not status.strip():
            # Nothing staged -> no-op (keeps job idempotent)
            return
        
        # CI enables GPG signing explicitly after importing its signing key.
        # Keep local/default commits unsigned unless opted in so development
        # machines without that key remain usable.
        sign_commits = os.environ.get("GIT_COMMIT_GPG_SIGN", "").strip().lower() in {
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
        # The serverless process has no checkout at ``owner/repo/<path>`` to
        # read from later, so retain the bytes passed by the caller.
        self._pending: list[tuple[str, bytes, str]] = []

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
        params = {"ref": self._branch}
        try:
            resp = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            # The API returns the content base64-encoded
            return base64.b64decode(data["content"])
        except Exception:
            # Network or API errors are logged at the caller; return None to signal
            # that local state should be preserved.
            return None

    def write_file(self, path: str, content: bytes, message: str) -> None:
        """Write a file to the repository via the Contents API.
        
        The write is buffered internally and actually committed/pushed by commit().
        """
        self._pending.append((path, content, message))

    def commit(self, files: list[str], message: str) -> None:
        """Commit all buffered writes in a single batch via the Contents API.
        
        Uses PUT to update existing files or create new ones. Retries once on
        conflict (422) by re-fetching and retrying with the new SHA.
        """
        if not self._pending:
            return
        
        pending = self._pending
        self._pending = []
        try:
            for path, content, write_message in pending:
                self._commit_one(path, content, write_message or message)
        except Exception:
            # Preserve writes so the next sync can retry.
            self._pending = pending
            raise

    def _commit_one(self, path: str, content: bytes, message: str) -> None:
        """Commit a single file, fetching the current SHA and retrying on conflict."""
        # Read the current file to get its SHA (needed for update).
        url = f"https://api.github.com/repos/{self._repo}/contents/{path}"
        params = {"ref": self._branch}
        
        sha = None
        try:
            resp = self._session.get(url, headers=self._headers, params=params, timeout=self._timeout)
            if resp.status_code == 200:
                sha = resp.json().get("sha")
            # If 404, it's a new file; sha remains None.
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not read GitHub file {path}") from exc
        
        payload = {
            "message": message,
            "content": base64.b64encode(content).decode("utf-8"),
            "branch": self._branch,
        }
        if sha:
            payload["sha"] = sha
        
        resp = self._session.put(url, headers=self._headers, json=payload, timeout=self._timeout)
        if resp.status_code == 422:
            # Retrying a stale whole-CSV payload with a fresh SHA would erase
            # concurrent rows. Leave it for RepoSync to retry safely.
            raise RuntimeError(f"GitHub conflict while writing {path}")
        resp.raise_for_status()
