"""CSV renewal-reminder repository (section 28).

Idempotency key: mobile + reminder_type + expiry_date.
"""
from __future__ import annotations

from datetime import date

from application.ports.repositories import RenewalRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = [
    "mobile", "reminder_type", "expiry_date",
    "sent_at", "whatsapp_message_id", "status",
]


class CSVRenewalRepository(RenewalRepositoryPort):
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="mobile")

    def all(self) -> list[dict]:
        return self._csv.all()

    def append(self, record: dict) -> None:
        self._csv.append(record)

    def already_sent(self, mobile: str, reminder_type: str, expiry_date: date) -> bool:
        target = expiry_date.isoformat()
        for row in self._csv.all():
            if (
                row.get("mobile") == mobile
                and row.get("reminder_type") == reminder_type
                and row.get("expiry_date") == target
                and row.get("status") == "SENT"
            ):
                return True
        return False
