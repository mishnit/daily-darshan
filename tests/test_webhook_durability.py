"""Tests for webhook production-safety fixes: durable persistence (RepoSync),
guarded /health readiness, and per-message error isolation (always 200)."""
from __future__ import annotations

import json
import os

import pytest

from adapters.repo_sync import RepoSync
from application.ports.storage import GitHubRepositoryPort


class FakeGitHub(GitHubRepositoryPort):
    """In-memory GitHub Contents API stand-in."""

    def __init__(self, initial: dict | None = None, fail_read=False, fail_write=False):
        self.store: dict[str, bytes] = dict(initial or {})
        self.writes: list[tuple[str, bytes, str]] = []
        self._fail_read = fail_read
        self._fail_write = fail_write

    def read_file(self, path: str):
        if self._fail_read:
            raise RuntimeError("read boom")
        return self.store.get(path)

    def write_file(self, path: str, content: bytes, message: str) -> None:
        if self._fail_write:
            raise RuntimeError("write boom")
        self.store[path] = content
        self.writes.append((path, content, message))

    def commit(self, files, message):  # unused
        return None


# ----------------------------- RepoSync (P0 #6) ----------------------------- #

def test_reposync_disabled_is_noop(tmp_path):
    rs = RepoSync(FakeGitHub(), str(tmp_path), ["csv/x.csv"], enabled=False)
    assert rs.enabled is False
    rs.pull()
    assert rs.push("m") == []


def test_reposync_pull_overwrites_local_from_repo(tmp_path):
    (tmp_path / "csv").mkdir()
    local = tmp_path / "csv" / "subs.csv"
    local.write_text("stale\n")
    gh = FakeGitHub({"csv/subs.csv": b"fresh-from-repo\n"})
    rs = RepoSync(gh, str(tmp_path), ["csv/subs.csv"], enabled=True)
    rs.pull()
    assert local.read_text() == "fresh-from-repo\n"


def test_reposync_push_sends_local_to_repo(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "subs.csv").write_text("row1\n")
    gh = FakeGitHub()
    rs = RepoSync(gh, str(tmp_path), ["csv/subs.csv"], enabled=True)
    pushed = rs.push("Webhook update")
    assert pushed == ["csv/subs.csv"]
    assert gh.store["csv/subs.csv"] == b"row1\n"


def test_reposync_pull_tolerates_read_failure(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "subs.csv").write_text("local\n")
    rs = RepoSync(FakeGitHub(fail_read=True), str(tmp_path), ["csv/subs.csv"], enabled=True)
    rs.pull()  # must not raise
    assert (tmp_path / "csv" / "subs.csv").read_text() == "local\n"


def test_reposync_push_tolerates_write_failure(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "subs.csv").write_text("row\n")
    rs = RepoSync(FakeGitHub(fail_write=True), str(tmp_path), ["csv/subs.csv"], enabled=True)
    assert rs.push("m") == []  # failure swallowed, returns nothing pushed


def test_reposync_missing_repo_file_leaves_local(tmp_path):
    (tmp_path / "csv").mkdir()
    (tmp_path / "csv" / "subs.csv").write_text("local-header\n")
    gh = FakeGitHub()  # empty repo
    rs = RepoSync(gh, str(tmp_path), ["csv/subs.csv"], enabled=True)
    rs.pull()
    assert (tmp_path / "csv" / "subs.csv").read_text() == "local-header\n"


# --------------- Container wiring: enabled only when configured -------------- #

def test_container_reposync_disabled_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    from config import Container
    cfg = _min_config(tmp_path, persistence={"mode": "github_api"})
    c = Container(config=cfg, root=str(tmp_path))
    assert c.repo_sync.enabled is False  # no token/repo -> disabled


def test_container_reposync_enabled_with_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_REPO", "me/repo")
    from config import Container
    cfg = _min_config(tmp_path, persistence={"mode": "github_api"})
    c = Container(config=cfg, root=str(tmp_path))
    assert c.repo_sync.enabled is True


def _min_config(tmp_path, persistence=None):
    return {
        "plans": {"monthly": {"amount": 49, "days": 30}},
        "upi": {"payee_vpa": "t@upi", "payee_name": "T", "currency": "INR"},
        "image_sources": [], "image_source_config": {},
        "image_validation": {"allowed_formats": ["JPEG"], "min_width": 0, "min_height": 0},
        "paths": {
            "images_dir": str(tmp_path / "images"),
            "subscribers_csv": str(tmp_path / "subscribers.csv"),
            "payments_csv": str(tmp_path / "payments.csv"),
            "sentlog_csv": str(tmp_path / "sentlog.csv"),
            "renewals_csv": str(tmp_path / "renewals.csv"),
            "logs_csv": str(tmp_path / "logs.csv"),
            "processed_csv": str(tmp_path / "processed.csv"),
        },
        "renewal": {"reminder_days": [3, 1]},
        "persistence": persistence or {"mode": "local"},
        "delivery": {"mode": "image", "caption": "D - {date}", "max_send_retries": 1},
    }


# --------------------- /health readiness + error isolation ------------------- #

@pytest.fixture
def app_client(tmp_path, monkeypatch):
    """Reload main with an isolated container bound to tmp_path."""
    import importlib
    import config as config_mod
    config_mod.load_config.cache_clear()
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "")  # skip sig in these tests
    import main
    main = importlib.reload(main)
    main.container = config_mod.Container(config=_min_config(tmp_path), root=str(tmp_path))
    from tests.conftest import FakeWhatsApp
    main.container.whatsapp = FakeWhatsApp()
    from fastapi.testclient import TestClient
    return main, TestClient(main.app)


def test_health_ok_when_container_healthy(app_client):
    _, client = app_client
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_unhealthy_when_container_failed(monkeypatch):
    import importlib
    import main
    main = importlib.reload(main)
    # Simulate a failed container build.
    main.container = None
    main._container_error = "BadConfig: boom"
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    r = client.get("/health")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"
    assert "boom" in r.json()["reason"]


def test_webhook_returns_200_when_container_unavailable(monkeypatch):
    import importlib
    import main
    main = importlib.reload(main)
    main.container = None
    main._container_error = "BadConfig: boom"
    from fastapi.testclient import TestClient
    client = TestClient(main.app)
    body = json.dumps({"entry": []}).encode()
    r = client.post("/webhook", content=body)
    # No 500 — acks so Meta doesn't hammer retries.
    assert r.status_code == 200
    assert r.json()["status"] == "unavailable"


def _tap_payload(mobile, button_id, mid, name=None, kind="button_reply"):
    val = {"messages": [{
        "id": mid, "from": mobile, "type": "interactive",
        "interactive": {"type": "button" if kind == "button_reply" else "list",
                        kind: {"id": button_id, "title": button_id}},
    }]}
    if name is not None:
        val["contacts"] = [{"wa_id": mobile, "profile": {"name": name}}]
    return {"entry": [{"changes": [{"value": val}]}]}


def test_webhook_isolates_failing_message_and_still_200(app_client, monkeypatch):
    main, client = app_client

    # Make handling raise for one message, succeed for another.
    calls = {"n": 0}
    orig = main._handle_message

    def flaky(c, mobile, kind, value, name=""):
        calls["n"] += 1
        if mobile == "boom":
            raise RuntimeError("handler exploded")
        return orig(c, mobile, kind, value, name)

    monkeypatch.setattr(main, "_handle_message", flaky)

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "m1", "from": "boom", "type": "interactive",
         "interactive": {"type": "button", "button_reply": {"id": "PLAN_monthly", "title": "x"}}},
        {"id": "m2", "from": "9199", "type": "interactive",
         "interactive": {"type": "button", "button_reply": {"id": "PLAN_monthly", "title": "x"}},
         },
    ], "contacts": [{"wa_id": "9199", "profile": {"name": "Ok"}}]}}]}]}
    body = json.dumps(payload).encode()
    r = client.post("/webhook", content=body)

    # Always 200 (ack-fast) despite the first message throwing in the
    # background task; both messages attempted.
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert calls["n"] == 2
    # The good message still created a subscriber (plan tap with name).
    assert main.container.subscribers.find("9199") is not None


def test_webhook_acks_accepted_and_processes_in_background(app_client):
    """The POST returns 200 'accepted'; the message is processed off the
    response path (side effects visible after the request via TestClient)."""
    main, client = app_client
    payload = _tap_payload("9333", "PLAN_monthly", "bg1", name="Anita")
    import json as _json_mod
    r = client.post("/webhook", content=_json_mod.dumps(payload).encode())
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    # Background task ran (TestClient executes background tasks on response close):
    # the plan tap created the subscriber.
    assert main.container.subscribers.find("9333") is not None


def test_process_payload_never_raises_on_handler_error(app_client, monkeypatch):
    """_process_payload swallows handler errors — background tasks have no
    caller to catch them, so it must never propagate."""
    main, _ = app_client

    def boom(c, mobile, kind, value, name=""):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(main, "_handle_message", boom)
    payload = {"entry": [{"changes": [{"value": {
        "messages": [{"id": "x1", "from": "9444", "type": "text", "text": {"body": "SUBSCRIBE monthly"}}],
    }}]}]}
    # Must not raise despite the handler blowing up.
    main._process_payload(main.container, payload)
