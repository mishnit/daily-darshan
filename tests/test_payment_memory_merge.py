import csv
import io

import pytest

from repositories.payment_repository import CSVPaymentRepository, FIELDNAMES, PaymentMergeConflict


def encoded(*rows):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=FIELDNAMES, escapechar="\\")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def payment(reference="DD2609200001", **changes):
    row = {field: "" for field in FIELDNAMES}
    row.update(reference_id=reference, mobile="9199", plan="monthly", amount="49", status="PENDING")
    row.update(changes)
    return row


def test_payment_merge_combines_remote_verification_with_local_utr(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    repository = CSVPaymentRepository(str(tmp_path / "payments.csv"))
    base = payment()
    repository._csv.upsert(base["reference_id"], payment(utr="123456789012"))
    remote = payment(status="SUCCESS", verified_at="2026-09-20T10:00:00")

    assert repository.merge_remote(encoded(base), encoded(remote), strict=True) == []
    merged = repository._csv.find(base["reference_id"])
    assert merged["utr"] == "123456789012"
    assert merged["status"] == "SUCCESS"
    assert merged["verified_at"] == "2026-09-20T10:00:00"


def test_payment_merge_blocks_same_field_conflict_for_critical_lane(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    repository = CSVPaymentRepository(str(tmp_path / "payments.csv"))
    base = payment()
    repository._csv.upsert(base["reference_id"], payment(status="FAILED"))

    with pytest.raises(PaymentMergeConflict, match="DD2609200001:status"):
        repository.merge_remote(encoded(base), encoded(payment(status="SUCCESS")), strict=True)


def test_ordinary_payment_refresh_uses_remote_value_on_same_field_conflict(monkeypatch, tmp_path):
    monkeypatch.setenv("WEBHOOK_SINGLE_WRITER", "true")
    repository = CSVPaymentRepository(str(tmp_path / "payments.csv"))
    base = payment()
    repository._csv.upsert(base["reference_id"], payment(status="FAILED"))

    conflicts = repository.merge_remote(
        encoded(base), encoded(payment(status="SUCCESS")), strict=False,
    )
    assert conflicts == ["DD2609200001:status"]
    assert repository.find(base["reference_id"]).status.value == "SUCCESS"
