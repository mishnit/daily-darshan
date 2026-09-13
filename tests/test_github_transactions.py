"""Git Data API compare-and-swap tests (no real repository/network writes)."""
import base64
import requests
import pytest

from adapters.github import GitHubApiRepository
from adapters.repo_sync import RepoSync


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data or {}, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class GitServer:
    def __init__(self):
        self.head = "base"
        self.read_refs = []
        self.trees = []
        self.parents = {}
        self.patches = []

    def get(self, url, **kwargs):
        if 'git/trees/' in url:
            return Response({'tree': [
                {'path': name, 'sha': 'original-blob', 'mode': '100644', 'type': 'blob'}
                for name in ['subscribers.csv', 'payments.csv']
            ], 'truncated': False})
        if 'git/blobs/' in url:
            self.read_refs.append(self.head)
            return Response({'encoding': 'base64', 'content': base64.b64encode(b'original').decode()})
        if "git/ref/heads/" in url:
            return Response({"object": {"sha": self.head}})
        if "git/commits/" in url:
            return Response({"tree": {"sha": "base-tree"}})
        self.read_refs.append(kwargs["params"]["ref"])
        return Response({"content": base64.b64encode(b"original").decode()})

    def post(self, url, json, **kwargs):
        if url.endswith("git/blobs"):
            return Response({"sha": "blob-" + json["content"]})
        if url.endswith("git/trees"):
            self.trees.append(json)
            return Response({"sha": "new-tree"})
        self.parents["new-commit"] = json["parents"][0]
        return Response({"sha": "new-commit"})

    def patch(self, url, json, **kwargs):
        self.patches.append(json)
        if self.parents[json["sha"]] != self.head:
            return Response(status=422)
        self.head = json["sha"]
        return Response({"object": {"sha": self.head}})


def test_failed_commit_lookup_does_not_poison_snapshot(monkeypatch):
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    server.head = "next"
    original = server.get

    def fail_commit(url, **kwargs):
        if "git/commits/" in url:
            return Response(status=503)
        return original(url, **kwargs)

    monkeypatch.setattr(server, "get", fail_commit)
    with pytest.raises(requests.HTTPError):
        repo.begin_snapshot()
    assert repo._base_commit == "base"
    monkeypatch.setattr(server, "get", original)
    repo.begin_snapshot()
    assert repo._base_commit == "next"
    assert not repo.snapshot_unchanged


def test_git_reads_one_snapshot_even_if_branch_advances():
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    repo.read_file("subscribers.csv")
    server.head = "concurrent"
    repo.read_file("payments.csv")
    assert server.read_refs == ["base", "base"]


def test_git_reads_batch_from_one_snapshot():
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    files = repo.read_files(["subscribers.csv", "payments.csv"])
    assert files == {"subscribers.csv": b"original", "payments.csv": b"original"}
    assert server.read_refs == ["base", "base"]


def test_reposync_reuses_unchanged_remote_snapshot(tmp_path):
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    sync = RepoSync(repo, str(tmp_path), ["subscribers.csv"], enabled=True)

    sync.pull(strict=True)
    assert (tmp_path / "subscribers.csv").read_bytes() == b"original"
    assert server.read_refs == ["base"]
    sync.pull(strict=True)
    assert server.read_refs == ["base"]


def test_changed_head_reuses_identical_blob_bytes():
    server = GitServer()
    repo = GitHubApiRepository('owner/repo', session=server)
    repo.begin_snapshot()
    assert repo.read_files(['subscribers.csv'])['subscribers.csv'] == b'original'
    server.head = 'external-docs-change'
    repo.begin_snapshot()
    assert repo.read_files(['subscribers.csv'])['subscribers.csv'] == b'original'
    assert server.read_refs == ['base']

def test_git_commits_multiple_csv_files_atomically():
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    repo.write_file("subscribers.csv", b"subscriber", "m")
    repo.write_file("payments.csv", b"payment", "m")
    repo.commit(["subscribers.csv", "payments.csv"], "transaction")
    assert {entry["path"] for entry in server.trees[0]["tree"]} == {"subscribers.csv", "payments.csv"}
    assert server.parents == {"new-commit": "base"}
    assert server.patches == [{"sha": "new-commit", "force": False}]
    assert not repo._pending


def test_concurrent_remote_update_is_never_overwritten():
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    repo.read_file("subscribers.csv")
    repo.write_file("subscribers.csv", b"stale modification", "m")
    server.head = "another-writers-commit"
    with pytest.raises(requests.HTTPError):
        repo.commit(["subscribers.csv"], "m")
    assert server.head == "another-writers-commit"
    assert repo._pending
    repo.discard_pending()
    repo.begin_snapshot()
    assert repo._base_commit == "another-writers-commit"


def test_github_read_error_is_not_treated_as_missing_file():
    server = GitServer()
    repo = GitHubApiRepository("owner/repo", session=server)
    repo.begin_snapshot()
    server.get = lambda *a, **k: Response(status=503)
    with pytest.raises(requests.HTTPError):
        repo.read_file("subscribers.csv")
