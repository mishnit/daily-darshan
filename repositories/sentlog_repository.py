"""CSV sent-log repository with date+mobile idempotency (section 12)."""
from __future__ import annotations

from datetime import date

from domain.enums import DeliveryStatus
from application.ports.repositories import SentLogRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = ["date", "mobile", "image", "whatsapp_message_id", "status"]


class CSVSentLogRepository(SentLogRepositoryPort):
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="date")

    def all(self) -> list[dict]:
        return self._csv.all()

    def append(self, record: dict) -> None:
        self._csv.append(record)

    def was_sent(self, on_date: date, mobile: str) -> bool:
        target = on_date.isoformat()
        for row in self._csv.all():
            if (
                row.get("date") == target
                and row.get("mobile") == mobile
                and row.get("status") == DeliveryStatus.SENT.value
            ):
                return True
        return False
