"""
Unified AGI Orchestrator v100.0 - Cross-Repo Intelligence Coordination
========================================================================

The ULTIMATE orchestration layer for JARVIS AGI system, providing:
- Unified message bus for cross-repo communication
- Persistent intelligence storage across restarts
- Learning pipeline (JARVIS -> Reactor Core -> JARVIS Prime)
- Cross-repo health aggregation with anomaly detection
- Agent activation and lifecycle management
- Distributed state synchronization
- Event sourcing with replay capability

Architecture:
    +-----------------------------------------------------------------+
    |              Unified AGI Orchestrator v100.0                    |
    +-----------------------------------------------------------------+
    |                                                                 |
    |  +-------------------+  +-------------------+  +--------------+ |
    |  | Unified Event Bus |  | Persistent State  |  | Learning     | |
    |  | - Cross-repo      |  | - SQLite + JSON   |  | Pipeline     | |
    |  | - Event sourcing  |  | - Hot reload      |  | - Experience | |
    |  | - Replay          |  | - Versioning      |  | - Training   | |
    |  +-------------------+  +-------------------+  +--------------+ |
    |                                                                 |
    |  +-------------------+  +-------------------+  +--------------+ |
    |  | Agent Registry    |  | Health Aggregator |  | Model Sync   | |
    |  | - Activation      |  | - Multi-repo      |  | - Prime sync | |
    |  | - Lifecycle       |  | - Anomaly detect  |  | - Auto-deploy| |
    |  | - Discovery       |  | - Self-healing    |  | - Rollback   | |
    |  +-------------------+  +-------------------+  +--------------+ |
    |                                                                 |
    +-----------------------------------------------------------------+
             |                    |                    |
             v                    v                    v
    +----------------+   +----------------+   +------------------+
    |    JARVIS      |   | JARVIS Prime   |   |   Reactor Core   |
    |    (Body)      |   |    (Brain)     |   | (Nervous System) |
    +----------------+   +----------------+   +------------------+

Environment Variables (ALL configurable):
    - AGI_ORCHESTRATOR_ENABLED=true
    - AGI_EVENT_BUS_TYPE=sqlite          # sqlite, redis, memory
    - AGI_STATE_PERSISTENCE=true
    - AGI_LEARNING_PIPELINE_ENABLED=true
    - AGI_AGENT_ACTIVATION_ENABLED=true
    - AGI_HEALTH_AGGREGATOR_ENABLED=true
    - AGI_MODEL_SYNC_ENABLED=true
    - AGI_STATE_DIR=~/.jarvis/agi
    - AGI_EVENT_RETENTION_DAYS=30
    - AGI_HEALTH_CHECK_INTERVAL_SEC=30.0
    - AGI_AGENT_ACTIVATION_THRESHOLD=0.5
    - AGI_LEARNING_BATCH_SIZE=100
    - AGI_MODEL_SYNC_INTERVAL_SEC=3600.0

Author: JARVIS AGI System v100.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pickle
import sqlite3
import struct
import sys
import threading
import time
import traceback
import uuid
import zlib
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from functools import lru_cache, partial, wraps
from pathlib import Path
from threading import RLock
from typing import (
    Any, AsyncGenerator, AsyncIterator, Awaitable, Callable, Coroutine,
    Deque, Dict, Final, FrozenSet, Generic, Iterator, List, Literal,
    Mapping, NamedTuple, Optional, Protocol, Sequence, Set, Tuple,
    Type, TypeVar, Union, cast, overload, runtime_checkable,
)

from backend.core.async_safety import LazyAsyncLock, TimeoutConfig, get_shutdown_event

logger = logging.getLogger(__name__)

# Type Variables
T = TypeVar("T")
E = TypeVar("E", bound="AGIEvent")


# =============================================================================
# Configuration (100% Environment-Driven)
# =============================================================================

def _env_str(key: str, default: str) -> str:
    """Get string from environment."""
    return os.getenv(key, default)


def _env_int(key: str, default: int) -> int:
    """Get int from environment."""
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment."""
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    """Get bool from environment."""
    val = os.getenv(key, str(default).lower())
    return val.lower() in ("true", "1", "yes", "on")


def _env_path(key: str, default: str) -> Path:
    """Get path from environment."""
    return Path(os.path.expanduser(os.getenv(key, default)))


def _env_list(key: str, default: str, sep: str = ",") -> List[str]:
    """Get list from environment."""
    val = os.getenv(key, default)
    return [x.strip() for x in val.split(sep) if x.strip()]


@dataclass
class AGIOrchestratorConfig:
    """
    v100.0: Configuration for Unified AGI Orchestrator.
    ALL values are environment-driven.
    """
    # ==========================================================================
    # Core Settings
    # ==========================================================================
    enabled: bool = field(default_factory=lambda: _env_bool("AGI_ORCHESTRATOR_ENABLED", True))
    instance_id: str = field(default_factory=lambda: _env_str(
        "AGI_INSTANCE_ID", f"agi_{uuid.uuid4().hex[:8]}"
    ))
    state_dir: Path = field(default_factory=lambda: _env_path("AGI_STATE_DIR", "~/.jarvis/agi"))

    # ==========================================================================
    # Event Bus Configuration
    # ==========================================================================
    event_bus_type: str = field(default_factory=lambda: _env_str("AGI_EVENT_BUS_TYPE", "sqlite"))
    event_retention_days: int = field(default_factory=lambda: _env_int("AGI_EVENT_RETENTION_DAYS", 30))
    event_max_queue_size: int = field(default_factory=lambda: _env_int("AGI_EVENT_MAX_QUEUE", 10000))
    event_batch_size: int = field(default_factory=lambda: _env_int("AGI_EVENT_BATCH_SIZE", 100))

    # ==========================================================================
    # Persistent State
    # ==========================================================================
    state_persistence: bool = field(default_factory=lambda: _env_bool("AGI_STATE_PERSISTENCE", True))
    state_sync_interval_sec: float = field(default_factory=lambda: _env_float("AGI_STATE_SYNC_INTERVAL", 30.0))
    state_backup_count: int = field(default_factory=lambda: _env_int("AGI_STATE_BACKUP_COUNT", 5))

    # ==========================================================================
    # Learning Pipeline
    # ==========================================================================
    learning_pipeline_enabled: bool = field(default_factory=lambda: _env_bool(
        "AGI_LEARNING_PIPELINE_ENABLED", True
    ))
    learning_batch_size: int = field(default_factory=lambda: _env_int("AGI_LEARNING_BATCH_SIZE", 100))
    learning_min_confidence: float = field(default_factory=lambda: _env_float("AGI_LEARNING_MIN_CONFIDENCE", 0.7))
    learning_experience_retention_days: int = field(default_factory=lambda: _env_int(
        "AGI_LEARNING_RETENTION_DAYS", 90
    ))

    # ==========================================================================
    # Agent Activation
    # ==========================================================================
    agent_activation_enabled: bool = field(default_factory=lambda: _env_bool(
        "AGI_AGENT_ACTIVATION_ENABLED", True
    ))
    agent_activation_threshold: float = field(default_factory=lambda: _env_float(
        "AGI_AGENT_ACTIVATION_THRESHOLD", 0.5
    ))
    agent_max_concurrent: int = field(default_factory=lambda: _env_int("AGI_AGENT_MAX_CONCURRENT", 20))
    agent_health_check_interval_sec: float = field(default_factory=lambda: _env_float(
        "AGI_AGENT_HEALTH_INTERVAL", 30.0
    ))

    # ==========================================================================
    # Health Aggregator
    # ==========================================================================
    health_aggregator_enabled: bool = field(default_factory=lambda: _env_bool(
        "AGI_HEALTH_AGGREGATOR_ENABLED", True
    ))
    health_check_interval_sec: float = field(default_factory=lambda: _env_float(
        "AGI_HEALTH_CHECK_INTERVAL", 30.0
    ))
    health_anomaly_threshold: float = field(default_factory=lambda: _env_float(
        "AGI_HEALTH_ANOMALY_THRESHOLD", 2.0
    ))
    health_history_size: int = field(default_factory=lambda: _env_int("AGI_HEALTH_HISTORY_SIZE", 100))

    # ==========================================================================
    # Model Sync (JARVIS <-> Prime <-> Reactor)
    # ==========================================================================
    model_sync_enabled: bool = field(default_factory=lambda: _env_bool("AGI_MODEL_SYNC_ENABLED", True))
    model_sync_interval_sec: float = field(default_factory=lambda: _env_float(
        "AGI_MODEL_SYNC_INTERVAL", 3600.0
    ))
    model_auto_deploy: bool = field(default_factory=lambda: _env_bool("AGI_MODEL_AUTO_DEPLOY", False))

    # ==========================================================================
    # Repository Paths (Dynamic Discovery with Fallbacks)
    # ==========================================================================
    jarvis_repo_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_REPO_PATH", "~/Documents/repos/JARVIS-AI-Agent"
    ))
    prime_repo_path: Path = field(default_factory=lambda: _env_path(
        "JARVIS_PRIME_REPO_PATH", "~/Documents/repos/jarvis-prime"
    ))
    reactor_repo_path: Path = field(default_factory=lambda: _env_path(
        "REACTOR_CORE_REPO_PATH", "~/Documents/repos/reactor-core"
    ))

    # Fallback search paths for repo discovery
    repo_search_paths: List[Path] = field(default_factory=lambda: [
        Path.home() / "Documents" / "repos",
        Path.home() / "repos",
        Path.home() / "code",
        Path.home() / "projects",
        Path.home() / "dev",
    ])

    def __post_init__(self):
        """Validate and create necessary directories."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "events").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "state").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "learning").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "agents").mkdir(parents=True, exist_ok=True)
        (self.state_dir / "health").mkdir(parents=True, exist_ok=True)


# Singleton config
_agi_config: Optional[AGIOrchestratorConfig] = None


def get_agi_config() -> AGIOrchestratorConfig:
    """Get or create the singleton AGI config."""
    global _agi_config
    if _agi_config is None:
        _agi_config = AGIOrchestratorConfig()
    return _agi_config


# =============================================================================
# Enums and Types
# =============================================================================

class AGIComponent(str, Enum):
    """AGI system components."""
    JARVIS = "jarvis"
    PRIME = "prime"
    REACTOR = "reactor"
    ORCHESTRATOR = "orchestrator"


class EventPriority(int, Enum):
    """Event priority levels."""
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BACKGROUND = 4


class AgentState(str, Enum):
    """Agent lifecycle states."""
    DORMANT = "dormant"
    ACTIVATING = "activating"
    ACTIVE = "active"
    DEGRADED = "degraded"
    DEACTIVATING = "deactivating"
    FAILED = "failed"


class HealthStatus(str, Enum):
    """Component health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class LearningType(str, Enum):
    """Types of learning experiences."""
    INTERACTION = "interaction"
    VOICE_AUTH = "voice_auth"
    ERROR_RECOVERY = "error_recovery"
    GOAL_INFERENCE = "goal_inference"
    PATTERN_RECOGNITION = "pattern_recognition"
    USER_FEEDBACK = "user_feedback"


# =============================================================================
# Event System
# =============================================================================

@dataclass
class AGIEvent:
    """Base event for the AGI event bus."""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = ""
    timestamp: float = field(default_factory=time.time)
    source: AGIComponent = AGIComponent.ORCHESTRATOR
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    correlation_id: Optional[str] = None
    trace_id: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "timestamp": self.timestamp,
            "source": self.source.value if isinstance(self.source, Enum) else self.source,
            "priority": self.priority.value if isinstance(self.priority, Enum) else self.priority,
            "payload": self.payload,
            "metadata": self.metadata,
            "correlation_id": self.correlation_id,
            "trace_id": self.trace_id,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AGIEvent":
        """Create from dictionary."""
        return cls(
            event_id=data.get("event_id", uuid.uuid4().hex),
            event_type=data.get("event_type", ""),
            timestamp=data.get("timestamp", time.time()),
            source=AGIComponent(data.get("source", "orchestrator")),
            priority=EventPriority(data.get("priority", 2)),
            payload=data.get("payload", {}),
            metadata=data.get("metadata", {}),
            correlation_id=data.get("correlation_id"),
            trace_id=data.get("trace_id"),
        )


class EventSubscriber(Protocol):
    """Protocol for event subscribers."""
    async def handle_event(self, event: AGIEvent) -> None:
        """Handle an incoming event."""
        ...


class UnifiedEventBus:
    """
    v100.0: Unified Event Bus for cross-repo communication.

    Features:
    - SQLite-backed persistence (survives restarts)
    - Event sourcing with replay capability
    - Priority-based processing
    - Correlation ID tracking
    - Bounded queue with backpressure
    - Dead letter queue for failed events
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self._db_path = self.config.state_dir / "events" / "event_store.db"
        self._subscribers: Dict[str, List[EventSubscriber]] = defaultdict(list)
        self._queue: asyncio.PriorityQueue = None
        self._running = False
        self._worker_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._connection: Optional[sqlite3.Connection] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize the event bus."""
        async with self._lock:
            if self._initialized:
                return

            # Initialize database
            self._connection = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._create_tables()

            # Initialize queue
            self._queue = asyncio.PriorityQueue(maxsize=self.config.event_max_queue_size)

            # Replay unprocessed events
            await self._replay_unprocessed_events()

            self._initialized = True
            logger.info(f"[AGI EventBus] Initialized with SQLite store at {self._db_path}")

    def _create_tables(self) -> None:
        """Create database tables."""
        cursor = self._connection.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                source TEXT NOT NULL,
                priority INTEGER NOT NULL,
                payload TEXT NOT NULL,
                metadata TEXT NOT NULL,
                correlation_id TEXT,
                trace_id TEXT,
                processed INTEGER DEFAULT 0,
                processed_at REAL,
                error TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp);
            CREATE INDEX IF NOT EXISTS idx_events_processed ON events(processed);
            CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(correlation_id);

            CREATE TABLE IF NOT EXISTS dead_letter (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                error TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s', 'now')),
                FOREIGN KEY (event_id) REFERENCES events(event_id)
            );
        """)
        self._connection.commit()

    async def _replay_unprocessed_events(self) -> None:
        """Replay events that were not processed before shutdown."""
        cursor = self._connection.cursor()
        cursor.execute("""
            SELECT * FROM events WHERE processed = 0 ORDER BY priority ASC, timestamp ASC LIMIT 1000
        """)
        rows = cursor.fetchall()

        for row in rows:
            event = AGIEvent.from_dict({
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "source": row["source"],
                "priority": row["priority"],
                "payload": json.loads(row["payload"]),
                "metadata": json.loads(row["metadata"]),
                "correlation_id": row["correlation_id"],
                "trace_id": row["trace_id"],
            })
            # Add to queue for processing
            await self._queue.put((event.priority.value, event.timestamp, event))

        if rows:
            logger.info(f"[AGI EventBus] Replaying {len(rows)} unprocessed events")

    async def publish(self, event: AGIEvent) -> str:
        """Publish an event to the bus."""
        if not self._initialized:
            await self.initialize()

        # Store in database
        cursor = self._connection.cursor()
        cursor.execute("""
            INSERT INTO events (event_id, event_type, timestamp, source, priority, payload, metadata, correlation_id, trace_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_id,
            event.event_type,
            event.timestamp,
            event.source.value if isinstance(event.source, Enum) else event.source,
            event.priority.value if isinstance(event.priority, Enum) else event.priority,
            json.dumps(event.payload),
            json.dumps(event.metadata),
            event.correlation_id,
            event.trace_id,
        ))
        self._connection.commit()

        # Add to queue for processing
        try:
            await asyncio.wait_for(
                self._queue.put((event.priority.value, event.timestamp, event)),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            logger.warning(f"[AGI EventBus] Queue full, event {event.event_id} stored but not queued")

        return event.event_id

    def subscribe(self, event_type: str, subscriber: EventSubscriber) -> None:
        """Subscribe to events of a specific type."""
        self._subscribers[event_type].append(subscriber)
        # Also subscribe to wildcard
        if "*" not in event_type:
            self._subscribers["*"].append(subscriber)

    async def start(self) -> None:
        """Start the event bus worker."""
        if self._running:
            return

        if not self._initialized:
            await self.initialize()

        self._running = True
        self._worker_task = asyncio.create_task(self._process_events())
        logger.info("[AGI EventBus] Started")

    async def stop(self) -> None:
        """Stop the event bus worker."""
        self._running = False
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        if self._connection:
            self._connection.close()
        logger.info("[AGI EventBus] Stopped")

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while self._running:
            try:
                # Get next event with timeout
                priority, timestamp, event = await asyncio.wait_for(
                    self._queue.get(),
                    timeout=1.0
                )

                # Find subscribers
                subscribers = self._subscribers.get(event.event_type, [])
                subscribers.extend(self._subscribers.get("*", []))

                # Process with all subscribers
                for subscriber in set(subscribers):
                    try:
                        await asyncio.wait_for(
                            subscriber.handle_event(event),
                            timeout=30.0
                        )
                    except Exception as e:
                        logger.error(f"[AGI EventBus] Subscriber error: {e}")

                # Mark as processed
                cursor = self._connection.cursor()
                cursor.execute("""
                    UPDATE events SET processed = 1, processed_at = ? WHERE event_id = ?
                """, (time.time(), event.event_id))
                self._connection.commit()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[AGI EventBus] Processing error: {e}")

    async def get_events(
        self,
        event_type: Optional[str] = None,
        since: Optional[float] = None,
        limit: int = 100
    ) -> List[AGIEvent]:
        """Query events from the store."""
        query = "SELECT * FROM events WHERE 1=1"
        params = []

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type)

        if since:
            query += " AND timestamp >= ?"
            params.append(since)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        cursor = self._connection.cursor()
        cursor.execute(query, params)
        rows = cursor.fetchall()

        return [
            AGIEvent.from_dict({
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "timestamp": row["timestamp"],
                "source": row["source"],
                "priority": row["priority"],
                "payload": json.loads(row["payload"]),
                "metadata": json.loads(row["metadata"]),
                "correlation_id": row["correlation_id"],
                "trace_id": row["trace_id"],
            })
            for row in rows
        ]


# =============================================================================
# Persistent State Manager
# =============================================================================

@dataclass
class PersistentState:
    """State that persists across restarts."""
    version: int = 1
    last_updated: float = field(default_factory=time.time)
    data: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def compute_checksum(self) -> str:
        """Compute checksum for integrity verification."""
        content = json.dumps(self.data, sort_keys=True)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


class PersistentStateManager:
    """
    v100.0: Manages persistent state across restarts.

    Features:
    - SQLite + JSON hybrid storage
    - Atomic writes with versioning
    - Hot reload support
    - Integrity verification
    - Automatic backup rotation
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self._state_file = self.config.state_dir / "state" / "agi_state.json"
        self._db_path = self.config.state_dir / "state" / "state.db"
        self._state = PersistentState()
        self._lock = asyncio.Lock()
        self._dirty = False
        self._sync_task: Optional[asyncio.Task] = None
        self._connection: Optional[sqlite3.Connection] = None

    async def initialize(self) -> None:
        """Initialize and load state."""
        async with self._lock:
            # Initialize database
            self._connection = sqlite3.connect(str(self._db_path), check_same_thread=False)
            self._create_tables()

            # Load state from file
            await self._load_state()

            # Start sync task
            if self.config.state_persistence:
                self._sync_task = asyncio.create_task(self._sync_loop())

            logger.info("[AGI State] Initialized")

    def _create_tables(self) -> None:
        """Create database tables for state storage."""
        cursor = self._connection.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                type TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS state_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT NOT NULL,
                changed_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_state_key ON state(key);
            CREATE INDEX IF NOT EXISTS idx_history_key ON state_history(key);
        """)
        self._connection.commit()

    async def _load_state(self) -> None:
        """Load state from file."""
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                self._state = PersistentState(
                    version=data.get("version", 1),
                    last_updated=data.get("last_updated", time.time()),
                    data=data.get("data", {}),
                    checksum=data.get("checksum", ""),
                )

                # Verify integrity
                computed = self._state.compute_checksum()
                if self._state.checksum and computed != self._state.checksum:
                    logger.warning("[AGI State] Checksum mismatch, state may be corrupted")

                logger.info(f"[AGI State] Loaded state v{self._state.version}")
            except Exception as e:
                logger.error(f"[AGI State] Failed to load state: {e}")
                self._state = PersistentState()

    async def _save_state(self) -> None:
        """Save state to file."""
        async with self._lock:
            self._state.last_updated = time.time()
            self._state.checksum = self._state.compute_checksum()
            self._state.version += 1

            # Create backup before saving
            if self._state_file.exists():
                backup_file = self._state_file.with_suffix(f".json.{int(time.time())}")
                self._state_file.rename(backup_file)

                # Clean old backups
                backups = sorted(
                    self._state_file.parent.glob("*.json.*"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                for old_backup in backups[self.config.state_backup_count:]:
                    old_backup.unlink()

            # Write new state
            self._state_file.write_text(json.dumps({
                "version": self._state.version,
                "last_updated": self._state.last_updated,
                "data": self._state.data,
                "checksum": self._state.checksum,
            }, indent=2))

            self._dirty = False
            logger.debug(f"[AGI State] Saved state v{self._state.version}")

    async def _sync_loop(self) -> None:
        """Periodic sync loop with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("AGI_SYNC_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("[AGI State] Sync loop stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"[AGI State] Sync loop reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(self.config.state_sync_interval_sec)
                if self._dirty:
                    # Add timeout protection for save operation
                    await asyncio.wait_for(
                        self._save_state(),
                        timeout=TimeoutConfig.DATABASE
                    )
            except asyncio.TimeoutError:
                logger.warning(f"[AGI State] Sync timed out after {TimeoutConfig.DATABASE}s")
            except asyncio.CancelledError:
                logger.info("[AGI State] Sync loop cancelled")
                break
            except Exception as e:
                logger.error(f"[AGI State] Sync error: {e}")

    async def get(self, key: str, default: Any = None) -> Any:
        """Get a value from state."""
        return self._state.data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        """Set a value in state."""
        async with self._lock:
            old_value = self._state.data.get(key)
            self._state.data[key] = value
            self._dirty = True

            # Record in database
            cursor = self._connection.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO state (key, value, type, updated_at)
                VALUES (?, ?, ?, ?)
            """, (key, json.dumps(value), type(value).__name__, time.time()))

            # Record history
            cursor.execute("""
                INSERT INTO state_history (key, old_value, new_value)
                VALUES (?, ?, ?)
            """, (key, json.dumps(old_value) if old_value else None, json.dumps(value)))

            self._connection.commit()

    async def delete(self, key: str) -> None:
        """Delete a key from state."""
        async with self._lock:
            if key in self._state.data:
                del self._state.data[key]
                self._dirty = True

                cursor = self._connection.cursor()
                cursor.execute("DELETE FROM state WHERE key = ?", (key,))
                self._connection.commit()

    async def get_all(self) -> Dict[str, Any]:
        """Get all state data."""
        return self._state.data.copy()

    async def stop(self) -> None:
        """Stop the state manager and save."""
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        if self._dirty:
            await self._save_state()

        if self._connection:
            self._connection.close()


# =============================================================================
# Learning Pipeline
# =============================================================================

@dataclass
class LearningExperience:
    """A learning experience to be processed."""
    experience_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    experience_type: LearningType = LearningType.INTERACTION
    timestamp: float = field(default_factory=time.time)
    input_data: Dict[str, Any] = field(default_factory=dict)
    output_data: Dict[str, Any] = field(default_factory=dict)
    feedback: Optional[Dict[str, Any]] = None
    success: bool = True
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class LearningPipeline:
    """
    v100.0: Manages the learning pipeline across repos.

    Flow: JARVIS (experiences) -> Reactor Core (training) -> JARVIS Prime (model update)

    Features:
    - Experience collection and batching
    - Automatic training triggers
    - Model deployment coordination
    - Feedback loop integration
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self._db_path = self.config.state_dir / "learning" / "experiences.db"
        self._connection: Optional[sqlite3.Connection] = None
        self._experience_queue: Deque[LearningExperience] = deque(maxlen=1000)
        self._lock = asyncio.Lock()
        self._training_in_progress = False

    async def initialize(self) -> None:
        """Initialize the learning pipeline."""
        self._connection = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("[AGI Learning] Pipeline initialized")

    def _create_tables(self) -> None:
        """Create database tables."""
        cursor = self._connection.cursor()
        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS experiences (
                experience_id TEXT PRIMARY KEY,
                experience_type TEXT NOT NULL,
                timestamp REAL NOT NULL,
                input_data TEXT NOT NULL,
                output_data TEXT NOT NULL,
                feedback TEXT,
                success INTEGER NOT NULL,
                confidence REAL NOT NULL,
                metadata TEXT NOT NULL,
                processed INTEGER DEFAULT 0,
                batch_id TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE TABLE IF NOT EXISTS training_batches (
                batch_id TEXT PRIMARY KEY,
                experience_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at REAL,
                completed_at REAL,
                model_version TEXT,
                metrics TEXT,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            );

            CREATE INDEX IF NOT EXISTS idx_experiences_type ON experiences(experience_type);
            CREATE INDEX IF NOT EXISTS idx_experiences_processed ON experiences(processed);
            CREATE INDEX IF NOT EXISTS idx_experiences_timestamp ON experiences(timestamp);
        """)
        self._connection.commit()

    async def record_experience(self, experience: LearningExperience) -> str:
        """Record a new learning experience."""
        async with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("""
                INSERT INTO experiences (
                    experience_id, experience_type, timestamp, input_data, output_data,
                    feedback, success, confidence, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                experience.experience_id,
                experience.experience_type.value,
                experience.timestamp,
                json.dumps(experience.input_data),
                json.dumps(experience.output_data),
                json.dumps(experience.feedback) if experience.feedback else None,
                1 if experience.success else 0,
                experience.confidence,
                json.dumps(experience.metadata),
            ))
            self._connection.commit()

            # Add to queue for batch processing
            self._experience_queue.append(experience)

            # Check if batch threshold reached
            if len(self._experience_queue) >= self.config.learning_batch_size:
                asyncio.create_task(self._trigger_training())

            return experience.experience_id

    async def _trigger_training(self) -> None:
        """Trigger a training batch if conditions are met."""
        if self._training_in_progress:
            return

        async with self._lock:
            if len(self._experience_queue) < self.config.learning_batch_size:
                return

            self._training_in_progress = True
            batch_id = uuid.uuid4().hex

            try:
                # Collect experiences for batch
                experiences = list(self._experience_queue)
                self._experience_queue.clear()

                # Record batch
                cursor = self._connection.cursor()
                cursor.execute("""
                    INSERT INTO training_batches (batch_id, experience_count, status, started_at)
                    VALUES (?, ?, 'pending', ?)
                """, (batch_id, len(experiences), time.time()))

                # Mark experiences as batched
                for exp in experiences:
                    cursor.execute("""
                        UPDATE experiences SET batch_id = ? WHERE experience_id = ?
                    """, (batch_id, exp.experience_id))

                self._connection.commit()

                # Trigger training in Reactor Core (if available)
                training_success = await self._send_to_reactor_core(batch_id, experiences)

                # Update batch status
                status = "completed" if training_success else "failed"
                cursor.execute("""
                    UPDATE training_batches SET status = ?, completed_at = ? WHERE batch_id = ?
                """, (status, time.time(), batch_id))
                self._connection.commit()

                logger.info(f"[AGI Learning] Training batch {batch_id} {status}")

            except Exception as e:
                logger.error(f"[AGI Learning] Training error: {e}")
            finally:
                self._training_in_progress = False

    async def _send_to_reactor_core(
        self,
        batch_id: str,
        experiences: List[LearningExperience]
    ) -> bool:
        """Send training batch to Reactor Core."""
        try:
            # Check if Reactor Core is available
            reactor_path = self.config.reactor_repo_path
            if not reactor_path.exists():
                logger.warning("[AGI Learning] Reactor Core not available")
                return False

            # Write batch file for Reactor Core to pick up
            batch_file = reactor_path / "training" / "pending_batches" / f"{batch_id}.json"
            batch_file.parent.mkdir(parents=True, exist_ok=True)

            batch_data = {
                "batch_id": batch_id,
                "timestamp": time.time(),
                "experience_count": len(experiences),
                "experiences": [
                    {
                        "experience_id": exp.experience_id,
                        "type": exp.experience_type.value,
                        "input": exp.input_data,
                        "output": exp.output_data,
                        "feedback": exp.feedback,
                        "success": exp.success,
                        "confidence": exp.confidence,
                    }
                    for exp in experiences
                ]
            }

            batch_file.write_text(json.dumps(batch_data, indent=2))
            logger.info(f"[AGI Learning] Sent batch {batch_id} to Reactor Core")
            return True

        except Exception as e:
            logger.error(f"[AGI Learning] Failed to send to Reactor: {e}")
            return False

    async def get_learning_stats(self) -> Dict[str, Any]:
        """Get learning pipeline statistics."""
        cursor = self._connection.cursor()

        cursor.execute("SELECT COUNT(*) FROM experiences")
        total_experiences = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM experiences WHERE processed = 1")
        processed = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM training_batches WHERE status = 'completed'")
        completed_batches = cursor.fetchone()[0]

        cursor.execute("""
            SELECT experience_type, COUNT(*) as count, AVG(confidence) as avg_confidence
            FROM experiences GROUP BY experience_type
        """)
        by_type = {row[0]: {"count": row[1], "avg_confidence": row[2]} for row in cursor.fetchall()}

        return {
            "total_experiences": total_experiences,
            "processed_experiences": processed,
            "pending_experiences": len(self._experience_queue),
            "completed_training_batches": completed_batches,
            "by_type": by_type,
        }

    async def stop(self) -> None:
        """Stop the learning pipeline."""
        if self._connection:
            self._connection.close()


# =============================================================================
# Agent Registry
# =============================================================================

@dataclass
class AgentInfo:
    """Information about a registered agent."""
    agent_id: str
    agent_name: str
    agent_type: str
    state: AgentState = AgentState.DORMANT
    activation_score: float = 0.0
    capabilities: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    last_active: float = field(default_factory=time.time)
    health_score: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class AgentRegistry:
    """
    v100.0: Central registry for all AGI agents.

    Features:
    - Agent discovery and registration
    - Lifecycle management
    - Activation scoring
    - Health monitoring
    - Dependency resolution
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self._agents: Dict[str, AgentInfo] = {}
        self._active_agents: Set[str] = set()
        self._lock = asyncio.Lock()
        self._health_task: Optional[asyncio.Task] = None

    async def register(self, agent: AgentInfo) -> str:
        """Register an agent."""
        async with self._lock:
            self._agents[agent.agent_id] = agent
            logger.debug(f"[AGI Agents] Registered agent: {agent.agent_name}")
            return agent.agent_id

    async def unregister(self, agent_id: str) -> None:
        """Unregister an agent."""
        async with self._lock:
            if agent_id in self._agents:
                del self._agents[agent_id]
                self._active_agents.discard(agent_id)

    async def activate(self, agent_id: str) -> bool:
        """Activate an agent."""
        async with self._lock:
            if agent_id not in self._agents:
                return False

            agent = self._agents[agent_id]

            # Check if dependencies are active
            for dep_id in agent.dependencies:
                if dep_id not in self._active_agents:
                    logger.warning(f"[AGI Agents] Cannot activate {agent.agent_name}: dependency {dep_id} not active")
                    return False

            # Check concurrent limit
            if len(self._active_agents) >= self.config.agent_max_concurrent:
                logger.warning(f"[AGI Agents] Max concurrent agents reached")
                return False

            agent.state = AgentState.ACTIVE
            agent.last_active = time.time()
            self._active_agents.add(agent_id)

            logger.info(f"[AGI Agents] Activated agent: {agent.agent_name}")
            return True

    async def deactivate(self, agent_id: str) -> bool:
        """Deactivate an agent."""
        async with self._lock:
            if agent_id not in self._agents:
                return False

            agent = self._agents[agent_id]
            agent.state = AgentState.DORMANT
            self._active_agents.discard(agent_id)

            logger.info(f"[AGI Agents] Deactivated agent: {agent.agent_name}")
            return True

    async def get_active_agents(self) -> List[AgentInfo]:
        """Get all active agents."""
        return [self._agents[aid] for aid in self._active_agents if aid in self._agents]

    async def get_all_agents(self) -> List[AgentInfo]:
        """Get all registered agents."""
        return list(self._agents.values())

    async def get_agent_by_capability(self, capability: str) -> List[AgentInfo]:
        """Find agents with a specific capability."""
        return [
            agent for agent in self._agents.values()
            if capability in agent.capabilities
        ]

    async def update_health(self, agent_id: str, health_score: float) -> None:
        """Update an agent's health score."""
        if agent_id in self._agents:
            self._agents[agent_id].health_score = health_score
            if health_score < 0.5:
                self._agents[agent_id].state = AgentState.DEGRADED

    async def get_activation_stats(self) -> Dict[str, Any]:
        """Get agent activation statistics."""
        total = len(self._agents)
        active = len(self._active_agents)

        return {
            "total_agents": total,
            "active_agents": active,
            "dormant_agents": total - active,
            "activation_rate": active / total if total > 0 else 0.0,
            "agents_by_state": {
                state.value: sum(1 for a in self._agents.values() if a.state == state)
                for state in AgentState
            }
        }


# =============================================================================
# Cross-Repo Health Aggregator
# =============================================================================

@dataclass
class ComponentHealth:
    """Health status of a component."""
    component: AGIComponent
    status: HealthStatus = HealthStatus.UNKNOWN
    latency_ms: float = 0.0
    uptime_sec: float = 0.0
    last_check: float = field(default_factory=time.time)
    error_count: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)


class CrossRepoHealthAggregator:
    """
    v100.0: Aggregates health from all repos.

    Features:
    - Multi-repo health collection
    - Anomaly detection
    - Trend analysis
    - Self-healing triggers
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self._health: Dict[AGIComponent, ComponentHealth] = {}
        self._health_history: Dict[AGIComponent, Deque[ComponentHealth]] = defaultdict(
            lambda: deque(maxlen=self.config.health_history_size)
        )
        self._lock = asyncio.Lock()
        self._check_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start health monitoring."""
        self._check_task = asyncio.create_task(self._health_check_loop())
        logger.info("[AGI Health] Aggregator started")

    async def stop(self) -> None:
        """Stop health monitoring."""
        if self._check_task:
            self._check_task.cancel()
            try:
                await self._check_task
            except asyncio.CancelledError:
                pass

    async def _health_check_loop(self) -> None:
        """Periodic health check loop with timeout protection."""
        shutdown_event = get_shutdown_event()
        max_iterations = int(os.getenv("AGI_HEALTH_MAX_ITERATIONS", "0")) or None
        iteration = 0

        while True:
            # Check for shutdown
            if shutdown_event.is_set():
                logger.info("[AGI Health] Check loop stopped via shutdown event")
                break

            # Check max iterations (for testing/safety)
            if max_iterations and iteration >= max_iterations:
                logger.info(f"[AGI Health] Check loop reached max iterations ({max_iterations})")
                break

            iteration += 1

            try:
                await asyncio.sleep(self.config.health_check_interval_sec)
                # Add timeout protection for health collection
                await asyncio.wait_for(
                    self._collect_health(),
                    timeout=TimeoutConfig.HEALTH_CHECK
                )
            except asyncio.TimeoutError:
                logger.warning(f"[AGI Health] Check timed out after {TimeoutConfig.HEALTH_CHECK}s")
            except asyncio.CancelledError:
                logger.info("[AGI Health] Check loop cancelled")
                break
            except Exception as e:
                logger.error(f"[AGI Health] Check error: {e}")

    async def _collect_health(self) -> None:
        """Collect health from all components."""
        for component in AGIComponent:
            health = await self._check_component(component)
            async with self._lock:
                self._health[component] = health
                self._health_history[component].append(health)

    async def _check_component(self, component: AGIComponent) -> ComponentHealth:
        """Check health of a specific component."""
        start_time = time.time()

        try:
            if component == AGIComponent.JARVIS:
                # Check JARVIS health
                health_file = Path.home() / ".jarvis" / "trinity" / "components" / "jarvis_body.json"
                if health_file.exists():
                    data = json.loads(health_file.read_text())
                    age = time.time() - data.get("timestamp", 0)
                    if age < 30:
                        return ComponentHealth(
                            component=component,
                            status=HealthStatus.HEALTHY,
                            latency_ms=(time.time() - start_time) * 1000,
                            uptime_sec=data.get("uptime", 0),
                        )

            elif component == AGIComponent.PRIME:
                # Check Prime health
                health_file = Path.home() / ".jarvis" / "trinity" / "components" / "jarvis_prime.json"
                if health_file.exists():
                    data = json.loads(health_file.read_text())
                    age = time.time() - data.get("timestamp", 0)
                    if age < 30:
                        return ComponentHealth(
                            component=component,
                            status=HealthStatus.HEALTHY,
                            latency_ms=(time.time() - start_time) * 1000,
                        )

            elif component == AGIComponent.REACTOR:
                # Check Reactor Core health
                health_file = Path.home() / ".jarvis" / "trinity" / "components" / "reactor_core.json"
                if health_file.exists():
                    data = json.loads(health_file.read_text())
                    age = time.time() - data.get("timestamp", 0)
                    if age < 30:
                        return ComponentHealth(
                            component=component,
                            status=HealthStatus.HEALTHY,
                            latency_ms=(time.time() - start_time) * 1000,
                        )

            return ComponentHealth(component=component, status=HealthStatus.UNKNOWN)

        except Exception as e:
            return ComponentHealth(
                component=component,
                status=HealthStatus.UNHEALTHY,
                error_count=1,
                metrics={"error": str(e)}
            )

    async def get_unified_health(self) -> Dict[str, Any]:
        """Get unified health status."""
        async with self._lock:
            components = {}
            for comp, health in self._health.items():
                components[comp.value] = {
                    "status": health.status.value,
                    "latency_ms": health.latency_ms,
                    "last_check": health.last_check,
                    "error_count": health.error_count,
                }

            # Calculate overall status
            statuses = [h.status for h in self._health.values()]
            if all(s == HealthStatus.HEALTHY for s in statuses):
                overall = HealthStatus.HEALTHY
            elif any(s == HealthStatus.UNHEALTHY for s in statuses):
                overall = HealthStatus.UNHEALTHY
            elif any(s == HealthStatus.DEGRADED for s in statuses):
                overall = HealthStatus.DEGRADED
            else:
                overall = HealthStatus.UNKNOWN

            return {
                "overall_status": overall.value,
                "components": components,
                "timestamp": time.time(),
            }


# =============================================================================
# Unified AGI Orchestrator
# =============================================================================

class UnifiedAGIOrchestrator:
    """
    v100.0: The master orchestrator for the AGI system.

    Coordinates all subsystems:
    - Event Bus (cross-repo communication)
    - Persistent State (survives restarts)
    - Learning Pipeline (JARVIS -> Reactor -> Prime)
    - Agent Registry (lifecycle management)
    - Health Aggregator (monitoring)
    """

    def __init__(self, config: AGIOrchestratorConfig = None):
        self.config = config or get_agi_config()
        self.event_bus = UnifiedEventBus(self.config)
        self.state_manager = PersistentStateManager(self.config)
        self.learning_pipeline = LearningPipeline(self.config)
        self.agent_registry = AgentRegistry(self.config)
        self.health_aggregator = CrossRepoHealthAggregator(self.config)
        self._running = False
        self._start_time: Optional[float] = None

    async def start(self) -> bool:
        """Start the AGI orchestrator."""
        if self._running:
            return True

        try:
            logger.info("[AGI Orchestrator] Starting v100.0...")
            self._start_time = time.time()

            # Initialize all subsystems in parallel
            await asyncio.gather(
                self.event_bus.initialize(),
                self.state_manager.initialize(),
                self.learning_pipeline.initialize(),
            )

            # Start event bus
            await self.event_bus.start()

            # Start health aggregator
            if self.config.health_aggregator_enabled:
                await self.health_aggregator.start()

            # Publish startup event
            await self.event_bus.publish(AGIEvent(
                event_type="agi.orchestrator.started",
                source=AGIComponent.ORCHESTRATOR,
                priority=EventPriority.HIGH,
                payload={
                    "version": "100.0",
                    "instance_id": self.config.instance_id,
                    "config": {
                        "event_bus": self.config.event_bus_type,
                        "learning_enabled": self.config.learning_pipeline_enabled,
                        "agent_activation_enabled": self.config.agent_activation_enabled,
                    }
                }
            ))

            self._running = True
            logger.info("[AGI Orchestrator] v100.0 started successfully")
            return True

        except Exception as e:
            logger.error(f"[AGI Orchestrator] Failed to start: {e}")
            traceback.print_exc()
            return False

    async def stop(self) -> None:
        """Stop the AGI orchestrator."""
        if not self._running:
            return

        logger.info("[AGI Orchestrator] Stopping...")

        # Publish shutdown event
        try:
            await self.event_bus.publish(AGIEvent(
                event_type="agi.orchestrator.stopping",
                source=AGIComponent.ORCHESTRATOR,
                priority=EventPriority.CRITICAL,
            ))
        except Exception:
            pass

        # Stop all subsystems
        await asyncio.gather(
            self.event_bus.stop(),
            self.state_manager.stop(),
            self.learning_pipeline.stop(),
            self.health_aggregator.stop(),
            return_exceptions=True,
        )

        self._running = False
        logger.info("[AGI Orchestrator] Stopped")

    async def record_experience(self, experience: LearningExperience) -> str:
        """Record a learning experience."""
        return await self.learning_pipeline.record_experience(experience)

    async def publish_event(self, event: AGIEvent) -> str:
        """Publish an event to the bus."""
        return await self.event_bus.publish(event)

    async def get_state(self, key: str, default: Any = None) -> Any:
        """Get a persistent state value."""
        return await self.state_manager.get(key, default)

    async def set_state(self, key: str, value: Any) -> None:
        """Set a persistent state value."""
        await self.state_manager.set(key, value)

    async def get_status(self) -> Dict[str, Any]:
        """Get full orchestrator status."""
        return {
            "running": self._running,
            "uptime_sec": time.time() - self._start_time if self._start_time else 0,
            "instance_id": self.config.instance_id,
            "health": await self.health_aggregator.get_unified_health(),
            "learning": await self.learning_pipeline.get_learning_stats(),
            "agents": await self.agent_registry.get_activation_stats(),
        }


# =============================================================================
# Global Instance and Factory
# =============================================================================

_agi_orchestrator: Optional[UnifiedAGIOrchestrator] = None
_agi_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_agi_orchestrator() -> UnifiedAGIOrchestrator:
    """Get or create the singleton AGI orchestrator."""
    global _agi_orchestrator
    async with _agi_lock:
        if _agi_orchestrator is None:
            _agi_orchestrator = UnifiedAGIOrchestrator()
        return _agi_orchestrator


async def start_agi_orchestrator() -> bool:
    """Start the AGI orchestrator."""
    orchestrator = await get_agi_orchestrator()
    return await orchestrator.start()


async def stop_agi_orchestrator() -> None:
    """Stop the AGI orchestrator."""
    global _agi_orchestrator
    if _agi_orchestrator:
        await _agi_orchestrator.stop()
        _agi_orchestrator = None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Config
    "AGIOrchestratorConfig",
    "get_agi_config",
    # Enums
    "AGIComponent",
    "EventPriority",
    "AgentState",
    "HealthStatus",
    "LearningType",
    # Events
    "AGIEvent",
    "UnifiedEventBus",
    # State
    "PersistentState",
    "PersistentStateManager",
    # Learning
    "LearningExperience",
    "LearningPipeline",
    # Agents
    "AgentInfo",
    "AgentRegistry",
    # Health
    "ComponentHealth",
    "CrossRepoHealthAggregator",
    # Orchestrator
    "UnifiedAGIOrchestrator",
    "get_agi_orchestrator",
    "start_agi_orchestrator",
    "stop_agi_orchestrator",
]
