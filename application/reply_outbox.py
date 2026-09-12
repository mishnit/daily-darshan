"""Persist production webhook replies together with their conversation state."""
import json
from uuid import uuid4
from application.ports.whatsapp import WhatsAppResult


class QueuedReplies:
    def __init__(self, repository):
        self.repository = repository

    def __getattr__(self, method):
        if not method.startswith("send_"):
            raise AttributeError(method)

        def enqueue(*args, **kwargs):
            key = uuid4().hex
            self.repository.upsert(key, {
                "id": key, "method": method, "arguments": json.dumps([args, kwargs]),
                "status": "QUEUED", "whatsapp_message_id": "", "error": "",
            })
            return WhatsAppResult(ok=True)
        return enqueue


def drain_replies(repository, client, persist):
    failed = False
    for row in repository.all():
        if row["status"] in {"PENDING", "UNKNOWN"}:
            failed = True
            continue
        if row["status"] not in {"QUEUED", "FAILED"}:
            continue
        row["status"] = "PENDING"
        repository.upsert(row["id"], row)
        persist()
        args, kwargs = json.loads(row["arguments"])
        try:
            result = getattr(client, row["method"])(*args, **kwargs)
        except Exception:
            failed = True
            continue
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "")
        repository.upsert(row["id"], row)
        persist()
        failed |= not result.ok
    return failed
