"""Domain enums and shared value objects."""
from __future__ import annotations

from enum import Enum


class SubscriberStatus(str, Enum):
    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SUPERSEDED = "SUPERSEDED"  # a newer pending payment replaced this one (#4)


class ReminderType(str, Enum):
    THREE_DAY = "3_DAY"
    ONE_DAY = "1_DAY"

    @classmethod
    def for_days_remaining(cls, days_remaining: int) -> "ReminderType":
        mapping = {3: cls.THREE_DAY, 1: cls.ONE_DAY}
        if days_remaining not in mapping:
            raise ValueError(f"No reminder type for days_remaining={days_remaining}")
        return mapping[days_remaining]


class DeliveryStatus(str, Enum):
    SENT = "SENT"
    FAILED = "FAILED"


class DomainError(Exception):
    """Base class for domain rule violations."""


class InvalidStateTransition(DomainError):
    """Raised when a subscriber state transition is not allowed."""
