"""Configurable customer-facing payment messages."""
from __future__ import annotations

from datetime import timedelta


DEFAULT_REJECTION_MESSAGE = (
    "Payment {reference_id} was rejected after verification. "
    "You can send MENU and select Request review now. "
    "Starting another renewal or upgrade is temporarily blocked. "
    "This rejected checkout becomes eligible for automatic release on {release_date}, "
    "after {release_days} full calendar days. Keep your payment proof and do not pay again "
    "until the payment is resolved."
)


def payment_rejection_text(config: dict, payment) -> str:
    """Render one source of truth for proactive and on-demand rejection copy."""
    release_days = max(
        1, int(config.get("delivery", {}).get("failed_payment_release_days", 3))
    )
    rejected_on = payment.rejected_at.date() if payment.rejected_at else None
    release_on = rejected_on + timedelta(days=release_days) if rejected_on else None
    template = config.get("messages", {}).get(
        "payment_rejected", DEFAULT_REJECTION_MESSAGE
    )
    return template.format(
        reference_id=payment.reference_id,
        release_days=release_days,
        release_date=(
            release_on.strftime("%d %B %Y").lstrip("0")
            if release_on
            else f"after {release_days} full calendar days"
        ),
    )
