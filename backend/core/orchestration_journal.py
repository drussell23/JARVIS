# backend/core/orchestration_journal.py
"""
JARVIS Orchestration Journal v1.0
==================================
Append-only SQLite journal for cross-repo lifecycle orchestration.

Provides:
  - Durable journal (source of truth for all state transitions)
  - Lease management with CAS acquisition and epoch fencing
  - Idempotent write support (replay-safe across crashes)
  - Filtered replay for recovery and event distribution

Design doc: docs/plans/2026-02-24-cross-repo-control-plane-design.md
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("jarvis.orchestration_journal")

SCHEMA_VERSION = 2

# Configurable sync mode: NORMAL (dev) or FULL (production)
_SYNC_MODE = os.environ.get("JARVIS_SQLITE_SYNC_MODE", "NORMAL").upper()
if _SYNC_MODE not in ("NORMAL", "FULL"):
    logger.warning("Invalid JARVIS_SQLITE_SYNC_MODE=%r, defaulting to NORMAL", _SYNC_MODE)
    _SYNC_MODE = "NORMAL"

# Lease constants
LEASE_TTL_S = float(os.environ.get("JARVIS_LEASE_TTL_S", "15.0"))
LEASE_RENEW_INTERVAL_S = float(os.environ.get("JARVIS_LEASE_RENEW_S", "5.0"))
LEASE_ACQUIRE_TIMEOUT_S = float(os.environ.get("JARVIS_LEASE_ACQUIRE_TIMEOUT_S", "20.0"))
LEASE_ACQUIRE_RETRY_S = 0.25
MAX_REPLAY_ENTRIES = 1000
VALID_RESULTS = ("pending", "committed", "failed", "superseded")

# Compaction constants
COMPACTION_RETAIN_PRIOR_EPOCHS = int(os.environ.get("JARVIS_JOURNAL_RETAIN_PRIOR", "1000"))
COMPACTION_BATCH_SIZE = int(os.environ.get("JARVIS_JOURNAL_COMPACTION_BATCH_SIZE", "10000"))
COMPACTION_ARCHIVE_ENABLED = os.environ.get("JARVIS_JOURNAL_ARCHIVE_ENABLED", "true").lower() == "true"


class StaleEpochError(Exception):
    """Raised when a write is attempted with an outdated epoch.

    NOT retryable. The correct response is to abdicate leadership.
    """
    pass


@dataclass
class CompactionResult:
    """Result of a journal compaction operation."""
    entries_archived: int
    entries_remaining: int
    duration_s: float


class OrchestrationJournal:
    """Append-only SQLite journal with lease-based epoch fencing.

    Usage:
        journal = OrchestrationJournal()
        await journal.initialize(Path("~/.jarvis/control/orchestration.db"))
        await journal.acquire_lease("supervisor:12345:abc")
        seq = journal.fenced_write("start", "jarvis_prime")
        journal.mark_result(seq, "committed")
        entries = await journal.replay_from(0)
    """

    def __init__(self):
        self._db_path: Optional[Path] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._write_lock = threading.Lock()
        self._epoch: int = 0
        self._holder_id: str = ""
        self._lease_held: bool = False
        self._current_seq: int = 0
        self._shutdown_requested: bool = False
        self._lease_renewal_task: Optional[asyncio.Task] = None
        self._on_lease_lost_callbacks: List[Callable] = []

    # ── Properties ──────────────────────────────────────────────────

    @property
    def epoch(self) -> int:
        return self._epoch

    @property
    def lease_held(self) -> bool:
        return self._lease_held

    @property
    def current_seq(self) -> int:
        return self._current_seq

    @property
    def holder_id(self) -> str:
        return self._holder_id

    # ── Initialization ──────────────────────────────────────────────

    async def initialize(self, db_path: Path) -> None:
        """Create or open the journal database."""
        self._db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_db_sync)

    def _init_db_sync(self) -> None:
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level="DEFERRED",
        )
        self._conn.execute(f"PRAGMA journal_mode = WAL")
        self._conn.execute(f"PRAGMA synchronous = {_SYNC_MODE}")
        self._conn.execute("PRAGMA wal_autocheckpoint = 1000")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._apply_schema()
        self._load_current_seq()

    def _apply_schema(self) -> None:
        c = self._conn
        current_version = 0
        try:
            row = c.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row:
                current_version = row[0]
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet

        if current_version >= SCHEMA_VERSION:
            return  # Already at current version

        if current_version < 1:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS journal (
                    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
                    epoch           INTEGER NOT NULL,
                    timestamp       REAL NOT NULL,
                    wall_clock      TEXT NOT NULL,
                    actor           TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload         TEXT,
                    result          TEXT NOT NULL DEFAULT 'pending',
                    fence_token     INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_journal_epoch ON journal(epoch);
                CREATE INDEX IF NOT EXISTS idx_journal_target ON journal(target, seq);
                CREATE INDEX IF NOT EXISTS idx_journal_idemp
                    ON journal(idempotency_key) WHERE idempotency_key IS NOT NULL;

                CREATE TABLE IF NOT EXISTS component_state (
                    component       TEXT PRIMARY KEY,
                    status          TEXT NOT NULL,
                    epoch           INTEGER NOT NULL,
                    last_seq        INTEGER NOT NULL,
                    pid             INTEGER,
                    endpoint        TEXT,
                    api_version     TEXT,
                    capabilities    TEXT,
                    last_heartbeat  REAL,
                    heartbeat_ttl   REAL NOT NULL DEFAULT 30.0,
                    drain_deadline  REAL,
                    instance_id     TEXT,
                    FOREIGN KEY (last_seq) REFERENCES journal(seq)
                );

                CREATE TABLE IF NOT EXISTS lease (
                    id              INTEGER PRIMARY KEY CHECK (id = 1),
                    holder          TEXT NOT NULL,
                    epoch           INTEGER NOT NULL,
                    acquired_at     REAL NOT NULL,
                    ttl             REAL NOT NULL DEFAULT 15.0,
                    last_renewed    REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS contracts (
                    component       TEXT NOT NULL,
                    contract_type   TEXT NOT NULL,
                    contract_key    TEXT NOT NULL,
                    schema_hash     TEXT NOT NULL,
                    min_version     TEXT,
                    max_version     TEXT,
                    registered_at   REAL NOT NULL,
                    epoch           INTEGER NOT NULL,
                    PRIMARY KEY (component, contract_type, contract_key)
                );

                CREATE TABLE IF NOT EXISTS journal_archive (
                    seq             INTEGER PRIMARY KEY,
                    epoch           INTEGER NOT NULL,
                    timestamp       REAL NOT NULL,
                    wall_clock      TEXT NOT NULL,
                    actor           TEXT NOT NULL,
                    action          TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    idempotency_key TEXT,
                    payload         TEXT,
                    result          TEXT,
                    fence_token     INTEGER NOT NULL,
                    archived_at     REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS schema_version (
                    version     INTEGER PRIMARY KEY,
                    applied_at  TEXT NOT NULL,
                    description TEXT
                );
            """)

            c.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, datetime('now'), ?)",
                (1, "Initial schema: journal, component_state, lease, contracts"),
            )

        if current_version < 2:
            # V2: event_outbox table + component_state extensions for hysteresis
            c.executescript("""
                CREATE TABLE IF NOT EXISTS event_outbox (
                    seq             INTEGER NOT NULL,
                    event_type      TEXT NOT NULL,
                    target          TEXT NOT NULL,
                    payload         TEXT,
                    published       INTEGER NOT NULL DEFAULT 0,
                    published_at    REAL,
                    FOREIGN KEY (seq) REFERENCES journal(seq)
                );

                CREATE INDEX IF NOT EXISTS idx_outbox_unpublished
                    ON event_outbox(published, seq) WHERE published = 0;
            """)

            # Add new columns to component_state (idempotent — ignore if exists)
            for col_def in [
                ("start_timestamp", "REAL"),
                ("consecutive_failures", "INTEGER NOT NULL DEFAULT 0"),
                ("last_probe_category", "TEXT"),
            ]:
                try:
                    c.execute(
                        f"ALTER TABLE component_state ADD COLUMN {col_def[0]} {col_def[1]}"
                    )
                except sqlite3.OperationalError:
                    pass  # Column already exists

            c.execute(
                "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
                "VALUES (?, datetime('now'), ?)",
                (2, "Add event_outbox table and component_state hysteresis columns"),
            )

        c.commit()

    def _load_current_seq(self) -> None:
        row = self._conn.execute("SELECT MAX(seq) FROM journal").fetchone()
        self._current_seq = row[0] if row[0] is not None else 0

    # ── Lease Management ────────────────────────────────────────────

    async def acquire_lease(self, holder_id: str) -> bool:
        """Attempt CAS lease acquisition. Returns True if acquired."""
        self._holder_id = holder_id
        deadline = time.monotonic() + LEASE_ACQUIRE_TIMEOUT_S

        while time.monotonic() < deadline:
            with self._write_lock:
                acquired = self._try_acquire_sync()
                if acquired:
                    return True

            await asyncio.sleep(LEASE_ACQUIRE_RETRY_S)

        return False

    def _try_acquire_sync(self) -> bool:
        c = self._conn
        row = c.execute(
            "SELECT holder, epoch, last_renewed, ttl FROM lease WHERE id = 1"
        ).fetchone()

        now = time.time()

        if row is None:
            # First boot — no lease exists
            new_epoch = 1
            c.execute(
                "INSERT OR IGNORE INTO lease (id, holder, epoch, acquired_at, ttl, last_renewed) "
                "VALUES (1, ?, ?, ?, ?, ?)",
                (self._holder_id, new_epoch, now, LEASE_TTL_S, now),
            )
            if c.execute("SELECT changes()").fetchone()[0] == 1:
                c.commit()
                self._epoch = new_epoch
                self._lease_held = True
                self._write_journal_entry_sync(
                    "lease_acquired", "control_plane",
                    payload={"epoch": new_epoch, "holder": self._holder_id},
                )
                return True
            c.rollback()
            return False

        old_holder, old_epoch, old_last_renewed, ttl = row
        elapsed = now - old_last_renewed

        # We already hold it (re-entrant)
        if old_holder == self._holder_id:
            self._epoch = old_epoch
            self._lease_held = True
            return True

        # Clock regression safety
        if elapsed < 0:
            logger.warning(
                "[Lease] Wall clock regression (%.2fs). Waiting full TTL.", elapsed
            )
            return False

        if elapsed <= ttl:
            return False  # Lease still held by someone else

        # Expired — CAS claim
        new_epoch = old_epoch + 1
        result = c.execute(
            "UPDATE lease SET holder=?, epoch=?, acquired_at=?, "
            "last_renewed=?, ttl=? "
            "WHERE id=1 AND epoch=? AND holder=? AND last_renewed=?",
            (self._holder_id, new_epoch, now, now, LEASE_TTL_S,
             old_epoch, old_holder, old_last_renewed),
        )
        if result.rowcount == 1:
            c.commit()
            self._epoch = new_epoch
            self._lease_held = True
            self._write_journal_entry_sync(
                "lease_acquired", "control_plane",
                payload={
                    "epoch": new_epoch,
                    "old_holder": old_holder,
                    "old_epoch": old_epoch,
                    "reason": "ttl_expired",
                },
            )
            return True
        c.rollback()
        return False

    async def renew_lease(self) -> bool:
        """Renew the lease. Returns False if fenced."""
        with self._write_lock:
            now = time.time()
            result = self._conn.execute(
                "UPDATE lease SET last_renewed=? "
                "WHERE id=1 AND holder=? AND epoch=?",
                (now, self._holder_id, self._epoch),
            )
            if result.rowcount == 1:
                self._conn.commit()
                return True
            self._conn.rollback()
            self._lease_held = False
            return False

    async def release_lease(self) -> None:
        """Voluntarily release the lease."""
        self._shutdown_requested = True
        if self._lease_renewal_task:
            self._lease_renewal_task.cancel()
            try:
                await self._lease_renewal_task
            except asyncio.CancelledError:
                pass
        self._lease_held = False

    async def lease_renewal_loop(self) -> None:
        """Background task: renew lease periodically with jitter."""
        consecutive_failures = 0
        max_failures = 3

        while self._lease_held and not self._shutdown_requested:
            jitter = LEASE_RENEW_INTERVAL_S * 0.2 * (random.random() * 2 - 1)
            await asyncio.sleep(LEASE_RENEW_INTERVAL_S + jitter)

            if self._shutdown_requested:
                return

            try:
                ok = await self.renew_lease()
                if ok:
                    consecutive_failures = 0
                else:
                    self._lease_held = False
                    self._notify_lease_lost("fenced_by_new_epoch")
                    return
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    "[Lease] Renewal failed (%d/%d): %s",
                    consecutive_failures, max_failures, e,
                )
                if consecutive_failures >= max_failures:
                    self._lease_held = False
                    self._notify_lease_lost("consecutive_renewal_failures")
                    return

    def on_lease_lost(self, callback: Callable[[str], None]) -> None:
        """Register callback for lease loss notification."""
        self._on_lease_lost_callbacks.append(callback)

    def _notify_lease_lost(self, reason: str) -> None:
        for cb in self._on_lease_lost_callbacks:
            try:
                cb(reason)
            except Exception as e:
                logger.error("[Lease] Lease-lost callback error: %s", e)

    # ── Fenced Journal Writes ───────────────────────────────────────

    def fenced_write(
        self,
        action: str,
        target: str,
        *,
        idempotency_key: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> int:
        """Write to journal with epoch fence validation.

        Returns journal sequence number.
        Raises StaleEpochError if epoch doesn't match lease.
        """
        with self._write_lock:
            self._verify_epoch()

            # Idempotency check
            if idempotency_key:
                existing = self._conn.execute(
                    "SELECT seq FROM journal "
                    "WHERE idempotency_key=? AND result != 'failed'",
                    (idempotency_key,),
                ).fetchone()
                if existing:
                    return existing[0]

            seq = self._write_journal_entry_sync(
                action, target,
                idempotency_key=idempotency_key,
                payload=payload,
            )
            return seq

    def _verify_epoch(self) -> None:
        """Verify we still hold the lease at current epoch."""
        row = self._conn.execute(
            "SELECT epoch, holder FROM lease WHERE id=1"
        ).fetchone()
        if row is None or row[0] != self._epoch or row[1] != self._holder_id:
            raise StaleEpochError(
                f"Epoch mismatch: ours={self._epoch}, "
                f"current={row[0] if row else 'none'}, "
                f"holder={row[1] if row else 'none'}"
            )

    def _write_journal_entry_sync(
        self,
        action: str,
        target: str,
        *,
        idempotency_key: Optional[str] = None,
        payload: Optional[dict] = None,
    ) -> int:
        now = time.time()
        wall = datetime.fromtimestamp(now, tz=timezone.utc).isoformat()

        cursor = self._conn.execute(
            "INSERT INTO journal "
            "(epoch, timestamp, wall_clock, actor, action, target, "
            "idempotency_key, payload, result, fence_token) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                self._epoch, now, wall, self._holder_id,
                action, target, idempotency_key,
                json.dumps(payload) if payload else None,
                self._epoch,
            ),
        )
        self._conn.commit()
        seq = cursor.lastrowid
        self._current_seq = seq
        return seq

    def mark_result(self, seq: int, result: str) -> None:
        """Mark a journal entry's result. Verifies epoch fence."""
        if result not in VALID_RESULTS:
            raise ValueError(
                f"Invalid result {result!r}. Must be one of {VALID_RESULTS}"
            )
        with self._write_lock:
            self._verify_epoch()
            self._conn.execute(
                "UPDATE journal SET result=? WHERE seq=? AND epoch=?",
                (result, seq, self._epoch),
            )
            self._conn.commit()

    # ── Budget Reservation ───────────────────────────────────────────

    def reserve_budget(
        self,
        estimated_cost: float,
        op_id: str,
        *,
        daily_budget: float,
    ) -> int:
        """Atomically reserve budget via journal entry.

        Returns the journal seq number if reservation succeeds.
        Returns the existing seq if op_id was already reserved (idempotent).
        Returns 0 if budget would be exceeded.
        """
        idemp_key = f"budget_reserve:{op_id}"

        with self._write_lock:
            self._verify_epoch()

            # Idempotency check
            existing = self._conn.execute(
                "SELECT seq FROM journal "
                "WHERE idempotency_key=? AND result != 'failed'",
                (idemp_key,),
            ).fetchone()
            if existing:
                return existing[0]

            # Calculate available budget under the write lock
            available = self._calculate_available_sync(daily_budget)
            if estimated_cost > available:
                return 0

            # Reserve
            seq = self._write_journal_entry_sync(
                "budget_reserved", "budget",
                idempotency_key=idemp_key,
                payload={
                    "op_id": op_id,
                    "estimated_cost": estimated_cost,
                    "daily_budget": daily_budget,
                },
            )
            return seq

    def commit_budget(self, op_id: str, actual_cost: float) -> int:
        """Record actual cost for a previously reserved budget entry.

        Returns the journal seq of the commit entry.
        """
        idemp_key = f"budget_commit:{op_id}"
        return self.fenced_write(
            "budget_committed", "budget",
            idempotency_key=idemp_key,
            payload={"op_id": op_id, "actual_cost": actual_cost},
        )

    def release_budget(self, op_id: str) -> int:
        """Release a previously reserved budget (VM creation failed, etc.).

        Returns the journal seq of the release entry.
        """
        idemp_key = f"budget_release:{op_id}"
        return self.fenced_write(
            "budget_released", "budget",
            idempotency_key=idemp_key,
            payload={"op_id": op_id},
        )

    def calculate_available_budget(self, daily_budget: float) -> float:
        """Calculate remaining budget for today.

        Thread-safe: acquires write lock for consistent read.
        """
        with self._write_lock:
            return self._calculate_available_sync(daily_budget)

    def _calculate_available_sync(self, daily_budget: float) -> float:
        """Internal: calculate available budget (must hold _write_lock).

        Available = daily_budget - committed_costs - reserved_but_uncommitted
        """
        import time as _time
        from datetime import datetime, timezone

        # Today's midnight (UTC) as epoch seconds
        now = _time.time()
        today_start = datetime.fromtimestamp(now, tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()

        # Sum committed costs today
        committed_row = self._conn.execute(
            "SELECT COALESCE(SUM(json_extract(payload, '$.actual_cost')), 0) "
            "FROM journal WHERE action='budget_committed' AND timestamp >= ?",
            (today_start,),
        ).fetchone()
        committed_total = committed_row[0] if committed_row else 0.0

        # Sum reserved-but-not-committed costs today
        # A reservation is "outstanding" if there's a budget_reserved entry
        # with no corresponding budget_committed or budget_released entry
        reserved_row = self._conn.execute(
            """
            SELECT COALESCE(SUM(json_extract(j.payload, '$.estimated_cost')), 0)
            FROM journal j
            WHERE j.action = 'budget_reserved'
              AND j.timestamp >= ?
              AND j.result != 'failed'
              AND NOT EXISTS (
                  SELECT 1 FROM journal j2
                  WHERE (j2.action = 'budget_committed' OR j2.action = 'budget_released')
                    AND json_extract(j2.payload, '$.op_id') = json_extract(j.payload, '$.op_id')
                    AND j2.timestamp >= ?
              )
            """,
            (today_start, today_start),
        ).fetchone()
        reserved_total = reserved_row[0] if reserved_row else 0.0

        return daily_budget - committed_total - reserved_total

    # ── Outbox ───────────────────────────────────────────────────────

    def write_outbox(
        self,
        seq: int,
        event_type: str,
        target: str,
        *,
        payload: Optional[dict] = None,
    ) -> None:
        """Write an event to the outbox (same transaction as journal entry).

        The outbox entry references the journal seq via FK.
        Events remain unpublished until the outbox publisher picks them up.
        """
        with self._write_lock:
            self._conn.execute(
                "INSERT INTO event_outbox (seq, event_type, target, payload, published) "
                "VALUES (?, ?, ?, ?, 0)",
                (seq, event_type, target,
                 json.dumps(payload) if payload else None),
            )
            self._conn.commit()

    def get_unpublished_outbox(self) -> list:
        """Read unpublished outbox entries, ordered by seq."""
        rows = self._conn.execute(
            "SELECT seq, event_type, target, payload "
            "FROM event_outbox WHERE published = 0 ORDER BY seq ASC"
        ).fetchall()
        return [
            {
                "seq": r[0],
                "event_type": r[1],
                "target": r[2],
                "payload": json.loads(r[3]) if r[3] else None,
            }
            for r in rows
        ]

    def mark_outbox_published(self, seq: int) -> None:
        """Mark an outbox entry as published."""
        import time as _time
        with self._write_lock:
            self._conn.execute(
                "UPDATE event_outbox SET published = 1, published_at = ? WHERE seq = ?",
                (_time.time(), seq),
            )
            self._conn.commit()

    # ── Compaction ───────────────────────────────────────────────────

    def compact(self) -> "CompactionResult":
        """Compact journal: archive old entries, retain current epoch + last N from priors.

        Must hold lease. All operations are fenced.
        Archive + delete happen in a single transaction for atomicity.
        """
        start = time.time()

        with self._write_lock:
            self._verify_epoch()

            conn = self._conn

            # Step 1: Count prior-epoch entries
            row = conn.execute(
                "SELECT COUNT(*) FROM journal WHERE epoch < ?",
                (self._epoch,),
            ).fetchone()
            prior_count = row[0] if row else 0

            if prior_count <= COMPACTION_RETAIN_PRIOR_EPOCHS:
                remaining = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
                return CompactionResult(
                    entries_archived=0,
                    entries_remaining=remaining,
                    duration_s=time.time() - start,
                )

            # Step 2: Find boundary seq (keep newest COMPACTION_RETAIN_PRIOR_EPOCHS from prior epochs)
            to_remove = prior_count - COMPACTION_RETAIN_PRIOR_EPOCHS
            boundary_row = conn.execute(
                "SELECT seq FROM journal WHERE epoch < ? "
                "ORDER BY seq ASC LIMIT 1 OFFSET ?",
                (self._epoch, to_remove - 1),
            ).fetchone()

            if boundary_row is None:
                remaining = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
                return CompactionResult(
                    entries_archived=0,
                    entries_remaining=remaining,
                    duration_s=time.time() - start,
                )

            boundary_seq = boundary_row[0]

            # Step 3: FK safety -- update component_state refs that point to compactable entries
            components_to_update = conn.execute(
                "SELECT component, last_seq FROM component_state WHERE last_seq <= ?",
                (boundary_seq,),
            ).fetchall()

            if components_to_update:
                # Find nearest retained seq
                nearest = conn.execute(
                    "SELECT MIN(seq) FROM journal WHERE seq > ?",
                    (boundary_seq,),
                ).fetchone()
                new_seq = nearest[0] if nearest and nearest[0] is not None else boundary_seq + 1

                for comp_name, old_seq in components_to_update:
                    conn.execute(
                        "UPDATE component_state SET last_seq = ? WHERE component = ?",
                        (new_seq, comp_name),
                    )
                    logger.info(
                        "[Journal] Compaction: updated component_state %s last_seq %d -> %d",
                        comp_name, old_seq, new_seq,
                    )

            # Step 4: Archive + delete in single transaction
            archived_count = 0

            # Re-verify epoch (fencing inside transaction)
            self._verify_epoch()

            if COMPACTION_ARCHIVE_ENABLED:
                conn.execute(
                    """
                    INSERT INTO journal_archive (
                        seq, epoch, timestamp, wall_clock, actor, action, target,
                        idempotency_key, payload, result, fence_token, archived_at
                    )
                    SELECT
                        seq, epoch, timestamp, wall_clock, actor, action, target,
                        idempotency_key, payload, result, fence_token, ?
                    FROM journal
                    WHERE seq <= ? AND epoch < ?
                    """,
                    (time.time(), boundary_seq, self._epoch),
                )

            cursor = conn.execute(
                "DELETE FROM journal WHERE seq <= ? AND epoch < ?",
                (boundary_seq, self._epoch),
            )
            archived_count = cursor.rowcount
            conn.commit()

            # Step 5: Reclaim WAL space
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception as exc:
                logger.warning("[Journal] WAL checkpoint failed: %s", exc)

            remaining = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
            duration = time.time() - start

            logger.info(
                "[Journal] Compaction complete: archived=%d, remaining=%d, duration=%.2fs",
                archived_count, remaining, duration,
            )

            return CompactionResult(
                entries_archived=archived_count,
                entries_remaining=remaining,
                duration_s=duration,
            )

    # ── Replay ──────────────────────────────────────────────────────

    async def replay_from(
        self,
        after_seq: int,
        *,
        target_filter: Optional[List[str]] = None,
        action_filter: Optional[List[str]] = None,
    ) -> List[dict]:
        """Read journal entries after a given sequence for replay."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            self._replay_sync, after_seq, target_filter, action_filter,
        )

    def _replay_sync(
        self,
        after_seq: int,
        target_filter: Optional[List[str]],
        action_filter: Optional[List[str]],
    ) -> List[dict]:
        query = "SELECT seq, epoch, action, target, payload, timestamp, result FROM journal WHERE seq > ?"
        params: list = [after_seq]

        if target_filter:
            placeholders = ",".join("?" for _ in target_filter)
            query += f" AND target IN ({placeholders})"
            params.extend(target_filter)

        if action_filter:
            placeholders = ",".join("?" for _ in action_filter)
            query += f" AND action IN ({placeholders})"
            params.extend(action_filter)

        query += " ORDER BY seq ASC LIMIT ?"
        params.append(MAX_REPLAY_ENTRIES)

        rows = self._conn.execute(query, params).fetchall()
        return [
            {
                "seq": r[0],
                "epoch": r[1],
                "action": r[2],
                "target": r[3],
                "payload": json.loads(r[4]) if r[4] else None,
                "timestamp": r[5],
                "result": r[6],
            }
            for r in rows
        ]

    # ── Component State ─────────────────────────────────────────────

    def update_component_state(
        self,
        component: str,
        status: str,
        seq: int,
        **kwargs,
    ) -> None:
        """Update the derived component_state projection."""
        with self._write_lock:
            fields = ["status", "epoch", "last_seq"]
            values = [status, self._epoch, seq]

            for key in ("pid", "endpoint", "api_version", "capabilities",
                        "last_heartbeat", "heartbeat_ttl", "drain_deadline",
                        "instance_id"):
                if key in kwargs:
                    fields.append(key)
                    val = kwargs[key]
                    if key == "capabilities" and isinstance(val, (list, tuple)):
                        val = json.dumps(val)
                    values.append(val)

            set_clause = ", ".join(f"{f}=?" for f in fields)
            insert_fields = ", ".join(["component"] + fields)
            insert_placeholders = ", ".join(["?"] * (len(fields) + 1))

            self._conn.execute(
                f"INSERT INTO component_state ({insert_fields}) "
                f"VALUES ({insert_placeholders}) "
                f"ON CONFLICT(component) DO UPDATE SET {set_clause}",
                [component] + values + values,
            )
            self._conn.commit()

    def get_component_state(self, component: str) -> Optional[dict]:
        """Read current state of a component."""
        row = self._conn.execute(
            "SELECT component, status, epoch, last_seq, pid, endpoint, "
            "api_version, capabilities, last_heartbeat, heartbeat_ttl, "
            "drain_deadline, instance_id "
            "FROM component_state WHERE component=?",
            (component,),
        ).fetchone()
        if row is None:
            return None
        cols = [
            "component", "status", "epoch", "last_seq", "pid", "endpoint",
            "api_version", "capabilities", "last_heartbeat", "heartbeat_ttl",
            "drain_deadline", "instance_id",
        ]
        return dict(zip(cols, row))

    def get_all_component_states(self) -> Dict[str, dict]:
        """Read all component states."""
        rows = self._conn.execute(
            "SELECT component, status, epoch, last_seq, pid, endpoint, "
            "api_version, capabilities, last_heartbeat, heartbeat_ttl, "
            "drain_deadline, instance_id "
            "FROM component_state"
        ).fetchall()
        cols = [
            "component", "status", "epoch", "last_seq", "pid", "endpoint",
            "api_version", "capabilities", "last_heartbeat", "heartbeat_ttl",
            "drain_deadline", "instance_id",
        ]
        return {r[0]: dict(zip(cols, r)) for r in rows}

    # ── Cleanup ─────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close the database connection."""
        self._shutdown_requested = True
        if self._lease_renewal_task:
            self._lease_renewal_task.cancel()
            try:
                await self._lease_renewal_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            self._conn.close()
            self._conn = None
