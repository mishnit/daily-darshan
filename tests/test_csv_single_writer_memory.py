import csv

from repositories.csv_repository import CSVRepository, DuplicateKeyError
from repositories.processed_message_repository import CSVProcessedMessageRepository


def test_single_writer_keeps_rows_in_memory_until_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    path = tmp_path / "state.csv"
    repository = CSVRepository(str(path), ["id", "value"], "id")

    repository.append_unique("one", {"id": "one", "value": "initial"})
    repository.upsert("one", {"id": "one", "value": "updated"})

    assert repository.find("one") == {"id": "one", "value": "updated"}
    with path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == []

    assert repository.flush_memory() == 1
    with path.open(newline="", encoding="utf-8") as handle:
        assert list(csv.DictReader(handle)) == [{"id": "one", "value": "updated"}]
    assert repository.flush_memory() == 0


def test_single_writer_index_preserves_unique_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    repository = CSVRepository(str(tmp_path / "state.csv"), ["id", "value"], "id")
    repository.append_unique("one", {"id": "one", "value": "first"})

    try:
        repository.append_unique("one", {"id": "one", "value": "duplicate"})
    except DuplicateKeyError:
        pass
    else:
        raise AssertionError("duplicate key was accepted")

    assert repository.find("one")["value"] == "first"


def test_best_effort_dedupe_store_keeps_only_most_recent_rows(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    repository = CSVProcessedMessageRepository(str(tmp_path / "processed.csv"))
    for index in range(5):
        assert repository.mark_if_new(f"m{index}", "9199")
    assert repository.prune_to_recent(2) == 3
    assert {row["message_id"] for row in repository._csv.all()} == {"m3", "m4"}
