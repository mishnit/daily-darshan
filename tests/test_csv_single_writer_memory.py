import csv

from repositories.csv_repository import CSVRepository, DuplicateKeyError


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
