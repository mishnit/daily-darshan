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
                row.update(status="CANCELLED", error="Superseded state or expired reply window; send CONTINUE")
                repository.upsert(row["id"], row)
                persist()
                continue
        if float(row.get("next_attempt") or 0) > now:
            continue
        attempts = int(row.get("attempts") or 0)
        if attempts >= 5:
            failed = True
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
