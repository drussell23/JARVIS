"""UMF Dedup Ledger -- SQLite WAL effectively-once delivery guard.

Provides reserve/commit/abort semantics backed by SQLite in WAL mode.
Each inbound UMF message reserves its ``idempotency_key`` before
processing; duplicates are rejected deterministically.

Lifecycle
---------
1. **reserve** -- claim an idempotency key for a message.
2. **commit** -- mark processing as complete with an effect hash.
3. **abort**  -- release the key so the message can be retried.

TTL compaction prevents unbounded growth: ``compact()`` removes
rows whose ``reserved_at_ms + ttl_ms`` is in the past.

Design rules
------------
* Stdlib only (``sqlite3`` ships with Python).
* All public methods are ``async``; an ``asyncio.Lock`` serialises
  writes so that concurrent coroutines get deterministic outcomes.
* The SQLite connection runs in WAL mode with ``busy_timeout=5000``
  for resilience under concurrent readers.
"""
from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from backend.core.umf.types import ReserveResult

# ── Helpers ────────────────────────────────────────────────────────────


def _now_ms() -> int:
    """Current wall-clock time in milliseconds since epoch."""
    return int(time.time() * 1000)


# ── SQL constants ──────────────────────────────────────────────────────

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS dedup_ledger (
    idempotency_key TEXT    NOT NULL,
    message_id      TEXT    PRIMARY KEY,
    reserved_at_ms  INTEGER NOT NULL,
    ttl_ms          INTEGER NOT NULL,
    committed       INTEGER NOT NULL DEFAULT 0,
    effect_hash     TEXT    NOT NULL DEFAULT '',
    aborted         INTEGER NOT NULL DEFAULT 0,
    abort_reason    TEXT    NOT NULL DEFAULT ''
)
"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_dedup_idempotency_key
ON dedup_ledger (idempotency_key)
"""

_SELECT_BY_KEY = """\
SELECT idempotency_key, message_id, reserved_at_ms, ttl_ms,
       committed, effect_hash, aborted, abort_reason
FROM dedup_ledger
WHERE idempotency_key = ?
"""

_INSERT_ROW = """\
INSERT INTO dedup_ledger
    (idempotency_key, message_id, reserved_at_ms, ttl_ms)
VALUES (?, ?, ?, ?)
"""

_UPDATE_COMMIT = """\
UPDATE dedup_ledger
SET committed = 1, effect_hash = ?
WHERE message_id = ?
"""

_UPDATE_ABORT = """\
UPDATE dedup_ledger
SET aborted = 1, abort_reason = ?
WHERE message_id = ?
"""

_SELECT_BY_MSG = """\
SELECT idempotency_key, message_id, reserved_at_ms, ttl_ms,
       committed, effect_hash, aborted, abort_reason
FROM dedup_ledger
WHERE message_id = ?
"""

_DELETE_EXPIRED = """\
DELETE FROM dedup_ledger
WHERE committed = 0 AND aborted = 0
  AND (reserved_at_ms + ttl_ms) < ?
"""

_DELETE_BY_MSG = """\
DELETE FROM dedup_ledger
WHERE message_id = ?
"""

# ── SqliteDedupLedger ──────────────────────────────────────────────────


class SqliteDedupLedger:
    """Effectively-once delivery guard backed by SQLite WAL.

    Parameters
    ----------
    db_path:
        Filesystem path for the SQLite database file.  Created on
        ``start()`` if it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._lock = asyncio.Lock()
        self._conn: Optional[sqlite3.Connection] = None

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        """Open the database, enable WAL mode, and create the schema."""
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX)
        self._conn.commit()

    async def stop(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── public API ────────────────────────────────────────────────────

    async def reserve(
        self,
        idempotency_key: str,
        message_id: str,
        ttl_ms: int,
    ) -> ReserveResult:
        """Attempt to reserve an idempotency key for a message.

        Returns
        -------
        ReserveResult.reserved
            Key successfully claimed for this message.
        ReserveResult.duplicate
            Key already held by a live (non-aborted, non-expired) entry.
        ReserveResult.conflict
            Another message_id raced and won the INSERT.
        """
        assert self._conn is not None, "Ledger not started"
        now = _now_ms()

        async with self._lock:
            cur = self._conn.execute(_SELECT_BY_KEY, (idempotency_key,))
            existing = cur.fetchone()

            if existing is not None:
                (
                    _ik, _mid, reserved_at, row_ttl,
                    committed, _eh, aborted, _ar,
                ) = existing

                if aborted:
                    # Aborted entry -- remove so key can be re-used
                    self._conn.execute(_DELETE_BY_MSG, (_mid,))
                    self._conn.commit()
                elif (now - reserved_at) > row_ttl:
                    # Expired entry -- remove so key can be re-used
                    self._conn.execute(_DELETE_BY_MSG, (_mid,))
                    self._conn.commit()
                else:
                    # Live entry -- duplicate
                    return ReserveResult.duplicate

            try:
                self._conn.execute(
                    _INSERT_ROW, (idempotency_key, message_id, now, ttl_ms)
                )
                self._conn.commit()
                return ReserveResult.reserved
            except sqlite3.IntegrityError:
                return ReserveResult.conflict

    async def commit(self, message_id: str, effect_hash: str) -> None:
        """Mark a reserved entry as successfully committed.

        Parameters
        ----------
        message_id:
            The message whose reservation to mark complete.
        effect_hash:
            An opaque hash of the side-effect produced, used for
            idempotent replay verification.
        """
        assert self._conn is not None, "Ledger not started"
        async with self._lock:
            self._conn.execute(_UPDATE_COMMIT, (effect_hash, message_id))
            self._conn.commit()

    async def abort(self, message_id: str, reason: str) -> None:
        """Abort a reservation so the idempotency key can be re-used.

        Parameters
        ----------
        message_id:
            The message whose reservation to abort.
        reason:
            Human-readable reason for the abort (logged for forensics).
        """
        assert self._conn is not None, "Ledger not started"
        async with self._lock:
            self._conn.execute(_UPDATE_ABORT, (reason, message_id))
            self._conn.commit()

    async def get(self, message_id: str) -> Optional[Dict[str, object]]:
        """Retrieve a ledger row by message_id.

        Returns
        -------
        dict or None
            Column names mapped to values, or ``None`` if not found.
        """
        assert self._conn is not None, "Ledger not started"
        cur = self._conn.execute(_SELECT_BY_MSG, (message_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "idempotency_key": row[0],
            "message_id": row[1],
            "reserved_at_ms": row[2],
            "ttl_ms": row[3],
            "committed": row[4],
            "effect_hash": row[5],
            "aborted": row[6],
            "abort_reason": row[7],
        }

    async def compact(self) -> int:
        """Remove expired, uncommitted, non-aborted rows.

        Returns
        -------
        int
            Number of rows deleted.
        """
        assert self._conn is not None, "Ledger not started"
        now = _now_ms()
        async with self._lock:
            cur = self._conn.execute(_DELETE_EXPIRED, (now,))
            self._conn.commit()
            return cur.rowcount
