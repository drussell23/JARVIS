"""ActionCommitLedger — durable append-only record of committed actions.

State machine: RESERVED -> COMMITTED | ABORTED | EXPIRED
Transitions are atomic (SQLite transaction).
Storage: SQLite WAL (matches DedupLedger and TriageStateStore patterns).

Lifecycle
---------
1. ``reserve``  — claim a commit slot for an envelope+action pair.
2. ``commit``   — mark the slot as successfully executed.
3. ``abort``    — release the slot (action will not execute).
4. ``expire_stale`` — bulk-transition stale RESERVED rows to EXPIRED.

Pre-execution invariants (``check_pre_exec_invariants``) verify
fencing tokens, lease validity, and duplicate protection *before*
the side-effect fires.

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
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.contracts.decision_envelope import (
    DecisionEnvelope,
    DecisionType,
    IdempotencyKey,
)


# ── Enums ─────────────────────────────────────────────────────────────────


class CommitState(str, Enum):
    """Terminal states for an action commit record."""

    RESERVED = "reserved"
    COMMITTED = "committed"
    ABORTED = "aborted"
    EXPIRED = "expired"


# ── Data ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommitRecord:
    """Immutable snapshot of a single action commit row."""

    commit_id: str
    idempotency_key: str
    envelope_id: str
    trace_id: str
    decision_type: DecisionType
    action: str
    target_id: str
    fencing_token: int
    lock_owner: str
    session_id: str
    expires_at_monotonic: float
    state: CommitState
    reserved_at_epoch: float
    committed_at_epoch: Optional[float]
    outcome: Optional[str]
    abort_reason: Optional[str]
    metadata: Dict[str, Any]


# ── SQL constants ─────────────────────────────────────────────────────────

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS action_commits (
    commit_id            TEXT    PRIMARY KEY,
    idempotency_key      TEXT    NOT NULL,
    envelope_id          TEXT    NOT NULL,
    trace_id             TEXT    NOT NULL,
    decision_type        TEXT    NOT NULL,
    action               TEXT    NOT NULL,
    target_id            TEXT    NOT NULL,
    fencing_token        INTEGER NOT NULL,
    lock_owner           TEXT    NOT NULL,
    session_id           TEXT    NOT NULL,
    expires_at_monotonic REAL    NOT NULL,
    state                TEXT    NOT NULL,
    reserved_at_epoch    REAL    NOT NULL,
    committed_at_epoch   REAL,
    outcome              TEXT,
    abort_reason         TEXT,
    metadata             TEXT    NOT NULL DEFAULT '{}'
)
"""

_CREATE_INDEX_IDEM = """\
CREATE INDEX IF NOT EXISTS idx_action_commits_idempotency_key
ON action_commits (idempotency_key)
"""

_CREATE_INDEX_STATE = """\
CREATE INDEX IF NOT EXISTS idx_action_commits_state
ON action_commits (state)
"""

_CREATE_INDEX_TRACE = """\
CREATE INDEX IF NOT EXISTS idx_action_commits_trace_id
ON action_commits (trace_id)
"""

_INSERT_RESERVE = """\
INSERT INTO action_commits
    (commit_id, idempotency_key, envelope_id, trace_id, decision_type,
     action, target_id, fencing_token, lock_owner, session_id,
     expires_at_monotonic, state, reserved_at_epoch, committed_at_epoch,
     outcome, abort_reason, metadata)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
"""

_UPDATE_COMMIT = """\
UPDATE action_commits
SET state = ?, committed_at_epoch = ?, outcome = ?, metadata = ?
WHERE commit_id = ? AND state = ?
"""

_UPDATE_ABORT = """\
UPDATE action_commits
SET state = ?, abort_reason = ?
WHERE commit_id = ? AND state = ?
"""

_UPDATE_EXPIRE = """\
UPDATE action_commits
SET state = ?
WHERE state = ? AND expires_at_monotonic <= ?
"""

_SELECT_DUPLICATE = """\
SELECT 1 FROM action_commits
WHERE idempotency_key = ? AND state = ?
LIMIT 1
"""

_SELECT_BY_ID = """\
SELECT commit_id, idempotency_key, envelope_id, trace_id, decision_type,
       action, target_id, fencing_token, lock_owner, session_id,
       expires_at_monotonic, state, reserved_at_epoch, committed_at_epoch,
       outcome, abort_reason, metadata
FROM action_commits
WHERE commit_id = ?
"""

_SELECT_COMMITTED_BY_IDEM = """\
SELECT 1 FROM action_commits
WHERE idempotency_key = ? AND state = ?
LIMIT 1
"""


# ── Ledger ────────────────────────────────────────────────────────────────


class ActionCommitLedger:
    """Durable append-only record of committed actions.

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
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute(_CREATE_TABLE)
        self._conn.execute(_CREATE_INDEX_IDEM)
        self._conn.execute(_CREATE_INDEX_STATE)
        self._conn.execute(_CREATE_INDEX_TRACE)
        self._conn.commit()

    async def stop(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── public API ────────────────────────────────────────────────────

    async def reserve(
        self,
        envelope: DecisionEnvelope,
        action: str,
        target_id: str,
        fencing_token: int,
        lock_owner: str,
        session_id: str,
        idempotency_key: IdempotencyKey,
        lease_duration_s: float,
    ) -> str:
        """Reserve a commit slot for an action.

        Parameters
        ----------
        envelope:
            The DecisionEnvelope authorising this action.
        action:
            Short identifier for the action (e.g. ``"apply_label"``).
        target_id:
            Identifier of the target entity being acted upon.
        fencing_token:
            Monotonically increasing token from the lock manager.
        lock_owner:
            Identity of the worker holding the lock.
        session_id:
            Current session identifier.
        idempotency_key:
            Key used to detect duplicate actions.
        lease_duration_s:
            How long (seconds) this reservation is valid.

        Returns
        -------
        str
            A unique ``commit_id`` (UUID4) for this reservation.
        """
        assert self._conn is not None, "Ledger not started"

        commit_id = str(uuid.uuid4())
        now_mono = time.monotonic()
        now_epoch = time.time()
        expires_at = now_mono + lease_duration_s

        async with self._lock:
            self._conn.execute(
                _INSERT_RESERVE,
                (
                    commit_id,
                    idempotency_key.key,
                    envelope.envelope_id,
                    envelope.trace_id,
                    envelope.decision_type.value,
                    action,
                    target_id,
                    fencing_token,
                    lock_owner,
                    session_id,
                    expires_at,
                    CommitState.RESERVED.value,
                    now_epoch,
                    "{}",
                ),
            )
            self._conn.commit()

        return commit_id

    async def commit(
        self,
        commit_id: str,
        outcome: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Transition a RESERVED record to COMMITTED.

        Parameters
        ----------
        commit_id:
            The commit_id returned by ``reserve()``.
        outcome:
            Human-readable description of what happened.
        metadata:
            Optional dictionary of extra data to store alongside the outcome.

        Raises
        ------
        ValueError
            If the record is not in RESERVED state (already committed,
            aborted, or expired).
        """
        assert self._conn is not None, "Ledger not started"

        meta_json = json.dumps(metadata) if metadata else "{}"
        now_epoch = time.time()

        async with self._lock:
            cur = self._conn.execute(
                _UPDATE_COMMIT,
                (
                    CommitState.COMMITTED.value,
                    now_epoch,
                    outcome,
                    meta_json,
                    commit_id,
                    CommitState.RESERVED.value,
                ),
            )
            self._conn.commit()

            if cur.rowcount == 0:
                raise ValueError(
                    f"Commit {commit_id!r} not in RESERVED state "
                    f"(may be already committed, aborted, or expired)"
                )

    async def abort(self, commit_id: str, reason: str) -> None:
        """Transition a RESERVED record to ABORTED.

        Parameters
        ----------
        commit_id:
            The commit_id returned by ``reserve()``.
        reason:
            Human-readable reason for the abort.

        Raises
        ------
        ValueError
            If the record is not in RESERVED state.
        """
        assert self._conn is not None, "Ledger not started"

        async with self._lock:
            cur = self._conn.execute(
                _UPDATE_ABORT,
                (
                    CommitState.ABORTED.value,
                    reason,
                    commit_id,
                    CommitState.RESERVED.value,
                ),
            )
            self._conn.commit()

            if cur.rowcount == 0:
                raise ValueError(
                    f"Commit {commit_id!r} not in RESERVED state "
                    f"(may be already committed, aborted, or expired)"
                )

    async def expire_stale(self) -> int:
        """Bulk-transition expired RESERVED records to EXPIRED.

        Returns
        -------
        int
            Number of rows transitioned.
        """
        assert self._conn is not None, "Ledger not started"

        now_mono = time.monotonic()

        async with self._lock:
            cur = self._conn.execute(
                _UPDATE_EXPIRE,
                (
                    CommitState.EXPIRED.value,
                    CommitState.RESERVED.value,
                    now_mono,
                ),
            )
            self._conn.commit()
            return cur.rowcount

    async def is_duplicate(self, idempotency_key: IdempotencyKey) -> bool:
        """Check if an idempotency key has already been committed.

        Parameters
        ----------
        idempotency_key:
            The key to check.

        Returns
        -------
        bool
            ``True`` if a COMMITTED record exists for this key.
        """
        assert self._conn is not None, "Ledger not started"

        cur = self._conn.execute(
            _SELECT_DUPLICATE,
            (idempotency_key.key, CommitState.COMMITTED.value),
        )
        return cur.fetchone() is not None

    async def check_pre_exec_invariants(
        self, commit_id: str, current_fencing_token: int
    ) -> Tuple[bool, Optional[str]]:
        """Verify pre-execution invariants for a reserved commit.

        Checks:
        1. Fencing token matches the recorded value.
        2. Lease has not expired (monotonic clock).
        3. No other COMMITTED record shares the same idempotency key.

        Parameters
        ----------
        commit_id:
            The commit_id to validate.
        current_fencing_token:
            The caller's current fencing token from the lock manager.

        Returns
        -------
        (bool, Optional[str])
            ``(True, None)`` if all invariants hold, otherwise
            ``(False, reason)`` with a human-readable explanation.
        """
        assert self._conn is not None, "Ledger not started"

        cur = self._conn.execute(_SELECT_BY_ID, (commit_id,))
        row = cur.fetchone()
        if row is None:
            return False, f"Commit {commit_id!r} not found"

        record = self._row_to_record(row)

        # 1. Fencing token
        if record.fencing_token != current_fencing_token:
            return False, (
                f"Fencing token mismatch: record has {record.fencing_token}, "
                f"caller has {current_fencing_token}"
            )

        # 2. Lease expiry
        if time.monotonic() >= record.expires_at_monotonic:
            return False, (
                f"Lease expired for commit {commit_id!r} "
                f"(expires_at_monotonic={record.expires_at_monotonic:.3f})"
            )

        # 3. Duplicate check
        dup_cur = self._conn.execute(
            _SELECT_COMMITTED_BY_IDEM,
            (record.idempotency_key, CommitState.COMMITTED.value),
        )
        if dup_cur.fetchone() is not None:
            return False, (
                f"Duplicate: idempotency key {record.idempotency_key!r} "
                f"already has a COMMITTED record"
            )

        return True, None

    async def query(
        self,
        since_epoch: float,
        decision_type: Optional[DecisionType] = None,
        state: Optional[CommitState] = None,
    ) -> List[CommitRecord]:
        """Query commit records with optional filters.

        Parameters
        ----------
        since_epoch:
            Only return records with ``reserved_at_epoch >= since_epoch``.
        decision_type:
            Optional filter by decision type.
        state:
            Optional filter by commit state.

        Returns
        -------
        List[CommitRecord]
            Matching records, ordered by reserved_at_epoch ascending.
        """
        assert self._conn is not None, "Ledger not started"

        clauses = ["reserved_at_epoch >= ?"]
        params: list = [since_epoch]

        if decision_type is not None:
            clauses.append("decision_type = ?")
            params.append(decision_type.value)

        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)

        where = " AND ".join(clauses)
        sql = (
            "SELECT commit_id, idempotency_key, envelope_id, trace_id, "
            "decision_type, action, target_id, fencing_token, lock_owner, "
            "session_id, expires_at_monotonic, state, reserved_at_epoch, "
            "committed_at_epoch, outcome, abort_reason, metadata "
            f"FROM action_commits WHERE {where} "
            "ORDER BY reserved_at_epoch ASC"
        )

        cur = self._conn.execute(sql, params)
        return [self._row_to_record(row) for row in cur.fetchall()]

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _row_to_record(row: tuple) -> CommitRecord:
        """Convert a raw SQLite row to a ``CommitRecord``."""
        return CommitRecord(
            commit_id=row[0],
            idempotency_key=row[1],
            envelope_id=row[2],
            trace_id=row[3],
            decision_type=DecisionType(row[4]),
            action=row[5],
            target_id=row[6],
            fencing_token=row[7],
            lock_owner=row[8],
            session_id=row[9],
            expires_at_monotonic=row[10],
            state=CommitState(row[11]),
            reserved_at_epoch=row[12],
            committed_at_epoch=row[13],
            outcome=row[14],
            abort_reason=row[15],
            metadata=json.loads(row[16]) if row[16] else {},
        )
