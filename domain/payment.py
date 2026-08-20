"""Payment domain entity and UPI-intent / reference-id rules (Tech Doc section 6)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import quote

from dateutil.parser import isoparse

from .enums import PaymentStatus

# Reference format: DD + YYMMDD + four-digit sequence  ->  DD2608170001
_REFERENCE_RE = re.compile(r"^DD\d{6}\d{4}$")
# A UTR (Unique Transaction Reference) is a 12-digit numeric value for UPI.
_UTR_RE = re.compile(r"^\d{12}$")


def build_reference_id(on_date: date, sequence: int) -> str:
    """DD + YYMMDD + zero-padded 4-digit sequence."""
    if not 0 <= sequence <= 9999:
        raise ValueError("sequence must be between 0 and 9999")
    return f"DD{on_date.strftime('%y%m%d')}{sequence:04d}"


def is_valid_reference_id(reference_id: str) -> bool:
    return bool(_REFERENCE_RE.match(reference_id or ""))


def is_valid_utr(utr: str) -> bool:
    """Basic structural UTR validation. Not proof of payment (section 6)."""
    return bool(_UTR_RE.match((utr or "").strip()))


def _coerce_payment_status(value) -> PaymentStatus:
    """Map a raw CSV value to a PaymentStatus, defaulting safely to PENDING.

    A corrupt/unknown status must never be read as SUCCESS (which would grant a
    subscription without a verified payment). PENDING is the safe default.
    """
    raw = str(value).strip() if value not in (None, "") else "PENDING"
    try:
        return PaymentStatus(raw)
    except ValueError:
        return PaymentStatus.PENDING


def _coerce_amount(value) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


@dataclass
class Payment:
    """Mirrors payments.csv columns (section 8)."""

    reference_id: str
    mobile: str
    plan: str
    amount: float
    status: PaymentStatus = PaymentStatus.PENDING
    utr: str = ""
    created_at: datetime | None = None
    verified_at: datetime | None = None

    @classmethod
    def from_row(cls, row: dict) -> "Payment":
        return cls(
            reference_id=str(row["reference_id"]).strip(),
            mobile=str(row.get("mobile", "")).strip(),
            plan=str(row.get("plan", "")).strip(),
            amount=_coerce_amount(row.get("amount")),
            status=_coerce_payment_status(row.get("status", "PENDING")),
            utr=str(row.get("utr", "")).strip(),
            created_at=_parse_dt(row.get("created_at")),
            verified_at=_parse_dt(row.get("verified_at")),
        )

    def to_row(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "mobile": self.mobile,
            "plan": self.plan,
            "amount": self.amount,
            "status": self.status.value,
            "utr": self.utr,
            "created_at": self.created_at.isoformat() if self.created_at else "",
            "verified_at": self.verified_at.isoformat() if self.verified_at else "",
        }

    def upi_intent(self, payee_vpa: str, payee_name: str, currency: str = "INR") -> str:
        """upi://pay?pa=..&pn=..&am=..&cu=INR&tn=<reference_id> (section 6)."""
        params = (
            f"pa={quote(payee_vpa)}"
            f"&pn={quote(payee_name)}"
            f"&am={self.amount}"
            f"&cu={quote(currency)}"
            f"&tn={quote(self.reference_id)}"
        )
        return f"upi://pay?{params}"

    def record_utr(self, utr: str) -> None:
        if not is_valid_utr(utr):
            raise ValueError(f"Invalid UTR format: {utr!r}")
        self.utr = utr.strip()
        # Remains PENDING until an admin verifies (section 6).
        self.status = PaymentStatus.PENDING

    def mark_verified(self, at: datetime | None = None) -> None:
        self.status = PaymentStatus.SUCCESS
        self.verified_at = at or datetime.now()


def _parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return isoparse(str(value))
    except (ValueError, OverflowError, TypeError):
        return None
