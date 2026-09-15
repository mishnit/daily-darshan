"""Creation timestamps survive retries and schema upgrades without backfill."""
from datetime import datetime, timezone
import re

import pytest

from repositories.csv_repository import CSVRepository


@pytest.fixture
def container(tmp_path):
    from config import Container
    return Container(root=str(tmp_path))


@pytest.mark.parametrize("ledger,key", [
    ("welcomes", "reference_id"), ("reply_outbox", "id"),
    ("reply_retries", "message_id"), ("message_statuses", "key"),
])
def test_new_records_have_immutable_creation_timestamp(container, ledger, key):
    repository = getattr(container, ledger)
    repository = getattr(repository, "_csv", repository)
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    repository.upsert("new", {key: "new", "status": "QUEUED"})
    timestamp = repository.find("new")["timestamp"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}", timestamp)
    assert before <= datetime.fromisoformat(timestamp) <= datetime.now(timezone.utc).replace(tzinfo=None)
    repository.upsert("new", {key: "new", "status": "SENT"})
    assert repository.find("new")["timestamp"] == timestamp
    repository.update_where(lambda row: True, {"status": "DELIVERED", "timestamp": "overwrite"})
    assert repository.find("new")["timestamp"] == timestamp


def test_old_headers_migrate_without_inventing_historical_timestamps(tmp_path):
    path = str(tmp_path / "legacy.csv")
    old = CSVRepository(path, ["id", "status"], "id")
    old.append({"id": "old", "status": "QUEUED"})
    new = CSVRepository(path, ["id", "status"], "id", timestamp_new=True)
    new.append_unique("new", {"id": "new", "status": "QUEUED"})
    assert new.find("old")["timestamp"] == ""
    new.upsert("old", {"id": "old", "status": "SENT"})
    assert new.find("old")["timestamp"] == ""
    assert new.find("new")["timestamp"]


def test_repeated_status_callback_keeps_original_timestamp(container):
    statuses = container.message_statuses
    statuses.record("wamid.example", "delivered")
    first = statuses._csv.all()
    statuses.record("wamid.example", "delivered")
    assert statuses._csv.all() == first
