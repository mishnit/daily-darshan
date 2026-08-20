"""Subscriber domain entity with lifecycle state machine (Tech Doc section 7)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from uuid import uuid4

from dateutil.parser import isoparse

from .enums import InvalidStateTransition, SubscriberStatus


def new_subscription_id() -> str:
    """Unguessable, non-PII id used in the public per-subscriber page URL."""
    return uuid4().hex


# Control chars (incl. newline/tab/CR) that WhatsApp rejects in template
# parameters, plus the 4-char sequences some clients disallow.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_WS_RE = re.compile(r"\s+")
_MAX_NAME_LEN = 60


def sanitize_display_name(raw: str, fallback: str = "devotee") -> str:
    """Make a user-supplied name safe for a WhatsApp template parameter (#7).

    WhatsApp rejects template body params containing newlines, tabs, or runs of
    4+ spaces, and caps length. A profile/typed name can contain any of these.
    We strip control chars, collapse whitespace, cap length, and fall back to a
    generic greeting if nothing usable remains (never leaks the phone number).
    """
    if not raw:
        return fallback
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(raw))
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if not cleaned:
        return fallback
    if len(cleaned) > _MAX_NAME_LEN:
        cleaned = cleaned[:_MAX_NAME_LEN].rstrip()
    return cleaned or fallback

# Allowed transitions per section 7:
#   PENDING -> ACTIVE
#   ACTIVE -> PAUSED
#   PAUSED -> ACTIVE
#   PENDING/ACTIVE/PAUSED -> CANCELLED
#   ACTIVE -> EXPIRED
_ALLOWED_TRANSITIONS: dict[SubscriberStatus, set[SubscriberStatus]] = {
    SubscriberStatus.PENDING: {SubscriberStatus.ACTIVE, SubscriberStatus.CANCELLED},
    SubscriberStatus.ACTIVE: {
        SubscriberStatus.PAUSED,
        SubscriberStatus.CANCELLED,
        SubscriberStatus.EXPIRED,
    },
    SubscriberStatus.PAUSED: {SubscriberStatus.ACTIVE, SubscriberStatus.CANCELLED},
    SubscriberStatus.CANCELLED: set(),
    SubscriberStatus.EXPIRED: set(),
}


def _parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    try:
        return isoparse(str(value)).date()
    except (ValueError, OverflowError, TypeError):
        # Corrupt/hand-edited date -> treat as missing rather than crash the batch.
        return None


def _coerce_status(value) -> SubscriberStatus:
    """Map a raw CSV value to a SubscriberStatus, defaulting safely.

    An unknown/garbage status (e.g. a typo in a hand-edited CSV) must not crash
    the delivery/renewal batch. We fall back to CANCELLED, which is inert:
    a CANCELLED subscriber is never deliverable and never due for reminders,
    so a corrupt row is skipped rather than mis-served.
    """
    raw = str(value).strip() if value not in (None, "") else "PENDING"
    try:
        return SubscriberStatus(raw)
    except ValueError:
        return SubscriberStatus.CANCELLED


@dataclass
class Subscriber:
    """A WhatsApp subscriber. Mirrors subscribers.csv columns (section 8)."""

    mobile: str
    plan: str
    start_date: date | None = None
    end_date: date | None = None
    status: SubscriberStatus = SubscriberStatus.PENDING
    opt_in: bool = True
    subscription_id: str = ""  # unguessable id for the public page URL
    name: str = ""             # display name (e.g. WhatsApp profile name) for {{1}}
    awaiting_name: bool = False  # conversational state: expecting the user's name next
    opt_in_at: str = ""        # ISO timestamp when opt-in was granted (consent proof)
    opt_in_source: str = ""    # how consent was captured, e.g. "whatsapp_cta"

    # ------------------------------------------------------------------ #
    # Construction / serialization
    # ------------------------------------------------------------------ #
    @classmethod
    def from_row(cls, row: dict) -> "Subscriber":
        return cls(
            mobile=str(row["mobile"]).strip(),
            plan=str(row.get("plan", "")).strip(),
            start_date=_parse_date(row.get("start_date")),
            end_date=_parse_date(row.get("end_date")),
            status=_coerce_status(row.get("status", "PENDING")),
            opt_in=str(row.get("opt_in", "true")).strip().lower() in ("1", "true", "yes"),
            subscription_id=str(row.get("subscription_id", "")).strip(),
            name=str(row.get("name", "")).strip(),
            awaiting_name=str(row.get("awaiting_name", "false")).strip().lower() in ("1", "true", "yes"),
            opt_in_at=str(row.get("opt_in_at", "")).strip(),
            opt_in_source=str(row.get("opt_in_source", "")).strip(),
        )

    def to_row(self) -> dict:
        return {
            "mobile": self.mobile,
            "plan": self.plan,
            "start_date": self.start_date.isoformat() if self.start_date else "",
            "end_date": self.end_date.isoformat() if self.end_date else "",
            "status": self.status.value,
            "opt_in": "true" if self.opt_in else "false",
            "subscription_id": self.subscription_id,
            "name": self.name,
            "awaiting_name": "true" if self.awaiting_name else "false",
            "opt_in_at": self.opt_in_at,
            "opt_in_source": self.opt_in_source,
        }

    def grant_opt_in(self, source: str, at: str) -> None:
        """Record explicit consent to receive business-initiated messages."""
        self.opt_in = True
        self.opt_in_at = at
        self.opt_in_source = source

    def revoke_opt_in(self, at: str) -> None:
        """Honor an opt-out (STOP). Stops all business-initiated sends."""
        self.opt_in = False
        self.opt_in_at = at
        self.opt_in_source = "opt_out"

    def ensure_subscription_id(self) -> str:
        """Assign an id if missing; return it. Idempotent."""
        if not self.subscription_id:
            self.subscription_id = new_subscription_id()
        return self.subscription_id

    # ------------------------------------------------------------------ #
    # State machine
    # ------------------------------------------------------------------ #
    def _transition(self, target: SubscriberStatus) -> None:
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise InvalidStateTransition(
                f"Cannot transition {self.status.value} -> {target.value}"
            )
        self.status = target

    def activate(self, plan_days: int, on_date: date | None = None) -> None:
        """Activate a PENDING (or re-activate) subscription for plan_days."""
        on_date = on_date or date.today()
        self._transition(SubscriberStatus.ACTIVE)
        self.start_date = on_date
        self.end_date = on_date + timedelta(days=plan_days)

    def renew(self, plan_days: int, on_date: date | None = None) -> None:
        """Extend subscription.

        Per section 29: extend from the current expiry date when the
        subscription is still active/unexpired, otherwise from today.
        """
        on_date = on_date or date.today()
        base = self.end_date if (self.end_date and self.end_date >= on_date) else on_date
        self.end_date = base + timedelta(days=plan_days)
        if self.status in (SubscriberStatus.EXPIRED, SubscriberStatus.PAUSED):
            # Reactivate an expired/paused subscriber on renewal.
            self.status = SubscriberStatus.ACTIVE
            if self.start_date is None:
                self.start_date = on_date

    def pause(self) -> None:
        self._transition(SubscriberStatus.PAUSED)

    def resume(self) -> None:
        self._transition(SubscriberStatus.ACTIVE)

    def cancel(self) -> None:
        self._transition(SubscriberStatus.CANCELLED)

    def expire(self) -> None:
        self._transition(SubscriberStatus.EXPIRED)

    # ------------------------------------------------------------------ #
    # Business queries
    # ------------------------------------------------------------------ #
    def is_expired(self, on_date: date | None = None) -> bool:
        on_date = on_date or date.today()
        return self.end_date is not None and self.end_date < on_date

    def days_remaining(self, on_date: date | None = None) -> int | None:
        if self.end_date is None:
            return None
        on_date = on_date or date.today()
        return (self.end_date - on_date).days

    def is_deliverable(self, on_date: date | None = None) -> bool:
        """Base eligibility (excluding idempotency check, section 7).

        Requires ACTIVE status, valid opt-in, and an unexpired subscription.
        """
        on_date = on_date or date.today()
        return (
            self.status == SubscriberStatus.ACTIVE
            and self.opt_in
            and not self.is_expired(on_date)
        )
