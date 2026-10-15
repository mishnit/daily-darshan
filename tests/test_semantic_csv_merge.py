import csv
import io

import pytest

from adapters.repo_sync import RepoSync
from application.semantic_csv_merge import (
    SemanticMergeConflict,
    decode,
    merge_append_only,
    merge_keyed,
)
from repositories.csv_repository import CSVRepository


def encoded(fields, rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, escapechar="\\")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def repository(monkeypatch, tmp_path, fields, key="id"):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    return CSVRepository(str(tmp_path / (key + ".csv")), fields, key)


def test_semantic_merge_retains_changes_to_different_rows(monkeypatch, tmp_path):
    fields = ["id", "value"]
    repo = repository(monkeypatch, tmp_path, fields)
    base = [{"id": "base", "value": "old"}]
    repo.replace_memory_rows(base + [{"id": "local", "value": "L"}], dirty=True)
    remote = base + [{"id": "remote", "value": "R"}]

    assert merge_keyed(repo, encoded(fields, base), encoded(fields, remote), key_fields=("id",)) == []
    assert {row["id"] for row in repo.all()} == {"base", "local", "remote"}


def test_stale_memory_cannot_delete_a_remote_row(monkeypatch, tmp_path):
    fields = ["id", "status"]
    repo = repository(monkeypatch, tmp_path, fields)
    row = {"id": "today-mobile", "status": "SENT"}
    repo.replace_memory_rows([], dirty=True)

    merge_keyed(
        repo, encoded(fields, [row]), encoded(fields, [row]),
        key_fields=("id",), status_field="status",
    )

    assert repo.all() == [row]


def test_remote_retention_deletion_is_honored_when_memory_is_unchanged(monkeypatch, tmp_path):
    fields = ["id", "status"]
    repo = repository(monkeypatch, tmp_path, fields)
    row = {"id": "expired", "status": "SENT"}
    repo.replace_memory_rows([row], dirty=True)

    merge_keyed(
        repo, encoded(fields, [row]), encoded(fields, []),
        key_fields=("id",), status_field="status",
    )

    assert repo.all() == []


def test_batch_snapshot_cannot_delete_actions_image_candidate_after_merge_and_flush(
        monkeypatch, tmp_path):
    """Regression for production commit 78404b9's deleted pending candidate."""
    fields = ["id", "date", "status", "queued_at"]
    path = "csv/image_reviews.csv"
    candidate = {
        "id": "483ea394c2e94d5aac78db84ca688ee9",
        "date": "2026-10-12",
        "status": "PENDING",
        "queued_at": "2026-10-12T11:00:51.812002+05:30",
    }
    historical = {
        "id": "older", "date": "2026-10-11", "status": "APPROVED", "queued_at": "",
    }
    remote = encoded(fields, [historical, candidate])

    class Remote:
        def __init__(self):
            self.content = remote
            self.writes = []

        def begin_snapshot(self):
            return None

        def read_file(self, _path):
            return self.content

        def write_file(self, _path, content, _message):
            self.content = content
            self.writes.append(content)

        def commit(self, _files, _message):
            return None

    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    full = tmp_path / path
    full.parent.mkdir(parents=True)
    full.write_bytes(encoded(fields, [historical]))
    canonical = CSVRepository(str(full), fields, "id")
    stale_writer = CSVRepository(str(full), fields, "id")
    remote_store = Remote()
    sync = RepoSync(
        remote_store, str(tmp_path), [path], True,
        preserve_remote_csv_keys={path: ("id",)},
    )
    sync.accept_remote(path, remote)

    # Normal semantic reconciliation sees the Actions-created candidate.
    merge_keyed(
        canonical, remote, remote, key_fields=("id",),
        status_field="status", monotonic_status=False,
    )
    canonical.flush_memory()

    # Reproduce the production hazard: a stale registered cache flushes after
    # reconciliation and rewrites the file without today's candidate.
    stale_writer.replace_memory_rows([historical], dirty=True)
    stale_writer.flush_memory()
    assert candidate["id"].encode() not in full.read_bytes()

    sync.push("Batch webhook snapshot")

    persisted = decode(remote_store.content)
    assert {row["id"] for row in persisted} == {"older", candidate["id"]}
    assert decode(full.read_bytes()) == persisted


def test_batch_snapshot_cannot_revert_approved_image_to_pending(tmp_path):
    """Regression for snapshot 9fe8307 after auto-approval 5c7d83d."""
    fields = ["id", "date", "status", "queued_at", "approved_by", "approved_at", "approval_mode"]
    path = "csv/image_reviews.csv"
    pending = {"id": "8bb17b126d5c49c8bfea34e7320302d0", "date": "2026-10-13",
               "status": "PENDING", "queued_at": "2026-10-13T11:01:02+05:30",
               "approved_by": "", "approved_at": "", "approval_mode": ""}
    approved = dict(pending, status="APPROVED", approved_by="system:image_auto_approved",
                    approved_at="2026-10-13T11:16:18+05:30", approval_mode="AUTO_TIMEOUT")

    class Remote:
        content = encoded(fields, [approved])

        def write_file(self, _path, content, _message):
            self.content = content

        def commit(self, _files, _message):
            pass

    remote = Remote()
    full = tmp_path / path
    full.parent.mkdir(parents=True)
    full.write_bytes(encoded(fields, [pending]))
    sync = RepoSync(remote, str(tmp_path), [path], True,
                    preserve_remote_csv_keys={path: ("id",)},
                    preserve_remote_csv_status_ranks={path: {
                        "PENDING": 0, "SUPERSEDED": 1, "APPROVED": 2,
                    }})
    sync.accept_remote(path, remote.content)
    sync.push("Batch webhook snapshot")

    assert decode(remote.content) == [approved]
    assert decode(full.read_bytes()) == [approved]


def test_batch_snapshot_cannot_delete_sent_contact_slots_after_merge_and_flush(
        monkeypatch, tmp_path):
    """Regression for the 14 October snapshot that enabled a second delivery."""
    fields = ["date", "mobile", "image", "whatsapp_message_id", "status"]
    path = "csv/sentlog.csv"
    first = {"date": "2026-10-14", "mobile": "919108711599", "image": "https://vipseva.com/a",
             "whatsapp_message_id": "wamid.first", "status": "SENT"}
    second = dict(first, mobile="919535507255", image="https://vipseva.com/b",
                  whatsapp_message_id="wamid.second")
    remote_bytes = encoded(fields, [first, second])

    class Remote:
        content = remote_bytes

        def write_file(self, _path, content, _message):
            self.content = content

        def commit(self, _files, _message):
            pass

    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    full = tmp_path / path
    full.parent.mkdir(parents=True)
    full.write_bytes(encoded(fields, []))
    canonical = CSVRepository(str(full), fields, "mobile")
    stale_writer = CSVRepository(str(full), fields, "mobile")
    remote = Remote()
    sync = RepoSync(remote, str(tmp_path), [path], True,
                    preserve_remote_csv_keys={path: ("date", "mobile")},
                    preserve_remote_csv_status_ranks={path: {
                        "PENDING": 0, "FAILED": 1, "SENT": 2,
                        "DELIVERED": 3, "READ": 4,
                    }})
    sync.accept_remote(path, remote_bytes)

    merge_keyed(canonical, remote_bytes, remote_bytes,
                key_fields=("date", "mobile"), status_field="status")
    canonical.flush_memory()
    stale_writer.replace_memory_rows([], dirty=True)
    stale_writer.flush_memory()
    assert first["date"].encode() not in full.read_bytes()

    sync.push("Batch webhook snapshot")

    assert decode(remote.content) == [first, second]
    assert decode(full.read_bytes()) == [first, second]


def test_batch_snapshot_cannot_downgrade_delivered_contact_slot(tmp_path):
    fields = ["date", "mobile", "image", "whatsapp_message_id", "status"]
    path = "csv/sentlog.csv"
    delivered = {"date": "2026-10-14", "mobile": "919108711599", "image": "page",
                 "whatsapp_message_id": "wamid.delivered", "status": "DELIVERED"}
    stale = dict(delivered, whatsapp_message_id="reservation:old", status="PENDING")

    class Remote:
        content = encoded(fields, [delivered])

        def write_file(self, _path, content, _message):
            self.content = content

        def commit(self, _files, _message):
            pass

    full = tmp_path / path
    full.parent.mkdir(parents=True)
    full.write_bytes(encoded(fields, [stale]))
    remote = Remote()
    sync = RepoSync(remote, str(tmp_path), [path], True,
                    preserve_remote_csv_keys={path: ("date", "mobile")},
                    preserve_remote_csv_status_ranks={path: {
                        "PENDING": 0, "SENT": 2, "DELIVERED": 3,
                    }})
    sync.accept_remote(path, remote.content)
    sync.push("Batch webhook snapshot")

    assert decode(remote.content) == [delivered]
    assert decode(full.read_bytes()) == [delivered]


def test_delivery_status_progresses_monotonically(monkeypatch, tmp_path):
    fields = ["id", "whatsapp_message_id", "status"]
    repo = repository(monkeypatch, tmp_path, fields)
    base = [{"id": "one", "whatsapp_message_id": "wamid.1", "status": "PENDING"}]
    repo.replace_memory_rows(
        [{"id": "one", "whatsapp_message_id": "wamid.1", "status": "SENT"}], dirty=True,
    )
    remote = [{"id": "one", "whatsapp_message_id": "wamid.1", "status": "DELIVERED"}]

    merge_keyed(
        repo, encoded(fields, base), encoded(fields, remote),
        key_fields=("id",), status_field="status", strict=True,
    )
    assert repo.find("one")["status"] == "DELIVERED"


def test_image_review_can_supersede_an_unchanged_approved_baseline(monkeypatch, tmp_path):
    fields = ["id", "date", "status"]
    repo = repository(monkeypatch, tmp_path, fields)
    base = [
        {"id": "old", "date": "2026-10-07", "status": "APPROVED"},
        {"id": "new", "date": "2026-10-07", "status": "PENDING"},
    ]
    repo.replace_memory_rows([
        {"id": "old", "date": "2026-10-07", "status": "SUPERSEDED"},
        {"id": "new", "date": "2026-10-07", "status": "APPROVED"},
    ], dirty=True)

    merge_keyed(
        repo, encoded(fields, base), encoded(fields, base),
        key_fields=("id",), status_field="status", monotonic_status=False,
        strict=True,
    )

    assert repo.find("old")["status"] == "SUPERSEDED"
    assert repo.find("new")["status"] == "APPROVED"


def test_image_review_rejects_two_different_concurrent_decisions(monkeypatch, tmp_path):
    fields = ["id", "date", "status"]
    repo = repository(monkeypatch, tmp_path, fields)
    base = [{"id": "candidate", "date": "2026-10-07", "status": "PENDING"}]
    repo.replace_memory_rows([
        {"id": "candidate", "date": "2026-10-07", "status": "APPROVED"},
    ], dirty=True)
    remote = [{"id": "candidate", "date": "2026-10-07", "status": "SUPERSEDED"}]

    with pytest.raises(SemanticMergeConflict, match="status"):
        merge_keyed(
            repo, encoded(fields, base), encoded(fields, remote),
            key_fields=("id",), status_field="status", monotonic_status=False,
            strict=True,
        )


def test_strict_merge_blocks_same_mutable_field_conflict(monkeypatch, tmp_path):
    fields = ["id", "name"]
    repo = repository(monkeypatch, tmp_path, fields)
    base = [{"id": "one", "name": "old"}]
    repo.replace_memory_rows([{"id": "one", "name": "local"}], dirty=True)

    with pytest.raises(SemanticMergeConflict, match="name"):
        merge_keyed(
            repo, encoded(fields, base), encoded(fields, [{"id": "one", "name": "remote"}]),
            key_fields=("id",), strict=True,
        )


def test_applied_payment_refs_merge_is_semicolon_canonical_and_deduplicated(monkeypatch, tmp_path):
    fields = ["mobile", "applied_payment_refs"]
    repo = repository(monkeypatch, tmp_path, fields, key="mobile")
    base = [{"mobile": "9199", "applied_payment_refs": "DD2609160004"}]
    repo.replace_memory_rows([
        {"mobile": "9199", "applied_payment_refs": "DD2609160004"},
    ], dirty=True)
    remote = [{
        "mobile": "9199",
        "applied_payment_refs": "DD2609160004;DD2609200001",
    }]

    merge_keyed(
        repo, encoded(fields, base), encoded(fields, remote),
        key_fields=("mobile",), union_fields=("applied_payment_refs",),
    )

    assert repo.find("9199")["applied_payment_refs"] == "DD2609160004;DD2609200001"


def test_append_only_log_merge_is_an_idempotent_union(monkeypatch, tmp_path):
    fields = ["timestamp", "event"]
    repo = repository(monkeypatch, tmp_path, fields, key="timestamp")
    repo.replace_memory_rows([
        {"timestamp": "1", "event": "remote"},
        {"timestamp": "2", "event": "local"},
    ], dirty=True)
    remote = encoded(fields, [{"timestamp": "1", "event": "remote"}])

    merge_append_only(repo, remote, remote)
    merge_append_only(repo, remote, remote)
    assert repo.all() == [
        {"timestamp": "1", "event": "remote"},
        {"timestamp": "2", "event": "local"},
    ]


def test_append_only_merge_honors_remote_retention_pruning(monkeypatch, tmp_path):
    fields = ["timestamp", "event"]
    repo = repository(monkeypatch, tmp_path, fields, key="timestamp")
    old = {"timestamp": "1", "event": "old"}
    new = {"timestamp": "2", "event": "new-local"}
    repo.replace_memory_rows([old, new], dirty=True)

    merge_append_only(repo, encoded(fields, [old]), encoded(fields, []))
    assert repo.all() == [new]
