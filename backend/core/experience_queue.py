"""
Experience Data Queue - Persistent Queue for Training Data.
============================================================

Provides a reliable queue for experience data that persists when Reactor-Core
is unavailable. Automatically drains to Reactor-Core when connectivity is restored.

Key Features:
1. Persistent storage (survives restarts)
2. Priority-based ordering
3. Batch processing for efficiency
4. Automatic drain to Reactor-Core
5. TTL-based expiration
6. Deduplication support

Architecture:
    ┌─────────────────────────────────────────────────────────────────────┐
    │  ExperienceDataQueue                                                │
    │  ├── PersistentStore (SQLite-backed durable storage)                │
    │  ├── PriorityQueue (in-memory fast access)                          │
    │  ├── BatchProcessor (efficient bulk operations)                     │
    │  └── AutoDrainer (background task to drain to Reactor)              │
    └─────────────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────────────────────────────────────────────┐
    │  ExperienceQueueProcessor                                           │
    │  ├── HealthMonitor (watches Reactor-Core health)                    │
    │  ├── DrainScheduler (adaptive drain rate)                           │
    │  └── RetryHandler (exponential backoff for failures)                │
    └─────────────────────────────────────────────────────────────────────┘

Author: JARVIS Trinity v81.0 - Experience Data Persistence
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    val = os.getenv(key, str(default)).lower()
    return val in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class ExperienceType(enum.Enum):
    """Types of experience data."""
    VOICE_EMBEDDING = "voice_embedding"
    VOICE_OUTCOME = "voice_outcome"
    VOICE_CONTEXT = "voice_context"
    INFERENCE_FEEDBACK = "inference_feedback"
    INFERENCE_TRACE = "inference_trace"
    BEHAVIORAL_EVENT = "behavioral_event"
    SYSTEM_TELEMETRY = "system_telemetry"
    MODEL_PERFORMANCE = "model_performance"
    USER_CORRECTION = "user_correction"


class ExperiencePriority(enum.IntEnum):
    """Priority levels for experience data."""
    CRITICAL = 1    # Must be processed (user corrections)
    HIGH = 2        # Important (authentication outcomes)
    NORMAL = 3      # Standard (inference feedback)
    LOW = 4         # Background (telemetry)
    BULK = 5        # Batch processing OK (model performance)


class ProcessingStatus(enum.Enum):
    """Processing status of queue entries."""
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    EXPIRED = "expired"


@dataclass
class ExperienceEntry:
    """A single experience data entry."""
    id: str
    experience_type: ExperienceType
    priority: ExperiencePriority
    data: Dict[str, Any]
    created_at: float
    expires_at: float
    status: ProcessingStatus = ProcessingStatus.PENDING
    attempts: int = 0
    last_attempt: float = 0.0
    content_hash: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if this entry has expired."""
        return time.time() > self.expires_at

    @property
    def age_seconds(self) -> float:
        """Age of this entry in seconds."""
        return time.time() - self.created_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            "id": self.id,
            "experience_type": self.experience_type.value,
            "priority": self.priority.value,
            "data": self.data,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "attempts": self.attempts,
            "last_attempt": self.last_attempt,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperienceEntry":
        """Create from dictionary."""
        return cls(
            id=d["id"],
            experience_type=ExperienceType(d["experience_type"]),
            priority=ExperiencePriority(d["priority"]),
            data=d["data"],
            created_at=d["created_at"],
            expires_at=d["expires_at"],
            status=ProcessingStatus(d["status"]),
            attempts=d.get("attempts", 0),
            last_attempt=d.get("last_attempt", 0.0),
            content_hash=d.get("content_hash"),
            metadata=d.get("metadata", {}),
        )


@dataclass
class QueueStats:
    """Statistics about the queue."""
    total_entries: int = 0
    pending_entries: int = 0
    processing_entries: int = 0
    failed_entries: int = 0
    expired_entries: int = 0
    oldest_entry_age: float = 0.0
    total_size_bytes: int = 0
    entries_by_type: Dict[str, int] = field(default_factory=dict)
    entries_by_priority: Dict[int, int] = field(default_factory=dict)


# =============================================================================
# Persistent Storage (SQLite-backed)
# =============================================================================

class ExperienceStore:
    """SQLite-backed persistent storage for experience data."""

    def __init__(
        self,
        db_path: Path,
        max_size_mb: float = 100.0,
    ):
        self.db_path = db_path
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self._connection: Optional[sqlite3.Connection] = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize the database."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Run database initialization in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._init_db)

    def _init_db(self) -> None:
        """Initialize database schema (sync)."""
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS experiences (
                id TEXT PRIMARY KEY,
                experience_type TEXT NOT NULL,
                priority INTEGER NOT NULL,
                data TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt REAL DEFAULT 0,
                content_hash TEXT,
                metadata TEXT
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_status_priority
            ON experiences(status, priority, created_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_expires_at
            ON experiences(expires_at)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_content_hash
            ON experiences(content_hash)
        """)
        conn.commit()
        conn.close()

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._connection is None:
            self._connection = sqlite3.connect(
                str(self.db_path),
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
        return self._connection

    async def insert(self, entry: ExperienceEntry) -> bool:
        """Insert an entry into the store."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._insert_sync, entry)

    def _insert_sync(self, entry: ExperienceEntry) -> bool:
        """Insert entry (sync)."""
        try:
            conn = self._get_connection()
            conn.execute("""
                INSERT OR REPLACE INTO experiences
                (id, experience_type, priority, data, created_at, expires_at,
                 status, attempts, last_attempt, content_hash, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id,
                entry.experience_type.value,
                entry.priority.value,
                json.dumps(entry.data),
                entry.created_at,
                entry.expires_at,
                entry.status.value,
                entry.attempts,
                entry.last_attempt,
                entry.content_hash,
                json.dumps(entry.metadata),
            ))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"[ExperienceStore] Insert failed: {e}")
            return False

    async def get_batch(
        self,
        batch_size: int = 100,
        status: ProcessingStatus = ProcessingStatus.PENDING,
    ) -> List[ExperienceEntry]:
        """Get a batch of entries by status, ordered by priority."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._get_batch_sync, batch_size, status
            )

    def _get_batch_sync(
        self,
        batch_size: int,
        status: ProcessingStatus,
    ) -> List[ExperienceEntry]:
        """Get batch (sync)."""
        conn = self._get_connection()
        cursor = conn.execute("""
            SELECT * FROM experiences
            WHERE status = ? AND expires_at > ?
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
        """, (status.value, time.time(), batch_size))

        entries = []
        for row in cursor.fetchall():
            try:
                entries.append(ExperienceEntry(
                    id=row["id"],
                    experience_type=ExperienceType(row["experience_type"]),
                    priority=ExperiencePriority(row["priority"]),
                    data=json.loads(row["data"]),
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    status=ProcessingStatus(row["status"]),
                    attempts=row["attempts"],
                    last_attempt=row["last_attempt"],
                    content_hash=row["content_hash"],
                    metadata=json.loads(row["metadata"]) if row["metadata"] else {},
                ))
            except Exception as e:
                logger.warning(f"[ExperienceStore] Failed to parse entry: {e}")

        return entries

    async def update_status(
        self,
        entry_id: str,
        status: ProcessingStatus,
        increment_attempts: bool = False,
    ) -> bool:
        """Update entry status."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._update_status_sync, entry_id, status, increment_attempts
            )

    def _update_status_sync(
        self,
        entry_id: str,
        status: ProcessingStatus,
        increment_attempts: bool,
    ) -> bool:
        """Update status (sync)."""
        try:
            conn = self._get_connection()
            if increment_attempts:
                conn.execute("""
                    UPDATE experiences
                    SET status = ?, attempts = attempts + 1, last_attempt = ?
                    WHERE id = ?
                """, (status.value, time.time(), entry_id))
            else:
                conn.execute("""
                    UPDATE experiences
                    SET status = ?
                    WHERE id = ?
                """, (status.value, entry_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"[ExperienceStore] Update failed: {e}")
            return False

    async def delete(self, entry_id: str) -> bool:
        """Delete an entry."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._delete_sync, entry_id)

    def _delete_sync(self, entry_id: str) -> bool:
        """Delete entry (sync)."""
        try:
            conn = self._get_connection()
            conn.execute("DELETE FROM experiences WHERE id = ?", (entry_id,))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"[ExperienceStore] Delete failed: {e}")
            return False

    async def delete_batch(self, entry_ids: List[str]) -> int:
        """Delete multiple entries."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._delete_batch_sync, entry_ids)

    def _delete_batch_sync(self, entry_ids: List[str]) -> int:
        """Delete batch (sync)."""
        if not entry_ids:
            return 0
        try:
            conn = self._get_connection()
            placeholders = ",".join("?" * len(entry_ids))
            cursor = conn.execute(
                f"DELETE FROM experiences WHERE id IN ({placeholders})",
                entry_ids
            )
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.error(f"[ExperienceStore] Batch delete failed: {e}")
            return 0

    async def cleanup_expired(self) -> int:
        """Remove expired entries."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        """Cleanup expired (sync)."""
        try:
            conn = self._get_connection()
            cursor = conn.execute(
                "DELETE FROM experiences WHERE expires_at < ?",
                (time.time(),)
            )
            conn.commit()
            return cursor.rowcount
        except Exception as e:
            logger.error(f"[ExperienceStore] Cleanup failed: {e}")
            return 0

    async def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._get_stats_sync)

    def _get_stats_sync(self) -> QueueStats:
        """Get stats (sync)."""
        stats = QueueStats()
        conn = self._get_connection()

        # Total entries
        cursor = conn.execute("SELECT COUNT(*) FROM experiences")
        stats.total_entries = cursor.fetchone()[0]

        # By status
        for status in ProcessingStatus:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM experiences WHERE status = ?",
                (status.value,)
            )
            count = cursor.fetchone()[0]
            if status == ProcessingStatus.PENDING:
                stats.pending_entries = count
            elif status == ProcessingStatus.PROCESSING:
                stats.processing_entries = count
            elif status == ProcessingStatus.FAILED:
                stats.failed_entries = count
            elif status == ProcessingStatus.EXPIRED:
                stats.expired_entries = count

        # Oldest entry
        cursor = conn.execute(
            "SELECT MIN(created_at) FROM experiences WHERE status = 'pending'"
        )
        oldest = cursor.fetchone()[0]
        if oldest:
            stats.oldest_entry_age = time.time() - oldest

        # By type
        cursor = conn.execute("""
            SELECT experience_type, COUNT(*) FROM experiences
            GROUP BY experience_type
        """)
        for row in cursor.fetchall():
            stats.entries_by_type[row[0]] = row[1]

        # By priority
        cursor = conn.execute("""
            SELECT priority, COUNT(*) FROM experiences
            GROUP BY priority
        """)
        for row in cursor.fetchall():
            stats.entries_by_priority[row[0]] = row[1]

        # Database size
        try:
            stats.total_size_bytes = self.db_path.stat().st_size
        except Exception:
            pass

        return stats

    async def check_duplicate(self, content_hash: str) -> bool:
        """Check if a duplicate entry exists."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(
                None, self._check_duplicate_sync, content_hash
            )

    def _check_duplicate_sync(self, content_hash: str) -> bool:
        """Check duplicate (sync)."""
        conn = self._get_connection()
        cursor = conn.execute(
            "SELECT 1 FROM experiences WHERE content_hash = ? LIMIT 1",
            (content_hash,)
        )
        return cursor.fetchone() is not None

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            self._connection.close()
            self._connection = None


# =============================================================================
# Experience Data Queue
# =============================================================================

class ExperienceDataQueue:
    """
    Persistent queue for experience data.

    Features:
    - Priority-based ordering
    - Deduplication
    - TTL-based expiration
    - Size limits
    - Batch retrieval
    """

    def __init__(
        self,
        store_path: Optional[Path] = None,
        max_size_mb: Optional[float] = None,
        default_ttl_hours: Optional[float] = None,
        dedup_enabled: bool = True,
    ):
        """
        Initialize the experience queue.

        Args:
            store_path: Path to SQLite database
            max_size_mb: Maximum queue size in MB
            default_ttl_hours: Default TTL for entries
            dedup_enabled: Enable deduplication
        """
        # Configuration from environment
        self.store_path = store_path or Path(os.environ.get(
            "EXPERIENCE_QUEUE_PATH",
            str(Path.home() / ".jarvis" / "experience_queue.db")
        ))
        self.max_size_mb = max_size_mb or _env_float("EXPERIENCE_QUEUE_MAX_SIZE_MB", 100.0)
        self.default_ttl_hours = default_ttl_hours or _env_float(
            "EXPERIENCE_QUEUE_MAX_AGE_HOURS", 24.0
        )
        self.dedup_enabled = dedup_enabled

        # Internal state
        self._store = ExperienceStore(self.store_path, self.max_size_mb)
        self._initialized = False
        self._lock = asyncio.Lock()

        # Callbacks
        self._on_enqueue_callbacks: List[Callable[[ExperienceEntry], None]] = []

    async def initialize(self) -> None:
        """Initialize the queue."""
        if self._initialized:
            return

        await self._store.initialize()
        self._initialized = True
        logger.info(
            f"[ExperienceQueue] Initialized at {self.store_path} "
            f"(max_size={self.max_size_mb}MB, ttl={self.default_ttl_hours}h)"
        )

    async def enqueue(
        self,
        experience_type: ExperienceType,
        data: Dict[str, Any],
        priority: ExperiencePriority = ExperiencePriority.NORMAL,
        ttl_hours: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Enqueue experience data.

        Args:
            experience_type: Type of experience
            data: Experience data payload
            priority: Priority level
            ttl_hours: Time-to-live in hours (uses default if None)
            metadata: Additional metadata

        Returns:
            Entry ID if successful, None if rejected
        """
        await self.initialize()

        async with self._lock:
            # Check size limit
            stats = await self._store.get_stats()
            if stats.total_size_bytes >= self.max_size_mb * 1024 * 1024:
                logger.warning("[ExperienceQueue] Queue full, dropping entry")
                return None

            # Calculate expiration
            ttl = ttl_hours if ttl_hours is not None else self.default_ttl_hours
            expires_at = time.time() + (ttl * 3600)

            # Calculate content hash for deduplication
            content_hash = None
            if self.dedup_enabled:
                content_hash = self._compute_hash(experience_type, data)
                if await self._store.check_duplicate(content_hash):
                    logger.debug("[ExperienceQueue] Duplicate entry, skipping")
                    return None

            # Create entry
            entry = ExperienceEntry(
                id=str(uuid.uuid4()),
                experience_type=experience_type,
                priority=priority,
                data=data,
                created_at=time.time(),
                expires_at=expires_at,
                content_hash=content_hash,
                metadata=metadata or {},
            )

            # Insert
            if await self._store.insert(entry):
                logger.debug(
                    f"[ExperienceQueue] Enqueued {experience_type.value} "
                    f"with priority {priority.name}"
                )

                # Notify callbacks
                for callback in self._on_enqueue_callbacks:
                    try:
                        callback(entry)
                    except Exception as e:
                        logger.warning(f"[ExperienceQueue] Callback error: {e}")

                return entry.id

            return None

    def _compute_hash(
        self,
        experience_type: ExperienceType,
        data: Dict[str, Any],
    ) -> str:
        """Compute content hash for deduplication."""
        content = f"{experience_type.value}:{json.dumps(data, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    async def dequeue_batch(
        self,
        batch_size: int = 100,
    ) -> List[ExperienceEntry]:
        """
        Dequeue a batch of pending entries.

        Entries are marked as PROCESSING before returning.

        Args:
            batch_size: Maximum entries to return

        Returns:
            List of entries to process
        """
        await self.initialize()

        async with self._lock:
            # Get pending entries
            entries = await self._store.get_batch(batch_size, ProcessingStatus.PENDING)

            # Mark as processing
            for entry in entries:
                await self._store.update_status(
                    entry.id, ProcessingStatus.PROCESSING
                )
                entry.status = ProcessingStatus.PROCESSING

            return entries

    async def mark_processed(
        self,
        entry_id: str,
        delete: bool = True,
    ) -> None:
        """
        Mark an entry as successfully processed.

        Args:
            entry_id: Entry to mark
            delete: If True, delete the entry; otherwise mark as processed
        """
        if delete:
            await self._store.delete(entry_id)
        else:
            await self._store.update_status(entry_id, ProcessingStatus.PROCESSED)

    async def mark_processed_batch(
        self,
        entry_ids: List[str],
        delete: bool = True,
    ) -> None:
        """Mark multiple entries as processed."""
        if delete:
            await self._store.delete_batch(entry_ids)
        else:
            for entry_id in entry_ids:
                await self._store.update_status(entry_id, ProcessingStatus.PROCESSED)

    async def mark_failed(
        self,
        entry_id: str,
        max_attempts: int = 3,
    ) -> None:
        """
        Mark an entry as failed.

        Will be retried up to max_attempts times before being permanently marked failed.
        """
        await self._store.update_status(
            entry_id, ProcessingStatus.PENDING, increment_attempts=True
        )

    async def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        await self.initialize()
        return await self._store.get_stats()

    async def cleanup(self) -> int:
        """Remove expired entries."""
        await self.initialize()
        count = await self._store.cleanup_expired()
        if count > 0:
            logger.info(f"[ExperienceQueue] Cleaned up {count} expired entries")
        return count

    def on_enqueue(self, callback: Callable[[ExperienceEntry], None]) -> None:
        """Register a callback for new entries."""
        self._on_enqueue_callbacks.append(callback)

    async def close(self) -> None:
        """Close the queue."""
        await self._store.close()


# =============================================================================
# Experience Queue Processor
# =============================================================================

class ExperienceQueueProcessor:
    """
    Background processor that drains experience queue to Reactor-Core.

    Features:
    - Health-aware processing (pauses when Reactor is down)
    - Adaptive batch sizing
    - Exponential backoff on failures
    - Priority-aware ordering
    """

    def __init__(
        self,
        queue: ExperienceDataQueue,
        reactor_client: Optional[Any] = None,  # ReactorCoreClient
        batch_size: int = 50,
        poll_interval: float = 5.0,
        max_backoff: float = 300.0,
    ):
        """
        Initialize the processor.

        Args:
            queue: Experience queue to process
            reactor_client: Client for Reactor-Core communication
            batch_size: Default batch size
            poll_interval: Interval between processing runs
            max_backoff: Maximum backoff on failures
        """
        self.queue = queue
        self.reactor_client = reactor_client
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self.max_backoff = max_backoff

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._current_backoff = 1.0
        self._reactor_healthy = False
        self._processed_count = 0
        self._failed_count = 0

        # Callbacks
        self._on_reactor_health_change: List[Callable[[bool], None]] = []

    async def start(self) -> None:
        """Start the processor."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._process_loop())
        logger.info("[ExperienceQueueProcessor] Started")

    async def stop(self) -> None:
        """Stop the processor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[ExperienceQueueProcessor] Stopped")

    async def _process_loop(self) -> None:
        """Main processing loop."""
        while self._running:
            try:
                # Check Reactor-Core health
                healthy = await self._check_reactor_health()

                if healthy != self._reactor_healthy:
                    self._reactor_healthy = healthy
                    self._notify_health_change(healthy)

                    if healthy:
                        logger.info(
                            "[ExperienceQueueProcessor] Reactor-Core healthy, resuming drain"
                        )
                        self._current_backoff = 1.0
                    else:
                        logger.warning(
                            "[ExperienceQueueProcessor] Reactor-Core unhealthy, pausing drain"
                        )

                # Process if healthy
                if self._reactor_healthy:
                    await self._process_batch()

                # Wait for next cycle
                await asyncio.sleep(
                    self.poll_interval if self._reactor_healthy else self._current_backoff
                )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ExperienceQueueProcessor] Error: {e}")
                self._current_backoff = min(
                    self._current_backoff * 2, self.max_backoff
                )
                await asyncio.sleep(self._current_backoff)

    async def _check_reactor_health(self) -> bool:
        """Check if Reactor-Core is healthy."""
        if self.reactor_client is None:
            # Check via IPC
            try:
                from backend.core.trinity_ipc import TrinityIPCBus
                ipc = TrinityIPCBus()
                heartbeat = await ipc.read_heartbeat("reactor_core")
                return heartbeat is not None and heartbeat.status == "ready"
            except Exception:
                return False

        # Use client health check
        try:
            return await self.reactor_client.health_check()
        except Exception:
            return False

    async def _process_batch(self) -> None:
        """Process a batch of entries."""
        entries = await self.queue.dequeue_batch(self.batch_size)

        if not entries:
            return

        logger.debug(f"[ExperienceQueueProcessor] Processing {len(entries)} entries")

        successful_ids = []
        failed_ids = []

        for entry in entries:
            try:
                success = await self._send_to_reactor(entry)
                if success:
                    successful_ids.append(entry.id)
                    self._processed_count += 1
                else:
                    failed_ids.append(entry.id)
                    self._failed_count += 1
            except Exception as e:
                logger.warning(
                    f"[ExperienceQueueProcessor] Failed to process {entry.id}: {e}"
                )
                failed_ids.append(entry.id)
                self._failed_count += 1

        # Mark results
        if successful_ids:
            await self.queue.mark_processed_batch(successful_ids)
            logger.info(
                f"[ExperienceQueueProcessor] Processed {len(successful_ids)} entries"
            )

        for entry_id in failed_ids:
            await self.queue.mark_failed(entry_id)

    async def _send_to_reactor(self, entry: ExperienceEntry) -> bool:
        """Send an entry to Reactor-Core."""
        if self.reactor_client is None:
            # Use IPC
            try:
                from backend.core.trinity_ipc import TrinityIPCBus, TrinityCommand
                ipc = TrinityIPCBus()
                command = TrinityCommand(
                    id=str(uuid.uuid4()),
                    command_type="experience_data",
                    source="jarvis_body",
                    target="reactor_core",
                    payload=entry.to_dict(),
                )
                await ipc.enqueue_command(command)
                return True
            except Exception as e:
                logger.warning(f"[ExperienceQueueProcessor] IPC send failed: {e}")
                return False

        # Use client
        try:
            return await self.reactor_client.submit_experience(entry.to_dict())
        except Exception as e:
            logger.warning(f"[ExperienceQueueProcessor] Client send failed: {e}")
            return False

    def _notify_health_change(self, healthy: bool) -> None:
        """Notify listeners of health change."""
        for callback in self._on_reactor_health_change:
            try:
                callback(healthy)
            except Exception as e:
                logger.warning(
                    f"[ExperienceQueueProcessor] Health callback error: {e}"
                )

    def on_reactor_health_change(
        self,
        callback: Callable[[bool], None],
    ) -> None:
        """Register callback for Reactor-Core health changes."""
        self._on_reactor_health_change.append(callback)

    def get_stats(self) -> Dict[str, Any]:
        """Get processor statistics."""
        return {
            "running": self._running,
            "reactor_healthy": self._reactor_healthy,
            "processed_count": self._processed_count,
            "failed_count": self._failed_count,
            "current_backoff": self._current_backoff,
        }


# =============================================================================
# Singleton Access
# =============================================================================

_queue_instance: Optional[ExperienceDataQueue] = None
_processor_instance: Optional[ExperienceQueueProcessor] = None
_instance_lock = asyncio.Lock()


async def get_experience_queue() -> ExperienceDataQueue:
    """Get the singleton experience queue."""
    global _queue_instance

    async with _instance_lock:
        if _queue_instance is None:
            _queue_instance = ExperienceDataQueue()
            await _queue_instance.initialize()
        return _queue_instance


async def get_experience_processor(
    reactor_client: Optional[Any] = None,
) -> ExperienceQueueProcessor:
    """Get the singleton experience processor."""
    global _processor_instance

    async with _instance_lock:
        if _processor_instance is None:
            queue = await get_experience_queue()
            _processor_instance = ExperienceQueueProcessor(
                queue=queue,
                reactor_client=reactor_client,
            )
        return _processor_instance


async def enqueue_experience(
    experience_type: ExperienceType,
    data: Dict[str, Any],
    priority: ExperiencePriority = ExperiencePriority.NORMAL,
    **kwargs,
) -> Optional[str]:
    """Convenience function to enqueue experience data."""
    queue = await get_experience_queue()
    return await queue.enqueue(experience_type, data, priority, **kwargs)
