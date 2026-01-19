"""
v77.0: Persistent Message Queue - Gap #4
=========================================

Durable message queue for Trinity commands:
- SQLite-backed persistence
- Priority-based delivery
- Automatic retry with exponential backoff
- Dead letter queue for failed messages
- Message deduplication
- TTL-based expiration

Author: JARVIS v77.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional

logger = logging.getLogger(__name__)


class MessagePriority(Enum):
    """Message priority levels."""
    CRITICAL = 1  # Immediate processing
    HIGH = 3
    NORMAL = 5
    LOW = 7
    BACKGROUND = 9


class MessageStatus(Enum):
    """Message processing status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD = "dead"  # In dead letter queue


@dataclass
class QueueMessage:
    """A message in the queue."""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    channel: str = "default"
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: MessagePriority = MessagePriority.NORMAL
    status: MessageStatus = MessageStatus.PENDING
    ttl_seconds: float = 3600.0  # 1 hour default
    retry_count: int = 0
    max_retries: int = 3
    last_error: Optional[str] = None
    correlation_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "channel": self.channel,
            "payload": self.payload,
            "priority": self.priority.value,
            "status": self.status.value,
            "ttl_seconds": self.ttl_seconds,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "last_error": self.last_error,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueMessage":
        return cls(
            id=data.get("id", str(uuid.uuid4())),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            channel=data.get("channel", "default"),
            payload=data.get("payload", {}),
            priority=MessagePriority(data.get("priority", 5)),
            status=MessageStatus(data.get("status", "pending")),
            ttl_seconds=data.get("ttl_seconds", 3600.0),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            last_error=data.get("last_error"),
            correlation_id=data.get("correlation_id"),
        )

    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl_seconds


class PersistentMessageQueue:
    """
    SQLite-backed persistent message queue.

    Features:
    - Durable storage (survives restarts)
    - Priority-based ordering
    - Automatic retry with backoff
    - Dead letter queue
    - Channel-based routing
    - Deduplication
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or Path.home() / ".jarvis" / "trinity" / "queue.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._handlers: Dict[str, List[Callable]] = {}
        self._processing_task: Optional[asyncio.Task] = None
        self._running = False
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database."""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    channel TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    ttl_seconds REAL NOT NULL,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    last_error TEXT,
                    correlation_id TEXT
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_status
                ON messages(status, priority, created_at)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_messages_channel
                ON messages(channel, status)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS dead_letters (
                    id TEXT PRIMARY KEY,
                    original_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    failed_at REAL NOT NULL,
                    error TEXT,
                    retry_count INTEGER
                )
            """)
            conn.commit()

    @contextmanager
    def _get_conn(self):
        """Get SQLite connection with WAL mode for concurrency."""
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    async def start(self) -> None:
        """Start the message processor."""
        if self._running:
            return

        self._running = True
        self._processing_task = asyncio.create_task(self._process_loop())
        logger.info("[MessageQueue] Started")

    async def stop(self) -> None:
        """Stop the message processor."""
        self._running = False
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
        logger.info("[MessageQueue] Stopped")

    async def enqueue(self, message: QueueMessage) -> bool:
        """
        Add a message to the queue.

        Returns True if enqueued, False if duplicate.
        """
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    # Check for duplicate
                    existing = conn.execute(
                        "SELECT id FROM messages WHERE id = ?",
                        (message.id,)
                    ).fetchone()

                    if existing:
                        logger.debug(f"[MessageQueue] Duplicate message: {message.id}")
                        return False

                    conn.execute("""
                        INSERT INTO messages (
                            id, created_at, updated_at, channel, payload,
                            priority, status, ttl_seconds, retry_count,
                            max_retries, last_error, correlation_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        message.id,
                        message.created_at,
                        message.updated_at,
                        message.channel,
                        json.dumps(message.payload),
                        message.priority.value,
                        message.status.value,
                        message.ttl_seconds,
                        message.retry_count,
                        message.max_retries,
                        message.last_error,
                        message.correlation_id,
                    ))
                    conn.commit()

                return True

            except Exception as e:
                logger.error(f"[MessageQueue] Enqueue failed: {e}")
                return False

    async def dequeue(
        self,
        channel: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Optional[QueueMessage]:
        """
        v93.0: Get next message from queue with optional timeout.

        Returns highest priority pending message. If timeout is specified,
        will poll for a message until timeout expires.

        Args:
            channel: Optional channel to filter messages
            timeout: Optional timeout in seconds to wait for a message.
                    If None, returns immediately (non-blocking).
                    If specified, polls with exponential backoff until timeout.

        Returns:
            QueueMessage if found, None if no message available
        """
        start_time = time.time()
        poll_interval = 0.1  # Start with 100ms
        max_poll_interval = 1.0  # Cap at 1 second

        while True:
            message = await self._dequeue_once(channel)
            if message is not None:
                return message

            # If no timeout specified, return immediately
            if timeout is None:
                return None

            # Check if timeout expired
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                return None

            # Wait before polling again (with exponential backoff)
            remaining = timeout - elapsed
            wait_time = min(poll_interval, remaining, max_poll_interval)
            await asyncio.sleep(wait_time)

            # Exponential backoff (capped)
            poll_interval = min(poll_interval * 1.5, max_poll_interval)

    async def _dequeue_once(self, channel: Optional[str] = None) -> Optional[QueueMessage]:
        """v93.0: Internal single dequeue attempt."""
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    query = """
                        SELECT * FROM messages
                        WHERE status = 'pending'
                    """
                    params = []

                    if channel:
                        query += " AND channel = ?"
                        params.append(channel)

                    query += " ORDER BY priority ASC, created_at ASC LIMIT 1"

                    row = conn.execute(query, params).fetchone()

                    if not row:
                        return None

                    message = self._row_to_message(row)

                    # Check expiration
                    if message.is_expired():
                        conn.execute(
                            "UPDATE messages SET status = 'dead' WHERE id = ?",
                            (message.id,)
                        )
                        conn.commit()
                        return None

                    # Mark as processing
                    conn.execute(
                        "UPDATE messages SET status = 'processing', updated_at = ? WHERE id = ?",
                        (time.time(), message.id)
                    )
                    conn.commit()

                    message.status = MessageStatus.PROCESSING
                    return message

            except Exception as e:
                logger.error(f"[MessageQueue] Dequeue failed: {e}")
                return None

    async def complete(self, message_id: str) -> bool:
        """Mark message as completed."""
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    conn.execute(
                        "DELETE FROM messages WHERE id = ?",
                        (message_id,)
                    )
                    conn.commit()
                return True
            except Exception as e:
                logger.error(f"[MessageQueue] Complete failed: {e}")
                return False

    async def fail(self, message_id: str, error: str) -> bool:
        """
        Mark message as failed.

        Will retry if under max_retries, otherwise move to dead letter queue.
        """
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    row = conn.execute(
                        "SELECT * FROM messages WHERE id = ?",
                        (message_id,)
                    ).fetchone()

                    if not row:
                        return False

                    message = self._row_to_message(row)
                    message.retry_count += 1
                    message.last_error = error

                    if message.retry_count >= message.max_retries:
                        # Move to dead letter queue
                        conn.execute("""
                            INSERT INTO dead_letters (
                                id, original_id, channel, payload,
                                created_at, failed_at, error, retry_count
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            str(uuid.uuid4()),
                            message.id,
                            message.channel,
                            json.dumps(message.payload),
                            message.created_at,
                            time.time(),
                            error,
                            message.retry_count,
                        ))

                        conn.execute(
                            "DELETE FROM messages WHERE id = ?",
                            (message_id,)
                        )

                        logger.warning(f"[MessageQueue] Message {message_id} moved to DLQ")
                    else:
                        # Schedule retry
                        delay = self._calculate_backoff(message.retry_count)
                        conn.execute("""
                            UPDATE messages
                            SET status = 'pending', retry_count = ?,
                                last_error = ?, updated_at = ?, created_at = ?
                            WHERE id = ?
                        """, (
                            message.retry_count,
                            error,
                            time.time(),
                            time.time() + delay,  # Delay reprocessing
                            message_id,
                        ))

                        logger.info(f"[MessageQueue] Message {message_id} scheduled for retry in {delay}s")

                    conn.commit()
                    return True

            except Exception as e:
                logger.error(f"[MessageQueue] Fail failed: {e}")
                return False

    def subscribe(self, channel: str, handler: Callable[[QueueMessage], Coroutine]) -> None:
        """Subscribe to a channel with a handler."""
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)
        logger.info(f"[MessageQueue] Subscribed to channel: {channel}")

    async def get_stats(self) -> Dict[str, Any]:
        """Get queue statistics."""
        with self._get_conn() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE status = 'pending'"
            ).fetchone()[0]

            processing = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE status = 'processing'"
            ).fetchone()[0]

            dead_letters = conn.execute(
                "SELECT COUNT(*) FROM dead_letters"
            ).fetchone()[0]

            by_channel = conn.execute("""
                SELECT channel, COUNT(*) as count
                FROM messages WHERE status = 'pending'
                GROUP BY channel
            """).fetchall()

            return {
                "pending": pending,
                "processing": processing,
                "dead_letters": dead_letters,
                "by_channel": {row["channel"]: row["count"] for row in by_channel},
            }

    async def get_dead_letters(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get messages from dead letter queue."""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM dead_letters
                ORDER BY failed_at DESC
                LIMIT ?
            """, (limit,)).fetchall()

            return [dict(row) for row in rows]

    async def retry_dead_letter(self, dead_letter_id: str) -> bool:
        """Retry a message from dead letter queue."""
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    row = conn.execute(
                        "SELECT * FROM dead_letters WHERE id = ?",
                        (dead_letter_id,)
                    ).fetchone()

                    if not row:
                        return False

                    # Create new message
                    message = QueueMessage(
                        channel=row["channel"],
                        payload=json.loads(row["payload"]),
                        correlation_id=row["original_id"],
                    )

                    # Remove from DLQ
                    conn.execute(
                        "DELETE FROM dead_letters WHERE id = ?",
                        (dead_letter_id,)
                    )
                    conn.commit()

                await self.enqueue(message)
                return True

            except Exception as e:
                logger.error(f"[MessageQueue] Retry dead letter failed: {e}")
                return False

    def _row_to_message(self, row: sqlite3.Row) -> QueueMessage:
        """Convert database row to QueueMessage."""
        return QueueMessage(
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            channel=row["channel"],
            payload=json.loads(row["payload"]),
            priority=MessagePriority(row["priority"]),
            status=MessageStatus(row["status"]),
            ttl_seconds=row["ttl_seconds"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            last_error=row["last_error"],
            correlation_id=row["correlation_id"],
        )

    def _calculate_backoff(self, retry_count: int) -> float:
        """Calculate exponential backoff delay."""
        base = 1.0
        max_delay = 300.0  # 5 minutes max
        delay = base * (2 ** retry_count)
        return min(delay, max_delay)

    async def _process_loop(self) -> None:
        """Background loop to process messages."""
        while self._running:
            try:
                # Get all unique channels with handlers
                for channel in self._handlers.keys():
                    message = await self.dequeue(channel)

                    if message:
                        # Process with handlers
                        success = True
                        error_msg = None

                        for handler in self._handlers.get(message.channel, []):
                            try:
                                await handler(message)
                            except Exception as e:
                                success = False
                                error_msg = str(e)
                                logger.error(f"[MessageQueue] Handler error: {e}")
                                break

                        if success:
                            await self.complete(message.id)
                        else:
                            await self.fail(message.id, error_msg or "Unknown error")

                await asyncio.sleep(0.1)  # Prevent busy loop

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MessageQueue] Process loop error: {e}")
                await asyncio.sleep(1)

    async def cleanup_expired(self) -> int:
        """Remove expired messages. Returns count removed."""
        async with self._lock:
            try:
                with self._get_conn() as conn:
                    now = time.time()
                    result = conn.execute("""
                        DELETE FROM messages
                        WHERE created_at + ttl_seconds < ?
                    """, (now,))
                    count = result.rowcount
                    conn.commit()
                    return count
            except Exception:
                return 0
