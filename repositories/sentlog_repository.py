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
        self.persist = lambda: None

    def reserve(self, on_date, mobile, image):
        """Persist the contact slot BEFORE any provider request."""
        from uuid import uuid4
        reservation = "reservation:" + uuid4().hex
        with self._csv._exclusive_lock():
            if self.was_sent(on_date, mobile):
                return None
            self._csv._append_unlocked({"date": on_date.isoformat(), "mobile": mobile,
                         "image": image, "whatsapp_message_id": reservation,
                         "status": "PENDING"})
            # Persistence failure aborts the send. A remote PENDING reservation
            # deliberately remains blocking after a crash or ambiguous push.
            self.persist()
        return reservation

    def complete(self, reservation, result):
        status = "SENT" if result.ok else ("UNKNOWN" if result.unknown else "FAILED")
        self._csv.update_where(
            lambda row: row.get("whatsapp_message_id") == reservation,
            {"status": status, "whatsapp_message_id": result.message_id or reservation},
        )
        self.persist()

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
                and row.get("status") in {"SENT", "DELIVERED", "PENDING", "UNKNOWN"}
            ):
                return True
        return False

    def prune_before(self, cutoff: date) -> int:
        """Remove delivery entries older than cutoff, keeping malformed rows."""
        def keep(row: dict) -> bool:
            try:
                return date.fromisoformat(row.get("date", "")) >= cutoff
            except (TypeError, ValueError):
                return True

        return self._csv.retain(keep)

    def mark_failed(self, message_id: str) -> int:
        return self._csv.update_where(
            lambda row: row.get("whatsapp_message_id") == message_id,
            {"status": DeliveryStatus.FAILED.value},
        )
