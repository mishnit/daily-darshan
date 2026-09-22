import csv
import io

import pytest

from application.semantic_csv_merge import (
    SemanticMergeConflict,
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
