"""
JARVIS Persistent Intelligence Manager (v2.7)

Unified state persistence and synchronization layer for the JARVIS AI system.
Provides automatic local/cloud sync, checkpointing, recovery, and conflict resolution.

Architecture:
    ┌──────────────────────────────────────────────────────────────┐
    │                  PERSISTENT INTELLIGENCE                      │
    ├──────────────────────────────────────────────────────────────┤
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
    │  │ Local State │◄─►│ Sync Engine │◄─►│ Cloud State (GCP)  │  │
    │  │  (SQLite)   │   │             │   │   (Cloud SQL)      │  │
    │  └─────────────┘   └─────────────┘   └─────────────────────┘  │
    │         │                │                     │              │
    │         ▼                ▼                     ▼              │
    │  ┌───────────────────────────────────────────────────────┐   │
    │  │                  State Registry                        │   │
    │  │   • Agent states     • Session states                  │   │
    │  │   • User preferences • Learning data                   │   │
    │  │   • Context memory   • Conversation history            │   │
    │  └───────────────────────────────────────────────────────┘   │
    └──────────────────────────────────────────────────────────────┘

Features:
- Automatic conflict resolution with last-write-wins or merge strategies
- Incremental sync (delta updates)
- Offline-first with automatic sync on reconnect
- Checkpointing and recovery
- Cross-repo state sharing (JARVIS, Prime, Reactor)
- Event-driven state changes
- Compression for large state objects
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar, Union

import aiofiles
import aiosqlite

logger = logging.getLogger(__name__)

T = TypeVar("T")


class SyncStrategy(Enum):
    """Conflict resolution strategies."""
    LAST_WRITE_WINS = "last_write_wins"  # Most recent timestamp wins
    LOCAL_WINS = "local_wins"  # Local always takes precedence
    CLOUD_WINS = "cloud_wins"  # Cloud always takes precedence
    MERGE = "merge"  # Attempt to merge state (dict merge)
    MANUAL = "manual"  # Queue for manual resolution


class SyncStatus(Enum):
    """Sync status for state entries."""
    SYNCED = "synced"
    LOCAL_MODIFIED = "local_modified"
    CLOUD_MODIFIED = "cloud_modified"
    CONFLICT = "conflict"
    SYNCING = "syncing"
    ERROR = "error"


class PersistentIntelligenceHealth(str, Enum):
    """Closed-5 health vocabulary for the manager (Defect #3 Slice B,
    2026-05-03). Consumed by the PersistentIntelligenceHealthOracle
    adapter (Slice C) which surfaces health via the existing
    Production Oracle observer + GET /observability/production-oracle
    + SSE production_oracle_signal_observed. No parallel observability
    surface -- leverages the substrate already shipped in Tier 2 #6.
    """

    HEALTHY = "healthy"                       # init+checkpoint OK
    DEGRADED_READONLY = "degraded_readonly"   # readonly DB / WAL fail
    DEGRADED_DISK_FULL = "degraded_disk_full" # ENOSPC / disk full
    DEGRADED_OTHER = "degraded_other"         # unrecognized failure
    DISABLED = "disabled"                     # never initialized


class StateCategory(Enum):
    """Categories of persistent state."""
    AGENT = "agent"  # Agent-specific state
    SESSION = "session"  # User session state
    USER = "user"  # User preferences and profiles
    LEARNING = "learning"  # ML learning data
    CONTEXT = "context"  # Context memory
    CONVERSATION = "conversation"  # Conversation history
    SYSTEM = "system"  # System configuration
    CACHE = "cache"  # Cached computations (low priority sync)


@dataclass
class StateEntry:
    """A single state entry."""
    key: str
    category: StateCategory
    value: Any
    version: int = 1
    created_at: datetime = field(default_factory=datetime.now)
    modified_at: datetime = field(default_factory=datetime.now)
    synced_at: Optional[datetime] = None
    sync_status: SyncStatus = SyncStatus.LOCAL_MODIFIED
    checksum: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        """Compute checksum of value for change detection."""
        value_str = json.dumps(self.value, sort_keys=True, default=str)
        return hashlib.sha256(value_str.encode()).hexdigest()[:16]

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "key": self.key,
            "category": self.category.value,
            "value": self.value,
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "modified_at": self.modified_at.isoformat(),
            "synced_at": self.synced_at.isoformat() if self.synced_at else None,
            "sync_status": self.sync_status.value,
            "checksum": self.checksum,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StateEntry":
        """Deserialize from dictionary."""
        return cls(
            key=data["key"],
            category=StateCategory(data["category"]),
            value=data["value"],
            version=data.get("version", 1),
            created_at=datetime.fromisoformat(data["created_at"]),
            modified_at=datetime.fromisoformat(data["modified_at"]),
            synced_at=datetime.fromisoformat(data["synced_at"]) if data.get("synced_at") else None,
            sync_status=SyncStatus(data.get("sync_status", "local_modified")),
            checksum=data.get("checksum", ""),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SyncEvent:
    """Event generated during sync operations."""
    event_id: str
    event_type: str  # "uploaded", "downloaded", "conflict", "merged", "error"
    key: str
    category: StateCategory
    timestamp: datetime = field(default_factory=datetime.now)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SyncStats:
    """Statistics for sync operations."""
    total_entries: int = 0
    synced_entries: int = 0
    pending_uploads: int = 0
    pending_downloads: int = 0
    conflicts: int = 0
    last_sync: Optional[datetime] = None
    sync_duration_ms: float = 0.0
    errors: List[str] = field(default_factory=list)


class PersistentIntelligenceManager:
    """
    Unified state persistence manager for JARVIS.

    Provides:
    - Automatic local/cloud sync
    - Checkpointing and recovery
    - Cross-repo state sharing
    - Event-driven state changes
    - Conflict resolution

    Usage:
        manager = await PersistentIntelligenceManager.create()

        # Store state
        await manager.set("agent:goal_inference:patterns", patterns_dict, StateCategory.AGENT)

        # Retrieve state
        patterns = await manager.get("agent:goal_inference:patterns")

        # Force sync
        await manager.sync()

        # Get state with fallback
        config = await manager.get_or_default("user:preferences", default={})

        # Listen for changes
        manager.on_change("user:*", callback)
    """

    # Configurable settings via environment
    LOCAL_DB_PATH = os.getenv(
        "JARVIS_STATE_DB",
        str(Path.home() / ".jarvis" / "state" / "persistent_intelligence.db")
    )
    STATE_DIR = os.getenv(
        "JARVIS_STATE_DIR",
        str(Path.home() / ".jarvis" / "state")
    )
    CROSS_REPO_DIR = os.getenv(
        "JARVIS_CROSS_REPO_DIR",
        str(Path.home() / ".jarvis" / "cross_repo")
    )

    # Sync settings
    SYNC_INTERVAL_SECONDS = float(os.getenv("STATE_SYNC_INTERVAL", "30"))
    BATCH_SIZE = int(os.getenv("STATE_SYNC_BATCH_SIZE", "100"))
    COMPRESSION_THRESHOLD = int(os.getenv("STATE_COMPRESSION_THRESHOLD", "4096"))
    CHECKPOINT_INTERVAL_SECONDS = float(os.getenv("STATE_CHECKPOINT_INTERVAL", "300"))

    # Cloud settings
    CLOUD_ENABLED = os.getenv("STATE_CLOUD_SYNC", "true").lower() == "true"
    CLOUD_PROJECT = os.getenv("GCP_PROJECT_ID", "")
    CLOUD_INSTANCE = os.getenv("CLOUD_SQL_INSTANCE", "")

    def __init__(self) -> None:
        """Initialize the manager. Use create() for async initialization."""
        self._db: Optional[aiosqlite.Connection] = None
        self._running = False
        self._sync_task: Optional[asyncio.Task] = None
        self._checkpoint_task: Optional[asyncio.Task] = None

        # In-memory cache for fast access
        self._cache: Dict[str, StateEntry] = {}
        self._cache_lock = asyncio.Lock()

        # Change listeners: {pattern: [callbacks]}
        self._listeners: Dict[str, List[Callable]] = defaultdict(list)

        # Pending changes queue
        self._pending_sync: Set[str] = set()
        self._pending_lock = asyncio.Lock()

        # Sync statistics
        self._stats = SyncStats()

        # Cloud database adapter (lazy loaded)
        self._cloud_adapter: Optional[Any] = None

        # Cross-repo sync
        self._cross_repo_enabled = os.getenv("CROSS_REPO_SYNC", "true").lower() == "true"

        # Defect #3 Slice B (2026-05-03) — health state + circuit
        # breaker tracking. Health starts DISABLED; transitions to
        # HEALTHY after _init_local_db succeeds. Checkpoint failures
        # update health + accumulate consecutive-failure count; after
        # JARVIS_PERSISTENT_INTELLIGENCE_FAIL_THRESHOLD failures (default
        # 3) the checkpoint loop suspends to avoid log spam against
        # a permanently-degraded path.
        self._health: PersistentIntelligenceHealth = (
            PersistentIntelligenceHealth.DISABLED
        )
        self._consecutive_checkpoint_failures: int = 0
        self._checkpoint_suspended: bool = False

        logger.info(
            f"[PERSISTENT-INTELLIGENCE] Initialized with "
            f"local_db={self.LOCAL_DB_PATH}, "
            f"cloud_enabled={self.CLOUD_ENABLED}"
        )

    @property
    def health(self) -> "PersistentIntelligenceHealth":
        """Current health state (Defect #3 Slice B). Read by the
        PersistentIntelligenceHealthOracle adapter to project into
        the Production Oracle observer's signal stream."""
        return self._health

    @property
    def effective_db_path(self) -> str:
        """The path actually in use after fallback-chain resolution
        (Defect #3 Slice A). Differs from ``LOCAL_DB_PATH`` when the
        configured default is non-writable."""
        return getattr(self, "_effective_db_path", self.LOCAL_DB_PATH)

    @property
    def checkpoint_suspended(self) -> bool:
        """True when the checkpoint loop has been suspended due to
        consecutive failures (Defect #3 Slice B circuit breaker)."""
        return self._checkpoint_suspended

    @classmethod
    async def create(cls) -> "PersistentIntelligenceManager":
        """Create and initialize the manager."""
        manager = cls()
        await manager.initialize()
        return manager

    async def initialize(self) -> None:
        """Initialize database and start background tasks."""
        # Ensure directories exist
        Path(self.STATE_DIR).mkdir(parents=True, exist_ok=True)
        Path(self.CROSS_REPO_DIR).mkdir(parents=True, exist_ok=True)
        Path(self.LOCAL_DB_PATH).parent.mkdir(parents=True, exist_ok=True)

        # Initialize local database
        await self._init_local_db()

        # Load initial state into cache
        await self._load_cache()

        # Initialize cloud adapter if enabled
        if self.CLOUD_ENABLED:
            await self._init_cloud_adapter()

        self._running = True

        # Start background sync
        self._sync_task = asyncio.create_task(
            self._sync_loop(),
            name="persistent_intelligence_sync"
        )

        # Start checkpoint task
        self._checkpoint_task = asyncio.create_task(
            self._checkpoint_loop(),
            name="persistent_intelligence_checkpoint"
        )

        logger.info("[PERSISTENT-INTELLIGENCE] Initialization complete")

    def _resolve_writable_db_path(self) -> str:
        """Defect #3 Slice A (2026-05-03) — probe write capability +
        fall through a chain of fallbacks until a writable location
        is found.

        Resolution chain (first writable wins):
          1. ``JARVIS_STATE_DB`` env (operator-explicit override)
          2. ``JARVIS_STATE_DIR`` env (operator-explicit dir override)
          3. ``<cwd>/.jarvis/state/persistent_intelligence.db``
             (project-local fallback)
          4. ``tempfile.gettempdir()/jarvis_state/persistent_intelligence.db``
             (last resort, ephemeral)

        Probe is a single ``touch`` test on the parent directory --
        SQLite's WAL/journal write requires directory write
        permission, not just file write permission, so a
        directory-level probe catches the soak v5 readonly-DB
        pathology even when the .db file itself appears writable.

        Logs WARNING on every fallback transition so operators see
        why the chosen path differs from the configured default.
        Returns the chosen path; NEVER raises (the last-resort
        tempdir is always writable on a working system)."""
        import tempfile

        def _can_write(parent: Path) -> bool:
            try:
                parent.mkdir(parents=True, exist_ok=True)
                probe = parent / f"_writetest_{os.getpid()}_{id(self)}"
                try:
                    probe.touch()
                    probe.unlink(missing_ok=True)
                    return True
                except (OSError, PermissionError):
                    return False
            except (OSError, PermissionError):
                return False

        candidates: List[tuple] = [
            (self.LOCAL_DB_PATH, "configured (LOCAL_DB_PATH)"),
            (
                str(Path(self.STATE_DIR) / "persistent_intelligence.db"),
                "STATE_DIR-derived",
            ),
            (
                str(
                    Path.cwd() / ".jarvis" / "state"
                    / "persistent_intelligence.db"
                ),
                "project-local cwd fallback",
            ),
            (
                str(
                    Path(tempfile.gettempdir()) / "jarvis_state"
                    / "persistent_intelligence.db"
                ),
                "tempdir last-resort fallback",
            ),
        ]
        first_path = candidates[0][0]
        for path_str, label in candidates:
            parent = Path(path_str).parent
            if _can_write(parent):
                if path_str != first_path:
                    logger.warning(
                        "[PERSISTENT-INTELLIGENCE] DB path fallback: "
                        "configured=%s (non-writable) -> chosen=%s "
                        "(%s). Defect #3 fix 2026-05-03 -- write-"
                        "permission probe at init avoids checkpoint-"
                        "loop log spam against a readonly path.",
                        first_path, path_str, label,
                    )
                return path_str
        # Should never reach here on a working system, but if EVERY
        # candidate fails, return the last one and let aiosqlite
        # raise informatively at connect time.
        logger.error(
            "[PERSISTENT-INTELLIGENCE] all DB-path candidates non-"
            "writable: %s",
            [c[0] for c in candidates],
        )
        return candidates[-1][0]

    async def _init_local_db(self) -> None:
        """Initialize SQLite database. Defect #3 fix (2026-05-03) --
        resolves writable path via fallback chain BEFORE connecting
        so a readonly default location triggers a graceful fall-
        through instead of permanent checkpoint-loop log spam.

        On successful connection + table creation, flips
        ``self._health`` from DISABLED to HEALTHY. On connection
        failure, leaves DISABLED so the oracle adapter surfaces the
        DISABLED verdict to operators."""
        self._effective_db_path: str = self._resolve_writable_db_path()
        try:
            self._db = await aiosqlite.connect(self._effective_db_path)
            self._health = PersistentIntelligenceHealth.HEALTHY
        except Exception:  # noqa: BLE001 -- defensive
            self._health = PersistentIntelligenceHealth.DISABLED
            raise

        # Create tables
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS state_entries (
                key TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                value_json TEXT NOT NULL,
                value_compressed BLOB,
                version INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                modified_at TEXT NOT NULL,
                synced_at TEXT,
                sync_status TEXT DEFAULT 'local_modified',
                checksum TEXT,
                metadata_json TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_state_category ON state_entries(category);
            CREATE INDEX IF NOT EXISTS idx_state_sync_status ON state_entries(sync_status);
            CREATE INDEX IF NOT EXISTS idx_state_modified ON state_entries(modified_at);

            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                event_type TEXT NOT NULL,
                key TEXT,
                category TEXT,
                details_json TEXT,
                success INTEGER
            );

            CREATE TABLE IF NOT EXISTS checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                checkpoint_type TEXT NOT NULL,
                entries_count INTEGER,
                file_path TEXT,
                metadata_json TEXT
            );
        """)

        await self._db.commit()
        logger.debug("[PERSISTENT-INTELLIGENCE] Local database initialized")

    async def _init_cloud_adapter(self) -> None:
        """Initialize cloud database adapter."""
        try:
            from backend.intelligence.cloud_database_adapter import CloudDatabaseAdapter
            self._cloud_adapter = await CloudDatabaseAdapter.create()
            logger.info("[PERSISTENT-INTELLIGENCE] Cloud adapter initialized")
        except ImportError:
            logger.warning("[PERSISTENT-INTELLIGENCE] Cloud adapter not available")
        except Exception as e:
            logger.warning(f"[PERSISTENT-INTELLIGENCE] Cloud init failed: {e}")

    async def _load_cache(self) -> None:
        """Load frequently accessed state into memory cache."""
        if not self._db:
            return

        async with self._cache_lock:
            cursor = await self._db.execute("""
                SELECT key, category, value_json, value_compressed, version,
                       created_at, modified_at, synced_at, sync_status,
                       checksum, metadata_json
                FROM state_entries
                ORDER BY modified_at DESC
                LIMIT 1000
            """)

            async for row in cursor:
                entry = self._row_to_entry(row)
                self._cache[entry.key] = entry

        logger.debug(f"[PERSISTENT-INTELLIGENCE] Loaded {len(self._cache)} entries to cache")

    def _row_to_entry(self, row: tuple) -> StateEntry:
        """Convert database row to StateEntry."""
        (key, category, value_json, value_compressed, version,
         created_at, modified_at, synced_at, sync_status,
         checksum, metadata_json) = row

        # Decompress if needed
        if value_compressed:
            value = json.loads(gzip.decompress(value_compressed).decode())
        else:
            value = json.loads(value_json) if value_json else None

        return StateEntry(
            key=key,
            category=StateCategory(category),
            value=value,
            version=version,
            created_at=datetime.fromisoformat(created_at),
            modified_at=datetime.fromisoformat(modified_at),
            synced_at=datetime.fromisoformat(synced_at) if synced_at else None,
            sync_status=SyncStatus(sync_status),
            checksum=checksum or "",
            metadata=json.loads(metadata_json) if metadata_json else {},
        )

    async def set(
        self,
        key: str,
        value: Any,
        category: StateCategory = StateCategory.SYSTEM,
        metadata: Optional[Dict[str, Any]] = None,
        sync_strategy: SyncStrategy = SyncStrategy.LAST_WRITE_WINS,
    ) -> StateEntry:
        """
        Store a state value.

        Args:
            key: Unique key for the state
            value: Value to store (must be JSON-serializable)
            category: State category
            metadata: Optional metadata
            sync_strategy: How to handle conflicts

        Returns:
            The created/updated StateEntry
        """
        now = datetime.now()

        async with self._cache_lock:
            existing = self._cache.get(key)

            if existing:
                # Update existing entry
                entry = StateEntry(
                    key=key,
                    category=category,
                    value=value,
                    version=existing.version + 1,
                    created_at=existing.created_at,
                    modified_at=now,
                    sync_status=SyncStatus.LOCAL_MODIFIED,
                    metadata=metadata or existing.metadata,
                )
            else:
                # Create new entry
                entry = StateEntry(
                    key=key,
                    category=category,
                    value=value,
                    version=1,
                    created_at=now,
                    modified_at=now,
                    sync_status=SyncStatus.LOCAL_MODIFIED,
                    metadata=metadata or {},
                )

            self._cache[key] = entry

        # Persist to database
        await self._persist_entry(entry)

        # Mark for sync
        async with self._pending_lock:
            self._pending_sync.add(key)

        # Notify listeners
        await self._notify_listeners(key, entry)

        # Write to cross-repo if enabled
        if self._cross_repo_enabled:
            await self._write_cross_repo(entry)

        return entry

    async def get(
        self,
        key: str,
        default: T = None,
    ) -> Optional[Union[Any, T]]:
        """
        Retrieve a state value.

        Args:
            key: The key to retrieve
            default: Default value if not found

        Returns:
            The value, or default if not found
        """
        async with self._cache_lock:
            if key in self._cache:
                return self._cache[key].value

        # Try database
        if self._db:
            cursor = await self._db.execute(
                "SELECT * FROM state_entries WHERE key = ?",
                (key,)
            )
            row = await cursor.fetchone()

            if row:
                entry = self._row_to_entry(row)
                async with self._cache_lock:
                    self._cache[key] = entry
                return entry.value

        return default

    async def get_entry(self, key: str) -> Optional[StateEntry]:
        """Get full state entry with metadata."""
        async with self._cache_lock:
            if key in self._cache:
                return self._cache[key]

        if self._db:
            cursor = await self._db.execute(
                "SELECT * FROM state_entries WHERE key = ?",
                (key,)
            )
            row = await cursor.fetchone()

            if row:
                entry = self._row_to_entry(row)
                async with self._cache_lock:
                    self._cache[key] = entry
                return entry

        return None

    async def get_or_default(
        self,
        key: str,
        default: T,
    ) -> Union[Any, T]:
        """Get value or return default (never returns None)."""
        value = await self.get(key)
        return value if value is not None else default

    async def delete(self, key: str) -> bool:
        """Delete a state entry."""
        async with self._cache_lock:
            if key in self._cache:
                del self._cache[key]

        if self._db:
            await self._db.execute(
                "DELETE FROM state_entries WHERE key = ?",
                (key,)
            )
            await self._db.commit()

        # Mark for cloud deletion
        async with self._pending_lock:
            self._pending_sync.add(f"DELETE:{key}")

        return True

    async def get_by_category(
        self,
        category: StateCategory,
        limit: int = 100,
    ) -> List[StateEntry]:
        """Get all entries in a category."""
        entries = []

        if self._db:
            cursor = await self._db.execute(
                """
                SELECT * FROM state_entries
                WHERE category = ?
                ORDER BY modified_at DESC
                LIMIT ?
                """,
                (category.value, limit)
            )

            async for row in cursor:
                entries.append(self._row_to_entry(row))

        return entries

    async def get_by_prefix(
        self,
        prefix: str,
        limit: int = 100,
    ) -> List[StateEntry]:
        """Get all entries with a key prefix."""
        entries = []

        if self._db:
            cursor = await self._db.execute(
                """
                SELECT * FROM state_entries
                WHERE key LIKE ?
                ORDER BY modified_at DESC
                LIMIT ?
                """,
                (f"{prefix}%", limit)
            )

            async for row in cursor:
                entries.append(self._row_to_entry(row))

        return entries

    async def _persist_entry(self, entry: StateEntry) -> None:
        """Persist entry to local database."""
        if not self._db:
            return

        value_json = json.dumps(entry.value, default=str)
        value_compressed = None

        # Compress large values
        if len(value_json) > self.COMPRESSION_THRESHOLD:
            value_compressed = gzip.compress(value_json.encode())
            value_json = None

        await self._db.execute(
            """
            INSERT OR REPLACE INTO state_entries
            (key, category, value_json, value_compressed, version,
             created_at, modified_at, synced_at, sync_status, checksum, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.key,
                entry.category.value,
                value_json,
                value_compressed,
                entry.version,
                entry.created_at.isoformat(),
                entry.modified_at.isoformat(),
                entry.synced_at.isoformat() if entry.synced_at else None,
                entry.sync_status.value,
                entry.checksum,
                json.dumps(entry.metadata),
            )
        )
        await self._db.commit()

    async def _write_cross_repo(self, entry: StateEntry) -> None:
        """Write state to cross-repo directory for Trinity sharing."""
        if not self._cross_repo_enabled:
            return

        try:
            # Group by category for cross-repo files
            filename = f"{entry.category.value}_state.json"
            filepath = Path(self.CROSS_REPO_DIR) / filename

            # Read existing state
            existing = {}
            if filepath.exists():
                async with aiofiles.open(filepath, "r") as f:
                    content = await f.read()
                    existing = json.loads(content) if content else {}

            # Update with new entry
            existing[entry.key] = entry.to_dict()

            # Write atomically
            temp_path = filepath.with_suffix(".tmp")
            async with aiofiles.open(temp_path, "w") as f:
                await f.write(json.dumps(existing, indent=2, default=str))

            temp_path.rename(filepath)

        except Exception as e:
            logger.warning(f"[PERSISTENT-INTELLIGENCE] Cross-repo write failed: {e}")

    def on_change(
        self,
        pattern: str,
        callback: Callable[[str, StateEntry], None],
    ) -> None:
        """
        Register a callback for state changes.

        Args:
            pattern: Key pattern (supports * wildcard)
            callback: Function(key, entry) to call on changes
        """
        self._listeners[pattern].append(callback)

    async def _notify_listeners(self, key: str, entry: StateEntry) -> None:
        """Notify registered listeners of state change."""
        for pattern, callbacks in self._listeners.items():
            if self._matches_pattern(key, pattern):
                for callback in callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(key, entry)
                        else:
                            callback(key, entry)
                    except Exception as e:
                        logger.exception(f"[PERSISTENT-INTELLIGENCE] Listener error: {e}")

    def _matches_pattern(self, key: str, pattern: str) -> bool:
        """Check if key matches pattern (supports * wildcard)."""
        if pattern == "*":
            return True
        if "*" not in pattern:
            return key == pattern

        # Simple wildcard matching
        parts = pattern.split("*")
        if len(parts) == 2:
            prefix, suffix = parts
            return key.startswith(prefix) and key.endswith(suffix)

        return False

    async def sync(self, force: bool = False) -> SyncStats:
        """
        Synchronize local state with cloud.

        Args:
            force: If True, sync all entries regardless of status

        Returns:
            Sync statistics
        """
        if not self.CLOUD_ENABLED or not self._cloud_adapter:
            logger.debug("[PERSISTENT-INTELLIGENCE] Cloud sync disabled")
            return self._stats

        start_time = time.perf_counter()
        stats = SyncStats()

        try:
            # Get pending entries
            async with self._pending_lock:
                pending_keys = list(self._pending_sync)
                self._pending_sync.clear()

            if force:
                # Sync all modified entries
                if self._db:
                    cursor = await self._db.execute(
                        "SELECT key FROM state_entries WHERE sync_status != 'synced'"
                    )
                    async for row in cursor:
                        pending_keys.append(row[0])

            pending_keys = list(set(pending_keys))
            stats.pending_uploads = len(pending_keys)

            # Upload in batches
            for i in range(0, len(pending_keys), self.BATCH_SIZE):
                batch = pending_keys[i:i + self.BATCH_SIZE]
                await self._upload_batch(batch, stats)

            # Download cloud changes
            await self._download_changes(stats)

            stats.last_sync = datetime.now()
            stats.sync_duration_ms = (time.perf_counter() - start_time) * 1000

            self._stats = stats
            logger.info(
                f"[PERSISTENT-INTELLIGENCE] Sync complete: "
                f"uploaded={stats.synced_entries}, "
                f"downloaded={stats.pending_downloads}, "
                f"conflicts={stats.conflicts}, "
                f"duration={stats.sync_duration_ms:.1f}ms"
            )

        except Exception as e:
            logger.exception(f"[PERSISTENT-INTELLIGENCE] Sync error: {e}")
            stats.errors.append(str(e))

        return stats

    async def _upload_batch(
        self,
        keys: List[str],
        stats: SyncStats,
    ) -> None:
        """Upload a batch of entries to cloud."""
        for key in keys:
            try:
                # Handle deletions
                if key.startswith("DELETE:"):
                    actual_key = key[7:]
                    # TODO: Implement cloud deletion
                    continue

                entry = await self.get_entry(key)
                if not entry:
                    continue

                # Upload to cloud
                if self._cloud_adapter:
                    await self._cloud_adapter.upsert_state(
                        key=entry.key,
                        category=entry.category.value,
                        value=entry.value,
                        version=entry.version,
                        checksum=entry.checksum,
                    )

                # Mark as synced
                entry.sync_status = SyncStatus.SYNCED
                entry.synced_at = datetime.now()
                await self._persist_entry(entry)

                stats.synced_entries += 1

            except Exception as e:
                logger.warning(f"[PERSISTENT-INTELLIGENCE] Upload failed for {key}: {e}")
                stats.errors.append(f"Upload {key}: {e}")

    async def _download_changes(self, stats: SyncStats) -> None:
        """Download changes from cloud."""
        if not self._cloud_adapter:
            return

        try:
            # Get last sync timestamp
            last_sync = self._stats.last_sync or datetime.min

            # Fetch cloud changes
            changes = await self._cloud_adapter.get_changes_since(
                since=last_sync,
                limit=self.BATCH_SIZE,
            )

            for change in changes:
                await self._apply_cloud_change(change, stats)

            stats.pending_downloads = len(changes)

        except Exception as e:
            logger.warning(f"[PERSISTENT-INTELLIGENCE] Download failed: {e}")
            stats.errors.append(f"Download: {e}")

    async def _apply_cloud_change(
        self,
        cloud_entry: Dict[str, Any],
        stats: SyncStats,
    ) -> None:
        """Apply a cloud change to local state."""
        key = cloud_entry.get("key")
        if not key:
            return

        local = await self.get_entry(key)

        if not local:
            # New entry from cloud
            entry = StateEntry(
                key=key,
                category=StateCategory(cloud_entry["category"]),
                value=cloud_entry["value"],
                version=cloud_entry.get("version", 1),
                created_at=datetime.fromisoformat(cloud_entry["created_at"]),
                modified_at=datetime.fromisoformat(cloud_entry["modified_at"]),
                synced_at=datetime.now(),
                sync_status=SyncStatus.SYNCED,
                checksum=cloud_entry.get("checksum", ""),
            )
            async with self._cache_lock:
                self._cache[key] = entry
            await self._persist_entry(entry)

        elif local.checksum != cloud_entry.get("checksum"):
            # Conflict - use last-write-wins
            cloud_modified = datetime.fromisoformat(cloud_entry["modified_at"])

            if cloud_modified > local.modified_at:
                # Cloud wins
                local.value = cloud_entry["value"]
                local.version = max(local.version, cloud_entry.get("version", 1)) + 1
                local.modified_at = cloud_modified
                local.synced_at = datetime.now()
                local.sync_status = SyncStatus.SYNCED
                local.checksum = cloud_entry.get("checksum", local._compute_checksum())

                async with self._cache_lock:
                    self._cache[key] = local
                await self._persist_entry(local)

            else:
                # Local wins - will be uploaded on next sync
                stats.conflicts += 1

    async def _sync_loop(self) -> None:
        """Background sync loop."""
        while self._running:
            try:
                await asyncio.sleep(self.SYNC_INTERVAL_SECONDS)

                if self._pending_sync:
                    await self.sync()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"[PERSISTENT-INTELLIGENCE] Sync loop error: {e}")

    async def _checkpoint_loop(self) -> None:
        """Background checkpoint loop. Defect #3 Slice B fix
        (2026-05-03): circuit breaker on consecutive failures.

        After ``JARVIS_PERSISTENT_INTELLIGENCE_FAIL_THRESHOLD``
        consecutive failures (default 3), the loop suspends further
        checkpointing. Operators see ONE summary log + the health
        state transitions to DEGRADED_* (read by the oracle adapter
        for SSE / GET surfacing). Loop continues running so it can
        be re-armed if operators clear the underlying issue, but no
        further checkpoint attempts fire while suspended.

        Exponential backoff between failures (300s -> 600s -> 1200s)
        gives transient issues a chance to resolve before tripping
        the circuit. Successful checkpoint resets failure count +
        flips health back to HEALTHY."""
        try:
            fail_threshold = max(
                1, int(os.getenv(
                    "JARVIS_PERSISTENT_INTELLIGENCE_FAIL_THRESHOLD",
                    "3",
                )),
            )
        except (TypeError, ValueError):
            fail_threshold = 3
        base_interval = self.CHECKPOINT_INTERVAL_SECONDS
        while self._running:
            try:
                # Exponential backoff between failures: 1x, 2x, 4x.
                # Capped at fail_threshold attempts before suspension.
                if self._consecutive_checkpoint_failures > 0:
                    backoff_factor = min(
                        4, 2 ** (self._consecutive_checkpoint_failures - 1),
                    )
                    interval = base_interval * backoff_factor
                else:
                    interval = base_interval
                await asyncio.sleep(interval)
                if self._checkpoint_suspended:
                    # Suspended: no checkpoint attempts. Loop continues
                    # to keep the cancellation contract intact.
                    continue
                await self.create_checkpoint()
                # Success: reset failure count + restore health.
                if self._consecutive_checkpoint_failures > 0:
                    logger.info(
                        "[PERSISTENT-INTELLIGENCE] checkpoint recovered "
                        "after %d consecutive failures",
                        self._consecutive_checkpoint_failures,
                    )
                self._consecutive_checkpoint_failures = 0
                self._health = (
                    PersistentIntelligenceHealth.HEALTHY
                )
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._consecutive_checkpoint_failures += 1
                # Classify failure into health enum (Slice B).
                emsg = str(e).lower()
                if "readonly" in emsg or "read-only" in emsg:
                    self._health = (
                        PersistentIntelligenceHealth.DEGRADED_READONLY
                    )
                elif "no space" in emsg or "disk full" in emsg or "ENOSPC" in str(e):
                    self._health = (
                        PersistentIntelligenceHealth.DEGRADED_DISK_FULL
                    )
                else:
                    self._health = (
                        PersistentIntelligenceHealth.DEGRADED_OTHER
                    )
                if self._consecutive_checkpoint_failures < fail_threshold:
                    logger.warning(
                        "[PERSISTENT-INTELLIGENCE] checkpoint failed "
                        "(%d/%d before circuit-breaker suspension): %s",
                        self._consecutive_checkpoint_failures,
                        fail_threshold, e,
                    )
                else:
                    if not self._checkpoint_suspended:
                        self._checkpoint_suspended = True
                        logger.error(
                            "[PERSISTENT-INTELLIGENCE] CIRCUIT BREAKER "
                            "TRIPPED: %d consecutive checkpoint "
                            "failures; suspending further checkpoint "
                            "attempts. Health=%s. Last error: %s. "
                            "Operators clear by fixing the underlying "
                            "issue + restarting; oracle adapter will "
                            "surface this state via the Production "
                            "Oracle observer.",
                            self._consecutive_checkpoint_failures,
                            self._health.value, e,
                        )

    async def create_checkpoint(self, checkpoint_type: str = "auto") -> str:
        """
        Create a checkpoint of current state.

        Returns:
            Path to checkpoint file
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"checkpoint_{checkpoint_type}_{timestamp}.json.gz"
        filepath = Path(self.STATE_DIR) / "checkpoints" / filename

        filepath.parent.mkdir(parents=True, exist_ok=True)

        # Export all entries
        entries = []
        if self._db:
            cursor = await self._db.execute("SELECT * FROM state_entries")
            async for row in cursor:
                entry = self._row_to_entry(row)
                entries.append(entry.to_dict())

        # Compress and write
        data = json.dumps({"entries": entries, "timestamp": timestamp}, default=str)
        compressed = gzip.compress(data.encode())

        async with aiofiles.open(filepath, "wb") as f:
            await f.write(compressed)

        # Log checkpoint
        if self._db:
            await self._db.execute(
                """
                INSERT INTO checkpoints (timestamp, checkpoint_type, entries_count, file_path, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(),
                    checkpoint_type,
                    len(entries),
                    str(filepath),
                    json.dumps({"size_bytes": len(compressed)}),
                )
            )
            await self._db.commit()

        logger.info(
            f"[PERSISTENT-INTELLIGENCE] Checkpoint created: {filepath} "
            f"({len(entries)} entries, {len(compressed)} bytes)"
        )

        return str(filepath)

    async def restore_from_checkpoint(self, filepath: str) -> int:
        """
        Restore state from a checkpoint.

        Args:
            filepath: Path to checkpoint file

        Returns:
            Number of entries restored
        """
        async with aiofiles.open(filepath, "rb") as f:
            compressed = await f.read()

        data = json.loads(gzip.decompress(compressed).decode())
        entries = data.get("entries", [])

        restored = 0
        for entry_dict in entries:
            try:
                entry = StateEntry.from_dict(entry_dict)
                async with self._cache_lock:
                    self._cache[entry.key] = entry
                await self._persist_entry(entry)
                restored += 1
            except Exception as e:
                logger.warning(f"[PERSISTENT-INTELLIGENCE] Failed to restore entry: {e}")

        logger.info(f"[PERSISTENT-INTELLIGENCE] Restored {restored} entries from {filepath}")
        return restored

    def get_stats(self) -> SyncStats:
        """Get current sync statistics."""
        self._stats.total_entries = len(self._cache)
        return self._stats

    async def shutdown(self) -> None:
        """Gracefully shutdown the manager."""
        logger.info("[PERSISTENT-INTELLIGENCE] Shutting down...")
        self._running = False

        # Cancel background tasks
        if self._sync_task:
            self._sync_task.cancel()
        if self._checkpoint_task:
            self._checkpoint_task.cancel()

        # Final sync
        if self._pending_sync:
            await self.sync()

        # Create final checkpoint
        await self.create_checkpoint("shutdown")

        # Close database
        if self._db:
            await self._db.close()

        logger.info("[PERSISTENT-INTELLIGENCE] Shutdown complete")


# Global instance
_manager: Optional[PersistentIntelligenceManager] = None


async def get_persistent_intelligence() -> PersistentIntelligenceManager:
    """Get or create the global PersistentIntelligenceManager instance."""
    global _manager

    if _manager is None:
        _manager = await PersistentIntelligenceManager.create()

    return _manager


async def shutdown_persistent_intelligence() -> None:
    """Shutdown the global manager."""
    global _manager

    if _manager:
        await _manager.shutdown()
        _manager = None
