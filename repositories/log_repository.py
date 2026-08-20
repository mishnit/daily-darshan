"""CSV append-only event log (section 17)."""
from __future__ import annotations

from datetime import datetime

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
