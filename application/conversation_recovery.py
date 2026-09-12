"""Conversation freshness and recovery without repeating payment mutations."""
import hashlib
import json


def state_fingerprint(container, mobile):
    sub = container.subscribers.find(mobile)
    payments = [p.to_row() for p in container.payments.all() if p.mobile == mobile]
    payload = [sub.to_row() if sub else None, sorted(payments, key=lambda p: p["reference_id"])]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
