"""Canonical handling for applied payment-reference collections.

Historically these values are persisted as semicolon-separated identifiers.
Accepting commas on read lets the application repair values written by an
older merge implementation without treating a combined value as a new ID.
"""
from __future__ import annotations

import re


_SEPARATORS = re.compile(r"[;,]")


def parse_applied_payment_refs(value: str | None) -> set[str]:
    """Return distinct, trimmed references from canonical or legacy input."""
    return {
        reference.strip()
        for reference in _SEPARATORS.split(value or "")
        if reference.strip()
    }


def serialize_applied_payment_refs(values) -> str:
    """Persist references in their one canonical, deterministic format."""
    return ";".join(sorted({reference for value in values
                            for reference in parse_applied_payment_refs(str(value))}))


def is_canonical_payment_reference(value: str | None) -> bool:
    """A welcome key must represent exactly one unseparated reference."""
    return bool(value and value == value.strip() and not _SEPARATORS.search(value))
