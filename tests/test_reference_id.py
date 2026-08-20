"""Collision-free reference-id generation and CSV unique-append (concurrency)."""
from __future__ import annotations

import concurrent.futures
from datetime import date

import pytest

from application.payment_service import PaymentService, PaymentError
from repositories.csv_repository import CSVRepository, DuplicateKeyError
from repositories.payment_repository import CSVPaymentRepository

ON_DATE = date(2026, 8, 17)


# --------------------------- base repository --------------------------- #

def test_append_unique_rejects_duplicate(tmp_path):
    repo = CSVRepository(str(tmp_path / "t.csv"), ["id", "v"], key_field="id")
    repo.append_unique("A", {"id": "A", "v": "1"})
    with pytest.raises(DuplicateKeyError):
        repo.append_unique("A", {"id": "A", "v": "2"})
    # Original row is untouched.
    assert repo.find("A")["v"] == "1"


def test_append_unique_allows_distinct_keys(tmp_path):
    repo = CSVRepository(str(tmp_path / "t.csv"), ["id", "v"], key_field="id")
    repo.append_unique("A", {"id": "A", "v": "1"})
    repo.append_unique("B", {"id": "B", "v": "2"})
    assert {r["id"] for r in repo.all()} == {"A", "B"}


# --------------------------- sequential ids --------------------------- #

def test_reference_ids_increment_sequentially(repos, plans, upi_config):
    svc = PaymentService(repos["payments"], plans, upi_config)
    ids = [svc.create_payment(f"91{i:03d}", "monthly", ON_DATE).reference_id
           for i in range(5)]
    assert ids == [
        "DD2608170001", "DD2608170002", "DD2608170003",
        "DD2608170004", "DD2608170005",
    ]
    assert len(set(ids)) == 5


def test_service_raises_if_reference_row_preexists_unexpectedly(tmp_path, plans, upi_config, monkeypatch):
    """If next_sequence keeps returning a taken sequence, creation fails cleanly
    rather than overwriting an existing payment."""
    repo = CSVPaymentRepository(str(tmp_path / "payments.csv"))
    svc = PaymentService(repo, plans, upi_config)
    svc.create_payment("91000", "monthly", ON_DATE)  # takes DD2608170001

    # Force next_sequence to always collide with the existing row.
    monkeypatch.setattr(repo, "next_sequence", lambda on_date: 1)
    with pytest.raises(PaymentError):
        svc.create_payment("91001", "monthly", ON_DATE)


# --------------------------- concurrency --------------------------- #

def test_concurrent_creates_produce_unique_ids(tmp_path, plans, upi_config):
    """Many threads creating payments on the same day must never collide."""
    repo = CSVPaymentRepository(str(tmp_path / "payments.csv"))
    svc = PaymentService(repo, plans, upi_config)

    n = 40

    def make(i):
        return svc.create_payment(f"91{i:04d}", "monthly", ON_DATE).reference_id

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        ids = list(ex.map(make, range(n)))

    # No duplicates, and exactly n rows persisted.
    assert len(set(ids)) == n
    persisted = [p.reference_id for p in repo.all()]
    assert len(persisted) == n
    assert len(set(persisted)) == n
    # All ids share the correct day prefix.
    assert all(rid.startswith("DD260817") for rid in ids)
