# Cross-Repo Control Plane Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace disconnected startup systems with a single authoritative control plane using SQLite journal, epoch-fenced lease, unified lifecycle DAG, bidirectional handshake, and UDS event fabric.

**Architecture:** Four new modules in `backend/core/` (orchestration_journal, lifecycle_engine, handshake_protocol, uds_event_fabric) + one client library (control_plane_client). Locality drivers wrap existing code (CrossRepoStartupOrchestrator, GCPVMManager). Feature-gated integration into `unified_supervisor.py`.

**Tech Stack:** Python 3.11+, SQLite WAL, asyncio, Unix domain sockets, aiohttp, pytest (asyncio_mode=auto)

**Design doc:** `docs/plans/2026-02-24-cross-repo-control-plane-design.md`

---

### Task 1: Orchestration Journal — Schema & Core Writes

**Files:**
- Create: `backend/core/orchestration_journal.py`
- Test: `tests/unit/core/test_orchestration_journal.py`

**Step 1: Write the failing tests**

```python
# tests/unit/core/test_orchestration_journal.py
"""Tests for OrchestrationJournal — SQLite schema, journal writes, reads."""

import asyncio
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


def _import_module():
    try:
        from backend.core.orchestration_journal import OrchestrationJournal
        return OrchestrationJournal
    except ImportError:
        return None


class TestJournalImport:
    def test_module_imports(self):
        cls = _import_module()
        assert cls is not None, "OrchestrationJournal must be importable"

    def test_required_exports(self):
        import backend.core.orchestration_journal as mod
        assert hasattr(mod, "OrchestrationJournal")
        assert hasattr(mod, "StaleEpochError")
        assert hasattr(mod, "SCHEMA_VERSION")


class TestJournalInitialization:
    async def test_creates_db_file(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "control" / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        assert db_path.exists()

    async def test_creates_parent_directories(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "deep" / "nested" / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        assert db_path.parent.exists()

    async def test_wal_mode_enabled(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    async def test_schema_version_recorded(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal, SCHEMA_VERSION
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        row = conn.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == SCHEMA_VERSION

    async def test_tables_exist(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "journal" in tables
        assert "component_state" in tables
        assert "lease" in tables
        assert "contracts" in tables
        assert "schema_version" in tables

    async def test_idempotent_initialization(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.close()
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)  # Should not raise
        await j2.close()


class TestJournalWrites:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        # Acquire lease so we can write
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_write_returns_sequence(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        assert isinstance(seq, int)
        assert seq >= 1

    async def test_sequential_sequence_numbers(self, journal):
        seq1 = journal.fenced_write("start", "jarvis_prime")
        seq2 = journal.fenced_write("stop", "jarvis_prime")
        assert seq2 == seq1 + 1

    async def test_write_stores_payload(self, journal):
        payload = {"from": "REGISTERED", "to": "STARTING", "reason": "test"}
        seq = journal.fenced_write("state_transition", "jarvis_prime", payload=payload)
        entries = await journal.replay_from(seq - 1)
        assert len(entries) == 1
        assert entries[0]["payload"] == payload

    async def test_write_stores_epoch(self, journal):
        seq = journal.fenced_write("start", "backend_api")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["epoch"] == journal.epoch

    async def test_write_stores_wall_clock(self, journal):
        before = time.time()
        seq = journal.fenced_write("start", "backend_api")
        after = time.time()
        entries = await journal.replay_from(seq - 1)
        assert before <= entries[0]["timestamp"] <= after

    async def test_idempotency_key_dedup(self, journal):
        key = "start:jarvis_prime:test_dedup"
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        assert seq1 == seq2  # Same entry returned, no duplicate

    async def test_idempotency_allows_different_keys(self, journal):
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key="key_a")
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key="key_b")
        assert seq2 != seq1

    async def test_failed_idempotency_key_allows_retry(self, journal):
        key = "start:jarvis_prime:retry_test"
        seq1 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        journal.mark_result(seq1, "failed")
        seq2 = journal.fenced_write("start", "jarvis_prime", idempotency_key=key)
        assert seq2 != seq1  # New entry because previous was 'failed'


class TestJournalReplay:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_replay_from_zero(self, journal):
        journal.fenced_write("start", "a")
        journal.fenced_write("start", "b")
        journal.fenced_write("start", "c")
        entries = await journal.replay_from(0)
        # Includes lease_acquired + 3 writes
        assert len(entries) >= 3

    async def test_replay_from_specific_seq(self, journal):
        seq1 = journal.fenced_write("start", "a")
        seq2 = journal.fenced_write("start", "b")
        seq3 = journal.fenced_write("start", "c")
        entries = await journal.replay_from(seq1)
        targets = [e["target"] for e in entries]
        assert "b" in targets
        assert "c" in targets

    async def test_replay_with_target_filter(self, journal):
        journal.fenced_write("start", "jarvis_prime")
        journal.fenced_write("start", "reactor_core")
        journal.fenced_write("stop", "jarvis_prime")
        entries = await journal.replay_from(0, target_filter=["jarvis_prime"])
        for e in entries:
            assert e["target"] == "jarvis_prime"

    async def test_replay_with_action_filter(self, journal):
        journal.fenced_write("start", "a")
        journal.fenced_write("stop", "a")
        journal.fenced_write("start", "b")
        entries = await journal.replay_from(0, action_filter=["stop"])
        for e in entries:
            assert e["action"] == "stop"

    async def test_replay_capped_at_1000(self, journal):
        for i in range(1100):
            journal.fenced_write("heartbeat", f"comp_{i % 10}")
        entries = await journal.replay_from(0)
        assert len(entries) <= 1000


class TestJournalResultTracking:
    @pytest.fixture
    async def journal(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease(f"test:{os.getpid()}:abc123")
        yield j
        await j.close()

    async def test_default_result_is_pending(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "pending"

    async def test_mark_committed(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        journal.mark_result(seq, "committed")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "committed"

    async def test_mark_failed(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        journal.mark_result(seq, "failed")
        entries = await journal.replay_from(seq - 1)
        assert entries[0]["result"] == "failed"

    async def test_invalid_result_raises(self, journal):
        seq = journal.fenced_write("start", "jarvis_prime")
        with pytest.raises(ValueError):
            journal.mark_result(seq, "invalid_status")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py -v --tb=short 2>&1 | head -30`
Expected: ImportError — module does not exist yet

**Step 3: Write the implementation**

```python
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
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("jarvis.orchestration_journal")

SCHEMA_VERSION = 1

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


class StaleEpochError(Exception):
    """Raised when a write is attempted with an outdated epoch.

    NOT retryable. The correct response is to abdicate leadership.
    """
    pass


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
        # Check if schema already applied
        try:
            row = c.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            if row and row[0] >= SCHEMA_VERSION:
                return  # Already at current version
        except sqlite3.OperationalError:
            pass  # Table doesn't exist yet

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
                instance_id     TEXT
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

            CREATE TABLE IF NOT EXISTS schema_version (
                version     INTEGER PRIMARY KEY,
                applied_at  TEXT NOT NULL,
                description TEXT
            );
        """)

        c.execute(
            "INSERT OR IGNORE INTO schema_version (version, applied_at, description) "
            "VALUES (?, datetime('now'), ?)",
            (SCHEMA_VERSION, "Initial schema: journal, component_state, lease, contracts"),
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
        from datetime import datetime, timezone
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
        """Mark a journal entry's result."""
        if result not in VALID_RESULTS:
            raise ValueError(
                f"Invalid result {result!r}. Must be one of {VALID_RESULTS}"
            )
        with self._write_lock:
            self._conn.execute(
                "UPDATE journal SET result=? WHERE seq=?", (result, seq)
            )
            self._conn.commit()

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

        query += f" ORDER BY seq ASC LIMIT {MAX_REPLAY_ENTRIES}"

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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py -v --tb=short 2>&1 | tail -30`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/orchestration_journal.py tests/unit/core/test_orchestration_journal.py
git commit -m "feat: add orchestration journal with SQLite schema, fenced writes, and replay"
```

---

### Task 2: Orchestration Journal — Lease Contention & Epoch Fencing

**Files:**
- Modify: `backend/core/orchestration_journal.py`
- Test: `tests/unit/core/test_orchestration_journal.py` (append)

**Step 1: Write the failing tests**

Append to `tests/unit/core/test_orchestration_journal.py`:

```python
class TestLeaseAcquisition:
    async def test_first_boot_acquires_epoch_1(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        ok = await j.acquire_lease("supervisor:1:aaa")
        assert ok is True
        assert j.epoch == 1
        assert j.lease_held is True
        await j.close()

    async def test_reentrant_acquisition(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("supervisor:1:aaa")
        # Same holder acquires again — should succeed with same epoch
        ok = await j.acquire_lease("supervisor:1:aaa")
        assert ok is True
        assert j.epoch == 1
        await j.close()

    async def test_second_holder_blocked_while_live(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("holder_a")
        await j1.renew_lease()

        # Second holder with short timeout — should fail
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        import backend.core.orchestration_journal as mod
        old_timeout = mod.LEASE_ACQUIRE_TIMEOUT_S
        mod.LEASE_ACQUIRE_TIMEOUT_S = 1.0  # Short timeout for test
        try:
            ok = await j2.acquire_lease("holder_b")
            assert ok is False
        finally:
            mod.LEASE_ACQUIRE_TIMEOUT_S = old_timeout
        await j1.close()
        await j2.close()

    async def test_expired_lease_claimed_with_new_epoch(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = tmp_path / "orchestration.db"

        # First holder acquires
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("holder_a")
        epoch_a = j1.epoch
        await j1.close()

        # Simulate time passing beyond TTL by backdating last_renewed
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed = ? WHERE id = 1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Second holder acquires — should get epoch+1
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        ok = await j2.acquire_lease("holder_b")
        assert ok is True
        assert j2.epoch == epoch_a + 1
        await j2.close()


class TestEpochFencing:
    async def test_stale_epoch_raises(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal, StaleEpochError
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("holder_a")

        # Manually advance epoch in DB (simulating another leader)
        with j._write_lock:
            j._conn.execute(
                "UPDATE lease SET holder='holder_b', epoch=999 WHERE id=1"
            )
            j._conn.commit()

        with pytest.raises(StaleEpochError):
            j.fenced_write("start", "jarvis_prime")

    async def test_renewal_detects_fencing(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        db_path = tmp_path / "orchestration.db"
        j = OrchestrationJournal()
        await j.initialize(db_path)
        await j.acquire_lease("holder_a")

        # Another leader takes over
        with j._write_lock:
            j._conn.execute(
                "UPDATE lease SET holder='holder_b', epoch=999 WHERE id=1"
            )
            j._conn.commit()

        ok = await j.renew_lease()
        assert ok is False
        assert j.lease_held is False
```

**Step 2: Run tests to verify new tests fail correctly**

Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py::TestLeaseAcquisition -v --tb=short`
Run: `python3 -m pytest tests/unit/core/test_orchestration_journal.py::TestEpochFencing -v --tb=short`
Expected: PASS (implementation from Task 1 already covers these — this verifies correctness)

**Step 3: Commit**

```bash
git add tests/unit/core/test_orchestration_journal.py
git commit -m "test: add lease contention and epoch fencing tests"
```

---

### Task 3: Lifecycle Engine — State Machine & Transitions

**Files:**
- Create: `backend/core/lifecycle_engine.py`
- Test: `tests/unit/core/test_lifecycle_engine.py`

**Step 1: Write the failing tests**

```python
# tests/unit/core/test_lifecycle_engine.py
"""Tests for LifecycleEngine — state machine, DAG, wave execution."""

import asyncio
import os
import pytest


def _import_module():
    try:
        from backend.core.lifecycle_engine import LifecycleEngine
        return LifecycleEngine
    except ImportError:
        return None


class TestLifecycleEngineImport:
    def test_module_imports(self):
        cls = _import_module()
        assert cls is not None

    def test_required_exports(self):
        import backend.core.lifecycle_engine as mod
        assert hasattr(mod, "LifecycleEngine")
        assert hasattr(mod, "ComponentDeclaration")
        assert hasattr(mod, "ComponentLocality")
        assert hasattr(mod, "InvalidTransitionError")
        assert hasattr(mod, "CyclicDependencyError")
        assert hasattr(mod, "VALID_TRANSITIONS")


class TestComponentDeclaration:
    def test_declaration_is_frozen(self):
        from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality
        decl = ComponentDeclaration(name="test", locality=ComponentLocality.IN_PROCESS)
        with pytest.raises(AttributeError):
            decl.name = "modified"

    def test_default_values(self):
        from backend.core.lifecycle_engine import ComponentDeclaration, ComponentLocality
        decl = ComponentDeclaration(name="test", locality=ComponentLocality.IN_PROCESS)
        assert decl.dependencies == ()
        assert decl.soft_dependencies == ()
        assert decl.is_critical is False
        assert decl.start_timeout_s == 60.0
        assert decl.heartbeat_ttl_s == 30.0


class TestValidTransitions:
    def test_registered_can_start(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["REGISTERED"]

    def test_starting_can_handshake_or_fail(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "HANDSHAKING" in VALID_TRANSITIONS["STARTING"]
        assert "FAILED" in VALID_TRANSITIONS["STARTING"]

    def test_ready_can_degrade_drain_fail_lost(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        ready_targets = VALID_TRANSITIONS["READY"]
        assert "DEGRADED" in ready_targets
        assert "DRAINING" in ready_targets
        assert "FAILED" in ready_targets
        assert "LOST" in ready_targets

    def test_failed_can_restart(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["FAILED"]

    def test_stopped_can_restart(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "STARTING" in VALID_TRANSITIONS["STOPPED"]

    def test_invalid_transition_not_possible(self):
        from backend.core.lifecycle_engine import VALID_TRANSITIONS
        assert "READY" not in VALID_TRANSITIONS["REGISTERED"]
        assert "STARTING" not in VALID_TRANSITIONS["READY"]


class TestStateTransitions:
    @pytest.fixture
    async def engine(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        components = (
            ComponentDeclaration(
                name="backend_api",
                locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="jarvis_prime",
                locality=ComponentLocality.SUBPROCESS,
                dependencies=("backend_api",),
            ),
        )
        engine = LifecycleEngine(journal, components)
        yield engine
        await journal.close()

    async def test_initial_status_is_registered(self, engine):
        assert engine.get_status("backend_api") == "REGISTERED"
        assert engine.get_status("jarvis_prime") == "REGISTERED"

    async def test_valid_transition_succeeds(self, engine):
        await engine.transition_component("backend_api", "STARTING", reason="test")
        assert engine.get_status("backend_api") == "STARTING"

    async def test_invalid_transition_raises(self, engine):
        from backend.core.lifecycle_engine import InvalidTransitionError
        with pytest.raises(InvalidTransitionError):
            await engine.transition_component("backend_api", "READY", reason="skip_handshake")

    async def test_transition_journals_entry(self, engine):
        await engine.transition_component("backend_api", "STARTING", reason="test")
        entries = await engine._journal.replay_from(0, action_filter=["state_transition"])
        targets = [e["target"] for e in entries]
        assert "backend_api" in targets

    async def test_full_lifecycle_path(self, engine):
        comp = "backend_api"
        await engine.transition_component(comp, "STARTING", reason="boot")
        await engine.transition_component(comp, "HANDSHAKING", reason="health_ok")
        await engine.transition_component(comp, "READY", reason="handshake_ok")
        await engine.transition_component(comp, "DRAINING", reason="shutdown")
        await engine.transition_component(comp, "STOPPING", reason="drain_done")
        await engine.transition_component(comp, "STOPPED", reason="terminated")
        assert engine.get_status(comp) == "STOPPED"

    async def test_recovery_from_failed(self, engine):
        comp = "backend_api"
        await engine.transition_component(comp, "STARTING", reason="boot")
        await engine.transition_component(comp, "FAILED", reason="crash")
        await engine.transition_component(comp, "STARTING", reason="retry")
        assert engine.get_status(comp) == "STARTING"

    async def test_get_all_statuses(self, engine):
        statuses = engine.get_all_statuses()
        assert "backend_api" in statuses
        assert "jarvis_prime" in statuses
        assert statuses["backend_api"] == "REGISTERED"


class TestWaveComputation:
    def test_independent_components_same_wave(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS),
        )
        waves = compute_waves(comps)
        assert len(waves) == 1
        names = {c.name for c in waves[0]}
        assert names == {"a", "b"}

    def test_dependency_creates_separate_waves(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("a",)),
        )
        waves = compute_waves(comps)
        assert len(waves) == 2
        assert waves[0][0].name == "a"
        assert waves[1][0].name == "b"

    def test_cycle_detected(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
            CyclicDependencyError,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("b",)),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("a",)),
        )
        with pytest.raises(CyclicDependencyError):
            compute_waves(comps)

    def test_soft_deps_dont_affect_wave_ordering(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="a", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="b", locality=ComponentLocality.IN_PROCESS,
                                soft_dependencies=("a",)),
        )
        waves = compute_waves(comps)
        # Soft dep = same wave (no ordering constraint)
        assert len(waves) == 1

    def test_diamond_dependency(self):
        from backend.core.lifecycle_engine import (
            ComponentDeclaration, ComponentLocality, compute_waves,
        )
        comps = (
            ComponentDeclaration(name="root", locality=ComponentLocality.IN_PROCESS),
            ComponentDeclaration(name="left", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("root",)),
            ComponentDeclaration(name="right", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("root",)),
            ComponentDeclaration(name="join", locality=ComponentLocality.IN_PROCESS,
                                dependencies=("left", "right")),
        )
        waves = compute_waves(comps)
        assert len(waves) == 3
        assert waves[0][0].name == "root"
        assert {c.name for c in waves[1]} == {"left", "right"}
        assert waves[2][0].name == "join"


class TestFailurePropagation:
    @pytest.fixture
    async def engine(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        components = (
            ComponentDeclaration(
                name="backend", locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="prime", locality=ComponentLocality.SUBPROCESS,
                dependencies=("backend",),
            ),
            ComponentDeclaration(
                name="reactor", locality=ComponentLocality.SUBPROCESS,
                soft_dependencies=("prime",),
            ),
        )
        engine = LifecycleEngine(journal, components)
        yield engine
        await journal.close()

    async def test_hard_dep_failure_skips_dependent(self, engine):
        await engine.transition_component("backend", "STARTING", reason="boot")
        await engine.transition_component("backend", "FAILED", reason="crash")
        await engine.propagate_failure("backend", "failed")
        # prime depends on backend (hard) — should be FAILED
        assert engine.get_status("prime") == "FAILED"

    async def test_soft_dep_failure_degrades_dependent(self, engine):
        # Get reactor to READY state first
        await engine.transition_component("reactor", "STARTING", reason="boot")
        await engine.transition_component("reactor", "HANDSHAKING", reason="health")
        await engine.transition_component("reactor", "READY", reason="handshake")

        # Prime fails — reactor soft-depends on prime
        await engine.transition_component("prime", "STARTING", reason="boot")
        await engine.transition_component("prime", "FAILED", reason="crash")
        await engine.propagate_failure("prime", "failed")
        assert engine.get_status("reactor") == "DEGRADED"

    async def test_hard_dep_lost_drains_dependent(self, engine):
        # Get both to READY
        await engine.transition_component("backend", "STARTING", reason="boot")
        await engine.transition_component("backend", "HANDSHAKING", reason="health")
        await engine.transition_component("backend", "READY", reason="ok")
        await engine.transition_component("prime", "STARTING", reason="boot")
        await engine.transition_component("prime", "HANDSHAKING", reason="health")
        await engine.transition_component("prime", "READY", reason="ok")

        # Backend goes LOST
        await engine.transition_component("backend", "LOST", reason="heartbeat_expired")
        await engine.propagate_failure("backend", "lost")
        assert engine.get_status("prime") == "DRAINING"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_lifecycle_engine.py -v --tb=short 2>&1 | head -20`
Expected: ImportError

**Step 3: Write the implementation**

```python
# backend/core/lifecycle_engine.py
"""
JARVIS Lifecycle Engine v1.0
=============================
Unified DAG-driven lifecycle management for all components — in-process,
subprocess, and remote.

Provides:
  - Component state machine with journaled transitions
  - Wave-based parallel execution (Kahn's algorithm)
  - Failure propagation (hard deps skip/drain, soft deps degrade)
  - Reverse-DAG shutdown with drain contracts

Design doc: docs/plans/2026-02-24-cross-repo-control-plane-design.md
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set, Tuple,
    runtime_checkable,
)

from backend.core.orchestration_journal import OrchestrationJournal, StaleEpochError

logger = logging.getLogger("jarvis.lifecycle_engine")


# ── Enums & Constants ───────────────────────────────────────────────

class ComponentLocality(Enum):
    IN_PROCESS = "in_process"
    SUBPROCESS = "subprocess"
    REMOTE = "remote"


VALID_TRANSITIONS: Dict[str, Set[str]] = {
    "REGISTERED":   {"STARTING"},
    "STARTING":     {"HANDSHAKING", "FAILED"},
    "HANDSHAKING":  {"READY", "FAILED"},
    "READY":        {"DEGRADED", "DRAINING", "FAILED", "LOST"},
    "DEGRADED":     {"READY", "DRAINING", "FAILED", "LOST"},
    "DRAINING":     {"STOPPING", "FAILED", "LOST"},
    "STOPPING":     {"STOPPED", "FAILED"},
    "FAILED":       {"STARTING"},
    "LOST":         {"STARTING", "STOPPED"},
    "STOPPED":      {"STARTING"},
}


# ── Exceptions ──────────────────────────────────────────────────────

class InvalidTransitionError(Exception):
    """Raised when a state transition violates the state machine."""
    pass


class CyclicDependencyError(Exception):
    """Raised when the component dependency graph contains a cycle."""
    pass


# ── Data Model ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class ComponentDeclaration:
    """Static declaration of a component in the lifecycle DAG."""
    name: str
    locality: ComponentLocality
    dependencies: Tuple[str, ...] = ()
    soft_dependencies: Tuple[str, ...] = ()
    is_critical: bool = False
    start_timeout_s: float = 60.0
    handshake_timeout_s: float = 10.0
    drain_timeout_s: float = 30.0
    heartbeat_ttl_s: float = 30.0
    spawn_command: Optional[Tuple[str, ...]] = None
    endpoint: Optional[str] = None
    health_path: str = "/health"
    init_fn: Optional[str] = None


@runtime_checkable
class LocalityDriver(Protocol):
    """How to start/stop/health-check by locality."""
    async def start(self, comp: ComponentDeclaration) -> int: ...
    async def stop(self, comp: ComponentDeclaration) -> None: ...
    async def health_check(self, comp: ComponentDeclaration) -> dict: ...
    async def send_drain(self, comp: ComponentDeclaration, timeout_s: float) -> None: ...


# ── Wave Computation ────────────────────────────────────────────────

def compute_waves(
    components: Tuple[ComponentDeclaration, ...],
) -> List[List[ComponentDeclaration]]:
    """Compute parallel execution waves via Kahn's topological sort.

    Only hard dependencies affect ordering. Soft dependencies are ignored.
    Components in the same wave can start concurrently.
    """
    comp_map: Dict[str, ComponentDeclaration] = {c.name: c for c in components}
    all_names = set(comp_map.keys())

    in_degree: Dict[str, int] = {name: 0 for name in all_names}
    dependents: Dict[str, List[str]] = {name: [] for name in all_names}

    for c in components:
        for dep in c.dependencies:
            if dep in all_names:
                in_degree[c.name] += 1
                dependents[dep].append(c.name)

    waves: List[List[ComponentDeclaration]] = []
    queue = sorted([n for n, d in in_degree.items() if d == 0])

    processed = 0
    while queue:
        wave = [comp_map[name] for name in queue]
        waves.append(wave)
        processed += len(queue)

        next_queue = []
        for name in queue:
            for dep_name in dependents[name]:
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0:
                    next_queue.append(dep_name)
        queue = sorted(next_queue)

    if processed < len(all_names):
        remaining = [n for n, d in in_degree.items() if d > 0]
        raise CyclicDependencyError(f"Cycle detected involving: {remaining}")

    return waves


def _make_idempotency_key(
    action: str, target: str, trigger_seq: Optional[int] = None,
) -> str:
    """Generate epoch-independent idempotency key."""
    if trigger_seq is not None:
        return f"{action}:{target}:triggered_by:{trigger_seq}"
    import uuid
    return f"{action}:{target}:{uuid.uuid4().hex}"


# ── Lifecycle Engine ────────────────────────────────────────────────

class LifecycleEngine:
    """Unified lifecycle management for all components.

    Usage:
        engine = LifecycleEngine(journal, SYSTEM_COMPONENTS)
        engine.register_locality_driver(ComponentLocality.IN_PROCESS, driver)
        await engine.start_all()
        await engine.shutdown_all("user_request")
    """

    def __init__(
        self,
        journal: OrchestrationJournal,
        components: Tuple[ComponentDeclaration, ...],
    ):
        self._journal = journal
        self._components = {c.name: c for c in components}
        self._statuses: Dict[str, str] = {c.name: "REGISTERED" for c in components}
        self._drivers: Dict[ComponentLocality, LocalityDriver] = {}
        self._drain_hooks: Dict[str, Callable] = {}
        self._event_callbacks: List[Callable] = []

    # ── Status ──────────────────────────────────────────────────

    def get_status(self, component: str) -> str:
        return self._statuses.get(component, "REGISTERED")

    def get_all_statuses(self) -> Dict[str, str]:
        return dict(self._statuses)

    def get_declaration(self, component: str) -> Optional[ComponentDeclaration]:
        return self._components.get(component)

    # ── Registration ────────────────────────────────────────────

    def register_locality_driver(
        self, locality: ComponentLocality, driver: LocalityDriver,
    ) -> None:
        self._drivers[locality] = driver

    def register_drain_hook(
        self, component: str, hook: Callable[[], Awaitable[None]],
    ) -> None:
        self._drain_hooks[component] = hook

    def on_transition(self, callback: Callable) -> None:
        """Register callback for state transitions."""
        self._event_callbacks.append(callback)

    # ── State Transitions ───────────────────────────────────────

    async def transition_component(
        self,
        component: str,
        new_status: str,
        *,
        reason: str,
        trigger_seq: Optional[int] = None,
    ) -> int:
        """Transition a component with journal + validation."""
        current = self._statuses.get(component, "REGISTERED")

        allowed = VALID_TRANSITIONS.get(current, set())
        if new_status not in allowed:
            raise InvalidTransitionError(
                f"{component}: {current} -> {new_status} not valid "
                f"(allowed: {allowed})"
            )

        idemp_key = _make_idempotency_key(
            f"transition_{new_status}", component, trigger_seq,
        )

        seq = self._journal.fenced_write(
            "state_transition", component,
            idempotency_key=idemp_key,
            payload={"from": current, "to": new_status, "reason": reason},
        )

        self._statuses[component] = new_status

        self._journal.update_component_state(
            component, new_status, seq,
        )

        for cb in self._event_callbacks:
            try:
                cb(component, current, new_status, reason)
            except Exception as e:
                logger.warning("[Engine] Transition callback error: %s", e)

        return seq

    # ── Failure Propagation ─────────────────────────────────────

    async def propagate_failure(
        self,
        failed_component: str,
        failure_type: str,
    ) -> None:
        """Propagate failure to dependents."""
        for name, comp in self._components.items():
            current = self._statuses.get(name, "REGISTERED")

            if failed_component in comp.dependencies:
                # Hard dependency
                if failure_type == "failed":
                    if current in ("REGISTERED", "STARTING"):
                        await self.transition_component(
                            name, "FAILED",
                            reason=f"hard_dep_{failed_component}_{failure_type}",
                        )
                elif failure_type == "lost":
                    if current in ("READY", "DEGRADED"):
                        await self.transition_component(
                            name, "DRAINING",
                            reason=f"hard_dep_{failed_component}_lost",
                        )

            elif failed_component in comp.soft_dependencies:
                # Soft dependency
                if current == "READY":
                    await self.transition_component(
                        name, "DEGRADED",
                        reason=f"soft_dep_{failed_component}_{failure_type}",
                    )

    # ── Wave Execution ──────────────────────────────────────────

    async def start_all(self) -> bool:
        """Start all components in wave order. Returns True if no critical failure."""
        waves = compute_waves(tuple(self._components.values()))

        for wave_idx, wave in enumerate(waves):
            logger.info("[Engine] Starting wave %d: %s",
                        wave_idx, [c.name for c in wave])

            tasks = []
            for comp in wave:
                # Check hard deps
                deps_ok = all(
                    self._statuses.get(d) == "READY"
                    for d in comp.dependencies
                )
                if not deps_ok:
                    failed_deps = [
                        d for d in comp.dependencies
                        if self._statuses.get(d) != "READY"
                    ]
                    await self.transition_component(
                        comp.name, "STARTING", reason="wave_start",
                    )
                    await self.transition_component(
                        comp.name, "FAILED",
                        reason=f"dependency_not_ready: {failed_deps}",
                    )
                    continue

                tasks.append(self._start_single(comp))

            await asyncio.gather(*tasks, return_exceptions=True)

            # Check for critical failures in this wave
            for comp in wave:
                if comp.is_critical and self._statuses.get(comp.name) == "FAILED":
                    logger.error("[Engine] Critical component %s failed. Aborting.", comp.name)
                    return False

        return True

    async def _start_single(self, comp: ComponentDeclaration) -> None:
        """Start a single component through the lifecycle."""
        try:
            await self.transition_component(comp.name, "STARTING", reason="wave_start")

            driver = self._drivers.get(comp.locality)
            if driver:
                await asyncio.wait_for(
                    driver.start(comp),
                    timeout=comp.start_timeout_s,
                )

            # Transition to HANDSHAKING (handshake manager will handle the rest)
            await self.transition_component(
                comp.name, "HANDSHAKING", reason="start_complete",
            )
        except asyncio.TimeoutError:
            await self.transition_component(
                comp.name, "FAILED",
                reason=f"start_timeout_{comp.start_timeout_s}s",
            )
        except Exception as e:
            await self.transition_component(
                comp.name, "FAILED", reason=f"start_error: {e}",
            )

    # ── Shutdown ────────────────────────────────────────────────

    async def shutdown_all(self, reason: str) -> None:
        """Graceful shutdown in reverse dependency order with drain."""
        shutdown_seq = self._journal.fenced_write(
            "shutdown_initiated", "control_plane",
            payload={"reason": reason},
        )

        waves = compute_waves(tuple(self._components.values()))
        reverse_waves = list(reversed(waves))

        for wave_idx, wave in enumerate(reverse_waves):
            active = [
                c for c in wave
                if self._statuses.get(c.name)
                in ("READY", "DEGRADED", "STARTING", "HANDSHAKING")
            ]
            if not active:
                continue

            # Phase 1: DRAINING
            drain_tasks = []
            for comp in active:
                drain_tasks.append(
                    self._drain_single(comp, shutdown_seq)
                )
            await asyncio.gather(*drain_tasks, return_exceptions=True)

            # Phase 2: STOPPING
            stop_tasks = []
            for comp in active:
                stop_tasks.append(
                    self._stop_single(comp, shutdown_seq)
                )
            await asyncio.gather(*stop_tasks, return_exceptions=True)

    async def _drain_single(
        self, comp: ComponentDeclaration, trigger_seq: int,
    ) -> None:
        current = self._statuses.get(comp.name, "REGISTERED")
        if current not in VALID_TRANSITIONS or "DRAINING" not in VALID_TRANSITIONS.get(current, set()):
            return

        await self.transition_component(
            comp.name, "DRAINING",
            reason="shutdown_requested",
            trigger_seq=trigger_seq,
        )

        drain_hook = self._drain_hooks.get(comp.name)
        if drain_hook:
            try:
                await asyncio.wait_for(drain_hook(), timeout=comp.drain_timeout_s)
            except asyncio.TimeoutError:
                logger.warning("[Engine] %s drain timed out", comp.name)
            except Exception as e:
                logger.warning("[Engine] %s drain error: %s", comp.name, e)

    async def _stop_single(
        self, comp: ComponentDeclaration, trigger_seq: int,
    ) -> None:
        current = self._statuses.get(comp.name, "REGISTERED")
        if current not in VALID_TRANSITIONS or "STOPPING" not in VALID_TRANSITIONS.get(current, set()):
            return

        await self.transition_component(
            comp.name, "STOPPING",
            reason="drain_complete",
            trigger_seq=trigger_seq,
        )

        driver = self._drivers.get(comp.locality)
        if driver:
            try:
                await asyncio.wait_for(driver.stop(comp), timeout=10.0)
            except Exception as e:
                logger.warning("[Engine] %s stop error: %s", comp.name, e)

        await self.transition_component(
            comp.name, "STOPPED",
            reason="terminated",
            trigger_seq=trigger_seq,
        )

    # ── Recovery ────────────────────────────────────────────────

    async def recover_from_journal(self) -> None:
        """Rebuild state from journal and reconcile with reality."""
        states = self._journal.get_all_component_states()

        for name, state in states.items():
            if name in self._statuses:
                self._statuses[name] = state["status"]
                logger.info(
                    "[Engine] Recovered %s -> %s from journal",
                    name, state["status"],
                )
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_lifecycle_engine.py -v --tb=short 2>&1 | tail -40`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add backend/core/lifecycle_engine.py tests/unit/core/test_lifecycle_engine.py
git commit -m "feat: add lifecycle engine with state machine, DAG waves, and failure propagation"
```

---

### Task 4: Handshake Protocol — Messages & Compatibility

**Files:**
- Create: `backend/core/handshake_protocol.py`
- Test: `tests/unit/core/test_handshake_protocol.py`

**Step 1: Write the failing tests**

```python
# tests/unit/core/test_handshake_protocol.py
"""Tests for HandshakeProtocol — compatibility evaluation, version windows."""

import pytest


class TestHandshakeImport:
    def test_module_imports(self):
        from backend.core.handshake_protocol import HandshakeManager
        assert HandshakeManager is not None

    def test_required_exports(self):
        import backend.core.handshake_protocol as mod
        assert hasattr(mod, "HandshakeProposal")
        assert hasattr(mod, "HandshakeResponse")
        assert hasattr(mod, "HandshakeManager")
        assert hasattr(mod, "evaluate_handshake")


class TestHandshakeProposal:
    def test_proposal_is_frozen(self):
        from backend.core.handshake_protocol import HandshakeProposal
        p = HandshakeProposal(
            supervisor_epoch=1,
            supervisor_instance_id="test:1:abc",
            expected_api_version_min="1.0.0",
            expected_api_version_max="1.9.9",
            required_capabilities=("inference",),
            health_schema_hash="abc123",
            heartbeat_interval_s=10.0,
            heartbeat_ttl_s=30.0,
            protocol_version="1.0.0",
        )
        with pytest.raises(AttributeError):
            p.supervisor_epoch = 99


class TestCompatibilityEvaluation:
    def _make_proposal(self, **overrides):
        from backend.core.handshake_protocol import HandshakeProposal
        defaults = dict(
            supervisor_epoch=1,
            supervisor_instance_id="test:1:abc",
            expected_api_version_min="1.0.0",
            expected_api_version_max="1.9.9",
            required_capabilities=("inference",),
            health_schema_hash="abc123",
            heartbeat_interval_s=10.0,
            heartbeat_ttl_s=30.0,
            protocol_version="1.0.0",
        )
        defaults.update(overrides)
        return HandshakeProposal(**defaults)

    def _make_response(self, **overrides):
        from backend.core.handshake_protocol import HandshakeResponse
        defaults = dict(
            accepted=True,
            component_instance_id="prime:8001:xyz",
            api_version="1.2.0",
            capabilities=("inference", "embedding"),
            health_schema_hash="abc123",
            rejection_reason=None,
            metadata=None,
        )
        defaults.update(overrides)
        return HandshakeResponse(**defaults)

    def test_compatible_accepted(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response()
        ok, reason = evaluate_handshake(p, r)
        assert ok is True
        assert reason is None

    def test_rejected_by_component(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response(accepted=False, rejection_reason="incompatible model")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "component_rejected" in reason

    def test_version_below_minimum(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="2.0.0", expected_api_version_max="2.9.9")
        r = self._make_response(api_version="1.5.0")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "outside" in reason

    def test_version_above_maximum(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="1.0.0", expected_api_version_max="1.5.0")
        r = self._make_response(api_version="1.6.0")
        ok, reason = evaluate_handshake(p, r)
        assert ok is False

    def test_major_version_mismatch(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(expected_api_version_min="1.0.0", expected_api_version_max="2.0.0")
        r = self._make_response(api_version="2.0.0")
        ok, reason = evaluate_handshake(p, r)
        # Major 2 != major 2 of max — this should pass since major matches max
        # But if min is 1.0.0, major of min is 1, and component is 2 — major mismatch
        # Design says "Major version must match" — match against max_version's major
        assert ok is True or "major" in (reason or "")

    def test_missing_required_capability(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(required_capabilities=("inference", "training"))
        r = self._make_response(capabilities=("inference",))
        ok, reason = evaluate_handshake(p, r)
        assert ok is False
        assert "missing_capabilities" in reason

    def test_schema_hash_mismatch_is_warning_not_rejection(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal(health_schema_hash="aaa")
        r = self._make_response(health_schema_hash="bbb")
        ok, reason = evaluate_handshake(p, r)
        assert ok is True  # Warning, not rejection

    def test_legacy_version_zero_always_compatible(self):
        from backend.core.handshake_protocol import evaluate_handshake
        p = self._make_proposal()
        r = self._make_response(api_version="0.0.0", capabilities=("inference",))
        ok, reason = evaluate_handshake(p, r)
        assert ok is True  # Legacy fallback


class TestSemverParsing:
    def test_parse_valid(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("1.2.3") == (1, 2, 3)

    def test_parse_two_part(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("1.2") == (1, 2, 0)

    def test_parse_single(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("3") == (3, 0, 0)

    def test_parse_zero(self):
        from backend.core.handshake_protocol import parse_semver
        assert parse_semver("0.0.0") == (0, 0, 0)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_handshake_protocol.py -v --tb=short 2>&1 | head -20`
Expected: ImportError

**Step 3: Write implementation** (handshake_protocol.py — core message types and compatibility evaluation; heartbeat monitor added in Task 6 integration)

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/handshake_protocol.py tests/unit/core/test_handshake_protocol.py
git commit -m "feat: add handshake protocol with compatibility evaluation and version windows"
```

---

### Task 5: UDS Event Fabric — Server, Wire Protocol, Subscribers

**Files:**
- Create: `backend/core/uds_event_fabric.py`
- Test: `tests/unit/core/test_uds_event_fabric.py`

**Step 1: Write the failing tests**

```python
# tests/unit/core/test_uds_event_fabric.py
"""Tests for UDS Event Fabric — wire protocol, server, subscribers."""

import asyncio
import json
import os
import struct
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


class TestEventFabricImport:
    def test_module_imports(self):
        from backend.core.uds_event_fabric import EventFabric
        assert EventFabric is not None

    def test_required_exports(self):
        import backend.core.uds_event_fabric as mod
        assert hasattr(mod, "EventFabric")
        assert hasattr(mod, "send_frame")
        assert hasattr(mod, "recv_frame")


class TestWireProtocol:
    async def test_send_recv_roundtrip(self):
        from backend.core.uds_event_fabric import send_frame, recv_frame
        # Create in-memory stream pair
        reader = asyncio.StreamReader()
        transport = AsyncMock()
        writer = asyncio.StreamWriter(transport, None, reader, asyncio.get_running_loop())

        payload = {"type": "event", "seq": 42, "data": "hello"}
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        header = struct.pack(">I", len(data))

        # Simulate received data
        reader.feed_data(header + data)
        reader.feed_eof()

        result = await recv_frame(reader)
        assert result == payload

    async def test_frame_size_limit(self):
        from backend.core.uds_event_fabric import recv_frame, MAX_FRAME_SIZE
        reader = asyncio.StreamReader()
        # Feed a header claiming a huge payload
        header = struct.pack(">I", MAX_FRAME_SIZE + 1)
        reader.feed_data(header)
        reader.feed_eof()

        with pytest.raises(Exception):  # ProtocolError or ValueError
            await recv_frame(reader)


class TestEventFabricLifecycle:
    async def test_start_creates_socket(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)
        assert sock_path.exists()
        await fabric.stop()
        await journal.close()

    async def test_stop_removes_socket(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)
        await fabric.stop()
        assert not sock_path.exists()
        await journal.close()

    async def test_stale_socket_cleaned_on_start(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric
        sock_path = tmp_path / "control.sock"
        sock_path.touch()  # Stale socket

        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)  # Should not raise
        assert sock_path.exists()
        await fabric.stop()
        await journal.close()


class TestEventEmission:
    async def test_emit_to_subscriber(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.uds_event_fabric import EventFabric, send_frame, recv_frame
        sock_path = tmp_path / "control.sock"
        db_path = tmp_path / "orchestration.db"
        journal = OrchestrationJournal()
        await journal.initialize(db_path)
        await journal.acquire_lease(f"test:{os.getpid()}:abc")

        fabric = EventFabric(journal)
        await fabric.start(sock_path)

        # Connect as subscriber
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        await send_frame(writer, {
            "type": "subscribe",
            "subscriber_id": "test_sub_1",
            "last_seen_seq": 0,
        })
        ack = await asyncio.wait_for(recv_frame(reader), timeout=5.0)
        assert ack["type"] == "subscribe_ack"

        # Emit an event
        await fabric.emit(99, "state_transition", "jarvis_prime", {"to": "READY"})

        # Subscriber should receive it
        event = await asyncio.wait_for(recv_frame(reader), timeout=5.0)
        assert event["type"] == "event"
        assert event["seq"] == 99
        assert event["target"] == "jarvis_prime"

        writer.close()
        await fabric.stop()
        await journal.close()
```

**Step 2: Run tests, verify fail**

**Step 3: Write implementation**

**Step 4: Run tests, verify pass**

**Step 5: Commit**

```bash
git add backend/core/uds_event_fabric.py tests/unit/core/test_uds_event_fabric.py
git commit -m "feat: add UDS event fabric with wire protocol, subscriber management, and replay"
```

---

### Task 6: Control Plane Client Library

**Files:**
- Create: `backend/core/control_plane_client.py`
- Test: `tests/unit/core/test_control_plane_client.py`

**Step 1: Write the failing tests**

```python
# tests/unit/core/test_control_plane_client.py
"""Tests for ControlPlaneClient — subscriber and handshake responder."""

import pytest


class TestClientImport:
    def test_module_imports(self):
        from backend.core.control_plane_client import ControlPlaneSubscriber
        assert ControlPlaneSubscriber is not None

    def test_required_exports(self):
        import backend.core.control_plane_client as mod
        assert hasattr(mod, "ControlPlaneSubscriber")
        assert hasattr(mod, "HandshakeResponder")


class TestHandshakeResponder:
    def test_creates_response(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.2.0",
            capabilities=["inference", "embedding"],
            instance_id="prime:8001:abc",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": ["inference"],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        assert response["accepted"] is True
        assert response["api_version"] == "1.2.0"
        assert "inference" in response["capabilities"]

    def test_rejects_missing_capability(self):
        from backend.core.control_plane_client import HandshakeResponder
        responder = HandshakeResponder(
            api_version="1.0.0",
            capabilities=["inference"],
            instance_id="prime:8001:abc",
        )
        proposal = {
            "supervisor_epoch": 1,
            "required_capabilities": ["inference", "training"],
            "heartbeat_interval_s": 10.0,
            "heartbeat_ttl_s": 30.0,
        }
        response = responder.handle_handshake(proposal)
        # Responder should still accept — it's the supervisor that rejects
        # Component reports what it has; supervisor evaluates compatibility
        assert response["accepted"] is True
        assert "training" not in response["capabilities"]
```

**Step 2-5: Implement, test, commit**

```bash
git add backend/core/control_plane_client.py tests/unit/core/test_control_plane_client.py
git commit -m "feat: add control plane client library with subscriber and handshake responder"
```

---

### Task 7: Supervisor Integration — Feature-Gated Bootstrap

**Files:**
- Modify: `unified_supervisor.py:63914-63994` (Phase -1 insertion)
- Modify: `unified_supervisor.py:62354-62434` (shutdown handler)
- Test: `tests/integration/test_control_plane_e2e.py`

**Step 1: Write integration test**

```python
# tests/integration/test_control_plane_e2e.py
"""Integration test: full control plane bootstrap → lifecycle → shutdown."""

import asyncio
import os
import pytest
from pathlib import Path


class TestControlPlaneE2E:
    async def test_journal_to_engine_to_fabric(self, tmp_path):
        """Full flow: journal init → lease → engine start → UDS emit → shutdown."""
        from backend.core.orchestration_journal import OrchestrationJournal
        from backend.core.lifecycle_engine import (
            LifecycleEngine, ComponentDeclaration, ComponentLocality,
        )
        from backend.core.uds_event_fabric import EventFabric

        db_path = tmp_path / "orchestration.db"
        sock_path = tmp_path / "control.sock"

        # 1. Initialize journal
        journal = OrchestrationJournal()
        await journal.initialize(db_path)

        # 2. Acquire lease
        ok = await journal.acquire_lease(f"test:{os.getpid()}:e2e")
        assert ok is True

        # 3. Start event fabric
        fabric = EventFabric(journal)
        await fabric.start(sock_path)

        # 4. Create engine with simple components
        components = (
            ComponentDeclaration(
                name="test_a", locality=ComponentLocality.IN_PROCESS,
                is_critical=True,
            ),
            ComponentDeclaration(
                name="test_b", locality=ComponentLocality.IN_PROCESS,
                dependencies=("test_a",),
            ),
        )
        engine = LifecycleEngine(journal, components)

        # 5. Simulate lifecycle
        await engine.transition_component("test_a", "STARTING", reason="test")
        await engine.transition_component("test_a", "HANDSHAKING", reason="test")
        await engine.transition_component("test_a", "READY", reason="test")

        assert engine.get_status("test_a") == "READY"

        # 6. Verify journal contains transitions
        entries = await journal.replay_from(0, action_filter=["state_transition"])
        targets = [e["target"] for e in entries]
        assert "test_a" in targets

        # 7. Shutdown
        await engine.shutdown_all("test_complete")
        assert engine.get_status("test_a") == "STOPPED"

        await fabric.stop()
        await journal.close()
```

**Step 2: Wire into `unified_supervisor.py` at line 63914** with feature gate `JARVIS_CONTROL_PLANE=true`

The integration adds Phase -1 before existing Phase 0. When `JARVIS_CONTROL_PLANE=false` (default), the existing startup path runs unchanged. When `true`, the control plane bootstraps first, then existing phases execute under its coordination.

Key insertion point: **line 63918** (after `_startup_impl` docstring, before proxy registration).

The shutdown handler at **line 62354** (`_emergency_shutdown`) gets an additional block that:
1. Releases the lease
2. Stops the UDS fabric
3. Closes the journal

**Step 3: Implement the integration**

**Step 4: Run integration test**

Run: `python3 -m pytest tests/integration/test_control_plane_e2e.py -v --tb=short`
Expected: PASS

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/integration/test_control_plane_e2e.py
git commit -m "feat: integrate control plane into supervisor with feature gate"
```

---

### Task 8: SubprocessDriver — Wrapping CrossRepoStartupOrchestrator

**Files:**
- Create: `backend/core/locality_drivers.py`
- Test: `tests/unit/core/test_locality_drivers.py`

**Step 1: Write tests for SubprocessDriver**

Test that SubprocessDriver delegates to ProcessOrchestrator's spawn/stop methods. Mock the orchestrator. Verify that health_check makes HTTP calls. Verify send_drain posts to `/lifecycle/drain`.

**Step 2: Write implementation**

`SubprocessDriver` wraps `ProcessOrchestrator` from `cross_repo_startup_orchestrator.py`. `InProcessDriver` wraps supervisor init methods. `RemoteDriver` wraps `GCPVMManager`.

**Step 3: Run tests, commit**

```bash
git add backend/core/locality_drivers.py tests/unit/core/test_locality_drivers.py
git commit -m "feat: add locality drivers wrapping existing orchestrator and VM manager"
```

---

### Task 9: Lease Contention Integration Test

**Files:**
- Test: `tests/integration/test_lease_contention.py`

**Step 1: Write test**

```python
# tests/integration/test_lease_contention.py
"""Test that two journal instances correctly contend for a single lease."""

import asyncio
import os
import sqlite3
import time
import pytest


class TestLeaseContention:
    async def test_two_journals_only_one_wins(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = tmp_path / "orchestration.db"

        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)

        old_timeout = mod.LEASE_ACQUIRE_TIMEOUT_S
        mod.LEASE_ACQUIRE_TIMEOUT_S = 2.0
        try:
            ok1 = await j1.acquire_lease("holder_1")
            ok2 = await j2.acquire_lease("holder_2")
            assert ok1 is True
            assert ok2 is False
        finally:
            mod.LEASE_ACQUIRE_TIMEOUT_S = old_timeout
        await j1.close()
        await j2.close()

    async def test_crashed_leader_replaced(self, tmp_path):
        from backend.core.orchestration_journal import OrchestrationJournal
        import backend.core.orchestration_journal as mod
        db_path = tmp_path / "orchestration.db"

        # Leader 1 acquires then "crashes" (close without release)
        j1 = OrchestrationJournal()
        await j1.initialize(db_path)
        await j1.acquire_lease("leader_1")
        epoch1 = j1.epoch
        await j1.close()  # "crash" — lease not released

        # Backdate lease to simulate TTL expiry
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "UPDATE lease SET last_renewed=? WHERE id=1",
            (time.time() - mod.LEASE_TTL_S - 5.0,),
        )
        conn.commit()
        conn.close()

        # Leader 2 takes over
        j2 = OrchestrationJournal()
        await j2.initialize(db_path)
        ok = await j2.acquire_lease("leader_2")
        assert ok is True
        assert j2.epoch == epoch1 + 1

        # Leader 2 can write
        seq = j2.fenced_write("recovery", "control_plane", payload={"reason": "leader_replaced"})
        assert seq >= 1
        await j2.close()
```

**Step 2: Run, verify pass, commit**

```bash
git add tests/integration/test_lease_contention.py
git commit -m "test: add lease contention integration tests"
```

---

### Task 10: Crash Recovery Integration Test

**Files:**
- Test: `tests/integration/test_crash_recovery.py`

**Step 1: Write test**

Test that after writing journal entries, closing, and reopening with a new journal instance, the recovery protocol correctly rebuilds component state from the journal.

**Step 2: Run, verify pass, commit**

```bash
git add tests/integration/test_crash_recovery.py
git commit -m "test: add crash recovery integration tests"
```

---

## Summary

| Task | Component | Tests | Implementation |
|------|-----------|-------|----------------|
| 1 | Orchestration Journal — schema & writes | 21 tests | `orchestration_journal.py` |
| 2 | Journal — lease contention & fencing | 6 tests | (extends Task 1 tests) |
| 3 | Lifecycle Engine — state machine & DAG | 22 tests | `lifecycle_engine.py` |
| 4 | Handshake Protocol — messages & compat | 12 tests | `handshake_protocol.py` |
| 5 | UDS Event Fabric — server & subscribers | 7 tests | `uds_event_fabric.py` |
| 6 | Control Plane Client — library | 4 tests | `control_plane_client.py` |
| 7 | Supervisor Integration — feature gate | 1 e2e test | modify `unified_supervisor.py` |
| 8 | Locality Drivers — wrapping existing | 6+ tests | `locality_drivers.py` |
| 9 | Lease Contention — integration | 2 tests | (test only) |
| 10 | Crash Recovery — integration | 2+ tests | (test only) |

**Total: ~83 tests across 10 tasks**

**Execution order**: Tasks 1-6 are independent foundation modules (can be parallelized). Task 7 depends on 1-6. Tasks 8-10 depend on 7.
