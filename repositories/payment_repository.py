"""CSV payment repository with daily reference-id sequence support."""
from __future__ import annotations

import csv
import io
from datetime import date

from domain.payment import Payment
from application.ports.repositories import PaymentRepositoryPort

from .csv_repository import CSVRepository

FIELDNAMES = [
    "reference_id", "mobile", "plan", "amount",
    "status", "utr", "created_at", "verified_at",
    "activation_state", "utr_confirmed_at",
    "rejected_at",
    "payment_provider", "gateway_checkout_id", "gateway_payment_id", "checkout_url",
]


class PaymentMergeConflict(RuntimeError):
    """The same payment field changed differently in memory and GitHub."""


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

    @staticmethod
    def _decode(content: bytes | None) -> list[dict]:
        if not content:
            return []
        return list(csv.DictReader(io.StringIO(content.decode("utf-8")), escapechar="\\"))

    def merge_remote(self, baseline: bytes | None, remote: bytes | None, *, strict: bool) -> list[str]:
        """Three-way merge GitHub payments into memory by ``reference_id``.

        Independent field updates are combined. In strict mode a same-field
        conflict blocks an admin/UTR durability barrier. Ordinary refreshes
        preserve the local field and report the conflict for observability.
        """
        base = {row["reference_id"]: row for row in self._decode(baseline)}
        local = {row["reference_id"]: row for row in self._csv.all()}
        incoming = {row["reference_id"]: row for row in self._decode(remote)}
        merged = {}
        conflicts = []
        for reference in sorted(set(base) | set(local) | set(incoming)):
            before, ours, theirs = base.get(reference), local.get(reference), incoming.get(reference)
            if before is None:
                if ours is not None and theirs is not None and ours != theirs:
                    conflicts.append(f"{reference}:new-row")
                    merged[reference] = ours if strict else theirs
                else:
                    merged[reference] = ours or theirs
                continue
            if ours is None or theirs is None:
                survivor = theirs if ours is None else ours
                changed = survivor != before
                if changed:
                    conflicts.append(f"{reference}:deleted-row")
                    # A committed deletion/update wins in the ordinary lane;
                    # strict critical processing stops below instead.
                    merged[reference] = survivor if strict else theirs
                continue
            row = {}
            for field in FIELDNAMES:
                old = before.get(field, "")
                local_value = ours.get(field, "")
                remote_value = theirs.get(field, "")
                local_changed = local_value != old
                remote_changed = remote_value != old
                if local_changed and remote_changed and local_value != remote_value:
                    conflicts.append(f"{reference}:{field}")
                    # Ordinary refreshes treat the committed Git ledger as
                    # authoritative. Critical commands use strict=True and
                    # stop instead of choosing either side.
                    row[field] = local_value if strict else remote_value
                elif remote_changed:
                    row[field] = remote_value
                else:
                    row[field] = local_value
            merged[reference] = row
        if strict and conflicts:
            raise PaymentMergeConflict("Payment merge conflict: " + ", ".join(conflicts))
        rows = [merged[key] for key in sorted(merged) if merged[key] is not None]
        remote_rows = [incoming[key] for key in sorted(incoming)]
        self._csv.replace_memory_rows(rows, dirty=rows != remote_rows)
        return conflicts
