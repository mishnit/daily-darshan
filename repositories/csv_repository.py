"""Generic CSV persistence primitive (Tech Doc section 8).

CSVRepository provides read/append/update/find/all over a header-based CSV
file. Writes are atomic (temp file + os.replace) to reduce corruption risk.
Concurrent-writer safety for key uniqueness is provided by append_unique(),
which guards a read-check-append cycle with an exclusive OS file lock.
"""
from __future__ import annotations

import csv
import os
import tempfile
import threading
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone

try:  # POSIX file locking (macOS/Linux)
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows fallback
    _HAVE_FCNTL = False
    try:
        import msvcrt
    except ImportError:
        msvcrt = None


class DuplicateKeyError(Exception):
    """Raised when append_unique detects the key already exists."""


class CSVRepository:
    _instances = weakref.WeakSet()
    _instances_lock = threading.Lock()

    def __init__(self, path: str, fieldnames: list[str], key_field: str, *, timestamp_new: bool = False):
        self.path = path
        self.timestamp_new = timestamp_new
        self.fieldnames = list(fieldnames)
        if timestamp_new and "timestamp" not in self.fieldnames:
            self.fieldnames.append("timestamp")
        self.key_field = key_field
        self._lock_path = f"{self.path}.lock"
        self._ensure_file()
        self._memory_mode = os.environ.get("WEBHOOK_SINGLE_WRITER", "").lower() in {
            "1", "true", "yes",
        }
        self._memory_rows = self._read_disk() if self._memory_mode else None
        self._memory_index = self._build_memory_index() if self._memory_mode else None
        self._dirty = False
        if self._memory_mode:
            with self._instances_lock:
                self._instances.add(self)

    def _ensure_file(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.exists(self.path):
            return
        # Exclusive-create so we never truncate a file another process just
        # created; if we lose that race, the file now exists — that's fine.
        try:
            with open(self.path, "x", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=self.fieldnames, escapechar="\\").writeheader()
        except FileExistsError:
            pass

    def _read_disk(self) -> list[dict]:
        # Tolerate a transient empty/headerless read that can occur if another
        # process is mid os.replace(); DictReader yields fieldnames=None then.
        try:
            with open(self.path, newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh, escapechar="\\")
                if reader.fieldnames is None:
                    return []
                return [row for row in reader if row]
        except FileNotFoundError:
            return []

    def all(self) -> list[dict]:
        if self._memory_mode:
            return [dict(row) for row in self._memory_rows]
        return self._read_disk()

    def _build_memory_index(self) -> dict[str, int]:
        return {
            str(row.get(self.key_field, "")): index
            for index, row in enumerate(self._memory_rows)
        }

    @classmethod
    def flush_all_memory(cls) -> int:
        """Persist dirty single-writer caches immediately before a Git snapshot."""
        with cls._instances_lock:
            instances = list(cls._instances)
        return sum(repository.flush_memory() for repository in instances)

    @classmethod
    def reload_all_memory(cls) -> None:
        """Reload caches after the actor's one-time remote repository pull."""
        with cls._instances_lock:
            instances = list(cls._instances)
        for repository in instances:
            repository.reload_memory()

    def flush_memory(self) -> int:
        if not self._memory_mode or not self._dirty:
            return 0
        self._write_disk(self._memory_rows)
        self._dirty = False
        return 1

    def reload_memory(self) -> None:
        if self._memory_mode:
            self._memory_rows = self._read_disk()
            self._memory_index = self._build_memory_index()
            self._dirty = False

    def replace_memory_rows(self, rows: list[dict], *, dirty: bool) -> None:
        """Install a reconciled remote/local view in single-writer mode."""
        if not self._memory_mode:
            raise RuntimeError("Memory reconciliation requires WEBHOOK_SINGLE_WRITER")
        self._memory_rows = [self._row(row) for row in rows]
        self._memory_index = self._build_memory_index()
        self._dirty = bool(dirty)

    def all_locked(self) -> list[dict]:
        """all() taken under the exclusive lock (consistent snapshot vs writers)."""
        with self._exclusive_lock():
            return self.all()

    def find(self, key) -> dict | None:
        key = str(key)
        if self._memory_mode:
            index = self._memory_index.get(key)
            return None if index is None else dict(self._memory_rows[index])
        for row in self.all():
            if row.get(self.key_field) == key:
                return row
        return None

    def append(self, record: dict) -> None:
        with self._exclusive_lock():
            self._append_unlocked(record)

    def _append_unlocked(self, record: dict) -> None:
        if self.timestamp_new:
            record = dict(record, timestamp=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds"))
        row = self._row(record)
        if self._memory_mode:
            self._memory_rows.append(row)
            self._memory_index[str(row.get(self.key_field, ""))] = len(self._memory_rows) - 1
            self._dirty = True
            return
        with open(self.path, newline="", encoding="utf-8") as source:
            existing = next(csv.reader(source), [])
        if existing != self.fieldnames:
            self._write_all(self.all() + [row])
            return
        with open(self.path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=self.fieldnames, escapechar="\\").writerow(row)

    def _row(self, record: dict) -> dict:
        """Return a CSV-safe row.

        Python's CSV reader rejects NUL bytes outright.  User-supplied names
        can contain them, so normalize only that invalid byte to whitespace;
        higher layers perform their own display sanitization.
        """
        return {
            name: (str(record.get(name, "")).replace("\x00", " ")
                   if record.get(name, "") is not None else "")
            for name in self.fieldnames
        }

    @contextmanager
    def _exclusive_lock(self):
        """Cross-process advisory lock around a critical section.

        Uses a sidecar .lock file so the lock is independent of the data file's
        open/replace lifecycle. Falls back to a no-op only if no locking
        primitive is available on the platform.
        """
        # Render best-effort mode has exactly one state-writer actor. Sender
        # threads operate on immutable snapshots, so local CSV locking only
        # adds filesystem contention there.
        if os.environ.get("WEBHOOK_SINGLE_WRITER", "").lower() in {"1", "true", "yes"}:
            yield
            return
        lock_file = open(self._lock_path, "w")
        try:
            if _HAVE_FCNTL:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            try:
                if _HAVE_FCNTL:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                lock_file.close()

    def append_unique(self, key, record: dict) -> None:
        """Append only if key_field == key does not already exist.

        The read-check-append cycle runs under an exclusive file lock, so
        concurrent writers cannot both insert the same key. Raises
        DuplicateKeyError if the key is already present.
        """
        key = str(key)
        with self._exclusive_lock():
            if self._memory_mode:
                if key in self._memory_index:
                    raise DuplicateKeyError(key)
                self._append_unlocked(record)
                return
            for row in self.all():
                if row.get(self.key_field) == key:
                    raise DuplicateKeyError(key)
            self._append_unlocked(record)

    def update(self, key, record: dict) -> bool:
        """Replace the row whose key_field == key. Returns True if updated."""
        with self._exclusive_lock():
            return self._update_unlocked(key, record)

    def _update_unlocked(self, key, record: dict) -> bool:
        key = str(key)
        if self._memory_mode:
            index = self._memory_index.get(key)
            if index is None:
                return False
            if self.timestamp_new:
                record = dict(record, timestamp=self._memory_rows[index].get("timestamp", ""))
            row = self._row(record)
            self._memory_rows[index] = row
            new_key = str(row.get(self.key_field, ""))
            if new_key != key:
                self._memory_index.pop(key, None)
            self._memory_index[new_key] = index
            self._dirty = True
            return True
        rows = self.all()
        updated = False
        for i, row in enumerate(rows):
            if row.get(self.key_field) == key:
                if self.timestamp_new:
                    record = dict(record, timestamp=row.get("timestamp", ""))
                rows[i] = self._row(record)
                updated = True
                break
        if updated:
            self._write_all(rows)
        return updated

    def upsert(self, key, record: dict) -> None:
        with self._exclusive_lock():
            if not self._update_unlocked(key, record):
                self._append_unlocked(record)

    def delete(self, key) -> bool:
        """Remove the row for key. Returns whether a row was removed."""
        key = str(key)
        with self._exclusive_lock():
            rows = self.all()
            kept = [row for row in rows if row.get(self.key_field) != key]
            if len(kept) == len(rows):
                return False
            self._write_all(kept)
            return True

    def retain(self, predicate) -> int:
        """Atomically keep rows matching ``predicate`` and return removals."""
        with self._exclusive_lock():
            rows = self.all()
            kept = [row for row in rows if predicate(row)]
            removed = len(rows) - len(kept)
            if removed:
                self._write_all(kept)
            return removed

    def update_where(self, predicate, changes: dict) -> int:
        """Atomically update every row matching predicate."""
        with self._exclusive_lock():
            rows = self.all()
            updated = 0
            for row in rows:
                if predicate(row):
                    timestamp = row.get("timestamp", "")
                    row.update(changes)
                    if self.timestamp_new:
                        row["timestamp"] = timestamp
                    updated += 1
            if updated:
                self._write_all(rows)
            return updated

    def _write_all(self, rows: list[dict]) -> None:
        if self._memory_mode:
            self._memory_rows = [self._row(row) for row in rows]
            self._memory_index = self._build_memory_index()
            self._dirty = True
            return
        self._write_disk(rows)

    def _write_disk(self, rows: list[dict]) -> None:
        directory = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=self.fieldnames, escapechar="\\")
                writer.writeheader()
                for row in rows:
                    writer.writerow(self._row(row))
            os.replace(tmp, self.path)  # atomic on same filesystem
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
