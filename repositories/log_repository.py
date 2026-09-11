"""CSV append-only event log (section 17)."""
from __future__ import annotations

from datetime import date, datetime

from application.ports.repositories import LogRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = ["timestamp", "event", "mobile", "details"]


class CSVLogRepository(LogRepositoryPort):
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="timestamp")

    def log(self, event: str, mobile: str = "", details: str = "") -> None:
        self._csv.append({
            "timestamp": datetime.now().isoformat(),
            "event": event,
            "mobile": mobile,
            "details": details,
        })

    def all(self) -> list[dict]:
        return self._csv.all()

    def prune_before(self, cutoff: date) -> int:
        """Remove entries older than cutoff, preserving malformed rows safely."""
        def keep(row: dict) -> bool:
            try:
                return datetime.fromisoformat(row.get("timestamp", "")).date() >= cutoff
            except (TypeError, ValueError):
                return True

        return self._csv.retain(keep)
