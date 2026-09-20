"""Domain-aware three-way merges for CSVs shared by Render and Actions."""
from __future__ import annotations

import csv
import io


DELIVERY_RANK = {
    "": 0, "QUEUED": 0, "PENDING": 0,
    "UNKNOWN": 1, "FAILED": 1,
    "SENT": 2, "DELIVERED": 3, "READ": 4, "CANCELLED": 4,
}


class SemanticMergeConflict(RuntimeError):
    pass


def decode(content: bytes | None) -> list[dict]:
    if not content:
        return []
    return list(csv.DictReader(io.StringIO(content.decode("utf-8")), escapechar="\\"))


def merge_keyed(repository, baseline, remote, *, key_fields, strict=False,
                status_field=None, status_ranks=None, union_fields=()):
    """Merge independent keys and changed fields against their common baseline."""
    key = lambda row: tuple(row.get(field, "") for field in key_fields)
    base = {key(row): row for row in decode(baseline)}
    local = {key(row): row for row in repository.all()}
    incoming = {key(row): row for row in decode(remote)}
    merged = {}
    conflicts = []
    for identity in sorted(set(base) | set(local) | set(incoming)):
        before, ours, theirs = base.get(identity), local.get(identity), incoming.get(identity)
        if before is not None and (ours is None or theirs is None):
            if ours is None and theirs is None:
                continue
            survivor = theirs if ours is None else ours
            if survivor != before:
                conflicts.append(f"{identity}:deleted-row")
                if strict:
                    merged[identity] = survivor
                elif theirs is not None:
                    merged[identity] = theirs
            continue
        if ours is None:
            merged[identity] = theirs
            continue
        if theirs is None:
            merged[identity] = ours
            continue
        old = before or {field: "" for field in repository.fieldnames}
        row = {}
        for field in repository.fieldnames:
            local_value = ours.get(field, "")
            remote_value = theirs.get(field, "")
            old_value = old.get(field, "")
            if field == status_field:
                ranks = status_ranks or DELIVERY_RANK
                row[field] = max(
                    (local_value, remote_value),
                    key=lambda value: ranks.get(str(value).upper(), 0),
                )
                continue
            if field in union_fields:
                values = {
                    item.strip()
                    for value in (local_value, remote_value)
                    for item in str(value).split(",")
                    if item.strip()
                }
                row[field] = ",".join(sorted(values))
                continue
            local_changed = local_value != old_value
            remote_changed = remote_value != old_value
            if local_changed and remote_changed and local_value != remote_value:
                conflicts.append(f"{identity}:{field}")
                row[field] = local_value if strict else remote_value
            elif remote_changed:
                row[field] = remote_value
            else:
                row[field] = local_value
        merged[identity] = row
    if strict and conflicts:
        raise SemanticMergeConflict("CSV merge conflict: " + ", ".join(conflicts))
    rows = [merged[identity] for identity in sorted(merged) if merged[identity] is not None]
    remote_rows = [incoming[identity] for identity in sorted(incoming)]
    repository.replace_memory_rows(rows, dirty=rows != remote_rows)
    return conflicts


def merge_append_only(repository, baseline, remote):
    """Union new immutable events while honoring remote retention pruning."""
    remote_rows = decode(remote)
    fields = repository.fieldnames
    fingerprint = lambda row: tuple(row.get(field, "") for field in fields)
    baseline_markers = {fingerprint(row) for row in decode(baseline)}
    seen = {fingerprint(row) for row in remote_rows}
    rows = list(remote_rows)
    for row in repository.all():
        marker = fingerprint(row)
        # A baseline row absent remotely was intentionally pruned. Only rows
        # created locally after that baseline may be appended again.
        if marker not in seen and marker not in baseline_markers:
            seen.add(marker)
            rows.append(row)
    repository.replace_memory_rows(rows, dirty=rows != remote_rows)
    return []
