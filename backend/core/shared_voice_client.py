# backend/core/shared_voice_client.py
"""
Shared Voice Client - Cross-repo voice announcement client.

This is a standalone module designed to be copied to or symlinked from
JARVIS Prime and Reactor Core repos. It has NO dependencies on JARVIS internals.

Sends announcements to the VoiceOrchestrator via Unix domain socket IPC.
Provides:
- Bounded local queue with drop-oldest policy when disconnected
- Reconnect loop with exponential backoff
- Coalesces large queues on reconnect
- VoiceContext mapping for semantic categories

Usage:
    from shared_voice_client import VoiceClient, VoicePriority, VoiceContext, announce

    # Option 1: Use the convenience function
    await announce(
        message="JARVIS Prime ready",
        context=VoiceContext.TRINITY,
        priority=VoicePriority.HIGH,
        source="jarvis_prime",
    )

    # Option 2: Use client directly for more control
    client = VoiceClient(source="jarvis_prime")
    await client.start()
    await client.announce("Model loaded", VoicePriority.NORMAL, "init")
    await client.stop()

Author: JARVIS Voice Orchestrator v1.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any, Deque, Dict, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Priority & Context Enums (Backward compatible with trinity_voice_coordinator)
# =============================================================================

class VoicePriority(Enum):
    """Priority levels for voice announcements."""
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    NORMAL = "NORMAL"
    LOW = "LOW"
    BACKGROUND = "BACKGROUND"

    # Numeric aliases for backward compatibility
    @classmethod
    def from_int(cls, value: int) -> "VoicePriority":
        """Convert integer priority to enum (backward compat)."""
        mapping = {0: cls.CRITICAL, 1: cls.HIGH, 2: cls.NORMAL, 3: cls.LOW, 4: cls.BACKGROUND}
        return mapping.get(value, cls.NORMAL)


class VoiceContext(Enum):
    """
    Semantic context for voice announcements.

    Maps to categories in the VoiceOrchestrator coalescer:
    - STARTUP → "init"
    - TRINITY → "init"
    - RUNTIME → "general"
    - NARRATOR → "general"
    - ALERT → "error"
    - SUCCESS → "ready"
    - SHUTDOWN → "shutdown"
    """
    STARTUP = "startup"
    TRINITY = "trinity"
    RUNTIME = "runtime"
    NARRATOR = "narrator"
    ALERT = "alert"
    SUCCESS = "success"
    SHUTDOWN = "shutdown"
    HEALTH = "health"
    PROGRESS = "progress"

    @property
    def category(self) -> str:
        """Map context to coalescer category."""
        mapping = {
            VoiceContext.STARTUP: "init",
            VoiceContext.TRINITY: "init",
            VoiceContext.RUNTIME: "general",
            VoiceContext.NARRATOR: "general",
            VoiceContext.ALERT: "error",
            VoiceContext.SUCCESS: "ready",
            VoiceContext.SHUTDOWN: "shutdown",
            VoiceContext.HEALTH: "health",
            VoiceContext.PROGRESS: "progress",
        }
        return mapping.get(self, "general")


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class QueuedMessage:
    """Message queued locally when disconnected."""
    text: str
    priority: VoicePriority
    category: str
    source: str
    timestamp: float
    metadata: Optional[Dict[str, Any]] = None


# =============================================================================
# VoiceClient
# =============================================================================

class VoiceClient:
    """
    Cross-repo voice client. Sends to VoiceOrchestrator via Unix socket.

    Features:
    - Bounded local queue with drop-oldest policy
    - Reconnect loop with exponential backoff
    - Coalesces large queues on reconnect
    - Thread-safe for concurrent announce() calls
    """

    def __init__(self, source: str):
        """
        Initialize client.

        Args:
            source: Identifier for this client (e.g., "jarvis_prime", "reactor_core")
        """
        self._source = source
        self._socket_path = self._get_socket_path()
        self._connected = False
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None

        # Bounded local queue with drop-oldest policy
        max_size = int(os.environ.get("VOICE_CLIENT_QUEUE_MAX", "20"))
        self._local_queue: Deque[QueuedMessage] = deque(maxlen=max_size)
        self._dropped_count = 0

        # Lock for thread safety
        self._lock = asyncio.Lock()

        # Reconnect task
        self._reconnect_task: Optional[asyncio.Task] = None
        self._shutdown = False

        # Metrics
        self._sent_count = 0
        self._queued_count = 0

    def _get_socket_path(self) -> str:
        """Get socket path with expansion."""
        raw = os.environ.get("VOICE_SOCKET_PATH", "~/.jarvis/voice.sock")
        return os.path.expanduser(os.path.expandvars(raw))

    async def start(self) -> None:
        """Start the reconnect loop."""
        self._shutdown = False
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def stop(self) -> None:
        """Stop client gracefully."""
        self._shutdown = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            if self._writer:
                self._writer.close()
                try:
                    await self._writer.wait_closed()
                except Exception:
                    pass
                self._writer = None
                self._reader = None
            self._connected = False

    async def announce(
        self,
        text: str,
        priority: VoicePriority = VoicePriority.NORMAL,
        category: str = "general",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """
        Announce a message.

        Args:
            text: Message to announce
            priority: Priority level
            category: Category for coalescing (e.g., "init", "health", "error")
            metadata: Optional metadata for logging/debugging

        Returns:
            True if sent immediately, False if queued locally
        """
        # MASTER MUTE — see backend/core/voice_mute. Every speech
        # path gets the same answer; a mute honoured by only
        # some of them is not a mute.
        try:
            from backend.core.voice_mute import voice_muted
            if voice_muted():
                return False
        except Exception:  # noqa: BLE001
            pass
        async with self._lock:
            if self._connected:
                success = await self._send(text, priority, category, metadata)
                if success:
                    self._sent_count += 1
                    return True
                # Send failed - disconnect and queue
                self._connected = False

            self._queue_locally(text, priority, category, metadata)
            return False

    def _queue_locally(
        self,
        text: str,
        priority: VoicePriority,
        category: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue message locally. Drops oldest if full."""
        if len(self._local_queue) == self._local_queue.maxlen:
            self._dropped_count += 1
        self._local_queue.append(
            QueuedMessage(
                text=text,
                priority=priority,
                category=category,
                source=self._source,
                timestamp=time.time(),
                metadata=metadata,
            )
        )
        self._queued_count += 1

    def _format_message(
        self,
        text: str,
        priority: VoicePriority,
        category: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Format message as JSON-line."""
        data = {
            "text": text,
            "priority": priority.value,
            "category": category,
            "source": self._source,
            "timestamp": time.time(),
        }
        if metadata:
            data["metadata"] = metadata
        return json.dumps(data) + "\n"

    async def _send(
        self,
        text: str,
        priority: VoicePriority,
        category: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Send to socket. Returns False on failure."""
        if not self._writer:
            return False

        try:
            line = self._format_message(text, priority, category, metadata)
            self._writer.write(line.encode())
            await self._writer.drain()
            return True
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            logger.debug(f"[VoiceClient:{self._source}] Send failed: {e}")
            return False

    async def _reconnect_loop(self) -> None:
        """Background task: reconnect with exponential backoff."""
        backoff = 1.0
        max_backoff = float(os.environ.get("VOICE_CLIENT_MAX_BACKOFF_S", "30"))

        while not self._shutdown:
            if not self._connected:
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        self._socket_path
                    )
                    async with self._lock:
                        self._connected = True
                    backoff = 1.0
                    logger.info(f"[VoiceClient:{self._source}] Connected to {self._socket_path}")
                    await self._drain_on_reconnect()
                except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                    logger.debug(f"[VoiceClient:{self._source}] Connect failed: {e}")
                    async with self._lock:
                        self._connected = False
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, max_backoff)
            else:
                await asyncio.sleep(1.0)

    async def _drain_on_reconnect(self) -> None:
        """Drain local queue on reconnect. Coalesces if large."""
        queue_size = len(self._local_queue)

        if queue_size == 0:
            return

        if queue_size > 5:
            # Coalesce: send summary instead of flooding
            summary = f"{self._source} reconnected with {queue_size} pending messages"
            if self._dropped_count > 0:
                summary += f" ({self._dropped_count} dropped)"
            await self._send(summary, VoicePriority.LOW, "reconnect")
            self._local_queue.clear()
            self._dropped_count = 0
            logger.info(f"[VoiceClient:{self._source}] Coalesced {queue_size} pending messages")
        else:
            # Drain individually with rate limit
            rate_limit_ms = int(os.environ.get("VOICE_CLIENT_DRAIN_RATE_MS", "100"))
            while self._local_queue:
                msg = self._local_queue.popleft()
                success = await self._send(msg.text, msg.priority, msg.category, msg.metadata)
                if not success:
                    self._local_queue.appendleft(msg)
                    async with self._lock:
                        self._connected = False
                    break
                await asyncio.sleep(rate_limit_ms / 1000)

    @property
    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self._connected

    @property
    def metrics(self) -> Dict[str, Any]:
        """Get client metrics."""
        return {
            "source": self._source,
            "connected": self._connected,
            "sent_count": self._sent_count,
            "queued_count": self._queued_count,
            "dropped_count": self._dropped_count,
            "pending_count": len(self._local_queue),
        }


# =============================================================================
# Convenience Functions (backward compatible with trinity_voice_coordinator)
# =============================================================================

_default_clients: Dict[str, VoiceClient] = {}


async def get_or_create_client(source: str) -> VoiceClient:
    """Get or create a client for the given source."""
    global _default_clients
    if source not in _default_clients:
        client = VoiceClient(source=source)
        await client.start()
        _default_clients[source] = client
    return _default_clients[source]


async def announce(
    message: str,
    context: VoiceContext = VoiceContext.RUNTIME,
    priority: VoicePriority = VoicePriority.NORMAL,
    source: str = "jarvis",
    metadata: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Announce via the default client for the given source.

    This signature is backward-compatible with trinity_voice_coordinator.announce().

    Args:
        message: Text to announce
        context: Semantic context (maps to category)
        priority: Priority level
        source: Source identifier
        metadata: Optional metadata

    Returns:
        True if sent immediately, False if queued locally
    """
    # MASTER MUTE — see backend/core/voice_mute. Every speech
    # path gets the same answer; a mute honoured by only
    # some of them is not a mute.
    try:
        from backend.core.voice_mute import voice_muted
        if voice_muted():
            return False
    except Exception:  # noqa: BLE001
        pass
    client = await get_or_create_client(source)
    return await client.announce(
        text=message,
        priority=priority,
        category=context.category if isinstance(context, VoiceContext) else str(context),
        metadata=metadata,
    )


async def get_voice_coordinator() -> Optional[VoiceClient]:
    """
    Backward-compatible alias. Returns the default JARVIS client.

    Note: In the new architecture, there's no coordinator object on the client side.
    This returns the client for compatibility with code expecting a coordinator.
    """
    return await get_or_create_client("jarvis")


async def shutdown_all_clients() -> None:
    """Shutdown all clients gracefully."""
    global _default_clients
    for client in _default_clients.values():
        await client.stop()
    _default_clients.clear()


# =============================================================================
# Backward compatibility aliases
# =============================================================================

# These allow existing code to use either integer or enum priorities
def _convert_priority(p: Any) -> VoicePriority:
    """Convert various priority formats to VoicePriority."""
    if isinstance(p, VoicePriority):
        return p
    if isinstance(p, int):
        return VoicePriority.from_int(p)
    if isinstance(p, str):
        try:
            return VoicePriority[p.upper()]
        except KeyError:
            return VoicePriority.NORMAL
    return VoicePriority.NORMAL
