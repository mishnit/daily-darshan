"""Processed-message store for webhook idempotency (review fix #1).

WhatsApp/Meta re-delivers webhooks when the endpoint is slow or on their retry
schedule, and the same message can arrive more than once. We dedupe on the
WhatsApp message id so a repeated delivery does not create duplicate payments
or re-run side effects.
"""
from __future__ import annotations

from datetime import datetime

from .csv_repository import CSVRepository, DuplicateKeyError

FIELDNAMES = ["message_id", "mobile", "received_at"]


class CSVProcessedMessageRepository:
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="message_id")

    def mark_if_new(self, message_id: str, mobile: str = "") -> bool:
        """Atomically record message_id. Returns True if newly recorded,
        False if it was already processed (duplicate delivery)."""
        if not message_id:
            # No id to dedupe on: treat as new but do not persist.
            return True
        try:
            self._csv.append_unique(message_id, {
                "message_id": message_id,
                "mobile": mobile,
                "received_at": datetime.now().isoformat(),
            })
            return True
        except DuplicateKeyError:
            return False

    def was_processed(self, message_id: str) -> bool:
        return self._csv.find(message_id) is not None
