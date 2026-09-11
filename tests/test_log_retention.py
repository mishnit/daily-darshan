from datetime import date

from repositories.log_repository import CSVLogRepository
from repositories.sentlog_repository import CSVSentLogRepository


def test_log_cleanup_keeps_cutoff_and_newer_rows(tmp_path):
    repo = CSVLogRepository(str(tmp_path / "logs.csv"))
    for timestamp in (
        "2026-08-12T23:59:59",
        "2026-08-13T00:00:00",
        "2026-09-11T08:00:00",
        "not-a-timestamp",
    ):
        repo._csv.append({"timestamp": timestamp, "event": "TEST"})

    assert repo.prune_before(date(2026, 8, 13)) == 1
    assert [row["timestamp"] for row in repo.all()] == [
        "2026-08-13T00:00:00",
        "2026-09-11T08:00:00",
        "not-a-timestamp",
    ]


def test_sentlog_cleanup_keeps_cutoff_and_newer_rows(tmp_path):
    repo = CSVSentLogRepository(str(tmp_path / "sentlog.csv"))
    for sent_date in ("2026-08-12", "2026-08-13", "2026-09-11", "invalid"):
        repo.append({"date": sent_date, "mobile": "9199", "status": "SENT"})

    assert repo.prune_before(date(2026, 8, 13)) == 1
    assert [row["date"] for row in repo.all()] == [
        "2026-08-13",
        "2026-09-11",
        "invalid",
    ]


def test_cleanup_is_idempotent(tmp_path):
    repo = CSVSentLogRepository(str(tmp_path / "sentlog.csv"))
    repo.append({"date": "2026-09-11", "mobile": "9199", "status": "SENT"})

    assert repo.prune_before(date(2026, 8, 13)) == 0
    assert repo.prune_before(date(2026, 8, 13)) == 0
