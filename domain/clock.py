"""Application clock helpers for the service's Asia/Kolkata business day."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


INDIA_TZ = ZoneInfo("Asia/Kolkata")


def today_ist(now: datetime | None = None) -> date:
    """Return the calendar date used for subscriptions, payments and delivery."""
    current = now or datetime.now(INDIA_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=INDIA_TZ)
    else:
        current = current.astimezone(INDIA_TZ)
    return current.date()
