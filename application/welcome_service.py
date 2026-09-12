"""Drain durable activation notifications after page publication.

PENDING/UNKNOWN attempts require reconciliation, never blind retries.
The caller must serialize repository writes and supply durable persistence.
"""


def drain_welcomes(container, on_date, persist, publication_check):
    failures = 0
    container.message_statuses.reconcile(container.welcomes)
    # Persist queued intentions and reconciled receipts before any provider call.
    persist()
    for row in container.welcomes.all():
        if row["status"] in {"PENDING", "UNKNOWN"}:
            failures += 1
            continue
        if row["status"] not in {"QUEUED", "FAILED"}:
            continue
        sub = container.subscribers.find(row["mobile"])
        if not sub or not sub.opt_in:
            row.update(status="CANCELLED", error="Subscriber missing or opted out")
            container.welcomes.upsert(row["reference_id"], row)
            persist()
            continue
        if row["reference_id"] not in sub.applied_payment_refs.split(";"):
            failures += 1
            continue
        if not publication_check(sub, on_date):
            failures += 1
            continue
        row.update(status="PENDING", error="")
        container.welcomes.upsert(row["reference_id"], row)
        persist()
        try:
            result = container.delivery_service.send_welcome(sub)
        except Exception:
            # The persisted reservation remains blocking if the transport crashed.
            failures += 1
            continue
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "")
        container.welcomes.upsert(row["reference_id"], row)
        persist()
        failures += int(not result.ok)
    return failures
