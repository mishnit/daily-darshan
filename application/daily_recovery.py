"""Select the next idempotent recovery step for today's publication pipeline."""
from __future__ import annotations

import argparse
import os
from datetime import date

from application.image_approval import (
    auto_approval_wait_seconds,
    deployment_ready,
    required,
)
from config import Container
from domain.clock import today_ist


def recovery_action(container: Container, on_date: date) -> tuple[str, str]:
    """Return the first incomplete stage and an operator-readable reason."""
    eligible = [
        subscriber for subscriber in container.subscribers.all()
        if container.subscriber_service.is_eligible(subscriber.mobile, on_date)
    ]
    missing = [
        subscriber.mobile for subscriber in eligible
        if not container.sentlog.was_sent(on_date, subscriber.mobile)
    ]
    if not missing:
        return "complete", f"all {len(eligible)} eligible subscribers already have a daily contact slot"

    if not required(container.config):
        if deployment_ready(container.root, on_date):
            return "delivery", f"{len(missing)} eligible subscribers still need today's contact"
        return "image", "approval is disabled but today's published image/pages are unavailable"

    approved = [
        row for row in container.image_reviews.all()
        if row.get("date") == on_date.isoformat() and row.get("status") == "APPROVED"
    ]
    if len(approved) == 1:
        if deployment_ready(container.root, on_date):
            return "delivery", f"{len(missing)} eligible subscribers still need today's contact"
        return "pages", "today's image is approved but the matching rendered approval stamp is absent"

    pending = [
        row for row in container.image_reviews.all()
        if row.get("date") == on_date.isoformat() and row.get("status") == "PENDING"
    ]
    if not pending:
        return "image", "today has no pending or approved image candidate"
    wait = auto_approval_wait_seconds(container, on_date)
    if wait:
        return "wait", f"manual image-review window has {wait} seconds remaining"
    return "auto_approve", "pending image review exceeded its configured deadline"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Recovery date in YYYY-MM-DD; defaults to today in IST")
    args = parser.parse_args(argv)
    try:
        on_date = date.fromisoformat(args.date) if args.date else today_ist()
    except ValueError:
        parser.error("--date must be YYYY-MM-DD")

    action, reason = recovery_action(Container(), on_date)
    print(f"action={action}")
    print(f"reason={reason}")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as target:
            target.write(f"action={action}\nreason={reason}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
