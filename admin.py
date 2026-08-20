"""Admin CLI for out-of-band operations (Tech Doc sections 6, 15).

A user-submitted UTR is NOT proof of payment. An admin confirms the real UPI
transaction, then uses this tool to mark the payment SUCCESS and (optionally)
activate the subscriber in one step.

Examples:
    # Show payments awaiting verification
    python admin.py list-pending

    # Verify a payment only (status -> SUCCESS)
    python admin.py verify DD2608190001

    # Verify AND activate the subscriber (PENDING -> ACTIVE with dates)
    python admin.py verify DD2608190001 --activate

    # Verify, activate, and commit the CSV changes to git in one shot
    python admin.py verify DD2608190001 --activate --commit

    # Reject a payment that does not match a real transaction
    python admin.py reject DD2608190001
"""
from __future__ import annotations

import argparse
import sys

from adapters.github import LocalGitRepository
from application.payment_service import PaymentError
from application.subscriber_service import SubscriberError
from config import Container
from domain.enums import PaymentStatus


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _changed_csv_paths(container: Container) -> list[str]:
    paths = container.config["paths"]
    return [paths["payments_csv"], paths["subscribers_csv"], paths["logs_csv"]]


def _commit(container: Container, message: str, include_pages: bool = False) -> None:
    git = LocalGitRepository(root=container.root)
    files = _changed_csv_paths(container)
    if include_pages:
        files.append(container.config.get("delivery", {}).get("pages_dir", "docs"))
    git.commit(files, message)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_list_pending(container: Container, args) -> int:
    pending = [p for p in container.payments.all() if p.status == PaymentStatus.PENDING]
    if not pending:
        print("No pending payments.")
        return 0
    print(f"{'reference_id':<16} {'mobile':<15} {'plan':<10} {'amount':>8}  utr")
    print("-" * 64)
    for p in pending:
        print(f"{p.reference_id:<16} {p.mobile:<15} {p.plan:<10} {p.amount:>8}  {p.utr or '(none)'}")
    return 0


def cmd_verify(container: Container, args) -> int:
    reference_id = args.reference_id
    try:
        payment = container.payment_service.verify_payment(reference_id)
    except PaymentError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Verified payment {reference_id}: status=SUCCESS "
          f"(mobile={payment.mobile}, plan={payment.plan}, amount={payment.amount})")

    committed_msg = f"Verify payment {reference_id}"

    if args.activate:
        try:
            svc = container.subscriber_service
            existing = container.subscribers.find(payment.mobile)
            # Align the subscriber's plan with the paid plan (creates PENDING if new).
            svc.upsert_pending(payment.mobile, payment.plan)
            # Renew (extend from current expiry, §29) vs. first-time activate.
            already = existing is not None and existing.status.value in ("ACTIVE", "PAUSED", "EXPIRED")
            if already or args.renew:
                sub = svc.renew(payment.mobile)
                action = "Renewed"
            else:
                sub = svc.activate(payment.mobile)
                action = "Activated"
        except SubscriberError as exc:
            print(f"ERROR during activation: {exc}", file=sys.stderr)
            print("Payment was verified but subscriber activation failed. "
                  "Fix the subscriber and re-run with --activate, or activate manually.",
                  file=sys.stderr)
            return 1
        print(f"{action} subscriber {payment.mobile}: status={sub.status.value}, "
              f"start={sub.start_date}, end={sub.end_date}")
        # Generate the per-subscriber page now so their branded URL works
        # immediately (not only after the next daily image job).
        try:
            from datetime import date as _date
            container.page_renderer.write_page(
                sub, _date.today(), delivered=True,
                images_dir=container.config["paths"]["images_dir"], root=container.root,
            )
        except Exception as exc:  # page generation must not block activation
            print(f"WARN: could not render page for {payment.mobile}: {exc}", file=sys.stderr)
        committed_msg = f"Verify payment {reference_id} and activate {payment.mobile}"

    if args.commit:
        _commit(container, committed_msg, include_pages=args.activate)
        print(f"Committed changes: {committed_msg!r}")
    else:
        print("Changes written to CSV (not committed). "
              "Commit them, or re-run with --commit.")
    return 0


def cmd_reject(container: Container, args) -> int:
    reference_id = args.reference_id
    payment = container.payments.find(reference_id)
    if payment is None:
        print(f"ERROR: Payment not found: {reference_id}", file=sys.stderr)
        return 1
    payment.status = PaymentStatus.FAILED
    container.payments.update(payment)
    container.logs.log("PAYMENT_REJECTED", payment.mobile, reference_id)
    print(f"Rejected payment {reference_id}: status=FAILED")
    if args.commit:
        _commit(container, f"Reject payment {reference_id}")
        print("Committed changes.")
    else:
        print("Changes written to CSV (not committed). Re-run with --commit to commit.")
    return 0


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="admin.py", description="Daily Darshan admin CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-pending", help="List payments awaiting verification")
    p_list.set_defaults(func=cmd_list_pending)

    p_verify = sub.add_parser("verify", help="Mark a payment SUCCESS (optionally activate subscriber)")
    p_verify.add_argument("reference_id", help="Payment reference id, e.g. DD2608190001")
    p_verify.add_argument("--activate", action="store_true",
                          help="Also activate the subscriber (PENDING -> ACTIVE with dates)")
    p_verify.add_argument("--renew", action="store_true",
                          help="Force renewal semantics (extend from current expiry) instead of activate")
    p_verify.add_argument("--commit", action="store_true",
                          help="git add + commit the changed CSV files")
    p_verify.set_defaults(func=cmd_verify)

    p_reject = sub.add_parser("reject", help="Mark a payment FAILED")
    p_reject.add_argument("reference_id", help="Payment reference id")
    p_reject.add_argument("--commit", action="store_true", help="git add + commit the change")
    p_reject.set_defaults(func=cmd_reject)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    container = Container()
    return args.func(container, args)


if __name__ == "__main__":
    raise SystemExit(main())
