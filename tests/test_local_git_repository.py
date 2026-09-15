"""Tests for staging generated files in the Actions checkout."""
from __future__ import annotations

from adapters.github import LocalGitRepository
import subprocess
import pytest


def test_concurrent_log_appends_preserve_both_writers(tmp_path, monkeypatch):
    def git(root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout

    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    first = tmp_path / "first"
    git(tmp_path, "clone", str(remote), str(first))
    git(first, "config", "user.name", "Test")
    git(first, "config", "user.email", "test@example.com")
    git(first, "config", "commit.gpgsign", "false")
    (first / "csv").mkdir()
    (first / "csv/logs.csv").write_text("timestamp,event\n0,initial\n")
    git(first, "add", ".")
    git(first, "commit", "-m", "base")
    git(first, "push", "origin", "HEAD")
    second = tmp_path / "second"
    git(tmp_path, "clone", str(remote), str(second))
    monkeypatch.setenv("GIT_COMMIT_GPG_SIGN", "false")
    (first / "csv/logs.csv").write_text("timestamp,event\n0,initial\n1,remote\n")
    LocalGitRepository(str(first)).commit(["csv/logs.csv"], "remote")
    (second / "csv/logs.csv").write_text("timestamp,event\n0,initial\n2,image\n")
    git(second, "config", "user.name", "Test")
    git(second, "config", "user.email", "test@example.com")
    git(second, "config", "commit.gpgsign", "false")
    LocalGitRepository(str(second)).commit(["csv/logs.csv"], "image")
    assert (second / "csv/logs.csv").read_text() == "timestamp,event\n0,initial\n1,remote\n2,image\n"
    assert git(second, "status", "--porcelain") == ""
    assert git(second, "rev-parse", "HEAD") == git(remote, "rev-parse", "HEAD")


@pytest.mark.parametrize("conflicts", ["csv/subscribers.csv\n", "csv/logs.csv\ncsv/payments.csv\n"])
def test_business_conflicts_are_never_auto_resolved(tmp_path, monkeypatch, conflicts):
    repository = LocalGitRepository(str(tmp_path))
    monkeypatch.setattr(repository, "_git", lambda *a, **kw: conflicts)
    with pytest.raises(RuntimeError, match="requires reconciliation"):
        repository._resolve_append_only_logs()


def test_log_deletions_are_not_restored_by_merge(tmp_path, monkeypatch):
    repository = LocalGitRepository(str(tmp_path))
    responses = iter(["csv/logs.csv\n", "header\nold\n", "header\n", "header\nold\nnew\n"])
    monkeypatch.setattr(repository, "_git", lambda *a, **kw: next(responses))
    with pytest.raises(RuntimeError, match="edits/deletions"):
        repository._resolve_append_only_logs()


@pytest.mark.parametrize("reason,expected_pushes", [("[rejected] (fetch first)", 5), ("permission denied", 1)])
def test_push_recovery_is_bounded_and_only_retries_collisions(tmp_path, monkeypatch, reason, expected_pushes):
    repository = LocalGitRepository(str(tmp_path))
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if args == ("push",):
            raise subprocess.CalledProcessError(1, ["git", "push"], stderr=reason)
        return "csv/logs.csv\n"
    monkeypatch.setattr(repository, "_stage_files", lambda files: None)
    monkeypatch.setattr(repository, "_git", run)
    monkeypatch.setattr("adapters.github.time.sleep", lambda _: None)
    monkeypatch.setattr("adapters.github.subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    with pytest.raises(subprocess.CalledProcessError):
        repository.commit(["csv/logs.csv"], "test")
    assert calls.count(("push",)) == expected_pushes
    assert calls.count(("fetch", "origin")) == expected_pushes - 1


def test_git_diagnostic_redacts_authenticated_urls():
    from adapters.github import GitCommandError
    error = GitCommandError(1, ["git", "push"], stderr="fatal: https://secret@github.com/repo rejected")
    assert "secret" not in str(error)
    assert "rejected" in str(error)


def test_actions_force_adds_docs_but_not_other_paths(monkeypatch, tmp_path):
    repository = LocalGitRepository(root=str(tmp_path))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setattr(
        repository,
        "_git",
        lambda *args, **_kwargs: calls.append(args) or "",
    )

    repository._stage_files(["docs/images/2026-08-27.jpg", "csv/logs.csv"])

    assert calls == [
        ("add", "-f", "docs/images/2026-08-27.jpg"),
        ("add", "csv/logs.csv"),
    ]


def test_local_commit_respects_docs_ignore_rule(monkeypatch, tmp_path):
    repository = LocalGitRepository(root=str(tmp_path))
    calls: list[tuple[str, ...]] = []
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        repository,
        "_git",
        lambda *args, **_kwargs: calls.append(args) or "",
    )

    repository._stage_files(["docs/images/2026-08-27.jpg"])

    assert calls == [("add", "docs/images/2026-08-27.jpg")]


def test_commit_ignores_unstaged_changes_outside_requested_files(monkeypatch, tmp_path):
    repository = LocalGitRepository(root=str(tmp_path))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(repository, "_stage_files", lambda _files: None)
    monkeypatch.setattr(
        repository,
        "_git",
        lambda *args, **_kwargs: calls.append(args) or "",
    )

    repository.commit(["docs/subscriber.html"], "No-op image refresh")

    assert calls == [
        ("diff", "--cached", "--name-only", "--", "docs/subscriber.html"),
    ]
