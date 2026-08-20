"""One-off migration: backfill subscription_id for existing subscribers.

The subscribers.csv schema gained a `subscription_id` column (used in the
per-subscriber page URL). Older rows have it blank. This script assigns an
unguessable id to any row missing one, in place, idempotently.

Usage:
    python -m migrations.backfill_subscription_ids            # uses config.json paths
    python -m migrations.backfill_subscription_ids --commit   # also git-commit the change
"""
from __future__ import annotations

import argparse

from adapters.github import LocalGitRepository
from config import Container
from domain.subscriber import new_subscription_id


def backfill(container: Container) -> int:
    """Assign ids to subscribers that lack one. Returns count updated."""
    repo = container.subscribers
    updated = 0
    for sub in repo.all():
        if not sub.subscription_id:
            sub.subscription_id = new_subscription_id()
            repo.update(sub)
            updated += 1
            container.logs.log("SUBSCRIPTION_ID_BACKFILLED", sub.mobile, sub.subscription_id)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Backfill subscription_id column")
    parser.add_argument("--commit", action="store_true", help="git add + commit the change")
    args = parser.parse_args(argv)

    container = Container()
    count = backfill(container)
    print(f"Backfilled subscription_id for {count} subscriber(s).")

    if args.commit and count:
        git = LocalGitRepository(root=container.root)
        git.commit(
            [container.config["paths"]["subscribers_csv"], container.config["paths"]["logs_csv"]],
            "Backfill subscription_id for existing subscribers",
        )
        print("Committed changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
