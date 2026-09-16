"""Real Git races verify that a retry rebuilds pages from the winning CSV."""
import json
from datetime import date
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from application import image_publication as publication


@pytest.mark.parametrize("collisions", [1, 5])
def test_retry_renders_latest_subscriber_and_downloads_only_once(tmp_path, monkeypatch, collisions):
    def git(root, *args):
        return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    root = tmp_path / "runner"
    git(tmp_path, "clone", str(remote), str(root))
    git(root, "checkout", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "commit.gpgsign", "false")
    (root / "config.json").write_text(json.dumps({"paths": {"logs_csv": "logs.csv"}}))
    (root / "subscriber.csv").write_text("old expiry")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "push", "-u", "origin", "main")
    downloads = []
    rendered = []
    def container(config, root):
        def collect(day):
            downloads.append(day)
            return [b"downloaded-image"]
        return SimpleNamespace(root=root, config=config, image_service=SimpleNamespace(collect_daily_images=collect))
    monkeypatch.setattr(publication, "Container", container)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(publication.time, "sleep", lambda _: None)
    def render(container, transaction, day, **kwargs):
        checkout = Path(container.root)
        state = (checkout / "subscriber.csv").read_text()
        rendered.append(state)
        assert container.image_service.collect_daily_images(day) == [b"downloaded-image"]
        (checkout / "page.html").write_text(state)
        transaction.commit(["page.html"], "page")
        if len(rendered) <= collisions:
            (root / "subscriber.csv").write_text(f"renewed expiry {len(rendered)}")
            git(root, "add", "subscriber.csv")
            git(root, "commit", "-m", "Concurrent activation")
            git(root, "push")
        return 0
    monkeypatch.setattr("scheduler.run_image", render)
    monkeypatch.setattr("scheduler.run_expiry_sweep", lambda *args: 0)
    if collisions == 5:
        with pytest.raises(subprocess.CalledProcessError):
            publication.publish_image(str(root), date(2026, 9, 15))
        assert len(rendered) == 5
    else:
        assert publication.publish_image(str(root), date(2026, 9, 15)) == 0
        assert rendered == ["old expiry", "renewed expiry 1"]
        assert git(remote, "show", "main:page.html") == "renewed expiry 1"
    assert len(downloads) == 1
    assert git(remote, "show", "main:subscriber.csv") == f"renewed expiry {collisions}"
    assert git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_publication_excludes_external_audit_file(tmp_path):
    transaction = publication.PublicationFiles(str(tmp_path))
    transaction.commit(["docs/index.html", str(tmp_path / "audit.csv")], "test")
    assert transaction.files == {"docs/index.html"}
