"""Durable status inbox, including callbacks received before the send ledger."""
from .csv_repository import CSVRepository, DuplicateKeyError


class MessageStatusRepository:
    def __init__(self, path):
        self._csv = CSVRepository(path, ["key", "message_id", "status"], "key")

    def record(self, message_id, status):
        if not message_id or status not in {"failed", "delivered", "read"}:
            return
        key = f"{message_id}:{status}"
        try:
            self._csv.append_unique(key, {"key": key, "message_id": message_id, "status": status})
        except DuplicateKeyError:
            pass

    def reconcile(self, *ledgers):
        statuses = {}
        for row in self._csv.all():
            statuses.setdefault(row["message_id"], set()).add(row["status"])
        for message_id, values in statuses.items():
            # A positive receipt must never be undone by a delayed failure.
            state = "DELIVERED" if values & {"delivered", "read"} else "FAILED"
            for ledger in ledgers:
                getattr(ledger, "_csv", ledger).update_where(
                    lambda row: row.get("whatsapp_message_id") == message_id,
                    {"status": state},
                )
