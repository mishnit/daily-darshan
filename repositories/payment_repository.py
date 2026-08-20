"""CSV payment repository with daily reference-id sequence support."""
from __future__ import annotations

from datetime import date

from domain.payment import Payment
from application.ports.repositories import PaymentRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = [
    "reference_id", "mobile", "plan", "amount",
    "status", "utr", "created_at", "verified_at",
]


class CSVPaymentRepository(PaymentRepositoryPort):
    def __init__(self, path: str):
        self._csv = CSVRepository(path, FIELDNAMES, key_field="reference_id")

    def find(self, reference_id: str) -> Payment | None:
        row = self._csv.find(reference_id)
        return Payment.from_row(row) if row else None

    def all(self) -> list[Payment]:
        result = []
        for r in self._csv.all():
            try:
                result.append(Payment.from_row(r))
            except (KeyError, ValueError, TypeError):
                # Skip an unparseable/corrupt row rather than abort the batch.
                continue
        return result

    def append(self, payment: Payment) -> None:
        self._csv.append(payment.to_row())

    def append_unique(self, payment: Payment) -> None:
        """Append only if reference_id is not already present.

        Raises repositories.csv_repository.DuplicateKeyError on collision.
        """
        self._csv.append_unique(payment.reference_id, payment.to_row())

    def update(self, payment: Payment) -> None:
        self._csv.upsert(payment.reference_id, payment.to_row())

    def next_sequence(self, on_date: date) -> int:
        """Count existing reference ids for on_date and return count+1.

        Reference ids embed YYMMDD (positions 2..8), so we match by prefix.
        Uses a locked read so the count is not taken mid-write by another
        process (the append_unique lock still guarantees final uniqueness).
        """
        prefix = f"DD{on_date.strftime('%y%m%d')}"
        same_day = [r for r in self._csv.all_locked()
                    if str(r.get("reference_id", "")).startswith(prefix)]
        return len(same_day) + 1
