"""
v78.0: Command Buffer for Early Trinity Commands
================================================

Handles commands that arrive before the Coding Council is fully initialized.
Provides FIFO buffering with priority support and automatic replay.

Features:
- Buffer early commands with configurable capacity
- Priority-based ordering (urgent commands first)
- TTL-based expiration (stale commands dropped)
- Automatic replay when system ready
- Persistence to disk for crash recovery
- Backpressure signaling when buffer full
- Statistics and monitoring

Architecture:
    Command Arrives → [Check Ready?] → Yes → Execute Immediately
                            ↓ No
                    [Check Buffer Full?] → Yes → Reject/Backpressure
                            ↓ No
                    [Add to Buffer] → [Persist to Disk]
                            ↓
                    [System Ready Signal]
                            ↓
                    [Replay Buffered Commands]

Usage:
    from backend.core.coding_council.advanced.command_buffer import (
        get_command_buffer,
        CommandPriority,
    )

    buffer = await get_command_buffer()

    # Buffer a command
    await buffer.enqueue(command_data, priority=CommandPriority.HIGH)

    # Signal system is ready
    await buffer.signal_ready()  # Triggers replay

Author: JARVIS v78.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, IntEnum
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Tuple,
)
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# Enums
# =============================================================================

class CommandPriority(IntEnum):
    """Priority levels for buffered commands."""
    CRITICAL = 0     # System-critical, must execute first
    URGENT = 1       # Time-sensitive operations
    HIGH = 2         # Important but not time-sensitive
    NORMAL = 3       # Standard priority
    LOW = 4          # Background tasks, can wait
    BACKGROUND = 5   # Lowest priority, execute last


class CommandState(Enum):
    """State of a buffered command."""
    BUFFERED = "buffered"           # In buffer, waiting
    REPLAYING = "replaying"         # Currently being replayed
    EXECUTED = "executed"           # Successfully executed
    FAILED = "failed"               # Execution failed
    EXPIRED = "expired"             # TTL exceeded
    DROPPED = "dropped"             # Dropped due to buffer full


class CommandType(Enum):
    """Types of Trinity commands."""
    CODE_REVIEW = "code_review"
    REFACTOR = "refactor"
    GENERATE = "generate"
    ANALYZE = "analyze"
    FIX = "fix"
    OPTIMIZE = "optimize"
    TEST = "test"
    DOCUMENT = "document"
    TRINITY_SYNC = "trinity_sync"
    HEARTBEAT = "heartbeat"
    GENERIC = "generic"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass(order=True)
class BufferedCommand:
    """
    A command buffered for later execution.

    Ordered by priority (lower = higher priority), then by timestamp (earlier first).
    """
    priority: int = field(compare=True)
    timestamp: float = field(compare=True)
    command_id: str = field(compare=False)
    command_type: CommandType = field(compare=False, default=CommandType.GENERIC)
    payload: Dict[str, Any] = field(compare=False, default_factory=dict)
    state: CommandState = field(compare=False, default=CommandState.BUFFERED)
    source: str = field(compare=False, default="unknown")
    ttl_seconds: float = field(compare=False, default=300.0)  # 5 min default
    retry_count: int = field(compare=False, default=0)
    max_retries: int = field(compare=False, default=3)
    metadata: Dict[str, Any] = field(compare=False, default_factory=dict)
    callback: Optional[str] = field(compare=False, default=None)

    def __post_init__(self):
        if not self.command_id:
            self.command_id = str(uuid4())

    @property
    def is_expired(self) -> bool:
        """Check if command has exceeded TTL."""
        return (time.time() - self.timestamp) > self.ttl_seconds

    @property
    def age_seconds(self) -> float:
        """Get command age in seconds."""
        return time.time() - self.timestamp

    @property
    def can_retry(self) -> bool:
        """Check if command can be retried."""
        return self.retry_count < self.max_retries

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for persistence."""
        return {
            "command_id": self.command_id,
            "priority": self.priority,
            "timestamp": self.timestamp,
            "command_type": self.command_type.value,
            "payload": self.payload,
            "state": self.state.value,
            "source": self.source,
            "ttl_seconds": self.ttl_seconds,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "metadata": self.metadata,
            "callback": self.callback,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BufferedCommand":
        """Create from dictionary."""
        return cls(
            command_id=data.get("command_id", str(uuid4())),
            priority=data.get("priority", CommandPriority.NORMAL),
            timestamp=data.get("timestamp", time.time()),
            command_type=CommandType(data.get("command_type", "generic")),
            payload=data.get("payload", {}),
            state=CommandState(data.get("state", "buffered")),
            source=data.get("source", "unknown"),
            ttl_seconds=data.get("ttl_seconds", 300.0),
            retry_count=data.get("retry_count", 0),
            max_retries=data.get("max_retries", 3),
            metadata=data.get("metadata", {}),
            callback=data.get("callback"),
        )


@dataclass
class BufferStats:
    """Statistics about the command buffer."""
    total_buffered: int = 0
    total_replayed: int = 0
    total_executed: int = 0
    total_failed: int = 0
    total_expired: int = 0
    total_dropped: int = 0
    current_size: int = 0
    max_size: int = 0
    avg_wait_time_ms: float = 0.0
    oldest_command_age_seconds: float = 0.0
    by_priority: Dict[str, int] = field(default_factory=dict)
    by_type: Dict[str, int] = field(default_factory=dict)


# =============================================================================
# Command Buffer Implementation
# =============================================================================

class CommandBuffer:
    """
    Buffer for early Trinity commands.

    Handles commands that arrive before the Coding Council is fully initialized.
    Provides priority ordering, TTL expiration, and automatic replay.

    Thread-safe and async-compatible.
    """

    def __init__(
        self,
        max_size: int = 1000,
        default_ttl: float = 300.0,
        persist_to_disk: bool = True,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.log = logger_instance or logger
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.persist_to_disk = persist_to_disk

        self._buffer: List[BufferedCommand] = []
        self._executed: Dict[str, BufferedCommand] = {}
        self._failed: Dict[str, BufferedCommand] = {}
        self._lock = asyncio.Lock()
        self._ready_event = asyncio.Event()
        self._replay_in_progress = False
        self._executor: Optional[Callable] = None
        self._callbacks: Dict[str, Callable] = {}
        self._persist_file = Path.home() / ".jarvis" / "trinity" / "command_buffer.json"
        self._stats = BufferStats()

        # Ensure persist directory exists
        if self.persist_to_disk:
            self._persist_file.parent.mkdir(parents=True, exist_ok=True)

    def set_executor(self, executor: Callable[[BufferedCommand], Awaitable[bool]]):
        """
        Set the command executor function.

        Args:
            executor: Async function that executes a command and returns True on success
        """
        self._executor = executor

    def register_callback(self, command_id: str, callback: Callable):
        """Register a callback to be called when a command completes."""
        self._callbacks[command_id] = callback

    @property
    def is_ready(self) -> bool:
        """Check if the system is ready (no buffering needed)."""
        return self._ready_event.is_set()

    @property
    def size(self) -> int:
        """Get current buffer size."""
        return len(self._buffer)

    @property
    def is_full(self) -> bool:
        """Check if buffer is at capacity."""
        return len(self._buffer) >= self.max_size

    async def enqueue(
        self,
        payload: Dict[str, Any],
        command_type: CommandType = CommandType.GENERIC,
        priority: CommandPriority = CommandPriority.NORMAL,
        source: str = "unknown",
        ttl: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        callback: Optional[Callable] = None,
    ) -> Optional[str]:
        """
        Enqueue a command for buffering or immediate execution.

        If the system is ready, the command is executed immediately.
        Otherwise, it's buffered for later replay.

        Args:
            payload: Command payload data
            command_type: Type of command
            priority: Command priority
            source: Source of the command
            ttl: Time-to-live in seconds (None for default)
            metadata: Additional metadata
            callback: Optional callback for completion

        Returns:
            Command ID if buffered/executed, None if dropped
        """
        command = BufferedCommand(
            priority=priority,
            timestamp=time.time(),
            command_id=str(uuid4()),
            command_type=command_type,
            payload=payload,
            source=source,
            ttl_seconds=ttl or self.default_ttl,
            metadata=metadata or {},
        )

        # Register callback if provided
        if callback:
            self._callbacks[command.command_id] = callback

        # If system is ready, execute immediately
        if self.is_ready and self._executor:
            self.log.debug(f"[CommandBuffer] Executing immediately: {command.command_id}")
            try:
                success = await self._executor(command)
                command.state = CommandState.EXECUTED if success else CommandState.FAILED
                self._stats.total_executed += 1 if success else 0
                self._stats.total_failed += 0 if success else 1
                await self._fire_callback(command)
                return command.command_id
            except Exception as e:
                self.log.error(f"[CommandBuffer] Immediate execution failed: {e}")
                # Fall through to buffering

        # Buffer the command
        async with self._lock:
            # Check capacity
            if self.is_full:
                # Try to make room by removing expired commands
                await self._cleanup_expired()

                if self.is_full:
                    self.log.warning(
                        f"[CommandBuffer] Buffer full ({self.max_size}), dropping command"
                    )
                    command.state = CommandState.DROPPED
                    self._stats.total_dropped += 1
                    return None

            # Add to buffer (sorted by priority, then timestamp)
            self._buffer.append(command)
            self._buffer.sort()

            self._stats.total_buffered += 1
            self._stats.current_size = len(self._buffer)
            self._stats.max_size = max(self._stats.max_size, len(self._buffer))

            # Update stats by type and priority
            type_key = command_type.value
            self._stats.by_type[type_key] = self._stats.by_type.get(type_key, 0) + 1
            priority_key = CommandPriority(priority).name
            self._stats.by_priority[priority_key] = self._stats.by_priority.get(priority_key, 0) + 1

            self.log.info(
                f"[CommandBuffer] Buffered command: {command.command_id} "
                f"(type={command_type.value}, priority={CommandPriority(priority).name}, "
                f"buffer_size={len(self._buffer)})"
            )

            # Persist to disk
            if self.persist_to_disk:
                await self._persist()

            return command.command_id

    async def signal_ready(self):
        """
        Signal that the system is ready.

        This triggers replay of all buffered commands.
        """
        self.log.info("[CommandBuffer] System ready signal received")
        self._ready_event.set()

        # Start replay
        await self.replay_all()

    async def replay_all(self) -> Tuple[int, int, int]:
        """
        Replay all buffered commands.

        Returns:
            Tuple of (executed, failed, expired) counts
        """
        if self._replay_in_progress:
            self.log.warning("[CommandBuffer] Replay already in progress")
            return (0, 0, 0)

        if not self._executor:
            self.log.error("[CommandBuffer] No executor set, cannot replay")
            return (0, 0, 0)

        self._replay_in_progress = True
        executed = 0
        failed = 0
        expired = 0

        try:
            async with self._lock:
                # Clean up expired first
                expired = await self._cleanup_expired()

                # Copy and clear buffer
                commands = self._buffer.copy()
                self._buffer.clear()

            self.log.info(
                f"[CommandBuffer] Replaying {len(commands)} buffered commands..."
            )

            for command in commands:
                if command.is_expired:
                    command.state = CommandState.EXPIRED
                    expired += 1
                    continue

                command.state = CommandState.REPLAYING
                wait_time_ms = command.age_seconds * 1000

                try:
                    success = await self._executor(command)
                    if success:
                        command.state = CommandState.EXECUTED
                        self._executed[command.command_id] = command
                        executed += 1
                        self._stats.avg_wait_time_ms = (
                            (self._stats.avg_wait_time_ms + wait_time_ms) / 2
                        )
                    else:
                        # Retry if possible
                        if command.can_retry:
                            command.retry_count += 1
                            command.state = CommandState.BUFFERED
                            async with self._lock:
                                self._buffer.append(command)
                        else:
                            command.state = CommandState.FAILED
                            self._failed[command.command_id] = command
                            failed += 1
                except Exception as e:
                    self.log.error(f"[CommandBuffer] Replay failed: {e}")
                    command.state = CommandState.FAILED
                    self._failed[command.command_id] = command
                    failed += 1

                await self._fire_callback(command)

            # Update stats
            self._stats.total_replayed += executed
            self._stats.total_executed += executed
            self._stats.total_failed += failed
            self._stats.total_expired += expired
            self._stats.current_size = len(self._buffer)

            self.log.info(
                f"[CommandBuffer] Replay complete: executed={executed}, "
                f"failed={failed}, expired={expired}"
            )

        finally:
            self._replay_in_progress = False
            if self.persist_to_disk:
                await self._persist()

        return (executed, failed, expired)

    async def _cleanup_expired(self) -> int:
        """Remove expired commands from buffer."""
        initial_count = len(self._buffer)
        now = time.time()

        self._buffer = [
            cmd for cmd in self._buffer
            if (now - cmd.timestamp) <= cmd.ttl_seconds
        ]

        expired_count = initial_count - len(self._buffer)
        if expired_count > 0:
            self.log.info(f"[CommandBuffer] Cleaned up {expired_count} expired commands")

        return expired_count

    async def _fire_callback(self, command: BufferedCommand):
        """Fire callback for completed command."""
        callback = self._callbacks.pop(command.command_id, None)
        if callback:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(command)
                else:
                    callback(command)
            except Exception as e:
                self.log.error(f"[CommandBuffer] Callback error: {e}")

    async def get_command(self, command_id: str) -> Optional[BufferedCommand]:
        """Get a command by ID."""
        # Check buffer
        for cmd in self._buffer:
            if cmd.command_id == command_id:
                return cmd

        # Check executed/failed
        if command_id in self._executed:
            return self._executed[command_id]
        if command_id in self._failed:
            return self._failed[command_id]

        return None

    async def cancel_command(self, command_id: str) -> bool:
        """Cancel a buffered command."""
        async with self._lock:
            for i, cmd in enumerate(self._buffer):
                if cmd.command_id == command_id:
                    self._buffer.pop(i)
                    self._stats.current_size = len(self._buffer)
                    self.log.info(f"[CommandBuffer] Cancelled command: {command_id}")
                    return True
        return False

    async def flush(self) -> int:
        """Flush all buffered commands (drop them)."""
        async with self._lock:
            count = len(self._buffer)
            self._stats.total_dropped += count
            self._buffer.clear()
            self._stats.current_size = 0
            self.log.info(f"[CommandBuffer] Flushed {count} commands")
            return count

    def get_stats(self) -> BufferStats:
        """Get buffer statistics."""
        self._stats.current_size = len(self._buffer)
        if self._buffer:
            oldest = min(cmd.timestamp for cmd in self._buffer)
            self._stats.oldest_command_age_seconds = time.time() - oldest
        return self._stats

    async def _persist(self):
        """Persist buffer state to disk."""
        try:
            state = {
                "timestamp": datetime.now().isoformat(),
                "commands": [cmd.to_dict() for cmd in self._buffer],
                "stats": {
                    "total_buffered": self._stats.total_buffered,
                    "total_replayed": self._stats.total_replayed,
                    "total_executed": self._stats.total_executed,
                    "total_failed": self._stats.total_failed,
                    "total_expired": self._stats.total_expired,
                    "total_dropped": self._stats.total_dropped,
                },
            }
            self._persist_file.write_text(json.dumps(state, indent=2))
        except Exception as e:
            self.log.debug(f"[CommandBuffer] Failed to persist: {e}")

    async def load_state(self) -> bool:
        """Load buffer state from disk."""
        try:
            if not self._persist_file.exists():
                return False

            state = json.loads(self._persist_file.read_text())

            # Load commands (only non-expired)
            for cmd_data in state.get("commands", []):
                cmd = BufferedCommand.from_dict(cmd_data)
                if not cmd.is_expired:
                    self._buffer.append(cmd)

            # Sort buffer
            self._buffer.sort()
            self._stats.current_size = len(self._buffer)

            self.log.info(
                f"[CommandBuffer] Loaded {len(self._buffer)} commands from disk"
            )
            return True

        except Exception as e:
            self.log.debug(f"[CommandBuffer] Failed to load state: {e}")
            return False

    def visualize(self) -> str:
        """Generate visualization of buffer contents."""
        if not self._buffer:
            return "[CommandBuffer] Empty"

        lines = [
            f"[CommandBuffer] {len(self._buffer)} commands buffered:",
            "-" * 60,
        ]

        for i, cmd in enumerate(self._buffer[:10]):  # Show first 10
            priority_name = CommandPriority(cmd.priority).name
            age = f"{cmd.age_seconds:.1f}s"
            ttl_remaining = max(0, cmd.ttl_seconds - cmd.age_seconds)

            lines.append(
                f"  {i+1}. [{priority_name}] {cmd.command_type.value} "
                f"(age={age}, TTL={ttl_remaining:.0f}s, retries={cmd.retry_count})"
            )

        if len(self._buffer) > 10:
            lines.append(f"  ... and {len(self._buffer) - 10} more")

        return "\n".join(lines)


# =============================================================================
# Singleton Instance
# =============================================================================

_command_buffer: Optional[CommandBuffer] = None
_buffer_lock: Optional[asyncio.Lock] = None  # v78.1: Lazy init for Python 3.9 compat


def _get_buffer_lock() -> asyncio.Lock:
    """v78.1: Lazy lock initialization to avoid 'no running event loop' error on import."""
    global _buffer_lock
    if _buffer_lock is None:
        _buffer_lock = asyncio.Lock()
    return _buffer_lock


async def get_command_buffer() -> CommandBuffer:
    """Get or create the singleton command buffer instance."""
    global _command_buffer

    async with _get_buffer_lock():
        if _command_buffer is None:
            _command_buffer = CommandBuffer()
            # Try to load previous state
            await _command_buffer.load_state()
        return _command_buffer


def get_command_buffer_sync() -> Optional[CommandBuffer]:
    """Get the command buffer synchronously (may be None)."""
    return _command_buffer
