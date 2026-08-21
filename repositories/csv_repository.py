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
from contextlib import contextmanager

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
    def __init__(self, path: str, fieldnames: list[str], key_field: str):
        self.path = path
        self.fieldnames = fieldnames
        self.key_field = key_field
        self._lock_path = f"{self.path}.lock"
        self._ensure_file()

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

    def all(self) -> list[dict]:
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

    def all_locked(self) -> list[dict]:
        """all() taken under the exclusive lock (consistent snapshot vs writers)."""
        with self._exclusive_lock():
            return self.all()

    def find(self, key) -> dict | None:
        key = str(key)
        for row in self.all():
            if row.get(self.key_field) == key:
                return row
        return None

    def append(self, record: dict) -> None:
        row = self._row(record)
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
            for row in self.all():
                if row.get(self.key_field) == key:
                    raise DuplicateKeyError(key)
            self.append(record)

    def update(self, key, record: dict) -> bool:
        """Replace the row whose key_field == key. Returns True if updated."""
        key = str(key)
        rows = self.all()
        updated = False
        for i, row in enumerate(rows):
            if row.get(self.key_field) == key:
                rows[i] = self._row(record)
                updated = True
                break
        if updated:
            self._write_all(rows)
        return updated

    def upsert(self, key, record: dict) -> None:
        if not self.update(key, record):
            self.append(record)

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

    def _write_all(self, rows: list[dict]) -> None:
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
