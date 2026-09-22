"""Configurable customer-facing payment messages."""
from __future__ import annotations

from datetime import timedelta


DEFAULT_REJECTION_MESSAGE = (
    "Payment {reference_id} was rejected after verification. "
    "This admin decision is final for that payment reference, so its UTR cannot be revised or "
    "submitted for review again. Creating another payment is temporarily blocked until "
    "{release_date}, after {release_days} full calendar days. After release, send MENU to create "
    "a new eligible payment. Keep your payment proof and do not pay again during the blocked period."
)

DEFAULT_APPROVAL_MESSAGE = (
    "Payment {reference_id} for the {purchased_plan} plan was approved and applied. "
    "Your Daily Darshan subscription is active through {expiry_date}. "
    "Your updated personalised page and confirmation message are being prepared, and page "
    "publication is awaiting confirmation. "
    "You can earn 1 Karma point daily by sharing your Daily Darshan image with close friends "
    "and family from your personalised page. "
    "Please do not pay again for this reference."
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


def payment_approval_text(config: dict, payment, subscriber) -> str:
    """Render the immediate post-approval and subsequent status copy."""
    template = config.get("messages", {}).get(
        "payment_approved", DEFAULT_APPROVAL_MESSAGE
    )
    return template.format(
        reference_id=payment.reference_id,
        purchased_plan=payment.plan.capitalize(),
        active_plan=subscriber.plan.capitalize(),
        expiry_date=(
            subscriber.end_date.strftime("%d %B %Y").lstrip("0")
            if subscriber and subscriber.end_date
            else "pending confirmation"
        ),
    )
