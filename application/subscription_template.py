"""Body contract for the approved shared subscription-status template."""
from datetime import date

from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber, sanitize_display_name


SUBSCRIPTION_STATUS_TEMPLATE = "dailydarshan_subscription_status"


def subscription_status(subscriber: Subscriber, on_date: date, *, activated=False) -> str:
    """Describe entitlement on the scheduler's IST business date."""
    remaining = subscriber.days_remaining(on_date)
    if subscriber.status == SubscriberStatus.EXPIRED or (remaining is not None and remaining < 0):
        return "Expired"
    if subscriber.status != SubscriberStatus.ACTIVE:
        return subscriber.status.value.capitalize()
    if activated:
        return "Activated"
    if remaining == 0:
        return "Expiring today"
    if remaining is not None and 1 <= remaining <= 3:
        return f"Expiring in {remaining} {'day' if remaining == 1 else 'days'}"
    return "Active"


def template_body(template_name: str, subscriber: Subscriber, on_date: date, *, activated=False) -> list[str]:
    """Keep legacy one-variable templates compatible during rollout/rollback."""
    name = sanitize_display_name(subscriber.name, "devotee")
    if template_name == SUBSCRIPTION_STATUS_TEMPLATE:
        return [name, subscription_status(subscriber, on_date, activated=activated)]
    return [name]
