"""Durable status inbox, including callbacks received before the send ledger."""
from .csv_repository import CSVRepository, DuplicateKeyError


class MessageStatusRepository:
    def __init__(self, path):
        self._csv = CSVRepository(path, ["key", "message_id", "status"], "key", timestamp_new=True)

    def record(self, message_id, status):
        if not message_id or status not in {"failed", "delivered", "read"}:
            return
        key = f"{message_id}:{status}"
        try:
            self._csv.append_unique(key, {"key": key, "message_id": message_id, "status": status})
        except DuplicateKeyError:
            pass

    def reconcile(self, *ledgers, consume=False):
        statuses = {}
        for row in self._csv.all():
            statuses.setdefault(row["message_id"], set()).add(row["status"])
        reconciled = set()
        for message_id, values in statuses.items():
            # A positive receipt must never be undone by a delayed failure.
            if "read" in values:
                state = "READ"
            elif "delivered" in values:
                state = "DELIVERED"
            else:
                state = "FAILED"
            for ledger in ledgers:
                repository = getattr(ledger, "_csv", ledger)
                matched = any(
                    row.get("whatsapp_message_id") == message_id
                    for row in repository.all()
                )
                if not matched:
                    continue
                repository.update_where(
                    lambda row: (
                        row.get("whatsapp_message_id") == message_id
                        and self._can_advance(row.get("status", ""), state)
                    ),
                    {"status": state},
                )
                if matched:
                    reconciled.add(message_id)
        if consume and reconciled:
            self._csv.retain(lambda row: row.get("message_id") not in reconciled)

    @staticmethod
    def _can_advance(current, incoming):
        current = str(current).upper()
        if current == "READ":
            return False
        if current == "DELIVERED" and incoming == "FAILED":
            return False
        return True
