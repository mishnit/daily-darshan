"""SubscriberService: lifecycle + eligibility (section 7)."""
from __future__ import annotations

from datetime import date, datetime

from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from application.ports.repositories import (
    LogRepositoryPort,
    PaymentRepositoryPort,
    SentLogRepositoryPort,
    SubscriberRepositoryPort,
)


class SubscriberError(Exception):
    pass


class SubscriberService:
    def __init__(
        self,
        subscribers: SubscriberRepositoryPort,
        payments: PaymentRepositoryPort,
        plans: dict,
        sentlog: SentLogRepositoryPort | None = None,
        logs: LogRepositoryPort | None = None,
    ):
        self._subscribers = subscribers
        self._payments = payments
        self._plans = plans
        self._sentlog = sentlog
        self._logs = logs

    def _get(self, mobile: str) -> Subscriber:
        sub = self._subscribers.find(mobile)
        if sub is None:
            raise SubscriberError(f"Subscriber not found: {mobile}")
        return sub

    def _plan_days(self, plan: str) -> int:
        if plan not in self._plans:
            raise SubscriberError(f"Unknown plan: {plan}")
        return int(self._plans[plan]["days"])

    def upsert_pending(self, mobile: str, plan: str, name: str = "") -> Subscriber:
        """Create/refresh a PENDING subscriber during signup.

        `name` (e.g. the WhatsApp profile name) is stored when provided; a blank
        name never overwrites an existing stored name. New subscribers start
        **not opted-in** (opt_in=False) — consent is captured explicitly in the
        CTA flow before any business-initiated message (#9).
        """
        sub = self._subscribers.find(mobile)
        if sub is None:
            sub = Subscriber(mobile=mobile, plan=plan, status=SubscriberStatus.PENDING,
                             name=name.strip(), opt_in=False)
            sub.ensure_subscription_id()
            self._subscribers.append(sub)
        else:
            sub.plan = plan
            if name.strip():
                sub.name = name.strip()
            sub.ensure_subscription_id()  # backfill id for pre-existing rows
            self._subscribers.update(sub)
        return sub

    def grant_opt_in(self, mobile: str, source: str = "whatsapp_cta") -> Subscriber:
        """Record explicit opt-in consent with a timestamp + source (#9)."""
        sub = self._get(mobile)
        sub.grant_opt_in(source, datetime.now().isoformat())
        self._subscribers.update(sub)
        self._log("OPT_IN_GRANTED", mobile, source)
        return sub

    def revoke_opt_in(self, mobile: str) -> Subscriber | None:
        """Honor an opt-out (STOP): set opt_in False with timestamp (#9)."""
        sub = self._subscribers.find(mobile)
        if sub is None:
            return None
        sub.revoke_opt_in(datetime.now().isoformat())
        self._subscribers.update(sub)
        self._log("OPT_OUT", mobile, "")
        return sub

    # ------------------------------------------------------------------ #
    # Conversational name capture (explicit subscribe-flow name prompt)
    # ------------------------------------------------------------------ #
    def set_name(self, mobile: str, name: str) -> Subscriber:
        sub = self._get(mobile)
        sub.name = name.strip()
        self._subscribers.update(sub)
        return sub

    def set_awaiting_name(self, mobile: str, awaiting: bool) -> None:
        sub = self._subscribers.find(mobile)
        if sub is not None:
            sub.awaiting_name = awaiting
            self._subscribers.update(sub)

    def is_awaiting_name(self, mobile: str) -> bool:
        sub = self._subscribers.find(mobile)
        return bool(sub and sub.awaiting_name)

    def activate(self, mobile: str, on_date: date | None = None) -> Subscriber:
        sub = self._get(mobile)
        sub.ensure_subscription_id()  # guarantee an id exists before it's used in a URL
        sub.activate(self._plan_days(sub.plan), on_date)
        self._subscribers.update(sub)
        self._log("SUBSCRIBER_ACTIVATED", mobile, sub.end_date.isoformat() if sub.end_date else "")
        return sub

    def renew(self, mobile: str, on_date: date | None = None) -> Subscriber:
        sub = self._get(mobile)
        sub.ensure_subscription_id()  # guarantee an id for the page URL
        sub.renew(self._plan_days(sub.plan), on_date)
        self._subscribers.update(sub)
        self._log("SUBSCRIBER_RENEWED", mobile, sub.end_date.isoformat() if sub.end_date else "")
        return sub

    def pause(self, mobile: str) -> Subscriber:
        sub = self._get(mobile)
        sub.pause()
        self._subscribers.update(sub)
        self._log("SUBSCRIBER_PAUSED", mobile, "")
        return sub

    def resume(self, mobile: str) -> Subscriber:
        sub = self._get(mobile)
        sub.resume()
        self._subscribers.update(sub)
        self._log("SUBSCRIBER_RESUMED", mobile, "")
        return sub

    def cancel(self, mobile: str) -> Subscriber:
        sub = self._get(mobile)
        sub.cancel()
        self._subscribers.update(sub)
        self._log("SUBSCRIBER_CANCELLED", mobile, "")
        return sub

    def sweep_expired(self, on_date: date | None = None) -> list[str]:
        """Flip ACTIVE subscribers whose end_date has passed to EXPIRED.

        Keeps stored status in sync with reality (eligibility is already
        date-gated, but reports/admin views read the stored status). Only ACTIVE
        subscribers are swept — PAUSED is an intentional hold and CANCELLED is
        terminal, so neither is auto-expired. Idempotent: an already-EXPIRED
        subscriber is not touched again.

        Returns the list of mobiles transitioned this run.
        """
        on_date = on_date or date.today()
        # Snapshot only the mobiles to consider; re-read each row fresh right
        # before flipping so a concurrent webhook write to subscribers.csv
        # (e.g. a new opt-in) is not clobbered by a stale in-memory copy. We
        # mutate only the status field, leaving other columns as last written.
        candidates = [
            s.mobile for s in self._subscribers.all()
            if s.status == SubscriberStatus.ACTIVE and s.is_expired(on_date)
        ]
        expired: list[str] = []
        for mobile in candidates:
            sub = self._subscribers.find(mobile)  # re-read latest row
            if sub is None:
                continue
            # Re-check under the fresh read: the row may have changed (renewed,
            # cancelled, paused) since the snapshot; only expire if still due.
            if sub.status != SubscriberStatus.ACTIVE or not sub.is_expired(on_date):
                continue
            sub.expire()  # ACTIVE -> EXPIRED (allowed transition)
            self._subscribers.update(sub)
            self._log("SUBSCRIBER_EXPIRED", sub.mobile,
                      sub.end_date.isoformat() if sub.end_date else "")
            expired.append(sub.mobile)
        return expired

    def is_eligible(self, mobile: str, on_date: date | None = None) -> bool:
        """Delivery eligibility: active, opted-in, unexpired, and unsent today.

        Payment verification activates or renews a subscription in the normal
        workflow, but delivery itself is governed by the subscription record.
        This allows an administrator to grant an active subscription without a
        corresponding payment row.
        """
        on_date = on_date or date.today()
        sub = self._subscribers.find(mobile)
        if sub is None or not sub.is_deliverable(on_date):
            return False
        if self._sentlog is not None and self._sentlog.was_sent(on_date, mobile):
            return False
        return True

    def _log(self, event: str, mobile: str, details: str) -> None:
        if self._logs:
            self._logs.log(event, mobile, details)
