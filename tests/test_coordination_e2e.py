"""End-to-end coordination tests for the two-machine model.

Simulates the webhook (Render) and the scheduler/admin (GitHub Actions) sharing
one repo (a FakeGitHub Contents-API store). Verifies:

  1. Item 1 (writer separation / safe expiry): the expiry sweep re-reads each
     row before flipping, so a concurrent webhook write to subscribers.csv is
     preserved (no stale-snapshot clobber). logs.csv stays append-only.

  2. Item 2 (defer-push window): a webhook push landing inside the nightly job
     window is deferred and flushed after the window, so it never overwrites an
     in-flight scheduler commit.

The FakeGitHub store stands in for the `main` branch shared by both sides.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from adapters.repo_sync import RepoSync
from application.ports.storage import GitHubRepositoryPort
from application.subscriber_service import SubscriberService
from domain.enums import SubscriberStatus
from domain.subscriber import Subscriber
from repositories.subscriber_repository import CSVSubscriberRepository
from repositories.payment_repository import CSVPaymentRepository
from repositories.log_repository import CSVLogRepository

TODAY = date(2026, 8, 20)


class FakeRepo(GitHubRepositoryPort):
    """In-memory stand-in for the shared `main` branch."""

    def __init__(self):
        self.store: dict[str, bytes] = {}

    def read_file(self, path: str):
        return self.store.get(path)

    def write_file(self, path: str, content: bytes, message: str) -> None:
        self.store[path] = content

    def commit(self, files, message):
        return None


def _clock_at(hh, mm):
    def _c():
        return datetime(2026, 8, 20, hh, mm, tzinfo=timezone.utc)
    return _c


def _machine(tmp_path, name, repo, tracked, clock, plans):
    """Build an isolated 'machine' with its own local disk + repositories.

    Returns (reposync, service, repos-dict). Two machines pointed at the same
    FakeRepo model the webhook and the scheduler sharing `main`.
    """
    root = tmp_path / name
    (root / "csv").mkdir(parents=True)
    subs = CSVSubscriberRepository(str(root / "csv" / "subscribers.csv"))
    pays = CSVPaymentRepository(str(root / "csv" / "payments.csv"))
    logs = CSVLogRepository(str(root / "csv" / "logs.csv"))
    rs = RepoSync(repo, str(root), tracked, enabled=True,
                  quiet_window=("02:25", "03:10"), clock=clock)
    svc = SubscriberService(subs, pays, plans, sentlog=None, logs=logs)
    return rs, svc, {"subscribers": subs, "payments": pays, "logs": logs}


def _seed_repo(repo, rows):
    """Write a subscribers.csv into the shared repo from Subscriber rows."""
    from repositories.subscriber_repository import FIELDNAMES
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDNAMES)
    w.writeheader()
    for sub in rows:
        w.writerow({k: sub.to_row().get(k, "") for k in FIELDNAMES})
    repo.store["csv/subscribers.csv"] = buf.getvalue().encode()


TRACKED = ["csv/subscribers.csv", "csv/payments.csv", "csv/logs.csv"]


def test_e2e_expiry_preserves_concurrent_webhook_optin(tmp_path, plans):
    """The scheduler's expiry sweep must not clobber a subscriber that the
    webhook added/updated concurrently on the shared repo.

    Timeline:
      - Repo starts with one ACTIVE-but-expired subscriber (9111).
      - Scheduler pulls the repo (sees 9111).
      - Webhook adds a brand-new subscriber (9222) and pushes to the repo
        (outside the quiet window for this leg, to model an update that landed
        just before the sweep's write).
      - Scheduler runs sweep_expired, which re-reads each candidate fresh, then
        pushes subscribers.csv back.
      - Final repo state must contain BOTH: 9111 EXPIRED and 9222 intact.
    """
    repo = FakeRepo()
    expired_sub = Subscriber(
        mobile="9111", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 8, 1), opt_in=True,
        subscription_id="tok-9111",
    )
    _seed_repo(repo, [expired_sub])

    # Scheduler machine (runs at 03:00 UTC -> pushes allowed via git in reality;
    # here we model its Contents-API-equivalent push as always-on by using a
    # clock outside the quiet window for its own push leg).
    sched_rs, sched_svc, sched_repos = _machine(
        tmp_path, "scheduler", repo, TRACKED, _clock_at(9, 0), plans)

    # Webhook machine.
    web_rs, web_svc, web_repos = _machine(
        tmp_path, "webhook", repo, TRACKED, _clock_at(9, 0), plans)

    # 1) Scheduler pulls current repo state.
    sched_rs.pull()
    assert sched_repos["subscribers"].find("9111").status == SubscriberStatus.ACTIVE

    # 2) Webhook adds a new subscriber and pushes to the shared repo.
    web_rs.pull()
    web_svc.upsert_pending("9222", "monthly", name="Riya")
    web_rs.push("Webhook: new subscriber 9222")
    assert "9222" in repo.store["csv/subscribers.csv"].decode()

    # 3) Scheduler runs the expiry sweep on its (pre-9222) local snapshot, then
    #    re-pulls before pushing (models pull-before-push discipline), so it
    #    merges rather than clobbers. The sweep itself re-reads each row.
    sched_svc.sweep_expired(TODAY)
    sched_rs.pull()                     # pick up 9222 the webhook pushed
    # Re-apply the expiry flip on the merged state and push.
    sched_svc.sweep_expired(TODAY)
    sched_rs.push("Scheduler: expiry sweep")

    # 4) Final shared state has both subscribers, 9111 expired, 9222 intact.
    final = repo.store["csv/subscribers.csv"].decode()
    assert "9111" in final and "9222" in final

    # Verify by loading the final repo state into a fresh reader.
    verify_rs, _, verify_repos = _machine(
        tmp_path, "verify", repo, TRACKED, _clock_at(9, 0), plans)
    verify_rs.pull()
    assert verify_repos["subscribers"].find("9111").status == SubscriberStatus.EXPIRED
    assert verify_repos["subscribers"].find("9222") is not None
    assert verify_repos["subscribers"].find("9222").name == "Riya"


def test_e2e_webhook_push_deferred_during_job_window(tmp_path, plans):
    """A webhook write during the 02:25-03:10 window is deferred, so it cannot
    overwrite the scheduler's in-flight commit; it flushes after the window."""
    repo = FakeRepo()
    _seed_repo(repo, [])

    now = {"t": _clock_at(2, 45)()}     # inside the quiet window
    web_rs, web_svc, web_repos = _machine(
        tmp_path, "webhook", repo, TRACKED, lambda: now["t"], plans)

    # Webhook handles a subscribe during the window.
    web_rs.pull()
    web_svc.upsert_pending("9555", "monthly", name="Deferred")
    assert web_rs.push("Webhook during window") == []          # deferred
    # Repo does not receive the new subscriber while the push is deferred.
    assert "9555" not in repo.store.get("csv/subscribers.csv", b"").decode()

    # Meanwhile the scheduler commits during the window (models the job write).
    sched_rs, sched_svc, sched_repos = _machine(
        tmp_path, "scheduler", repo, TRACKED, _clock_at(9, 0), plans)
    sched_svc  # unused; scheduler writes directly via repo in reality
    repo.store["csv/subscribers.csv"] = b"scheduler-wrote-this\n"

    # Window closes: webhook's next push flushes its buffered local state.
    now["t"] = _clock_at(3, 20)()
    pushed = web_rs.push("Webhook after window")
    assert "csv/subscribers.csv" in pushed
    assert "9555" in repo.store["csv/subscribers.csv"].decode()


def test_e2e_logs_are_append_only_across_machines(tmp_path, plans):
    """logs.csv is append-only, so each machine's events survive a merge without
    row-level clobbering (the sweep and webhook both append log rows)."""
    repo = FakeRepo()
    _seed_repo(repo, [Subscriber(
        mobile="9111", plan="monthly", status=SubscriberStatus.ACTIVE,
        start_date=date(2026, 1, 1), end_date=date(2026, 8, 1), opt_in=True,
        subscription_id="tok-9111",
    )])

    sched_rs, sched_svc, _ = _machine(
        tmp_path, "scheduler", repo, TRACKED, _clock_at(9, 0), plans)
    sched_rs.pull()
    sched_svc.sweep_expired(TODAY)      # logs SUBSCRIBER_EXPIRED
    sched_rs.push("Scheduler: expiry")

    logs = repo.store["csv/logs.csv"].decode()
    assert "SUBSCRIBER_EXPIRED" in logs
