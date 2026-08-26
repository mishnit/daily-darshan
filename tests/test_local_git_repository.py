"""Tests for staging generated files in the Actions checkout."""
from __future__ import annotations

from adapters.github import LocalGitRepository


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
