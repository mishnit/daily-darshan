"""Release stale rejected checkouts without deleting financial evidence."""
from __future__ import annotations

from datetime import date, datetime, time

from domain.clock import INDIA_TZ
from domain.enums import PaymentStatus


def _legacy_rejection_dates(logs) -> dict[str, date]:
    """Recover rejection dates written before payments gained rejected_at."""
    result: dict[str, date] = {}
    for row in logs.all():
        if row.get("event") != "PAYMENT_REJECTED":
            continue
        reference = str(row.get("details", "")).strip()
        try:
            rejected = datetime.fromisoformat(str(row.get("timestamp", ""))).date()
        except (TypeError, ValueError):
            continue
        if reference and (reference not in result or rejected > result[reference]):
            result[reference] = rejected
    return result


def release_stale_failed_payments(container, on_date: date, *, after_days: int = 3):
    """Change sufficiently old FAILED rows to SUPERSEDED and retain all evidence.

    The transition releases the checkout selector while preserving the payment,
    UTR, rejection timestamp, and audit trail. Rows with no trustworthy rejection
    timestamp remain FAILED rather than being released from their creation date.
    """
    if after_days < 1:
        raise ValueError("failed payment release period must be at least 1 day")
    legacy_dates = _legacy_rejection_dates(container.logs)
    released = []
    for payment in container.payments.all():
        if payment.status != PaymentStatus.FAILED:
            continue
        rejected_on = (
            payment.rejected_at.date()
            if payment.rejected_at
            else legacy_dates.get(payment.reference_id)
        )
        if rejected_on is None or (on_date - rejected_on).days < after_days:
            continue
        payment.mark_superseded(datetime.combine(on_date, time.min, tzinfo=INDIA_TZ))
        container.payments.update(payment)
        container.logs.log(
            "PAYMENT_REJECTION_AUTO_RELEASED",
            payment.mobile,
            payment.reference_id,
        )
        released.append(payment)
    return released
