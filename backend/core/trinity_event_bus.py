"""
Trinity Event Bus v100.2 - Unified Cross-Repository Event Streaming
====================================================================

Provides RabbitMQ/NATS-style event streaming across Trinity repositories:
- JARVIS (Body) - Main AI agent
- JARVIS Prime (Mind) - Local LLM inference
- Reactor Core (Nerves) - Training pipeline

Features:
- Async pub/sub with topic-based routing
- Priority queues (CRITICAL, HIGH, NORMAL, LOW)
- Persistent event storage with replay capability
- Cross-repo event streaming via WebSocket/HTTP
- Distributed correlation IDs for tracing
- Dead letter queue for failed deliveries
- Backpressure handling
- Event deduplication

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                    TRINITY EVENT BUS                             │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                  │
    │   ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐  │
    │   │   JARVIS    │    │   Prime     │    │   Reactor Core      │  │
    │   │   (Body)    │◄──►│   (Mind)    │◄──►│   (Nerves)          │  │
    │   └──────┬──────┘    └──────┬──────┘    └──────────┬──────────┘  │
    │          │                  │                       │            │
    │          └──────────────────┼───────────────────────┘            │
    │                             │                                    │
    │                    ┌────────▼────────┐                           │
    │                    │  Event Router   │                           │
    │                    │  ┌───────────┐  │                           │
    │                    │  │ Topic Map │  │                           │
    │                    │  └───────────┘  │                           │
    │                    └────────┬────────┘                           │
    │                             │                                    │
    │    ┌────────────────────────┼────────────────────────┐           │
    │    │                        │                        │           │
    │    ▼                        ▼                        ▼           │
    │ ┌──────────┐          ┌──────────┐          ┌──────────────┐     │
    │ │ CRITICAL │          │  NORMAL  │          │     LOW      │     │
    │ │  Queue   │          │  Queue   │          │    Queue     │     │
    │ └──────────┘          └──────────┘          └──────────────┘     │
    │                                                                  │
    │    ┌─────────────────────────────────────────────────────────┐   │
    │    │                 Event Store (WAL)                       │   │
    │    │  • Persistent storage with replay                       │   │ 
    │    │  • Cross-repo sync via file/socket                      │   │
    │    │  • Deduplication via event fingerprints                 │   │
    │    └─────────────────────────────────────────────────────────┘   │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    bus = await TrinityEventBus.create()

    # Subscribe to events
    await bus.subscribe("training.*", handle_training_event)
    await bus.subscribe("model.deployed", handle_deployment)

    # Publish events (cross-repo)
    await bus.publish(TrinityEvent(
        topic="training.started",
        source=RepoType.JARVIS,
        payload={"model": "voice_auth", "dataset_size": 1000}
    ))

    # Request/response pattern
    response = await bus.request("prime.inference", {"prompt": "..."})
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import socket
import struct
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Awaitable, Callable, Deque, Dict, Generic, List,
    Optional, Pattern, Set, Tuple, TypeVar, Union
)
import re

import aiofiles

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)

T = TypeVar("T")


# =============================================================================
# Configuration (100% Environment-Driven)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


class EventBusConfig:
    """Configuration for Trinity Event Bus."""

    # Event store settings
    EVENT_STORE_PATH = _env_str(
        "TRINITY_EVENT_STORE_PATH",
        str(Path.home() / ".jarvis" / "trinity" / "events")
    )
    EVENT_STORE_MAX_SIZE_MB = _env_int("TRINITY_EVENT_STORE_MAX_MB", 100)
    EVENT_RETENTION_HOURS = _env_int("TRINITY_EVENT_RETENTION_HOURS", 168)  # 7 days

    # Queue settings
    QUEUE_MAX_SIZE = _env_int("TRINITY_QUEUE_MAX_SIZE", 10000)
    BATCH_SIZE = _env_int("TRINITY_EVENT_BATCH_SIZE", 100)

    # Network settings
    BUS_PORT = _env_int("TRINITY_BUS_PORT", 9999)
    BROADCAST_PORT = _env_int("TRINITY_BROADCAST_PORT", 9998)
    MULTICAST_GROUP = _env_str("TRINITY_MULTICAST_GROUP", "239.255.255.250")

    # Timing
    PUBLISH_TIMEOUT = _env_float("TRINITY_PUBLISH_TIMEOUT", 5.0)
    DELIVERY_TIMEOUT = _env_float("TRINITY_DELIVERY_TIMEOUT", 30.0)
    HEARTBEAT_INTERVAL = _env_float("TRINITY_HEARTBEAT_INTERVAL", 5.0)
    SYNC_INTERVAL = _env_float("TRINITY_SYNC_INTERVAL", 1.0)

    # Retry settings
    MAX_RETRIES = _env_int("TRINITY_MAX_RETRIES", 3)
    RETRY_BACKOFF_BASE = _env_float("TRINITY_RETRY_BACKOFF", 1.0)

    # Deduplication
    DEDUP_WINDOW_SECONDS = _env_int("TRINITY_DEDUP_WINDOW", 60)

    # Cross-repo paths
    JARVIS_PATH = Path(_env_str(
        "JARVIS_REPO",
        str(Path.home() / "Documents/repos/JARVIS-AI-Agent")
    ))
    PRIME_PATH = Path(_env_str(
        "JARVIS_PRIME_REPO",
        str(Path.home() / "Documents/repos/jarvis-prime")
    ))
    REACTOR_PATH = Path(_env_str(
        "REACTOR_CORE_REPO",
        str(Path.home() / "Documents/repos/reactor-core")
    ))


# =============================================================================
# Enums and Data Types
# =============================================================================

class RepoType(Enum):
    """Trinity repository types."""
    JARVIS = "jarvis"
    PRIME = "prime"
    REACTOR = "reactor"
    BROADCAST = "broadcast"  # All repos


class EventPriority(Enum):
    """Event priority levels."""
    CRITICAL = 0   # Immediate processing (errors, security)
    HIGH = 1       # Fast processing (user requests)
    NORMAL = 2     # Standard processing (most events)
    LOW = 3        # Background processing (analytics, logging)


class EventStatus(Enum):
    """Event delivery status."""
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    EXPIRED = "expired"
    DLQ = "dlq"  # Dead letter queue


class EventType(Enum):
    """Standard event types."""
    # Lifecycle events
    STARTUP = "lifecycle.startup"
    SHUTDOWN = "lifecycle.shutdown"
    HEALTH_CHECK = "lifecycle.health"
    HEARTBEAT = "lifecycle.heartbeat"

    # Training events
    TRAINING_STARTED = "training.started"
    TRAINING_PROGRESS = "training.progress"
    TRAINING_COMPLETED = "training.completed"
    TRAINING_FAILED = "training.failed"

    # Model events
    MODEL_LOADED = "model.loaded"
    MODEL_DEPLOYED = "model.deployed"
    MODEL_HOTSWAP = "model.hotswap"
    MODEL_ROLLBACK = "model.rollback"

    # Inference events
    INFERENCE_REQUEST = "inference.request"
    INFERENCE_RESPONSE = "inference.response"
    INFERENCE_ERROR = "inference.error"

    # Voice events
    VOICE_AUTH_ATTEMPT = "voice.auth_attempt"
    VOICE_AUTH_RESULT = "voice.auth_result"
    VOICE_PROFILE_UPDATE = "voice.profile_update"

    # Learning events
    EXPERIENCE_CAPTURED = "learning.experience"
    PATTERN_DETECTED = "learning.pattern"
    KNOWLEDGE_UPDATED = "learning.knowledge"

    # System events
    ERROR = "system.error"
    WARNING = "system.warning"
    METRICS = "system.metrics"


@dataclass
class TrinityEvent:
    """A cross-repository event."""
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    topic: str = ""
    source: RepoType = RepoType.JARVIS
    target: RepoType = RepoType.BROADCAST
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""
    causation_id: str = ""  # ID of event that caused this one
    timestamp: datetime = field(default_factory=datetime.now)
    expires_at: Optional[datetime] = None
    retry_count: int = 0
    status: EventStatus = EventStatus.PENDING

    def __post_init__(self):
        if not self.correlation_id:
            self.correlation_id = self.event_id
        if self.expires_at is None:
            # Default expiry based on priority
            ttl_map = {
                EventPriority.CRITICAL: timedelta(hours=24),
                EventPriority.HIGH: timedelta(hours=12),
                EventPriority.NORMAL: timedelta(hours=6),
                EventPriority.LOW: timedelta(hours=1),
            }
            self.expires_at = self.timestamp + ttl_map[self.priority]

    @property
    def fingerprint(self) -> str:
        """Unique fingerprint for deduplication."""
        content = f"{self.topic}:{self.source.value}:{json.dumps(self.payload, sort_keys=True)}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    @property
    def is_expired(self) -> bool:
        return datetime.now() > self.expires_at if self.expires_at else False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "topic": self.topic,
            "source": self.source.value,
            "target": self.target.value,
            "priority": self.priority.value,
            "payload": self.payload,
            "metadata": self.metadata,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "timestamp": self.timestamp.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "retry_count": self.retry_count,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrinityEvent":
        return cls(
            event_id=data["event_id"],
            topic=data["topic"],
            source=RepoType(data["source"]),
            target=RepoType(data["target"]),
            priority=EventPriority(data["priority"]),
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {}),
            correlation_id=data.get("correlation_id", ""),
            causation_id=data.get("causation_id", ""),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            retry_count=data.get("retry_count", 0),
            status=EventStatus(data.get("status", "pending")),
        )

    def to_bytes(self) -> bytes:
        """Serialize for network transmission."""
        data = json.dumps(self.to_dict(), default=str).encode()
        return gzip.compress(data)

    @classmethod
    def from_bytes(cls, data: bytes) -> "TrinityEvent":
        """Deserialize from network."""
        json_data = gzip.decompress(data).decode()
        return cls.from_dict(json.loads(json_data))


@dataclass
class Subscription:
    """A topic subscription."""
    subscription_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    pattern: str = ""  # Topic pattern (supports * and # wildcards)
    handler: Callable[[TrinityEvent], Awaitable[None]] = None
    source_filter: Optional[RepoType] = None
    priority_filter: Optional[EventPriority] = None
    created_at: datetime = field(default_factory=datetime.now)
    active: bool = True

    def matches(self, topic: str) -> bool:
        """Check if topic matches pattern."""
        # Convert MQTT-style wildcards to regex
        pattern = self.pattern.replace(".", r"\.")
        pattern = pattern.replace("*", r"[^.]+")
        pattern = pattern.replace("#", r".*")
        pattern = f"^{pattern}$"
        return bool(re.match(pattern, topic))


@dataclass
class BusMetrics:
    """Event bus metrics."""
    events_published: int = 0
    events_delivered: int = 0
    events_failed: int = 0
    events_expired: int = 0
    events_deduplicated: int = 0
    total_latency_ms: float = 0.0
    max_latency_ms: float = 0.0
    active_subscriptions: int = 0
    queue_depths: Dict[str, int] = field(default_factory=dict)
    cross_repo_syncs: int = 0
    dlq_size: int = 0

    def avg_latency_ms(self) -> float:
        if self.events_delivered == 0:
            return 0.0
        return self.total_latency_ms / self.events_delivered


# =============================================================================
# Event Store (Write-Ahead Log)
# =============================================================================

class EventStore:
    """
    Persistent event store with replay capability.

    Uses a write-ahead log (WAL) pattern for durability.
    """

    def __init__(self, path: Optional[str] = None):
        self.path = Path(path or EventBusConfig.EVENT_STORE_PATH)
        self.path.mkdir(parents=True, exist_ok=True)

        self._current_file: Optional[Path] = None
        self._file_handle: Optional[Any] = None
        self._write_lock = asyncio.Lock()
        self._events_in_file = 0
        self._max_events_per_file = EventBusConfig.BATCH_SIZE * 100

    async def append(self, event: TrinityEvent) -> None:
        """Append event to store."""
        async with self._write_lock:
            if self._current_file is None or self._events_in_file >= self._max_events_per_file:
                await self._rotate_file()

            line = json.dumps(event.to_dict(), default=str) + "\n"
            async with aiofiles.open(self._current_file, "a") as f:
                await f.write(line)

            self._events_in_file += 1

    async def _rotate_file(self) -> None:
        """Rotate to a new log file."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._current_file = self.path / f"events_{timestamp}.jsonl"
        self._events_in_file = 0

    async def replay(
        self,
        since: Optional[datetime] = None,
        topic_filter: Optional[str] = None,
        limit: int = 1000,
    ) -> List[TrinityEvent]:
        """Replay events from store."""
        events = []

        # Get all log files sorted by time
        log_files = sorted(self.path.glob("events_*.jsonl"))

        for log_file in log_files:
            if len(events) >= limit:
                break

            try:
                async with aiofiles.open(log_file, "r") as f:
                    async for line in f:
                        if len(events) >= limit:
                            break

                        try:
                            data = json.loads(line.strip())
                            event = TrinityEvent.from_dict(data)

                            # Apply filters
                            if since and event.timestamp < since:
                                continue
                            if topic_filter and not event.topic.startswith(topic_filter):
                                continue

                            events.append(event)
                        except (json.JSONDecodeError, KeyError):
                            continue

            except Exception as e:
                logger.warning(f"[EventStore] Error reading {log_file}: {e}")

        return events

    async def cleanup(self, older_than_hours: Optional[int] = None) -> int:
        """Remove old events."""
        hours = older_than_hours or EventBusConfig.EVENT_RETENTION_HOURS
        cutoff = datetime.now() - timedelta(hours=hours)
        removed = 0

        for log_file in self.path.glob("events_*.jsonl"):
            try:
                # Parse timestamp from filename
                timestamp_str = log_file.stem.replace("events_", "")
                file_time = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")

                if file_time < cutoff:
                    log_file.unlink()
                    removed += 1

            except (ValueError, OSError) as e:
                logger.warning(f"[EventStore] Cleanup error for {log_file}: {e}")

        return removed


# =============================================================================
# Cross-Repo Transport
# =============================================================================

class CrossRepoTransport:
    """
    Handles cross-repository event transport.

    Uses multiple methods:
    1. UDP multicast for local network discovery
    2. File-based sync for shared filesystem
    3. HTTP for remote repos

    v100.2: Added circuit breaker pattern for multicast, graceful degradation,
    and environment-driven configuration.
    """

    def __init__(self, local_repo: RepoType):
        self.local_repo = local_repo
        self._sockets: Dict[str, socket.socket] = {}
        self._running = False
        self._receive_task: Optional[asyncio.Task] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._event_handlers: List[Callable[[TrinityEvent], Awaitable[None]]] = []

        # Sync directory
        self._sync_dir = Path.home() / ".jarvis" / "trinity" / "bus_sync"
        self._sync_dir.mkdir(parents=True, exist_ok=True)

        # Track processed events
        self._processed_events: Set[str] = set()
        self._processed_lock = asyncio.Lock()

        # v100.2: Multicast circuit breaker state
        self._multicast_enabled = os.getenv("TRINITY_MULTICAST_ENABLED", "true").lower() == "true"
        self._multicast_failed = False
        self._multicast_fail_count = 0
        self._multicast_max_failures = int(os.getenv("TRINITY_MULTICAST_MAX_FAILURES", "50"))
        self._multicast_disabled_logged = False

    async def start(self) -> None:
        """Start the transport layer."""
        if self._running:
            return

        self._running = True

        # Start file-based sync (works across all platforms)
        self._sync_task = asyncio.create_task(self._file_sync_loop())

        # v100.2: Only try multicast if enabled via environment
        if self._multicast_enabled:
            try:
                await self._setup_multicast()
                self._receive_task = asyncio.create_task(self._receive_loop())
                logger.info(f"[Transport] Multicast enabled for {self.local_repo.value}")
            except Exception as e:
                logger.info(f"[Transport] Multicast not available (using file sync): {e}")
                self._multicast_failed = True
        else:
            logger.info(f"[Transport] Multicast disabled by config (TRINITY_MULTICAST_ENABLED=false)")

        logger.info(f"[Transport] Started for {self.local_repo.value} (file sync active)")

    async def stop(self) -> None:
        """Stop the transport layer."""
        self._running = False

        if self._receive_task:
            self._receive_task.cancel()
        if self._sync_task:
            self._sync_task.cancel()

        for sock in self._sockets.values():
            sock.close()
        self._sockets.clear()

    async def _setup_multicast(self) -> None:
        """Setup UDP multicast socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass  # Not available on all platforms

        sock.bind(("", EventBusConfig.BROADCAST_PORT))

        # Join multicast group
        mreq = struct.pack(
            "4sl",
            socket.inet_aton(EventBusConfig.MULTICAST_GROUP),
            socket.INADDR_ANY
        )
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.setblocking(False)

        self._sockets["multicast"] = sock

    async def send(self, event: TrinityEvent) -> bool:
        """Send event to other repos."""
        success = False

        # File-based sync (always works)
        try:
            await self._write_to_sync_file(event)
            success = True
        except Exception as e:
            logger.warning(f"[Transport] File sync failed: {e}")

        # UDP multicast
        if "multicast" in self._sockets:
            try:
                data = event.to_bytes()
                sock = self._sockets["multicast"]

                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    lambda: sock.sendto(
                        data,
                        (EventBusConfig.MULTICAST_GROUP, EventBusConfig.BROADCAST_PORT)
                    )
                )
                success = True
            except Exception as e:
                logger.warning(f"[Transport] Multicast send failed: {e}")

        return success

    async def _write_to_sync_file(self, event: TrinityEvent) -> None:
        """Write event to sync file for cross-repo pickup."""
        filename = f"{event.source.value}_events.jsonl"
        filepath = self._sync_dir / filename

        async with aiofiles.open(filepath, "a") as f:
            line = json.dumps(event.to_dict(), default=str) + "\n"
            await f.write(line)

    async def _file_sync_loop(self) -> None:
        """Background loop to sync events from file."""
        while self._running:
            try:
                await self._process_sync_files()
                await asyncio.sleep(EventBusConfig.SYNC_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[Transport] File sync error: {e}")
                await asyncio.sleep(5.0)

    async def _process_sync_files(self) -> None:
        """Process events from other repos' sync files."""
        for repo in RepoType:
            if repo == self.local_repo or repo == RepoType.BROADCAST:
                continue

            filename = f"{repo.value}_events.jsonl"
            filepath = self._sync_dir / filename

            if not filepath.exists():
                continue

            try:
                events_to_process = []

                async with aiofiles.open(filepath, "r") as f:
                    async for line in f:
                        try:
                            data = json.loads(line.strip())
                            event = TrinityEvent.from_dict(data)

                            # Check if already processed
                            async with self._processed_lock:
                                if event.event_id in self._processed_events:
                                    continue
                                self._processed_events.add(event.event_id)

                            events_to_process.append(event)
                        except (json.JSONDecodeError, KeyError):
                            continue

                # Process events
                for event in events_to_process:
                    for handler in self._event_handlers:
                        try:
                            await handler(event)
                        except Exception as e:
                            logger.exception(f"[Transport] Handler error: {e}")

                # Truncate processed file
                if events_to_process:
                    async with aiofiles.open(filepath, "w") as f:
                        await f.write("")

            except Exception as e:
                logger.warning(f"[Transport] Error processing {filepath}: {e}")

        # Cleanup old processed IDs
        async with self._processed_lock:
            if len(self._processed_events) > 10000:
                # Keep only recent half
                self._processed_events = set(list(self._processed_events)[-5000:])

    async def _receive_loop(self) -> None:
        """
        Receive events via UDP multicast.

        v2.8 FIX: Uses asyncio's native sock_recvfrom() for non-blocking sockets
        instead of run_in_executor which causes EAGAIN (errno 35) errors.

        v100.2: Added circuit breaker pattern - if multicast continuously fails,
        gracefully disable it and rely on file-based sync instead.
        """
        sock = self._sockets.get("multicast")
        if not sock:
            return

        loop = asyncio.get_event_loop()
        base_backoff = 0.1
        max_backoff = 5.0

        while self._running:
            # v100.2: Circuit breaker - stop if multicast has failed too many times
            if self._multicast_fail_count >= self._multicast_max_failures:
                if not self._multicast_disabled_logged:
                    logger.info(
                        f"[Transport] Multicast disabled after {self._multicast_fail_count} failures. "
                        f"File-based sync will handle cross-repo events."
                    )
                    self._multicast_disabled_logged = True
                    self._multicast_failed = True
                # Stop the receive loop entirely - file sync is handling events
                break

            try:
                # v2.8: Use asyncio's native non-blocking socket receive
                try:
                    data, addr = await asyncio.wait_for(
                        loop.sock_recvfrom(sock, 65535),
                        timeout=1.0  # 1 second timeout to check _running flag
                    )
                except asyncio.TimeoutError:
                    # No data received within timeout - normal for UDP multicast
                    # Gradually decrease fail count on successful polls (recovery)
                    if self._multicast_fail_count > 0:
                        self._multicast_fail_count = max(0, self._multicast_fail_count - 1)
                    continue

                # Reset error counter on successful receive
                self._multicast_fail_count = 0

                try:
                    event = TrinityEvent.from_bytes(data)

                    # Skip own events
                    if event.source == self.local_repo:
                        continue

                    # Check deduplication
                    async with self._processed_lock:
                        if event.event_id in self._processed_events:
                            continue
                        self._processed_events.add(event.event_id)

                    # Dispatch to handlers
                    for handler in self._event_handlers:
                        try:
                            await handler(event)
                        except Exception as e:
                            logger.debug(f"[Transport] Handler error: {e}")

                except Exception as e:
                    logger.debug(f"[Transport] Failed to parse event: {e}")

            except asyncio.CancelledError:
                break
            except OSError as e:
                # Handle specific socket errors
                import errno
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    # Resource temporarily unavailable - normal for non-blocking
                    await asyncio.sleep(0.01)
                    continue
                elif e.errno == errno.EBADF:
                    # Bad file descriptor - socket was closed
                    logger.debug("[Transport] Multicast socket closed, stopping receive loop")
                    break
                else:
                    # Other socket error - increment circuit breaker counter
                    self._multicast_fail_count += 1
                    # Only log first few errors, then go silent
                    if self._multicast_fail_count <= 3:
                        logger.debug(f"[Transport] Socket error (errno={e.errno}): {e}")
                    backoff = min(base_backoff * (2 ** min(self._multicast_fail_count, 6)), max_backoff)
                    await asyncio.sleep(backoff)
            except Exception as e:
                self._multicast_fail_count += 1
                # Only log first few errors to avoid spam
                if self._multicast_fail_count <= 3:
                    logger.debug(f"[Transport] Receive error: {e}")

                # Exponential backoff with cap
                backoff = min(base_backoff * (2 ** min(self._multicast_fail_count, 6)), max_backoff)
                await asyncio.sleep(backoff)

    def on_event(self, handler: Callable[[TrinityEvent], Awaitable[None]]) -> None:
        """Register event handler."""
        self._event_handlers.append(handler)


# =============================================================================
# Trinity Event Bus
# =============================================================================

class TrinityEventBus:
    """
    Unified event bus for Trinity cross-repo communication.

    Features:
    - Topic-based pub/sub with wildcards
    - Priority queues
    - Event persistence and replay
    - Cross-repo transport
    - Request/response pattern
    - Distributed tracing
    """

    def __init__(self, local_repo: RepoType = RepoType.JARVIS):
        self.local_repo = local_repo
        self._running = False

        # Priority queues
        self._queues: Dict[EventPriority, asyncio.Queue[TrinityEvent]] = {
            priority: asyncio.Queue(maxsize=EventBusConfig.QUEUE_MAX_SIZE)
            for priority in EventPriority
        }

        # Subscriptions
        self._subscriptions: List[Subscription] = []
        self._subscription_lock = asyncio.Lock()

        # Request/response futures
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self._request_lock = asyncio.Lock()

        # Deduplication
        self._recent_fingerprints: Deque[Tuple[str, float]] = deque(maxlen=10000)
        self._dedup_lock = asyncio.Lock()

        # Dead letter queue
        self._dlq: Deque[TrinityEvent] = deque(maxlen=1000)

        # Components
        self._store = EventStore()
        self._transport = CrossRepoTransport(local_repo)

        # Metrics
        self._metrics = BusMetrics()

        # Background tasks
        self._processor_tasks: List[asyncio.Task] = []
        self._cleanup_task: Optional[asyncio.Task] = None

        logger.info(f"[TrinityEventBus] Initialized for {local_repo.value}")

    @classmethod
    async def create(
        cls,
        local_repo: RepoType = RepoType.JARVIS,
    ) -> "TrinityEventBus":
        """Create and start the event bus."""
        bus = cls(local_repo)
        await bus.start()
        return bus

    async def start(self) -> None:
        """Start the event bus."""
        if self._running:
            return

        self._running = True

        # Start transport
        self._transport.on_event(self._handle_remote_event)
        await self._transport.start()

        # Start queue processors
        for priority in EventPriority:
            task = asyncio.create_task(
                self._process_queue(priority),
                name=f"bus_processor_{priority.name}"
            )
            self._processor_tasks.append(task)

        # Start cleanup task
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        logger.info("[TrinityEventBus] Started")

    async def stop(self) -> None:
        """Stop the event bus."""
        if not self._running:
            return

        self._running = False

        # Stop transport
        await self._transport.stop()

        # Cancel tasks
        for task in self._processor_tasks:
            task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()

        # Wait for tasks
        all_tasks = self._processor_tasks + ([self._cleanup_task] if self._cleanup_task else [])
        await asyncio.gather(*all_tasks, return_exceptions=True)

        self._processor_tasks.clear()
        self._cleanup_task = None

        # Cancel pending requests
        async with self._request_lock:
            for future in self._pending_requests.values():
                if not future.done():
                    future.cancel()
            self._pending_requests.clear()

        logger.info("[TrinityEventBus] Stopped")

    async def publish(
        self,
        event: TrinityEvent,
        persist: bool = True,
    ) -> str:
        """
        Publish an event.

        Args:
            event: The event to publish
            persist: Whether to persist to event store

        Returns:
            Event ID
        """
        if not self._running:
            raise RuntimeError("Event bus not running")

        # Set source if not set
        if event.source == RepoType.JARVIS:
            event.source = self.local_repo

        # Deduplication
        async with self._dedup_lock:
            now = time.time()
            fingerprint = event.fingerprint

            # Check recent fingerprints
            for fp, ts in self._recent_fingerprints:
                if fp == fingerprint and (now - ts) < EventBusConfig.DEDUP_WINDOW_SECONDS:
                    self._metrics.events_deduplicated += 1
                    logger.debug(f"[TrinityEventBus] Deduplicated event {event.event_id}")
                    return event.event_id

            self._recent_fingerprints.append((fingerprint, now))

        # Persist
        if persist:
            await self._store.append(event)

        # Add to local queue
        queue = self._queues[event.priority]
        try:
            queue.put_nowait(event)
            self._metrics.events_published += 1
        except asyncio.QueueFull:
            self._dlq.append(event)
            self._metrics.dlq_size = len(self._dlq)
            logger.warning(f"[TrinityEventBus] Queue full, event {event.event_id} sent to DLQ")

        # Send to other repos
        if event.target == RepoType.BROADCAST or event.target != self.local_repo:
            await self._transport.send(event)
            self._metrics.cross_repo_syncs += 1

        logger.debug(f"[TrinityEventBus] Published {event.topic} (id={event.event_id[:8]})")
        return event.event_id

    async def publish_raw(
        self,
        topic: str,
        data: Dict[str, Any],
        priority: EventPriority = EventPriority.NORMAL,
        target: RepoType = RepoType.BROADCAST,
        persist: bool = True,
        correlation_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> str:
        """
        Publish raw dict data as an event (convenience method).

        This method wraps raw dictionary data into a TrinityEvent and publishes it.
        Useful for cross-repo forwarding where the caller has raw data rather than
        a pre-constructed TrinityEvent object.

        Args:
            topic: Event topic (e.g., "reactor.experiences", "training.started")
            data: Raw payload dictionary to include in the event
            priority: Event priority level (default: NORMAL)
            target: Target repository (default: BROADCAST to all)
            persist: Whether to persist to event store (default: True)
            correlation_id: Optional correlation ID for tracing
            causation_id: Optional ID of event that caused this one

        Returns:
            Event ID of the published event
        """
        # Extract event metadata from data if present (for Trinity-formatted events)
        event_id = data.get("event_id", str(uuid.uuid4()))
        source_str = data.get("source", self.local_repo.value)

        # Resolve source repo type
        try:
            source = RepoType(source_str) if isinstance(source_str, str) else source_str
        except ValueError:
            source = self.local_repo

        # Build the event
        event = TrinityEvent(
            event_id=event_id,
            topic=topic,
            source=source,
            target=target,
            priority=priority,
            payload=data.get("payload", data),  # Use payload field if present, else whole data
            metadata=data.get("metadata", {}),
            correlation_id=correlation_id or data.get("correlation_id", ""),
            causation_id=causation_id or data.get("causation_id", ""),
        )

        return await self.publish(event, persist=persist)

    async def subscribe(
        self,
        pattern: str,
        handler: Callable[[TrinityEvent], Awaitable[None]],
        source_filter: Optional[RepoType] = None,
        priority_filter: Optional[EventPriority] = None,
    ) -> str:
        """
        Subscribe to events matching a pattern.

        Args:
            pattern: Topic pattern (supports * and # wildcards)
            handler: Async handler function
            source_filter: Only receive from specific repo
            priority_filter: Only receive specific priority

        Returns:
            Subscription ID
        """
        sub = Subscription(
            pattern=pattern,
            handler=handler,
            source_filter=source_filter,
            priority_filter=priority_filter,
        )

        async with self._subscription_lock:
            self._subscriptions.append(sub)
            self._metrics.active_subscriptions = len(self._subscriptions)

        logger.debug(f"[TrinityEventBus] Subscribed to '{pattern}' (id={sub.subscription_id[:8]})")
        return sub.subscription_id

    async def unsubscribe(self, subscription_id: str) -> bool:
        """Unsubscribe by ID."""
        async with self._subscription_lock:
            for i, sub in enumerate(self._subscriptions):
                if sub.subscription_id == subscription_id:
                    del self._subscriptions[i]
                    self._metrics.active_subscriptions = len(self._subscriptions)
                    return True
        return False

    async def request(
        self,
        topic: str,
        payload: Dict[str, Any],
        target: RepoType = RepoType.BROADCAST,
        timeout: float = 30.0,
    ) -> Dict[str, Any]:
        """
        Request/response pattern.

        Args:
            topic: Request topic
            payload: Request payload
            target: Target repo
            timeout: Response timeout

        Returns:
            Response payload
        """
        # Create request event
        request_id = str(uuid.uuid4())
        event = TrinityEvent(
            topic=f"{topic}.request",
            source=self.local_repo,
            target=target,
            priority=EventPriority.HIGH,
            payload=payload,
            metadata={"request_id": request_id, "reply_to": f"{topic}.response"},
            correlation_id=request_id,
        )

        # Create future for response
        future: asyncio.Future = asyncio.Future()

        async with self._request_lock:
            self._pending_requests[request_id] = future

        try:
            # Publish request
            await self.publish(event)

            # Wait for response
            response = await asyncio.wait_for(future, timeout=timeout)
            return response

        except asyncio.TimeoutError:
            logger.warning(f"[TrinityEventBus] Request {request_id} timed out")
            raise

        finally:
            async with self._request_lock:
                self._pending_requests.pop(request_id, None)

    async def respond(
        self,
        request_event: TrinityEvent,
        response_payload: Dict[str, Any],
    ) -> str:
        """
        Respond to a request event.

        Args:
            request_event: The original request
            response_payload: Response data

        Returns:
            Response event ID
        """
        reply_to = request_event.metadata.get("reply_to", f"{request_event.topic}.response")
        request_id = request_event.metadata.get("request_id", request_event.correlation_id)

        response_event = TrinityEvent(
            topic=reply_to,
            source=self.local_repo,
            target=request_event.source,
            priority=request_event.priority,
            payload=response_payload,
            metadata={"request_id": request_id},
            correlation_id=request_event.correlation_id,
            causation_id=request_event.event_id,
        )

        return await self.publish(response_event)

    async def replay(
        self,
        since: Optional[datetime] = None,
        topic_filter: Optional[str] = None,
        limit: int = 100,
    ) -> List[TrinityEvent]:
        """Replay events from store."""
        return await self._store.replay(since, topic_filter, limit)

    async def _process_queue(self, priority: EventPriority) -> None:
        """Process events from a priority queue."""
        queue = self._queues[priority]

        # Priority-based timeouts
        timeouts = {
            EventPriority.CRITICAL: 0.001,
            EventPriority.HIGH: 0.005,
            EventPriority.NORMAL: 0.01,
            EventPriority.LOW: 0.05,
        }
        timeout = timeouts[priority]

        while self._running:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
                start = time.perf_counter()

                # Skip expired events
                if event.is_expired:
                    self._metrics.events_expired += 1
                    continue

                # Deliver to local subscribers
                await self._deliver_event(event)

                # Track latency
                latency_ms = (time.perf_counter() - start) * 1000
                self._metrics.total_latency_ms += latency_ms
                self._metrics.max_latency_ms = max(self._metrics.max_latency_ms, latency_ms)
                self._metrics.events_delivered += 1

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[TrinityEventBus] Queue processing error: {e}")

    async def _deliver_event(self, event: TrinityEvent) -> None:
        """Deliver event to matching subscribers."""
        async with self._subscription_lock:
            matching = [
                sub for sub in self._subscriptions
                if sub.active and sub.matches(event.topic)
                and (sub.source_filter is None or sub.source_filter == event.source)
                and (sub.priority_filter is None or sub.priority_filter == event.priority)
            ]

        # Check if this is a response to a pending request
        request_id = event.metadata.get("request_id")
        if request_id and ".response" in event.topic:
            async with self._request_lock:
                future = self._pending_requests.get(request_id)
                if future and not future.done():
                    future.set_result(event.payload)
                    return

        # Deliver to subscribers
        if matching:
            tasks = [
                self._execute_handler(sub, event)
                for sub in matching
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _execute_handler(
        self,
        sub: Subscription,
        event: TrinityEvent,
    ) -> None:
        """Execute a subscription handler."""
        try:
            await asyncio.wait_for(
                sub.handler(event),
                timeout=EventBusConfig.DELIVERY_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.warning(
                f"[TrinityEventBus] Handler timeout for {event.topic}"
            )
            self._metrics.events_failed += 1
        except Exception as e:
            logger.exception(f"[TrinityEventBus] Handler error: {e}")
            self._metrics.events_failed += 1

    async def _handle_remote_event(self, event: TrinityEvent) -> None:
        """Handle event received from remote repo."""
        # Add to local queue
        queue = self._queues[event.priority]
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dlq.append(event)
            self._metrics.dlq_size = len(self._dlq)

    async def _cleanup_loop(self) -> None:
        """Background cleanup."""
        while self._running:
            try:
                await asyncio.sleep(3600)  # Every hour

                # Cleanup event store
                removed = await self._store.cleanup()
                if removed > 0:
                    logger.info(f"[TrinityEventBus] Cleaned up {removed} old event files")

                # Update queue depths
                self._metrics.queue_depths = {
                    p.name: q.qsize()
                    for p, q in self._queues.items()
                }

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[TrinityEventBus] Cleanup error: {e}")

    def get_metrics(self) -> BusMetrics:
        """Get current metrics."""
        self._metrics.queue_depths = {
            p.name: q.qsize()
            for p, q in self._queues.items()
        }
        self._metrics.dlq_size = len(self._dlq)
        return self._metrics


# =============================================================================
# Global Instance
# =============================================================================

_bus: Optional[TrinityEventBus] = None
_bus_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_trinity_event_bus(
    local_repo: RepoType = RepoType.JARVIS,
) -> TrinityEventBus:
    """Get or create the global event bus."""
    global _bus

    async with _bus_lock:
        if _bus is None:
            _bus = await TrinityEventBus.create(local_repo)
        return _bus


async def shutdown_trinity_event_bus() -> None:
    """Shutdown the global event bus."""
    global _bus

    async with _bus_lock:
        if _bus:
            await _bus.stop()
            _bus = None


def is_event_bus_running() -> bool:
    """
    Check if the event bus exists and is running WITHOUT creating a new instance.

    This is critical for probes/health checks that need to verify state
    without side effects.

    Returns:
        True if bus exists and is running, False otherwise
    """
    return _bus is not None and getattr(_bus, '_running', False)


def get_event_bus_if_exists() -> Optional[TrinityEventBus]:
    """
    Get the event bus instance if it exists, without creating a new one.

    Returns:
        The existing bus instance or None if not initialized
    """
    return _bus
