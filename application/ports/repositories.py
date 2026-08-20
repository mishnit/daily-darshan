"""Repository ports (persistence abstraction, Tech Doc section 8)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from domain.payment import Payment
from domain.subscriber import Subscriber


class SubscriberRepositoryPort(ABC):
    @abstractmethod
    def find(self, mobile: str) -> Subscriber | None: ...

    def find_by_subscription_id(self, subscription_id: str) -> Subscriber | None:
        """Optional lookup by public subscription id (default: unsupported)."""
        return None

    @abstractmethod
    def all(self) -> list[Subscriber]: ...

    @abstractmethod
    def append(self, subscriber: Subscriber) -> None: ...

    @abstractmethod
    def update(self, subscriber: Subscriber) -> None: ...


class PaymentRepositoryPort(ABC):
    @abstractmethod
    def find(self, reference_id: str) -> Payment | None: ...

    @abstractmethod
    def all(self) -> list[Payment]: ...

    @abstractmethod
    def append(self, payment: Payment) -> None: ...

    @abstractmethod
    def append_unique(self, payment: Payment) -> None:
        """Append only if reference_id is unused; raise on collision."""

    @abstractmethod
    def update(self, payment: Payment) -> None: ...

    @abstractmethod
    def next_sequence(self, on_date: date) -> int:
        """Return the next daily sequence number for reference-id generation."""


class SentLogRepositoryPort(ABC):
    @abstractmethod
    def all(self) -> list[dict]: ...

    @abstractmethod
    def append(self, record: dict) -> None: ...

    @abstractmethod
    def was_sent(self, on_date: date, mobile: str) -> bool:
        """Idempotency check: successful delivery for date+mobile (section 12)."""


class RenewalRepositoryPort(ABC):
    @abstractmethod
    def all(self) -> list[dict]: ...

    @abstractmethod
    def append(self, record: dict) -> None: ...

    @abstractmethod
    def already_sent(self, mobile: str, reminder_type: str, expiry_date: date) -> bool:
        """Idempotency: mobile + reminder_type + expiry_date (section 28)."""


class LogRepositoryPort(ABC):
    @abstractmethod
    def log(self, event: str, mobile: str = "", details: str = "") -> None: ...
