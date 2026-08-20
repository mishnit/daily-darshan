"""PaymentService: plan selection, reference id, UPI intent, UTR, verify (section 6)."""
from __future__ import annotations

from datetime import date, datetime

from domain.enums import PaymentStatus
from domain.payment import Payment, build_reference_id, is_valid_utr
from application.ports.repositories import (
    LogRepositoryPort,
    PaymentRepositoryPort,
)
from repositories.csv_repository import DuplicateKeyError


class PaymentError(Exception):
    pass


# Bounded retry to find a free daily sequence when writers race.
_MAX_REFERENCE_ATTEMPTS = 10000


class PaymentService:
    def __init__(
        self,
        payments: PaymentRepositoryPort,
        plans: dict,
        upi_config: dict,
        logs: LogRepositoryPort | None = None,
    ):
        self._payments = payments
        self._plans = plans
        self._upi = upi_config
        self._logs = logs

    def generate_reference_id(self, on_date: date | None = None) -> str:
        on_date = on_date or date.today()
        seq = self._payments.next_sequence(on_date)
        return build_reference_id(on_date, seq)

    def _supersede_pending(self, mobile: str, keep_reference_id: str = "") -> int:
        """Mark prior PENDING payments for a mobile as SUPERSEDED (#4).

        Prevents orphaned pending rows accumulating when a user starts checkout
        multiple times without paying. Only the latest pending payment stays
        actionable. Returns count superseded.
        """
        count = 0
        for p in self._payments.all():
            if (p.mobile == mobile and p.status == PaymentStatus.PENDING
                    and p.reference_id != keep_reference_id):
                p.status = PaymentStatus.SUPERSEDED
                self._payments.update(p)
                self._log("PAYMENT_SUPERSEDED", mobile, p.reference_id)
                count += 1
        return count

    def create_payment(self, mobile: str, plan: str, on_date: date | None = None) -> Payment:
        if plan not in self._plans:
            raise PaymentError(f"Unknown plan: {plan}")
        amount = float(self._plans[plan]["amount"])
        on_date = on_date or date.today()

        # (#4) Supersede any earlier still-pending payments for this mobile so
        # only the newest is actionable; a UTR then attaches unambiguously.
        self._supersede_pending(mobile)

        # Collision-free insert: recompute the next sequence, build the id, and
        # attempt a unique append. If a concurrent writer took that sequence,
        # append_unique raises DuplicateKeyError and we retry with the new count.
        last_error: Exception | None = None
        for _ in range(_MAX_REFERENCE_ATTEMPTS):
            reference_id = build_reference_id(on_date, self._payments.next_sequence(on_date))
            payment = Payment(
                reference_id=reference_id,
                mobile=mobile,
                plan=plan,
                amount=amount,
                status=PaymentStatus.PENDING,
                created_at=datetime.now(),
            )
            try:
                self._payments.append_unique(payment)
            except DuplicateKeyError as exc:
                last_error = exc
                continue
            self._log("PAYMENT_CREATED", mobile, reference_id)
            return payment

        raise PaymentError(
            f"Could not allocate a unique reference id for {on_date.isoformat()} "
            f"after {_MAX_REFERENCE_ATTEMPTS} attempts"
        ) from last_error

    def generate_upi_intent(self, payment: Payment) -> str:
        return payment.upi_intent(
            payee_vpa=self._upi["payee_vpa"],
            payee_name=self._upi["payee_name"],
            currency=self._upi.get("currency", "INR"),
        )

    def record_utr(self, reference_id: str, utr: str) -> Payment:
        payment = self._payments.find(reference_id)
        if payment is None:
            raise PaymentError(f"Payment not found: {reference_id}")
        if not is_valid_utr(utr):
            raise PaymentError(f"Invalid UTR: {utr!r}")
        payment.record_utr(utr)
        self._payments.update(payment)
        self._log("PAYMENT_UTR_RECEIVED", payment.mobile, f"{reference_id}:{utr}")
        return payment

    def verify_payment(self, reference_id: str) -> Payment:
        """Admin-only verification -> SUCCESS (section 6)."""
        payment = self._payments.find(reference_id)
        if payment is None:
            raise PaymentError(f"Payment not found: {reference_id}")
        payment.mark_verified()
        self._payments.update(payment)
        self._log("PAYMENT_VERIFIED", payment.mobile, reference_id)
        return payment

    def plan_days(self, plan: str) -> int:
        return int(self._plans[plan]["days"])

    def _log(self, event: str, mobile: str, details: str) -> None:
        if self._logs:
            self._logs.log(event, mobile, details)
