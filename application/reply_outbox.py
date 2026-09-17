"""Persist production webhook replies together with their conversation state."""
import json
import time
from uuid import uuid4
from application.ports.whatsapp import WhatsAppResult
import logging

log = logging.getLogger(__name__)


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
                
                # Fix: Only compute expiration window if last_inbound is valid (>0)
                last_inbound = float(state.get("last_inbound") or 0)
                expires_at = str(last_inbound + 23 * 3600) if last_inbound > 0 else "0"
                
                row.update(
                    mobile=mobile,
                    version=state.get("version", ""),
                    fingerprint=state_fingerprint(self.container, mobile),
                    expires_at=expires_at,
                    attempts="0",
                    next_attempt="0"
                )
            self.repository.upsert(key, row)
            return WhatsAppResult(ok=True)
        return enqueue


def drain_replies(repository, client, persist, container=None, now=None):
    now = time.time() if now is None else now
    failed = False
    for row in repository.all():
        row_id = row.get("id", "unknown")
        status = row.get("status")

        if status in {"PENDING", "UNKNOWN"}:
            updated_at = float(row.get("updated_at") or 0)
            if updated_at == 0 or (now - updated_at) > 60:
                log.warning(f"drain_replies: Outbox item {row_id} was stuck in {status}. Resetting to FAILED.")
                row["status"] = "FAILED"
                status = "FAILED"
            else:
                failed = True
                continue

        if status not in {"QUEUED", "FAILED"}:
            continue

        if container:
            try:
                from application.conversation_recovery import state_fingerprint
                mobile = row.get("mobile", "")
                state = container.conversations.find(mobile) or {}

                expires_at = float(row.get("expires_at") or 0)
                is_expired = (expires_at > 0 and expires_at <= now)

                version_mismatch = bool(row.get("version")) and (row.get("version") != state.get("version"))
                fingerprint_mismatch = bool(row.get("fingerprint")) and (row.get("fingerprint") != state_fingerprint(container, mobile))

                if version_mismatch or fingerprint_mismatch or is_expired:
                    reason = "expired reply window" if is_expired else "superseded state"
                    row.update(status="CANCELLED", error=f"Superseded state or expired reply window ({reason}); send MENU")
                    repository.upsert(row_id, row)
                    persist()
                    continue
            except Exception as e:
                log.exception(f"drain_replies: Error validating state for {row_id}: {e}")
                failed = True
                continue

        if float(row.get("next_attempt") or 0) > now:
            continue

        attempts = int(row.get("attempts") or 0)
        if attempts >= 5:
            failed = True
            continue

        row["status"] = "PENDING"
        row["attempts"] = str(attempts + 1)
        row["updated_at"] = str(now)
        repository.upsert(row_id, row)
        persist()

        args, kwargs = json.loads(row["arguments"])
        try:
            result = getattr(client, row["method"])(*args, **kwargs)
        except Exception:
            failed = True
            continue

        row.update(
            status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
            whatsapp_message_id=result.message_id or "",
            error=result.error or "",
            next_attempt=str(now + min(3600, 60 * 2 ** attempts))
        )
        repository.upsert(row_id, row)
        persist()
        failed |= not result.ok
    return failed


def prepare_replies(repository, container=None, now=None, mobiles=None, limit=5):
    """Reserve eligible replies for sending in the caller's next commit."""
    now = time.time() if now is None else now
    prepared = []
    failed = False

    for row in repository.all():
        row_id = row.get("id", "unknown")

        # Filter by mobile if specified
        if mobiles is not None and row.get('mobile') not in mobiles:
            continue

        status = row.get("status")

        # Handle stuck PENDING or UNKNOWN messages
        if status in {"PENDING", "UNKNOWN"}:
            updated_at = float(row.get("updated_at") or 0)
            # Fix: Reset if updated_at is missing/0 OR older than 60s
            if updated_at == 0 or (now - updated_at) > 60:
                log.warning(f"Outbox item {row_id} was stuck in {status} (updated_at={updated_at}). Resetting to FAILED for retry.")
                row["status"] = "FAILED"
                status = "FAILED"
            else:
                log.warning(f"Outbox item {row_id} is currently in-flight ({status}).")
                failed = True
                continue

        if status not in {"QUEUED", "FAILED"}:
            continue

        # Validate state and expiration if container is provided
        if container:
            try:
                from application.conversation_recovery import state_fingerprint
                mobile = row.get("mobile", "")
                state = container.conversations.find(mobile) or {}

                expires_at = float(row.get("expires_at") or 0)
                is_expired = (expires_at > 0 and expires_at <= now)

                version_mismatch = bool(row.get("version")) and (row.get("version") != state.get("version"))
                fingerprint_mismatch = bool(row.get("fingerprint")) and (row.get("fingerprint") != state_fingerprint(container, mobile))

                if version_mismatch or fingerprint_mismatch or is_expired:
                    reason = "expired reply window" if is_expired else "superseded state"
                    log.info(f"Cancelling outbox item {row_id}: {reason}")
                    row.update(status="CANCELLED", error=f"Superseded state or expired reply window ({reason}); send MENU")
                    repository.upsert(row_id, row)
                    continue
            except Exception as e:
                log.exception(f"Error validating conversation state for outbox item {row_id}: {e}")
                failed = True
                continue

        # Check attempt backoff
        if float(row.get("next_attempt") or 0) > now:
            continue

        attempts = int(row.get("attempts") or 0)
        if attempts >= 5:
            log.error(f"Outbox item {row_id} has exceeded max attempts ({attempts}/5). Marking failed.")
            failed = True
            continue

        if len(prepared) >= limit:
            continue

        # Mark as PENDING for outbound delivery
        row["status"] = "PENDING"
        row["attempts"] = str(attempts + 1)
        row["updated_at"] = str(now)

        try:
            repository.upsert(row_id, row)
            prepared.append(row_id)
        except Exception as e:
            log.exception(f"Failed to update outbox item {row_id} to PENDING: {e}")
            failed = True

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
