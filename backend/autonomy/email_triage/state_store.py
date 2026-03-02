"""Durable state store for the email triage system.

SQLite WAL-based persistence with single-writer architecture.
Follows the ExperienceStore pattern (backend/core/experience_queue.py).

Architecture:
- All writes go through a dedicated asyncio.Queue drained by a single
  background writer task (_writer_loop). This eliminates SQLite write
  contention under load.
- Reads use run_in_executor() directly (safe — WAL allows concurrent
  reads while writer holds lock).
- Write queue is bounded (maxsize=100). If full, oldest non-critical
  writes are dropped with warning (never block the triage cycle).

6 Tables:
  triage_snapshots   — Last N committed snapshots
  dedup_ledger       — Notification dedup records
  interrupt_budget   — Budget tracking
  notification_outbox — Durable pending notifications
  action_ledger      — Full decision audit trail
  sender_reputation  — Learned sender stats

PII Minimization (Gate #7):
  Stored JSON contains only: message_id, sender_domain (NOT full sender),
  tier, score, action, explanation_reasons. No subject/snippet/full address.

Timestamps (Gate #2):
  ALL persisted timestamps use time.time() (wall-clock epoch), NOT
  time.monotonic(). A session_id (UUID per process lifetime) is stored
  alongside snapshots for cross-reboot safety.

Schema Evolution (Gate #9):
  open() reads PRAGMA user_version, compares to expected version, and
  runs migration functions in order. JSON blobs include "_v" field.
  If DB version > expected (future code), enter read-only mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import stat
import time
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger("jarvis.email_triage.state_store")

# Schema version: bump when tables change. Migration functions keyed by
# (from_version, to_version).
_EXPECTED_SCHEMA_VERSION = 1

# Default DB path
_DEFAULT_DB_DIR = os.path.expanduser("~/.jarvis")
_DEFAULT_DB_NAME = "email_triage_state.db"

# Write queue bounds
_DEFAULT_WRITE_QUEUE_SIZE = int(os.getenv("EMAIL_TRIAGE_WRITE_QUEUE_SIZE", "100"))

# Session ID: unique per process lifetime
_SESSION_ID = uuid4().hex[:12]


def _default_db_path() -> str:
    return os.path.join(_DEFAULT_DB_DIR, _DEFAULT_DB_NAME)


# ---------------------------------------------------------------------------
# Write operation types (for the single-writer queue)
# ---------------------------------------------------------------------------

class _WriteOp:
    """Base class for write operations dispatched to the writer loop."""

    __slots__ = ("future",)

    def __init__(self) -> None:
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()


class _SaveSnapshotOp(_WriteOp):
    __slots__ = (
        "cycle_id", "committed_at_epoch", "session_id",
        "triaged_emails_min", "report_summary", "fencing_token",
    )

    def __init__(
        self, cycle_id: str, committed_at_epoch: float, session_id: str,
        triaged_emails_min: str, report_summary: str, fencing_token: int,
    ):
        super().__init__()
        self.cycle_id = cycle_id
        self.committed_at_epoch = committed_at_epoch
        self.session_id = session_id
        self.triaged_emails_min = triaged_emails_min
        self.report_summary = report_summary
        self.fencing_token = fencing_token


class _RecordDedupOp(_WriteOp):
    __slots__ = ("idem_key", "tier", "notified_at")

    def __init__(self, idem_key: str, tier: int, notified_at: float):
        super().__init__()
        self.idem_key = idem_key
        self.tier = tier
        self.notified_at = notified_at


class _RecordInterruptOp(_WriteOp):
    __slots__ = ("timestamp_epoch", "type_")

    def __init__(self, timestamp_epoch: float, type_: str = "hourly"):
        super().__init__()
        self.timestamp_epoch = timestamp_epoch
        self.type_ = type_


class _EnqueueNotificationOp(_WriteOp):
    __slots__ = (
        "message_id", "action", "tier", "sender_domain",
        "created_at_epoch", "expires_at_epoch",
    )

    def __init__(
        self, message_id: str, action: str, tier: int, sender_domain: str,
        created_at_epoch: float, expires_at_epoch: float,
    ):
        super().__init__()
        self.message_id = message_id
        self.action = action
        self.tier = tier
        self.sender_domain = sender_domain
        self.created_at_epoch = created_at_epoch
        self.expires_at_epoch = expires_at_epoch


class _MarkDeliveredOp(_WriteOp):
    __slots__ = ("outbox_id", "delivered_at_epoch")

    def __init__(self, outbox_id: int, delivered_at_epoch: float):
        super().__init__()
        self.outbox_id = outbox_id
        self.delivered_at_epoch = delivered_at_epoch


class _IncrementOutboxAttemptsOp(_WriteOp):
    __slots__ = ("outbox_id", "last_error")

    def __init__(self, outbox_id: int, last_error: str = ""):
        super().__init__()
        self.outbox_id = outbox_id
        self.last_error = last_error


class _RecordActionOp(_WriteOp):
    __slots__ = (
        "cycle_id", "message_id", "tier", "action",
        "explanation_json", "decided_at_epoch",
    )

    def __init__(
        self, cycle_id: str, message_id: str, tier: int, action: str,
        explanation_json: str, decided_at_epoch: float,
    ):
        super().__init__()
        self.cycle_id = cycle_id
        self.message_id = message_id
        self.tier = tier
        self.action = action
        self.explanation_json = explanation_json
        self.decided_at_epoch = decided_at_epoch


class _UpdateSenderReputationOp(_WriteOp):
    __slots__ = ("sender_domain", "tier", "score", "seen_at_epoch")

    def __init__(
        self, sender_domain: str, tier: int, score: float, seen_at_epoch: float,
    ):
        super().__init__()
        self.sender_domain = sender_domain
        self.tier = tier
        self.score = score
        self.seen_at_epoch = seen_at_epoch


class _RunGCOp(_WriteOp):
    __slots__ = ("snapshot_retention", "action_ledger_ttl_s",
                 "outbox_delivered_ttl_s", "outbox_failed_ttl_s",
                 "sender_inactive_ttl_s")

    def __init__(
        self, snapshot_retention: int = 10,
        action_ledger_ttl_s: float = 30 * 86400,
        outbox_delivered_ttl_s: float = 7 * 86400,
        outbox_failed_ttl_s: float = 86400,
        sender_inactive_ttl_s: float = 90 * 86400,
    ):
        super().__init__()
        self.snapshot_retention = snapshot_retention
        self.action_ledger_ttl_s = action_ledger_ttl_s
        self.outbox_delivered_ttl_s = outbox_delivered_ttl_s
        self.outbox_failed_ttl_s = outbox_failed_ttl_s
        self.sender_inactive_ttl_s = sender_inactive_ttl_s


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class TriageStateStore:
    """Durable state store for the email triage system.

    Single-writer architecture: all writes go through an asyncio.Queue
    drained by a single background task. Reads use run_in_executor.
    """

    def __init__(self, db_path: str = ""):
        self._db_path = db_path or _default_db_path()
        self._conn: Optional[sqlite3.Connection] = None
        self._write_queue: asyncio.Queue[_WriteOp] = asyncio.Queue(
            maxsize=_DEFAULT_WRITE_QUEUE_SIZE,
        )
        self._writer_task: Optional[asyncio.Task] = None
        self._closed = False
        self._read_only = False  # Set True if DB version > expected
        self._executor = None  # Uses default ThreadPoolExecutor

    async def open(self) -> None:
        """Open the database, create tables, enable WAL, start writer."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(self._executor, self._open_sync)
        if not self._read_only:
            self._writer_task = asyncio.create_task(
                self._writer_loop(), name="triage_state_writer",
            )

    def _open_sync(self) -> None:
        """Synchronous DB initialization (runs in executor)."""
        # Ensure directory exists
        db_dir = os.path.dirname(self._db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)

        self._conn = sqlite3.connect(
            self._db_path,
            timeout=10.0,
            isolation_level=None,  # autocommit by default, manual txn control
        )
        self._conn.row_factory = sqlite3.Row

        # Enable WAL mode for concurrent reads
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

        # Check schema version (Gate #9)
        db_version = self._conn.execute("PRAGMA user_version").fetchone()[0]

        if db_version > _EXPECTED_SCHEMA_VERSION:
            logger.warning(
                "DB schema version %d > expected %d — entering read-only mode",
                db_version, _EXPECTED_SCHEMA_VERSION,
            )
            self._read_only = True
            return

        # Run migrations if needed
        if db_version < _EXPECTED_SCHEMA_VERSION:
            self._run_migrations(db_version)

        # Create tables (idempotent)
        self._create_tables()

        # Set file permissions (Gate #7: PII concern)
        try:
            os.chmod(self._db_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600
        except OSError:
            pass  # Best-effort on non-Unix

    def _create_tables(self) -> None:
        """Create all 6 tables (idempotent)."""
        c = self._conn
        c.execute("BEGIN")
        try:
            c.execute("""
                CREATE TABLE IF NOT EXISTS triage_snapshots (
                    cycle_id TEXT PRIMARY KEY,
                    committed_at_epoch REAL NOT NULL,
                    session_id TEXT NOT NULL,
                    triaged_emails_min TEXT NOT NULL,
                    report_summary TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS dedup_ledger (
                    idem_key TEXT PRIMARY KEY,
                    last_notified_at REAL NOT NULL,
                    tier INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS interrupt_budget (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp_epoch REAL NOT NULL,
                    type TEXT NOT NULL DEFAULT 'hourly'
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    sender_domain TEXT NOT NULL DEFAULT '',
                    created_at_epoch REAL NOT NULL,
                    delivered_at_epoch REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    expires_at_epoch REAL NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS action_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cycle_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    tier INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    explanation TEXT NOT NULL DEFAULT '{}',
                    decided_at_epoch REAL NOT NULL
                )
            """)

            c.execute("""
                CREATE TABLE IF NOT EXISTS sender_reputation (
                    sender_domain TEXT PRIMARY KEY,
                    total_count INTEGER NOT NULL DEFAULT 0,
                    tier_distribution TEXT NOT NULL DEFAULT '{}',
                    avg_score REAL NOT NULL DEFAULT 0.0,
                    last_seen_epoch REAL NOT NULL,
                    user_outcomes TEXT NOT NULL DEFAULT '{}'
                )
            """)

            # Indexes for common queries
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_snapshots_committed
                ON triage_snapshots(committed_at_epoch DESC)
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON notification_outbox(delivered_at_epoch)
                WHERE delivered_at_epoch IS NULL
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_action_ledger_decided
                ON action_ledger(decided_at_epoch)
            """)
            c.execute("""
                CREATE INDEX IF NOT EXISTS idx_interrupt_budget_ts
                ON interrupt_budget(timestamp_epoch)
            """)

            # Set schema version
            c.execute(f"PRAGMA user_version = {_EXPECTED_SCHEMA_VERSION}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _run_migrations(self, from_version: int) -> None:
        """Run schema migrations from from_version to _EXPECTED_SCHEMA_VERSION."""
        # Currently only version 1 exists. Future migrations go here:
        # if from_version < 2:
        #     self._migrate_v1_to_v2()
        if from_version == 0:
            # Fresh DB, tables will be created by _create_tables
            pass
        logger.info(
            "Schema migration: %d → %d", from_version, _EXPECTED_SCHEMA_VERSION,
        )

    async def close(self) -> None:
        """Close the database and stop the writer task."""
        self._closed = True
        if self._writer_task and not self._writer_task.done():
            # Send sentinel to unblock the writer
            sentinel = _WriteOp()
            sentinel.future = asyncio.get_event_loop().create_future()
            sentinel.future.set_result(None)
            try:
                self._write_queue.put_nowait(sentinel)
            except asyncio.QueueFull:
                pass
            self._writer_task.cancel()
            try:
                await self._writer_task
            except asyncio.CancelledError:
                pass
        if self._conn:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(self._executor, self._conn.close)
            self._conn = None

    # ------------------------------------------------------------------
    # Writer loop (Gate #6: single-writer serialization)
    # ------------------------------------------------------------------

    async def _writer_loop(self) -> None:
        """Single background task that drains the write queue."""
        loop = asyncio.get_event_loop()
        while not self._closed:
            try:
                op = await self._write_queue.get()
                if self._closed:
                    if not op.future.done():
                        op.future.set_result(None)
                    break
                try:
                    result = await loop.run_in_executor(
                        self._executor,
                        partial(self._execute_write, op),
                    )
                    if not op.future.done():
                        op.future.set_result(result)
                except Exception as exc:
                    if not op.future.done():
                        op.future.set_exception(exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Writer loop error: %s", exc, exc_info=True)

    def _execute_write(self, op: _WriteOp) -> Any:
        """Execute a single write operation (runs in executor, single-threaded)."""
        if self._conn is None:
            raise RuntimeError("State store not open")

        if isinstance(op, _SaveSnapshotOp):
            return self._exec_save_snapshot(op)
        elif isinstance(op, _RecordDedupOp):
            return self._exec_record_dedup(op)
        elif isinstance(op, _RecordInterruptOp):
            return self._exec_record_interrupt(op)
        elif isinstance(op, _EnqueueNotificationOp):
            return self._exec_enqueue_notification(op)
        elif isinstance(op, _MarkDeliveredOp):
            return self._exec_mark_delivered(op)
        elif isinstance(op, _IncrementOutboxAttemptsOp):
            return self._exec_increment_attempts(op)
        elif isinstance(op, _RecordActionOp):
            return self._exec_record_action(op)
        elif isinstance(op, _UpdateSenderReputationOp):
            return self._exec_update_sender_reputation(op)
        elif isinstance(op, _RunGCOp):
            return self._exec_run_gc(op)
        else:
            return None

    # ------------------------------------------------------------------
    # Write executors (all run inside _writer_loop → single-threaded)
    # ------------------------------------------------------------------

    def _exec_save_snapshot(self, op: _SaveSnapshotOp) -> Tuple[bool, str]:
        """Atomic fencing + snapshot save (Gate #1).

        BEGIN IMMEDIATE → check max fencing token → INSERT → COMMIT.
        Returns (committed: bool, reason: str).
        """
        c = self._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            row = c.execute(
                "SELECT COALESCE(MAX(fencing_token), 0) FROM triage_snapshots"
            ).fetchone()
            max_token = row[0] if row else 0

            if op.fencing_token < max_token:
                c.execute("ROLLBACK")
                return (False, f"stale_token:{op.fencing_token}<{max_token}")

            c.execute("""
                INSERT OR REPLACE INTO triage_snapshots
                (cycle_id, committed_at_epoch, session_id,
                 triaged_emails_min, report_summary, fencing_token, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                op.cycle_id, op.committed_at_epoch, op.session_id,
                op.triaged_emails_min, op.report_summary,
                op.fencing_token, _EXPECTED_SCHEMA_VERSION,
            ))
            c.execute("COMMIT")
            return (True, "committed")
        except Exception:
            c.execute("ROLLBACK")
            raise

    def _exec_record_dedup(self, op: _RecordDedupOp) -> None:
        c = self._conn
        c.execute("""
            INSERT INTO dedup_ledger (idem_key, last_notified_at, tier, count)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(idem_key) DO UPDATE SET
                last_notified_at = excluded.last_notified_at,
                tier = excluded.tier,
                count = count + 1
        """, (op.idem_key, op.notified_at, op.tier))

    def _exec_record_interrupt(self, op: _RecordInterruptOp) -> None:
        c = self._conn
        c.execute("""
            INSERT INTO interrupt_budget (timestamp_epoch, type)
            VALUES (?, ?)
        """, (op.timestamp_epoch, op.type_))

    def _exec_enqueue_notification(self, op: _EnqueueNotificationOp) -> int:
        c = self._conn
        cursor = c.execute("""
            INSERT INTO notification_outbox
            (message_id, action, tier, sender_domain,
             created_at_epoch, expires_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            op.message_id, op.action, op.tier, op.sender_domain,
            op.created_at_epoch, op.expires_at_epoch,
        ))
        return cursor.lastrowid

    def _exec_mark_delivered(self, op: _MarkDeliveredOp) -> None:
        c = self._conn
        c.execute("""
            UPDATE notification_outbox
            SET delivered_at_epoch = ?
            WHERE id = ?
        """, (op.delivered_at_epoch, op.outbox_id))

    def _exec_increment_attempts(self, op: _IncrementOutboxAttemptsOp) -> None:
        c = self._conn
        c.execute("""
            UPDATE notification_outbox
            SET attempts = attempts + 1, last_error = ?
            WHERE id = ?
        """, (op.last_error, op.outbox_id))

    def _exec_record_action(self, op: _RecordActionOp) -> None:
        c = self._conn
        c.execute("""
            INSERT INTO action_ledger
            (cycle_id, message_id, tier, action, explanation, decided_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            op.cycle_id, op.message_id, op.tier, op.action,
            op.explanation_json, op.decided_at_epoch,
        ))

    def _exec_update_sender_reputation(self, op: _UpdateSenderReputationOp) -> None:
        c = self._conn
        # Upsert: update running average + tier distribution
        existing = c.execute(
            "SELECT total_count, tier_distribution, avg_score FROM sender_reputation WHERE sender_domain = ?",
            (op.sender_domain,),
        ).fetchone()

        if existing:
            total = existing[0] + 1
            dist = json.loads(existing[1])
            dist[str(op.tier)] = dist.get(str(op.tier), 0) + 1
            new_avg = (existing[2] * existing[0] + op.score) / total
            c.execute("""
                UPDATE sender_reputation
                SET total_count = ?, tier_distribution = ?, avg_score = ?,
                    last_seen_epoch = ?
                WHERE sender_domain = ?
            """, (total, json.dumps(dist), new_avg, op.seen_at_epoch, op.sender_domain))
        else:
            dist = {str(op.tier): 1}
            c.execute("""
                INSERT INTO sender_reputation
                (sender_domain, total_count, tier_distribution, avg_score, last_seen_epoch)
                VALUES (?, 1, ?, ?, ?)
            """, (op.sender_domain, json.dumps(dist), op.score, op.seen_at_epoch))

    def _exec_run_gc(self, op: _RunGCOp) -> Dict[str, int]:
        """GC: TTL-based cleanup for all tables (Gate #8)."""
        now = time.time()
        deleted = {}
        c = self._conn

        # triage_snapshots: keep latest N
        c.execute("""
            DELETE FROM triage_snapshots
            WHERE cycle_id NOT IN (
                SELECT cycle_id FROM triage_snapshots
                ORDER BY committed_at_epoch DESC
                LIMIT ?
            )
        """, (op.snapshot_retention,))
        deleted["snapshots"] = c.execute("SELECT changes()").fetchone()[0]

        # action_ledger: TTL
        cutoff = now - op.action_ledger_ttl_s
        c.execute("DELETE FROM action_ledger WHERE decided_at_epoch < ?", (cutoff,))
        deleted["action_ledger"] = c.execute("SELECT changes()").fetchone()[0]

        # notification_outbox: delivered TTL + failed TTL
        delivered_cutoff = now - op.outbox_delivered_ttl_s
        c.execute(
            "DELETE FROM notification_outbox WHERE delivered_at_epoch IS NOT NULL AND delivered_at_epoch < ?",
            (delivered_cutoff,),
        )
        deleted["outbox_delivered"] = c.execute("SELECT changes()").fetchone()[0]

        failed_cutoff = now - op.outbox_failed_ttl_s
        c.execute(
            "DELETE FROM notification_outbox WHERE delivered_at_epoch IS NULL AND created_at_epoch < ?",
            (failed_cutoff,),
        )
        deleted["outbox_failed"] = c.execute("SELECT changes()").fetchone()[0]

        # sender_reputation: inactive TTL
        sender_cutoff = now - op.sender_inactive_ttl_s
        c.execute(
            "DELETE FROM sender_reputation WHERE last_seen_epoch < ?",
            (sender_cutoff,),
        )
        deleted["sender_reputation"] = c.execute("SELECT changes()").fetchone()[0]

        # interrupt_budget: 24h TTL
        budget_cutoff = now - 86400
        c.execute("DELETE FROM interrupt_budget WHERE timestamp_epoch < ?", (budget_cutoff,))
        deleted["interrupt_budget"] = c.execute("SELECT changes()").fetchone()[0]

        # dedup_ledger: expired entries (use max of tier1/tier2 windows + buffer)
        # We can't know exact config here, use 2h as safe max
        dedup_cutoff = now - 7200
        c.execute("DELETE FROM dedup_ledger WHERE last_notified_at < ?", (dedup_cutoff,))
        deleted["dedup_ledger"] = c.execute("SELECT changes()").fetchone()[0]

        return deleted

    # ------------------------------------------------------------------
    # Async write API (enqueues to writer loop)
    # ------------------------------------------------------------------

    async def _enqueue_write(self, op: _WriteOp) -> Any:
        """Enqueue a write operation and wait for result."""
        if self._read_only:
            logger.warning("State store in read-only mode, write dropped")
            return None
        if self._closed:
            return None

        try:
            self._write_queue.put_nowait(op)
        except asyncio.QueueFull:
            logger.warning(
                "Write queue full (%d), dropping %s",
                self._write_queue.qsize(), type(op).__name__,
            )
            return None

        return await op.future

    async def save_snapshot(
        self,
        cycle_id: str,
        report: Any,
        triaged_emails: Dict[str, Any],
        fencing_token: int,
    ) -> Tuple[bool, str]:
        """Save a snapshot with atomic fencing check (Gate #1).

        PII Minimization (Gate #7): Only stores sender_domain, tier, score,
        action. No subject, snippet, or full sender address.

        Returns (committed: bool, reason: str).
        """
        committed_at = time.time()

        # Build PII-minimized triaged_emails
        triaged_min = {}
        for msg_id, triaged in triaged_emails.items():
            triaged_min[msg_id] = {
                "_v": 1,
                "message_id": msg_id,
                "sender_domain": getattr(
                    getattr(triaged, "features", None), "sender_domain", "",
                ),
                "tier": getattr(getattr(triaged, "scoring", None), "tier", 0),
                "score": getattr(getattr(triaged, "scoring", None), "score", 0),
                "action": getattr(triaged, "notification_action", ""),
            }

        # Build report summary (aggregate stats only)
        report_summary = {
            "_v": 1,
            "cycle_id": cycle_id,
            "emails_fetched": getattr(report, "emails_fetched", 0),
            "emails_processed": getattr(report, "emails_processed", 0),
            "tier_counts": dict(getattr(report, "tier_counts", {})),
            "error_count": len(getattr(report, "errors", [])),
            "notifications_sent": getattr(report, "notifications_sent", 0),
        }

        op = _SaveSnapshotOp(
            cycle_id=cycle_id,
            committed_at_epoch=committed_at,
            session_id=_SESSION_ID,
            triaged_emails_min=json.dumps(triaged_min),
            report_summary=json.dumps(report_summary),
            fencing_token=fencing_token,
        )
        result = await self._enqueue_write(op)
        if result is None:
            return (False, "write_dropped")
        return result

    async def load_latest_snapshot(self) -> Optional[Dict[str, Any]]:
        """Load the most recent snapshot from the DB.

        Returns dict with: cycle_id, committed_at_epoch, session_id,
        triaged_emails_min (parsed), report_summary (parsed), fencing_token.
        Or None if no snapshots exist.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self._load_latest_snapshot_sync,
        )

    def _load_latest_snapshot_sync(self) -> Optional[Dict[str, Any]]:
        if self._conn is None:
            return None
        row = self._conn.execute("""
            SELECT cycle_id, committed_at_epoch, session_id,
                   triaged_emails_min, report_summary, fencing_token
            FROM triage_snapshots
            ORDER BY committed_at_epoch DESC
            LIMIT 1
        """).fetchone()
        if row is None:
            return None
        return {
            "cycle_id": row["cycle_id"],
            "committed_at_epoch": row["committed_at_epoch"],
            "session_id": row["session_id"],
            "triaged_emails_min": json.loads(row["triaged_emails_min"]),
            "report_summary": json.loads(row["report_summary"]),
            "fencing_token": row["fencing_token"],
        }

    async def record_dedup(self, idem_key: str, tier: int) -> None:
        """Record a notification for dedup tracking."""
        op = _RecordDedupOp(idem_key, tier, time.time())
        await self._enqueue_write(op)

    async def is_duplicate(self, idem_key: str, tier: int, window_s: float) -> bool:
        """Check if this email was already notified within the dedup window."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(self._is_duplicate_sync, idem_key, tier, window_s),
        )

    def _is_duplicate_sync(
        self, idem_key: str, tier: int, window_s: float,
    ) -> bool:
        if self._conn is None:
            return False
        cutoff = time.time() - window_s
        row = self._conn.execute(
            "SELECT last_notified_at FROM dedup_ledger WHERE idem_key = ? AND last_notified_at > ?",
            (idem_key, cutoff),
        ).fetchone()
        return row is not None

    async def record_interrupt(self, timestamp: Optional[float] = None) -> None:
        """Record an interrupt for budget tracking."""
        op = _RecordInterruptOp(timestamp or time.time())
        await self._enqueue_write(op)

    async def count_interrupts(self, since: float) -> int:
        """Count interrupts since a given timestamp."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(self._count_interrupts_sync, since),
        )

    def _count_interrupts_sync(self, since: float) -> int:
        if self._conn is None:
            return 0
        row = self._conn.execute(
            "SELECT COUNT(*) FROM interrupt_budget WHERE timestamp_epoch > ?",
            (since,),
        ).fetchone()
        return row[0] if row else 0

    async def enqueue_notification(
        self,
        message_id: str,
        action: str,
        tier: int,
        sender_domain: str,
        expires_at: float,
    ) -> Optional[int]:
        """Enqueue a notification for durable delivery. Returns outbox ID."""
        op = _EnqueueNotificationOp(
            message_id, action, tier, sender_domain,
            time.time(), expires_at,
        )
        return await self._enqueue_write(op)

    async def mark_delivered(self, outbox_id: int) -> None:
        """Mark an outbox entry as delivered."""
        op = _MarkDeliveredOp(outbox_id, time.time())
        await self._enqueue_write(op)

    async def increment_outbox_attempts(
        self, outbox_id: int, last_error: str = "",
    ) -> None:
        """Increment retry attempts on an outbox entry."""
        op = _IncrementOutboxAttemptsOp(outbox_id, last_error)
        await self._enqueue_write(op)

    async def get_pending_notifications(
        self, limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Get undelivered outbox entries."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(self._get_pending_notifications_sync, limit),
        )

    def _get_pending_notifications_sync(self, limit: int) -> List[Dict[str, Any]]:
        if self._conn is None:
            return []
        rows = self._conn.execute("""
            SELECT id, message_id, action, tier, sender_domain,
                   created_at_epoch, expires_at_epoch, attempts, last_error
            FROM notification_outbox
            WHERE delivered_at_epoch IS NULL
            ORDER BY created_at_epoch ASC
            LIMIT ?
        """, (limit,)).fetchall()
        return [dict(row) for row in rows]

    async def record_action(
        self,
        cycle_id: str,
        message_id: str,
        tier: int,
        action: str,
        explanation: Any = None,
    ) -> None:
        """Record a policy decision in the action ledger."""
        explanation_json = json.dumps(
            explanation if explanation is not None else {}, default=str,
        )
        op = _RecordActionOp(
            cycle_id, message_id, tier, action,
            explanation_json, time.time(),
        )
        await self._enqueue_write(op)

    async def get_sender_reputation(
        self, domain: str,
    ) -> Optional[Dict[str, Any]]:
        """Get sender reputation stats."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor,
            partial(self._get_sender_reputation_sync, domain),
        )

    def _get_sender_reputation_sync(self, domain: str) -> Optional[Dict[str, Any]]:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM sender_reputation WHERE sender_domain = ?",
            (domain,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["tier_distribution"] = json.loads(result["tier_distribution"])
        result["user_outcomes"] = json.loads(result["user_outcomes"])
        return result

    async def update_sender_reputation(
        self, domain: str, tier: int, score: float,
    ) -> None:
        """Update sender reputation with a new observation."""
        op = _UpdateSenderReputationOp(domain, tier, score, time.time())
        await self._enqueue_write(op)

    async def run_gc(
        self,
        snapshot_retention: Optional[int] = None,
    ) -> Dict[str, int]:
        """Run garbage collection on all tables (Gate #8)."""
        op = _RunGCOp(
            snapshot_retention=snapshot_retention or 10,
        )
        result = await self._enqueue_write(op)
        return result or {}

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    @property
    def session_id(self) -> str:
        return _SESSION_ID

    @property
    def is_read_only(self) -> bool:
        return self._read_only

    @property
    def is_open(self) -> bool:
        return self._conn is not None and not self._closed
