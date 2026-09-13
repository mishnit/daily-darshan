"""Drain durable activation notifications after page publication.

PENDING/UNKNOWN attempts require reconciliation, never blind retries.
The caller must serialize repository writes and supply durable persistence.
"""

_NOT_APPLIED = "Activation reference is not applied to subscriber"


def queue_missing_welcomes(container):
    """Create one durable welcome task for every applied ACTIVE payment.

    This makes a manual subscriber CSV activation idempotent: the applied
    payment reference is the unique notification key. Existing subscription
    IDs are never changed, so the subscriber keeps the same personalised page.
    """
    queued = conflicts = 0
    for sub in container.subscribers.all():
        if sub.status.value != "ACTIVE":
            continue
        references = {ref.strip() for ref in sub.applied_payment_refs.split(";") if ref.strip()}
        for reference_id in sorted(references):
            existing = container.welcomes.find(reference_id)
            if existing:
                if existing.get("mobile") != sub.mobile:
                    conflicts += 1
                    continue
                # A premature/manual row may have been cancelled before the
                # activation reference was committed. It is safe to revive
                # only that specific cancellation once the reference exists.
                if existing.get("status") == "CANCELLED" and existing.get("error") == _NOT_APPLIED:
                    existing.update(status="QUEUED", whatsapp_message_id="", error="")
                    container.welcomes.upsert(reference_id, existing)
                    queued += 1
                continue
            container.welcomes.upsert(reference_id, {
                "reference_id": reference_id,
                "mobile": sub.mobile,
                "status": "QUEUED",
                "whatsapp_message_id": "",
                "error": "",
            })
            queued += 1
    return queued, conflicts


def drain_welcomes(container, on_date, persist, publication_check):
    failures = 0
    sentlog = getattr(container, "sentlog", None)
    ledgers = [container.welcomes]
    if sentlog is not None:
        ledgers.append(sentlog)
    container.message_statuses.reconcile(*ledgers)
    # Persist queued intentions and reconciled receipts before any provider call.
    persist()
    for row in container.welcomes.all():
        if row["status"] in {"PENDING", "UNKNOWN"}:
            failures += 1
            continue
        if row["status"] not in {"QUEUED", "FAILED", "CANCELLED"}:
            continue
        sub = container.subscribers.find(row["mobile"])
        if sub and (sub.status.value != "ACTIVE" or not sub.end_date or sub.end_date < on_date):
            if row["status"] != "CANCELLED":
                row.update(status="CANCELLED", error="Activation welcome obsolete: subscription is not active and unexpired")
                container.welcomes.upsert(row["reference_id"], row)
                persist()
            continue
        applied = bool(sub and row["reference_id"] in sub.applied_payment_refs.split(";"))
        if not applied:
            row.update(status="CANCELLED", error=_NOT_APPLIED)
            container.welcomes.upsert(row["reference_id"], row)
            persist()
            continue
        published = bool(publication_check(sub, on_date))
        if published and row.get("publication_verified") != "true":
            row["publication_verified"] = "true"
            container.welcomes.upsert(row["reference_id"], row)
            persist()
        if row["status"] == "CANCELLED":
            continue
        if not sub or not sub.opt_in:
            row.update(status="CANCELLED", error="Subscriber missing or opted out")
            container.welcomes.upsert(row["reference_id"], row)
            persist()
            continue
        if not published:
            failures += 1
            continue
        # Welcome, renewal and delivery share one date+mobile contact slot.
        # A queued welcome keeps first priority on the next day if another
        # message already consumed today's slot.
        if sentlog is not None and sentlog.was_sent(on_date, sub.mobile):
            continue
        row.update(status="PENDING", error="")
        container.welcomes.upsert(row["reference_id"], row)
        reservation = None
        if sentlog is not None:
            reservation = sentlog.reserve(
                on_date, sub.mobile, f"welcome:{row['reference_id']}"
            )
            if reservation is None:
                row.update(status="QUEUED", error="")
                container.welcomes.upsert(row["reference_id"], row)
                persist()
                continue
        persist()
        try:
            result = container.delivery_service.send_welcome(sub)
        except Exception:
            # Both persisted reservations remain blocking if transport crashed.
            failures += 1
            continue
        row.update(status="SENT" if result.ok else "UNKNOWN" if result.unknown else "FAILED",
                   whatsapp_message_id=result.message_id or "", error=result.error or "")
        container.welcomes.upsert(row["reference_id"], row)
        if reservation is not None:
            sentlog.complete(reservation, result)
        persist()
        failures += int(not result.ok)
    return failures
