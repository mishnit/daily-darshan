"""CSV subscriber repository."""
from __future__ import annotations

from domain.subscriber import Subscriber
from application.ports.repositories import SubscriberRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = ["mobile", "plan", "start_date", "end_date", "status", "opt_in", "subscription_id", "name", "awaiting_name", "opt_in_at", "opt_in_source"]


class CSVSubscriberRepository(SubscriberRepositoryPort):
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="mobile")

    def find(self, mobile: str) -> Subscriber | None:
        row = self._csv.find(mobile)
        return Subscriber.from_row(row) if row else None

    def find_by_subscription_id(self, subscription_id: str) -> Subscriber | None:
        if not subscription_id:
            return None
        for row in self._csv.all():
            if str(row.get("subscription_id", "")).strip() == subscription_id:
                try:
                    return Subscriber.from_row(row)
                except (KeyError, ValueError, TypeError):
                    return None
        return None

    def all(self) -> list[Subscriber]:
        result = []
        for r in self._csv.all():
            try:
                result.append(Subscriber.from_row(r))
            except (KeyError, ValueError, TypeError):
                # Skip an unparseable/corrupt row rather than abort the batch.
                continue
        return result

    def append(self, subscriber: Subscriber) -> None:
        self._csv.append(subscriber.to_row())

    def update(self, subscriber: Subscriber) -> None:
        self._csv.upsert(subscriber.mobile, subscriber.to_row())
