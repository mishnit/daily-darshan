"""Persist production webhook replies together with their conversation state."""
import json
import time
from uuid import uuid4
from application.ports.whatsapp import WhatsAppResult


class QueuedReplies:
    def __init__(self, repository, container=None):
        self.repository = repository
        self.container = container

    def __getattr__(self, method):
        if not method.startswith("send_"):
            raise AttributeError(method)

        def enqueue(*args, **kwargs):
            key = uuid4().hex
            row = {
                "id": key, "method": method, "arguments": json.dumps([args, kwargs]),
                "status": "QUEUED", "whatsapp_message_id": "", "error": "",
            }
            if self.container:
                from application.conversation_recovery import state_fingerprint
                mobile = str(args[0])
                state = self.container.conversations.find(mobile) or {}
                row.update(mobile=mobile, version=state.get("version", ""),
                           fingerprint=state_fingerprint(self.container, mobile),
                           expires_at=str(float(state.get("last_inbound") or 0) + 23 * 3600),
                           attempts="0", next_attempt="0")
            self.repository.upsert(key, row)
            return WhatsAppResult(ok=True)
        return enqueue


def drain_replies(repository, client, persist, container=None, now=None):
    now = time.time() if now is None else now
    failed = False
    for row in repository.all():
        if row["status"] in {"PENDING", "UNKNOWN"}:
            failed = True
            continue
        if row["status"] not in {"QUEUED", "FAILED"}:
            continue
        if container:
            from application.conversation_recovery import state_fingerprint
            state = container.conversations.find(row.get("mobile", "")) or {}
            if (not row.get("version") or row["version"] != state.get("version")
                    or row.get("fingerprint") != state_fingerprint(container, row.get("mobile", ""))
                    or float(row.get("expires_at") or 0) <= now):
                row.update(status="CANCELLED", error="Superseded state or expired reply window; send MENU")
                repository.upsert(row["id"], row)
                persist()
                continue
        if float(row.get("next_attempt") or 0) > now:
            continue
        attempts = int(row.get("attempts") or 0)
        if attempts >= 4:
            row.update(status='CANCELLED', error='Retry limit reached; send MENU for a fresh response')
            repository.upsert(row['id'], row)
            persist()
            continue
        row["status"] = "PENDING"
        row["attempts"] = str(attempts + 1)
        repository.upsert(row["id"], row)
        persist()
        args, kwargs = json.loads(row["arguments"])
        try:
            result = getattr(client, row["method"])(*args, **kwargs)
        except Exception:
            failed = True
            continue
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "",
                   next_attempt=str(now + min(3600, 60 * 2 ** attempts)))
        repository.upsert(row["id"], row)
        persist()
        failed |= not result.ok
    return failed


def prepare_replies(repository, container=None, now=None, mobiles=None, limit=5, reply_ids=None):
    """Reserve eligible replies for sending in the caller's next commit.

    The returned IDs are safe to send only after that commit succeeds.  This
    lets the webhook persist its inbound state and PENDING send reservations
    atomically, removing a redundant GitHub commit without weakening the
    crash/duplicate safeguard.
    """
    now = time.time() if now is None else now
    prepared = []
    failed = False
    for row in repository.all():
        if reply_ids is not None and row['id'] not in reply_ids:
            continue
        if mobiles is not None and row.get('mobile') not in mobiles:
            continue
        if row["status"] in {"PENDING", "UNKNOWN"}:
            failed = True
            continue
        if row["status"] not in {"QUEUED", "FAILED"}:
            continue
        if container:
            from application.conversation_recovery import state_fingerprint
            state = container.conversations.find(row.get("mobile", "")) or {}
            if (not row.get("version") or row["version"] != state.get("version")
                    or row.get("fingerprint") != state_fingerprint(container, row.get("mobile", ""))
                    or float(row.get("expires_at") or 0) <= now):
                row.update(status="CANCELLED", error="Superseded state or expired reply window; send MENU")
                repository.upsert(row["id"], row)
                continue
        if float(row.get("next_attempt") or 0) > now:
            continue
        attempts = int(row.get("attempts") or 0)
        if attempts >= 4:  # Initial attempt plus at most three retries.
            row.update(status='CANCELLED', error='Retry limit reached; send MENU for a fresh response')
            repository.upsert(row['id'], row)
            continue
        if len(prepared) >= limit:
            continue
        row["status"] = "PENDING"
        row["attempts"] = str(attempts + 1)
        repository.upsert(row["id"], row)
        prepared.append(row["id"])
    return prepared, failed


def send_prepared_replies(repository, client, prepared, now=None):
    """Send replies whose PENDING reservations are already durable."""
    now = time.time() if now is None else now
    failed = False
    for reply_id in prepared:
        row = next((item for item in repository.all() if item["id"] == reply_id), None)
        if row is None or row["status"] != "PENDING":
            failed = True
            continue
        args, kwargs = json.loads(row["arguments"])
        try:
            result = getattr(client, row["method"])(*args, **kwargs)
        except Exception:
            # PENDING is intentionally retained: the provider outcome is not
            # known, so an automatic retry could duplicate the message.
            failed = True
            continue
        attempts = max(1, int(row.get("attempts") or 1))
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "",
                   next_attempt=str(now + min(3600, 60 * 2 ** (attempts - 1))))
        repository.upsert(row["id"], row)
        failed |= not result.ok
    return failed


def snapshot_prepared_replies(repository, prepared):
    """Copy durable PENDING reservations before releasing the state lock.

    The returned dictionaries are detached from the CSV repository, so another
    webhook transaction may safely refresh local files while Meta is called.
    """
    wanted = set(prepared)
    rows = {row["id"]: dict(row) for row in repository.all() if row["id"] in wanted}
    return [rows[reply_id] for reply_id in prepared if reply_id in rows]


def send_reply_snapshots(client, snapshots, now=None):
    """Contact Meta without reading or writing shared CSV state."""
    now = time.time() if now is None else now
    outcomes = []
    failed = False
    for reserved in snapshots:
        row = dict(reserved)
        if row.get("status") != "PENDING":
            failed = True
            continue
        args, kwargs = json.loads(row["arguments"])
        try:
            result = getattr(client, row["method"])(*args, **kwargs)
        except Exception as exc:
            # Keep the durable row PENDING. The provider outcome is ambiguous.
            row.update(status="PENDING", error=f"transport_exception:{type(exc).__name__}")
            outcomes.append(row)
            failed = True
            continue
        attempts = max(1, int(row.get("attempts") or 1))
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "",
                   next_attempt=str(now + min(3600, 60 * 2 ** (attempts - 1))))
        if row['status'] == 'FAILED' and attempts >= 4:
            row['status'] = 'CANCELLED'
        outcomes.append(row)
        failed |= not result.ok
    return outcomes, failed


def merge_reply_outcomes(repository, outcomes):
    """Merge provider results into a freshly pulled snapshot without clobbering it."""
    failed = False
    for outcome in outcomes:
        current = repository.find(outcome["id"])
        if current is None:
            failed = True
            continue
        # A later status callback or reconciliation decision is authoritative.
        if current.get("status") != "PENDING":
            continue
        # Bind the result to the exact reservation that was sent.
        reservation_fields = ("method", "arguments", "mobile", "version", "fingerprint", "attempts")
        if any(current.get(field, "") != outcome.get(field, "") for field in reservation_fields):
            failed = True
            continue
        current.update(
            status=outcome["status"],
            whatsapp_message_id=outcome.get("whatsapp_message_id", ""),
            error=outcome.get("error", ""),
            next_attempt=outcome.get("next_attempt", current.get("next_attempt", "0")),
        )
        repository.upsert(current["id"], current)
    return failed
