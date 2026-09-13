"""Conversation freshness and recovery without repeating payment mutations."""
import hashlib
import json


def state_fingerprint(container, mobile):
    sub = container.subscribers.find(mobile)
    payments = [p.to_row() for p in container.payments.all() if p.mobile == mobile]
    # Publication can change without changing subscriber/payment rows. Do not
    # send a queued 'page being prepared' reply after publication is confirmed.
    welcomes = [{"reference_id": r["reference_id"], "publication_verified": r.get("publication_verified", ""),
                 "status": r.get("status", "")} for r in container.welcomes.all() if r["mobile"] == mobile]
    payload = [sub.to_row() if sub else None, sorted(payments, key=lambda p: p["reference_id"]),
               sorted(welcomes, key=lambda r: r["reference_id"])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
