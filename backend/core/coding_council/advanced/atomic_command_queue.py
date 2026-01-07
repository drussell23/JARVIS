"""
v78.0: Atomic Command Queue for Trinity Transport
=================================================

Provides atomic, race-condition-free command queuing for the file-based
Trinity transport layer between JARVIS, J-Prime, and Reactor-Core.

Features:
- Atomic file operations using OS-level locks
- Guaranteed ordering with sequence numbers
- Duplicate detection via content hashing
- Concurrent read safety with writer exclusion
- Automatic lock timeout and deadlock prevention
- Cross-process synchronization
- Crash recovery with journal logging

Architecture:
    JARVIS writes → [Atomic Lock] → Queue File → [Atomic Lock] → J-Prime reads
         ↑                                              ↓
         └─────── Reactor-Core reads/writes ────────────┘

    Lock acquisition order:
    1. Try non-blocking lock
    2. Wait with exponential backoff (max 10 attempts)
    3. Force steal if lock owner died (via PID check)
    4. Fail with LockAcquisitionError

Usage:
    from backend.core.coding_council.advanced.atomic_command_queue import (
        get_atomic_queue,
        AtomicQueueEntry,
    )

    queue = await get_atomic_queue("jarvis_to_jprime")

    # Enqueue atomically
    await queue.enqueue({"command": "analyze", "file": "main.py"})

    # Dequeue atomically
    entry = await queue.dequeue()
    if entry:
        process(entry.payload)
        await queue.acknowledge(entry.sequence_id)

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import struct
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
    Union,
)
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# Exceptions
# =============================================================================

class AtomicQueueError(Exception):
    """Base exception for atomic queue errors."""
    pass


class LockAcquisitionError(AtomicQueueError):
    """Failed to acquire lock."""
    pass


class QueueCorruptionError(AtomicQueueError):
    """Queue file is corrupted."""
    pass


class DuplicateEntryError(AtomicQueueError):
    """Duplicate entry detected."""
    pass


# =============================================================================
# Enums
# =============================================================================

class LockType(Enum):
    """Type of file lock."""
    SHARED = "shared"      # Multiple readers
    EXCLUSIVE = "exclusive" # Single writer


class EntryState(Enum):
    """State of a queue entry."""
    PENDING = "pending"           # Waiting to be processed
    PROCESSING = "processing"     # Currently being processed
    ACKNOWLEDGED = "acknowledged" # Successfully processed
    FAILED = "failed"            # Processing failed
    DEAD_LETTER = "dead_letter"  # Moved to dead letter queue


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class AtomicQueueEntry:
    """
    A single entry in the atomic queue.

    Each entry has:
    - Globally unique sequence ID
    - Content hash for deduplication
    - Timestamp for ordering and TTL
    - State tracking
    - Retry metadata
    """
    sequence_id: str
    payload: Dict[str, Any]
    content_hash: str
    timestamp: float = field(default_factory=time.time)
    state: EntryState = EntryState.PENDING
    source: str = "unknown"
    retry_count: int = 0
    max_retries: int = 3
    ttl_seconds: float = 3600.0  # 1 hour default
    processing_started: Optional[float] = None
    processing_timeout: float = 300.0  # 5 min processing timeout
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_expired(self) -> bool:
        """Check if entry has exceeded TTL."""
        return (time.time() - self.timestamp) > self.ttl_seconds

    @property
    def is_processing_timed_out(self) -> bool:
        """Check if processing has timed out."""
        if not self.processing_started:
            return False
        return (time.time() - self.processing_started) > self.processing_timeout

    @property
    def can_retry(self) -> bool:
        """Check if entry can be retried."""
        return self.retry_count < self.max_retries

    @staticmethod
    def compute_hash(payload: Dict[str, Any]) -> str:
        """Compute content hash for deduplication."""
        content = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            "sequence_id": self.sequence_id,
            "payload": self.payload,
            "content_hash": self.content_hash,
            "timestamp": self.timestamp,
            "state": self.state.value,
            "source": self.source,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "ttl_seconds": self.ttl_seconds,
            "processing_started": self.processing_started,
            "processing_timeout": self.processing_timeout,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AtomicQueueEntry":
        """Create from dictionary."""
        return cls(
            sequence_id=data["sequence_id"],
            payload=data["payload"],
            content_hash=data["content_hash"],
            timestamp=data.get("timestamp", time.time()),
            state=EntryState(data.get("state", "pending")),
            source=data.get("source", "unknown"),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            ttl_seconds=data.get("ttl_seconds", 3600.0),
            processing_started=data.get("processing_started"),
            processing_timeout=data.get("processing_timeout", 300.0),
            metadata=data.get("metadata", {}),
        )


@dataclass
class QueueStats:
    """Statistics about the atomic queue."""
    total_enqueued: int = 0
    total_dequeued: int = 0
    total_acknowledged: int = 0
    total_failed: int = 0
    total_duplicates_rejected: int = 0
    total_expired: int = 0
    total_dead_lettered: int = 0
    current_size: int = 0
    pending_count: int = 0
    processing_count: int = 0
    avg_processing_time_ms: float = 0.0
    lock_contentions: int = 0
    lock_timeouts: int = 0


# =============================================================================
# File Lock Implementation
# =============================================================================

class AtomicFileLock:
    """
    Cross-process file lock using fcntl.

    Provides:
    - Exclusive locks for writing
    - Shared locks for reading (multiple readers OK)
    - Automatic lock release on context exit
    - Deadlock prevention via timeouts
    - Stale lock detection via PID tracking
    """

    def __init__(
        self,
        lock_file: Path,
        lock_type: LockType = LockType.EXCLUSIVE,
        timeout: float = 10.0,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.lock_file = lock_file
        self.lock_type = lock_type
        self.timeout = timeout
        self.log = logger_instance or logger
        self._fd: Optional[int] = None
        self._acquired = False

    async def acquire(self) -> bool:
        """
        Acquire the file lock.

        Uses exponential backoff with jitter for contention handling.
        """
        start_time = time.time()
        attempt = 0
        max_attempts = 20
        base_delay = 0.05  # 50ms

        # Ensure lock file exists
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)

        while time.time() - start_time < self.timeout:
            attempt += 1

            try:
                # Open lock file
                self._fd = os.open(
                    str(self.lock_file),
                    os.O_RDWR | os.O_CREAT,
                    0o666
                )

                # Try to acquire lock
                lock_flags = (
                    fcntl.LOCK_EX if self.lock_type == LockType.EXCLUSIVE
                    else fcntl.LOCK_SH
                )
                lock_flags |= fcntl.LOCK_NB  # Non-blocking

                fcntl.flock(self._fd, lock_flags)
                self._acquired = True

                # v78.1: Write PID:generation:timestamp to lock file for enhanced stale detection
                if self.lock_type == LockType.EXCLUSIVE:
                    # Generation counter persisted per lock file
                    generation = getattr(self, '_generation', 0) + 1
                    self._generation = generation
                    timestamp = time.time()
                    lock_info = f"{os.getpid()}:{generation}:{timestamp}"

                    os.ftruncate(self._fd, 0)
                    os.lseek(self._fd, 0, os.SEEK_SET)
                    os.write(self._fd, lock_info.encode())
                    os.fsync(self._fd)

                return True

            except BlockingIOError:
                # Lock held by another process
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None

                # Check for stale lock
                if await self._check_stale_lock():
                    continue  # Retry immediately after clearing stale lock

                if attempt >= max_attempts:
                    break

                # Exponential backoff with jitter
                delay = min(
                    base_delay * (2 ** (attempt - 1)),
                    1.0  # Max 1 second
                )
                jitter = delay * 0.2 * (os.urandom(1)[0] / 255.0)
                await asyncio.sleep(delay + jitter)

            except Exception as e:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                self.log.error(f"[AtomicLock] Error acquiring lock: {e}")
                raise LockAcquisitionError(f"Failed to acquire lock: {e}")

        raise LockAcquisitionError(
            f"Lock acquisition timed out after {self.timeout}s ({attempt} attempts)"
        )

    async def _check_stale_lock(self) -> bool:
        """
        Check if the current lock holder is dead.

        v78.1: Enhanced stale lock detection with generation counter and timestamp.
        Lock file format: "pid:generation:timestamp"
        - pid: Process ID of lock holder
        - generation: Monotonically increasing counter per lock acquisition
        - timestamp: Unix timestamp when lock was acquired

        If dead or stale (> 5 minutes old), clear the stale lock.
        """
        STALE_THRESHOLD_SECONDS = 300  # 5 minutes

        try:
            if not self.lock_file.exists():
                return False

            # Read lock info from file
            content = self.lock_file.read_text().strip()
            if not content:
                return False

            # Parse lock file format
            parts = content.split(":")
            if len(parts) >= 1:
                holder_pid = int(parts[0])
            else:
                return False

            # Check timestamp if available (v78.1 format)
            lock_timestamp = None
            if len(parts) >= 3:
                try:
                    lock_timestamp = float(parts[2])
                except ValueError:
                    pass

            # Check if lock is stale by timestamp
            if lock_timestamp:
                age_seconds = time.time() - lock_timestamp
                if age_seconds > STALE_THRESHOLD_SECONDS:
                    self.log.warning(
                        f"[AtomicLock] Clearing stale lock (age: {age_seconds:.0f}s > {STALE_THRESHOLD_SECONDS}s)"
                    )
                    try:
                        self.lock_file.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return True

            # Check if process is still alive
            try:
                os.kill(holder_pid, 0)  # Signal 0 = check existence
                return False  # Process is alive
            except ProcessLookupError:
                # Process is dead, clear stale lock
                self.log.warning(
                    f"[AtomicLock] Clearing stale lock from dead PID {holder_pid}"
                )
                try:
                    self.lock_file.unlink(missing_ok=True)
                except Exception:
                    pass
                return True
            except PermissionError:
                # Process exists but we can't signal it
                return False

        except Exception as e:
            self.log.debug(f"[AtomicLock] Stale check failed: {e}")
            return False

    def release(self):
        """Release the file lock."""
        if self._fd is not None and self._acquired:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except Exception as e:
                self.log.debug(f"[AtomicLock] Release error: {e}")
            finally:
                self._fd = None
                self._acquired = False

    def __del__(self):
        """Ensure lock is released on garbage collection."""
        self.release()


@asynccontextmanager
async def atomic_lock(
    lock_file: Path,
    lock_type: LockType = LockType.EXCLUSIVE,
    timeout: float = 10.0,
):
    """Context manager for atomic file locking."""
    lock = AtomicFileLock(lock_file, lock_type, timeout)
    try:
        await lock.acquire()
        yield lock
    finally:
        lock.release()


# =============================================================================
# Atomic Queue Implementation
# =============================================================================

class AtomicCommandQueue:
    """
    Atomic command queue for Trinity transport.

    Provides race-condition-free queuing between JARVIS, J-Prime,
    and Reactor-Core using file-based transport with OS-level locks.

    Thread-safe and async-compatible. Multiple processes can safely
    read and write to the same queue.
    """

    def __init__(
        self,
        queue_name: str,
        base_dir: Optional[Path] = None,
        max_size: int = 10000,
        enable_deduplication: bool = True,
        dedup_window_seconds: float = 300.0,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.queue_name = queue_name
        self.log = logger_instance or logger
        self.max_size = max_size
        self.enable_deduplication = enable_deduplication
        self.dedup_window_seconds = dedup_window_seconds

        # Paths
        self.base_dir = base_dir or (Path.home() / ".jarvis" / "trinity" / "queues")
        self.queue_dir = self.base_dir / queue_name
        self.queue_file = self.queue_dir / "queue.json"
        self.lock_file = self.queue_dir / ".lock"
        self.sequence_file = self.queue_dir / "sequence"
        self.dead_letter_file = self.queue_dir / "dead_letter.json"
        self.journal_file = self.queue_dir / "journal.log"

        # Internal state
        self._sequence_counter = 0
        self._recent_hashes: Dict[str, float] = {}  # hash -> timestamp
        self._stats = QueueStats()
        self._local_lock = asyncio.Lock()

        # Ensure directories exist
        self.queue_dir.mkdir(parents=True, exist_ok=True)

        # Initialize sequence counter
        self._load_sequence_counter()

    def _load_sequence_counter(self):
        """Load sequence counter from disk."""
        try:
            if self.sequence_file.exists():
                self._sequence_counter = int(self.sequence_file.read_text().strip())
        except Exception:
            self._sequence_counter = 0

    def _atomic_increment_sequence(self) -> int:
        """
        Atomically increment sequence counter.

        v78.1: Fixed race condition where crash between increment and save
        could cause duplicate sequence IDs.

        Uses file locking to ensure atomicity:
        1. Lock sequence file
        2. Read current value
        3. Write incremented value
        4. Release lock
        5. Return new value
        """
        lock_file = self.sequence_file.with_suffix(".lock")

        try:
            # Open with exclusive lock
            fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)

                # Read current value from disk (not memory!)
                if self.sequence_file.exists():
                    try:
                        current = int(self.sequence_file.read_text().strip())
                    except (ValueError, OSError):
                        current = self._sequence_counter
                else:
                    current = self._sequence_counter

                # Increment and write atomically
                new_value = current + 1
                temp_file = self.sequence_file.with_suffix(".tmp")

                # Write with explicit fsync for durability
                with open(temp_file, 'w') as f:
                    f.write(str(new_value))
                    f.flush()
                    os.fsync(f.fileno())

                # Atomic rename
                temp_file.rename(self.sequence_file)

                # Update in-memory cache
                self._sequence_counter = new_value

                return new_value

            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

        except Exception as e:
            self.log.error(f"[AtomicQueue] Sequence increment failed: {e}")
            # Fallback: Use timestamp + random for uniqueness
            import random
            fallback = int(time.time() * 1000000) + random.randint(0, 999999)
            self._sequence_counter = fallback
            return fallback

    def _generate_sequence_id(self) -> str:
        """
        Generate a globally unique sequence ID.

        v78.1: Uses atomic increment to prevent duplicate IDs on crash.
        """
        seq_num = self._atomic_increment_sequence()
        return f"{self.queue_name}:{seq_num}:{uuid4().hex[:8]}"

    async def enqueue(
        self,
        payload: Dict[str, Any],
        source: str = "unknown",
        ttl_seconds: float = 3600.0,
        metadata: Optional[Dict[str, Any]] = None,
        skip_dedup: bool = False,
    ) -> Optional[str]:
        """
        Atomically enqueue a command.

        Args:
            payload: Command payload
            source: Source identifier
            ttl_seconds: Time-to-live
            metadata: Additional metadata
            skip_dedup: Skip deduplication check

        Returns:
            Sequence ID if enqueued, None if rejected (duplicate/full)
        """
        # Compute content hash for deduplication
        content_hash = AtomicQueueEntry.compute_hash(payload)

        # Local deduplication check (fast path)
        if self.enable_deduplication and not skip_dedup:
            now = time.time()
            # Clean old hashes
            self._recent_hashes = {
                h: t for h, t in self._recent_hashes.items()
                if now - t < self.dedup_window_seconds
            }

            if content_hash in self._recent_hashes:
                self.log.debug(f"[AtomicQueue] Duplicate rejected: {content_hash}")
                self._stats.total_duplicates_rejected += 1
                return None

        async with self._local_lock:
            try:
                async with atomic_lock(self.lock_file, LockType.EXCLUSIVE):
                    # Read current queue
                    entries = await self._read_queue()

                    # Check capacity
                    pending = [e for e in entries if e.state == EntryState.PENDING]
                    if len(pending) >= self.max_size:
                        self.log.warning(f"[AtomicQueue] Queue full ({self.max_size})")
                        return None

                    # Check for duplicate in queue
                    if self.enable_deduplication and not skip_dedup:
                        for entry in entries:
                            if entry.content_hash == content_hash and not entry.is_expired:
                                self.log.debug(
                                    f"[AtomicQueue] Duplicate in queue: {content_hash}"
                                )
                                self._stats.total_duplicates_rejected += 1
                                return None

                    # Create entry
                    sequence_id = self._generate_sequence_id()
                    entry = AtomicQueueEntry(
                        sequence_id=sequence_id,
                        payload=payload,
                        content_hash=content_hash,
                        source=source,
                        ttl_seconds=ttl_seconds,
                        metadata=metadata or {},
                    )

                    # Add to queue
                    entries.append(entry)

                    # Write atomically
                    await self._write_queue(entries)

                    # Log to journal
                    await self._journal_write("ENQUEUE", sequence_id, payload)

                    # Update local dedup cache
                    self._recent_hashes[content_hash] = time.time()

                    # Update stats
                    self._stats.total_enqueued += 1
                    self._stats.current_size = len(entries)
                    self._stats.pending_count = len(
                        [e for e in entries if e.state == EntryState.PENDING]
                    )

                    self.log.debug(
                        f"[AtomicQueue] Enqueued: {sequence_id} (queue_size={len(entries)})"
                    )

                    return sequence_id

            except LockAcquisitionError:
                self._stats.lock_timeouts += 1
                self.log.error("[AtomicQueue] Failed to acquire lock for enqueue")
                return None

    async def dequeue(
        self,
        count: int = 1,
        mark_processing: bool = True,
    ) -> List[AtomicQueueEntry]:
        """
        Atomically dequeue entries.

        Args:
            count: Number of entries to dequeue
            mark_processing: Whether to mark entries as processing

        Returns:
            List of dequeued entries
        """
        async with self._local_lock:
            try:
                async with atomic_lock(self.lock_file, LockType.EXCLUSIVE):
                    entries = await self._read_queue()

                    # Find pending entries
                    pending = [
                        e for e in entries
                        if e.state == EntryState.PENDING and not e.is_expired
                    ]

                    # Sort by timestamp (oldest first)
                    pending.sort(key=lambda e: e.timestamp)

                    # Get entries to return
                    to_return = pending[:count]

                    if mark_processing:
                        for entry in to_return:
                            entry.state = EntryState.PROCESSING
                            entry.processing_started = time.time()

                        # Write back
                        await self._write_queue(entries)

                    # Update stats
                    self._stats.total_dequeued += len(to_return)
                    self._stats.processing_count = len(
                        [e for e in entries if e.state == EntryState.PROCESSING]
                    )

                    return to_return

            except LockAcquisitionError:
                self._stats.lock_timeouts += 1
                self.log.error("[AtomicQueue] Failed to acquire lock for dequeue")
                return []

    async def acknowledge(
        self,
        sequence_id: str,
        success: bool = True,
        error_message: Optional[str] = None,
    ) -> bool:
        """
        Acknowledge processing of an entry.

        Args:
            sequence_id: ID of entry to acknowledge
            success: Whether processing succeeded
            error_message: Error message if failed

        Returns:
            True if acknowledged successfully
        """
        async with self._local_lock:
            try:
                async with atomic_lock(self.lock_file, LockType.EXCLUSIVE):
                    entries = await self._read_queue()

                    # Find entry
                    entry = None
                    for e in entries:
                        if e.sequence_id == sequence_id:
                            entry = e
                            break

                    if not entry:
                        self.log.warning(
                            f"[AtomicQueue] Entry not found for ack: {sequence_id}"
                        )
                        return False

                    if success:
                        # Calculate processing time
                        if entry.processing_started:
                            processing_time_ms = (
                                time.time() - entry.processing_started
                            ) * 1000
                            self._stats.avg_processing_time_ms = (
                                self._stats.avg_processing_time_ms + processing_time_ms
                            ) / 2

                        entry.state = EntryState.ACKNOWLEDGED
                        self._stats.total_acknowledged += 1

                        # Remove from queue
                        entries = [e for e in entries if e.sequence_id != sequence_id]

                    else:
                        entry.retry_count += 1
                        if entry.can_retry:
                            # Reset for retry
                            entry.state = EntryState.PENDING
                            entry.processing_started = None
                            entry.metadata["last_error"] = error_message
                        else:
                            # Move to dead letter
                            entry.state = EntryState.DEAD_LETTER
                            entry.metadata["final_error"] = error_message
                            await self._move_to_dead_letter(entry)
                            entries = [
                                e for e in entries if e.sequence_id != sequence_id
                            ]
                            self._stats.total_dead_lettered += 1

                        self._stats.total_failed += 1

                    # Write back
                    await self._write_queue(entries)

                    # Journal
                    action = "ACK" if success else "NACK"
                    await self._journal_write(action, sequence_id, {"error": error_message})

                    self._stats.current_size = len(entries)
                    return True

            except LockAcquisitionError:
                self._stats.lock_timeouts += 1
                return False

    async def peek(self, count: int = 10) -> List[AtomicQueueEntry]:
        """Peek at entries without dequeuing."""
        try:
            async with atomic_lock(self.lock_file, LockType.SHARED):
                entries = await self._read_queue()
                pending = [e for e in entries if e.state == EntryState.PENDING]
                pending.sort(key=lambda e: e.timestamp)
                return pending[:count]
        except LockAcquisitionError:
            return []

    async def size(self) -> int:
        """Get current queue size."""
        try:
            async with atomic_lock(self.lock_file, LockType.SHARED):
                entries = await self._read_queue()
                return len([e for e in entries if e.state == EntryState.PENDING])
        except LockAcquisitionError:
            return 0

    async def cleanup_expired(self) -> int:
        """Remove expired entries."""
        async with self._local_lock:
            try:
                async with atomic_lock(self.lock_file, LockType.EXCLUSIVE):
                    entries = await self._read_queue()
                    initial = len(entries)

                    # Remove expired
                    entries = [e for e in entries if not e.is_expired]

                    # Reset timed-out processing
                    for entry in entries:
                        if entry.is_processing_timed_out:
                            entry.state = EntryState.PENDING
                            entry.processing_started = None
                            entry.retry_count += 1

                    await self._write_queue(entries)

                    removed = initial - len(entries)
                    if removed > 0:
                        self.log.info(f"[AtomicQueue] Cleaned up {removed} expired entries")
                        self._stats.total_expired += removed

                    return removed

            except LockAcquisitionError:
                return 0

    async def _read_queue(self) -> List[AtomicQueueEntry]:
        """Read queue from disk."""
        if not self.queue_file.exists():
            return []

        try:
            data = json.loads(self.queue_file.read_text())
            return [AtomicQueueEntry.from_dict(e) for e in data]
        except json.JSONDecodeError as e:
            self.log.error(f"[AtomicQueue] Corrupted queue file: {e}")
            # Try to recover from journal
            return await self._recover_from_journal()

    async def _write_queue(self, entries: List[AtomicQueueEntry]):
        """Write queue to disk atomically."""
        data = [e.to_dict() for e in entries]

        # Write to temp file first
        temp_file = self.queue_file.with_suffix(".tmp")
        temp_file.write_text(json.dumps(data, indent=2))

        # Atomic rename
        temp_file.rename(self.queue_file)

    async def _move_to_dead_letter(self, entry: AtomicQueueEntry):
        """Move entry to dead letter queue."""
        try:
            # Read existing dead letters
            if self.dead_letter_file.exists():
                dead_letters = json.loads(self.dead_letter_file.read_text())
            else:
                dead_letters = []

            # Add entry
            dead_letters.append(entry.to_dict())

            # Write atomically
            temp_file = self.dead_letter_file.with_suffix(".tmp")
            temp_file.write_text(json.dumps(dead_letters, indent=2))
            temp_file.rename(self.dead_letter_file)

        except Exception as e:
            self.log.error(f"[AtomicQueue] Failed to write dead letter: {e}")

    async def _journal_write(
        self,
        action: str,
        sequence_id: str,
        data: Dict[str, Any],
    ):
        """Write to journal for crash recovery."""
        try:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "action": action,
                "sequence_id": sequence_id,
                "data": data,
            }
            with open(self.journal_file, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            self.log.debug(f"[AtomicQueue] Journal write failed: {e}")

    async def _recover_from_journal(self) -> List[AtomicQueueEntry]:
        """Recover queue state from journal."""
        if not self.journal_file.exists():
            return []

        entries: Dict[str, AtomicQueueEntry] = {}

        try:
            with open(self.journal_file, "r") as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        action = record.get("action")
                        seq_id = record.get("sequence_id")

                        if action == "ENQUEUE":
                            entries[seq_id] = AtomicQueueEntry(
                                sequence_id=seq_id,
                                payload=record.get("data", {}),
                                content_hash=AtomicQueueEntry.compute_hash(
                                    record.get("data", {})
                                ),
                            )
                        elif action == "ACK" and seq_id in entries:
                            del entries[seq_id]
                        elif action == "NACK" and seq_id in entries:
                            entries[seq_id].retry_count += 1

                    except json.JSONDecodeError:
                        continue

            return list(entries.values())

        except Exception as e:
            self.log.error(f"[AtomicQueue] Journal recovery failed: {e}")
            return []

    def get_stats(self) -> QueueStats:
        """Get queue statistics."""
        return self._stats

    def visualize(self) -> str:
        """Generate visualization of queue state."""
        lines = [
            f"[AtomicQueue: {self.queue_name}]",
            f"  Size: {self._stats.current_size}/{self.max_size}",
            f"  Pending: {self._stats.pending_count}",
            f"  Processing: {self._stats.processing_count}",
            f"  Total enqueued: {self._stats.total_enqueued}",
            f"  Total acknowledged: {self._stats.total_acknowledged}",
            f"  Total failed: {self._stats.total_failed}",
            f"  Duplicates rejected: {self._stats.total_duplicates_rejected}",
            f"  Lock timeouts: {self._stats.lock_timeouts}",
        ]
        return "\n".join(lines)


# =============================================================================
# Singleton Factory
# =============================================================================

_queues: Dict[str, AtomicCommandQueue] = {}
_queues_lock = asyncio.Lock()


async def get_atomic_queue(queue_name: str) -> AtomicCommandQueue:
    """Get or create an atomic queue instance."""
    global _queues

    async with _queues_lock:
        if queue_name not in _queues:
            _queues[queue_name] = AtomicCommandQueue(queue_name)
        return _queues[queue_name]


def get_atomic_queue_sync(queue_name: str) -> Optional[AtomicCommandQueue]:
    """Get an atomic queue synchronously (may be None)."""
    return _queues.get(queue_name)


# Standard Trinity queues
JARVIS_TO_JPRIME = "jarvis_to_jprime"
JPRIME_TO_JARVIS = "jprime_to_jarvis"
JARVIS_TO_REACTOR = "jarvis_to_reactor"
REACTOR_TO_JARVIS = "reactor_to_jarvis"
JPRIME_TO_REACTOR = "jprime_to_reactor"
REACTOR_TO_JPRIME = "reactor_to_jprime"
