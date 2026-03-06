"""Append-only journal backed by SQLite WAL -- durable mutation log.

Every state-store mutation is recorded as a ``JournalEntry`` in a
monotonically ordered, integrity-checksummed SQLite table.  The WAL
journal mode allows concurrent readers without blocking writers.

Lifecycle
---------
1. **open**  -- connect, set pragmas, create schema, resume revision counter.
2. **append** -- serialize and persist a new entry under the write lock.
3. **close** -- release the SQLite connection.

Design rules
------------
* Stdlib only (``sqlite3`` ships with Python).
* ``threading.Lock`` serializes writes; reads do not require the lock
  because SQLite WAL supports concurrent readers.
* SQL constants are module-level strings for easy auditing.
* Parameterized queries with ``?`` placeholders -- no string formatting.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

from backend.core.reactive_state.types import JournalEntry

# ── SQL constants ──────────────────────────────────────────────────────

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS state_journal (
    global_revision    INTEGER PRIMARY KEY,
    key                TEXT    NOT NULL,
    value              TEXT    NOT NULL,
    previous_value     TEXT    NOT NULL,
    version            INTEGER NOT NULL,
    epoch              INTEGER NOT NULL,
    writer             TEXT    NOT NULL,
    writer_session_id  TEXT    NOT NULL,
    origin             TEXT    NOT NULL,
    consistency_group  TEXT,
    timestamp_unix_ms  INTEGER NOT NULL,
    checksum           TEXT    NOT NULL
)
"""

_CREATE_INDEX_KEY = """\
CREATE INDEX IF NOT EXISTS idx_journal_key
ON state_journal (key, version)
"""

_CREATE_INDEX_EPOCH = """\
CREATE INDEX IF NOT EXISTS idx_journal_epoch
ON state_journal (epoch)
"""

_INSERT_ENTRY = """\
INSERT INTO state_journal
    (global_revision, key, value, previous_value, version, epoch,
     writer, writer_session_id, origin, consistency_group,
     timestamp_unix_ms, checksum)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_SINCE = """\
SELECT global_revision, key, value, previous_value, version, epoch,
       writer, writer_session_id, origin, consistency_group,
       timestamp_unix_ms, checksum
FROM state_journal
WHERE global_revision >= ?
ORDER BY global_revision
"""

_SELECT_BY_KEY = """\
SELECT global_revision, key, value, previous_value, version, epoch,
       writer, writer_session_id, origin, consistency_group,
       timestamp_unix_ms, checksum
FROM state_journal
WHERE key = ?
ORDER BY global_revision
"""

_SELECT_ALL_REVISIONS = """\
SELECT global_revision
FROM state_journal
ORDER BY global_revision
"""

_SELECT_MAX_REVISION = """\
SELECT MAX(global_revision) FROM state_journal
"""


# ── Checksum ───────────────────────────────────────────────────────────


def _compute_checksum(
    global_revision: int,
    key: str,
    value_json: str,
    previous_value_json: str,
    version: int,
    epoch: int,
    writer_session_id: str,
    consistency_group: Optional[str],
) -> str:
    """Deterministic SHA-256 checksum over the entry's identity fields.

    The payload is a JSON array of the fields in a fixed order, serialized
    with ``sort_keys=True`` and compact separators so the hash is
    reproducible regardless of dict ordering or whitespace.
    """
    payload = json.dumps(
        [
            global_revision,
            key,
            value_json,
            previous_value_json,
            version,
            epoch,
            writer_session_id,
            consistency_group,
        ],
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


# ── AppendOnlyJournal ──────────────────────────────────────────────────


class AppendOnlyJournal:
    """Durable append-only mutation journal backed by SQLite WAL.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database file.  Created on
        ``open()`` if it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._next_revision: int = 1

    # ── lifecycle ─────────────────────────────────────────────────────

    def open(self) -> None:
        """Open the database, enable WAL mode, and create the schema.

        Resumes the revision counter from the maximum stored revision so
        that appends continue monotonically after a restart.
        """
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX_KEY)
        self._conn.execute(_CREATE_INDEX_EPOCH)
        self._conn.commit()

        # Resume: pick up where the last session left off.
        cur = self._conn.execute(_SELECT_MAX_REVISION)
        row = cur.fetchone()
        max_rev = row[0] if row[0] is not None else 0
        self._next_revision = max_rev + 1

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── public API ────────────────────────────────────────────────────

    def append(
        self,
        *,
        key: str,
        value: Any,
        previous_value: Any,
        version: int,
        epoch: int,
        writer: str,
        writer_session_id: str,
        origin: str,
        consistency_group: Optional[str] = None,
    ) -> JournalEntry:
        """Append a new entry to the journal.

        Parameters
        ----------
        key:
            The state key that was mutated.
        value:
            New value after mutation (JSON-serializable).
        previous_value:
            Value before mutation (JSON-serializable).
        version:
            Per-key version after this mutation.
        epoch:
            Store epoch at the time of mutation.
        writer:
            Logical writer identity.
        writer_session_id:
            Session-scoped identifier for the writer instance.
        origin:
            How the value was produced (``"explicit"``, ``"default"``,
            ``"derived"``).
        consistency_group:
            Optional group tag for multi-key atomic writes.

        Returns
        -------
        JournalEntry
            The persisted journal entry with deserialized values.
        """
        assert self._conn is not None, "Journal not opened"

        value_json = json.dumps(value, sort_keys=True, separators=(",", ":"))
        previous_value_json = json.dumps(
            previous_value, sort_keys=True, separators=(",", ":")
        )
        timestamp_unix_ms = int(time.time() * 1000)

        with self._lock:
            revision = self._next_revision
            checksum = _compute_checksum(
                revision,
                key,
                value_json,
                previous_value_json,
                version,
                epoch,
                writer_session_id,
                consistency_group,
            )
            self._conn.execute(
                _INSERT_ENTRY,
                (
                    revision,
                    key,
                    value_json,
                    previous_value_json,
                    version,
                    epoch,
                    writer,
                    writer_session_id,
                    origin,
                    consistency_group,
                    timestamp_unix_ms,
                    checksum,
                ),
            )
            self._conn.commit()
            self._next_revision = revision + 1

        return JournalEntry(
            global_revision=revision,
            key=key,
            value=value,
            previous_value=previous_value,
            version=version,
            epoch=epoch,
            writer=writer,
            writer_session_id=writer_session_id,
            origin=origin,
            consistency_group=consistency_group,
            timestamp_unix_ms=timestamp_unix_ms,
            checksum=checksum,
        )

    def latest_revision(self) -> int:
        """Return the most recent global revision number.

        Returns ``0`` when the journal is empty.
        """
        with self._lock:
            return self._next_revision - 1

    def read_since(self, from_revision: int) -> List[JournalEntry]:
        """Return all entries with ``global_revision >= from_revision``.

        Parameters
        ----------
        from_revision:
            Inclusive lower bound on global_revision.

        Returns
        -------
        list[JournalEntry]
            Entries ordered by global_revision ascending.
        """
        assert self._conn is not None, "Journal not opened"
        cur = self._conn.execute(_SELECT_SINCE, (from_revision,))
        return [self._row_to_entry(row) for row in cur.fetchall()]

    def read_key_history(self, key: str) -> List[JournalEntry]:
        """Return all entries for a given key, ordered by revision.

        Parameters
        ----------
        key:
            The state key to query.

        Returns
        -------
        list[JournalEntry]
            All journal entries for this key, ordered ascending.
        """
        assert self._conn is not None, "Journal not opened"
        cur = self._conn.execute(_SELECT_BY_KEY, (key,))
        return [self._row_to_entry(row) for row in cur.fetchall()]

    def validate_no_gaps(self) -> List[str]:
        """Check for gaps in the global revision sequence.

        Returns
        -------
        list[str]
            One description string per detected gap.  Empty if the
            sequence is contiguous (or the journal is empty).
        """
        assert self._conn is not None, "Journal not opened"
        cur = self._conn.execute(_SELECT_ALL_REVISIONS)
        revisions = [row[0] for row in cur.fetchall()]

        gaps: List[str] = []
        for i in range(1, len(revisions)):
            expected = revisions[i - 1] + 1
            actual = revisions[i]
            if actual != expected:
                gaps.append(
                    f"Gap detected: revision {revisions[i - 1]} -> {actual} "
                    f"(expected {expected})"
                )
        return gaps

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _row_to_entry(row: tuple) -> JournalEntry:
        """Convert a raw SQLite row tuple to a ``JournalEntry``.

        JSON-encoded ``value`` and ``previous_value`` columns are
        deserialized back to Python objects.
        """
        return JournalEntry(
            global_revision=row[0],
            key=row[1],
            value=json.loads(row[2]),
            previous_value=json.loads(row[3]),
            version=row[4],
            epoch=row[5],
            writer=row[6],
            writer_session_id=row[7],
            origin=row[8],
            consistency_group=row[9],
            timestamp_unix_ms=row[10],
            checksum=row[11],
        )
