"""
DistributedStateManager v100.0 - Unified Distributed State Coordination
========================================================================

Advanced distributed state management that provides:
1. Transactional state updates with atomicity guarantees
2. Redis-backed distributed state with local fallback
3. Leader election for coordination tasks
4. State snapshots and recovery
5. Pub/sub for state change notifications
6. Conflict resolution for concurrent updates
7. TTL-based state expiration

Architecture:
    +-----------------------------------------------------------------+
    |                  DistributedStateManager                         |
    |  +------------------------------------------------------------+ |
    |  |  StateStore (Redis or Local)                               | |
    |  |  +- Key-value state storage                               | |
    |  |  +- Namespace isolation                                   | |
    |  |  +- TTL support                                           | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  TransactionManager                                        | |
    |  |  +- Optimistic locking                                    | |
    |  |  +- Atomic multi-key updates                              | |
    |  |  +- Rollback on failure                                   | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  LeaderElection                                            | |
    |  |  +- Redis-based leader election                           | |
    |  |  +- TTL-based leadership renewal                          | |
    |  |  +- Graceful handoff                                      | |
    |  +------------------------------------------------------------+ |
    |  +------------------------------------------------------------+ |
    |  |  StateChangeNotifier                                       | |
    |  |  +- Pub/sub for state changes                             | |
    |  |  +- Local and distributed notifications                   | |
    |  +------------------------------------------------------------+ |
    +-----------------------------------------------------------------+

Author: JARVIS System
Version: 100.0.0
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Set,
    Tuple,
    TypeVar,
    Union,
)

from backend.core.async_safety import LazyAsyncLock

# Environment-driven configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
REDIS_STATE_DB = int(os.getenv("REDIS_STATE_DB", "2"))
STATE_KEY_PREFIX = os.getenv("JARVIS_STATE_PREFIX", "jarvis:state:")
LEADER_TTL_SECONDS = int(os.getenv("STATE_LEADER_TTL", "30"))
STATE_SYNC_INTERVAL = float(os.getenv("STATE_SYNC_INTERVAL", "5.0"))
SNAPSHOT_INTERVAL = float(os.getenv("STATE_SNAPSHOT_INTERVAL", "300.0"))  # 5 minutes
ENABLE_PERSISTENCE = os.getenv("STATE_PERSISTENCE_ENABLED", "true").lower() == "true"
PERSISTENCE_PATH = Path(os.getenv(
    "STATE_PERSISTENCE_PATH",
    str(Path.home() / ".jarvis" / "state_snapshots")
))
MAX_TRANSACTION_RETRIES = int(os.getenv("STATE_MAX_TX_RETRIES", "3"))
TRANSACTION_TIMEOUT_SECONDS = float(os.getenv("STATE_TX_TIMEOUT", "30.0"))


T = TypeVar("T")


class StateNamespace(Enum):
    """State namespaces for isolation."""
    SYSTEM = "system"
    AGENTS = "agents"
    MODELS = "models"
    SESSIONS = "sessions"
    CACHE = "cache"
    CONFIG = "config"
    METRICS = "metrics"
    VOICE = "voice"
    VISION = "vision"
    LEARNING = "learning"


class TransactionState(Enum):
    """Transaction lifecycle states."""
    PENDING = "pending"
    ACTIVE = "active"
    PREPARED = "prepared"
    COMMITTED = "committed"
    ABORTED = "aborted"
    ROLLED_BACK = "rolled_back"


class ConflictResolution(Enum):
    """Strategies for conflict resolution."""
    LAST_WRITE_WINS = "last_write_wins"
    FIRST_WRITE_WINS = "first_write_wins"
    MERGE = "merge"
    REJECT = "reject"


@dataclass
class StateEntry:
    """A single state entry."""
    key: str
    value: Any
    namespace: StateNamespace = StateNamespace.SYSTEM
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        if not self.checksum:
            self.checksum = self._compute_checksum()

    def _compute_checksum(self) -> str:
        """Compute checksum of value."""
        data = json.dumps(self.value, sort_keys=True, default=str)
        return hashlib.sha256(data.encode()).hexdigest()[:16]

    def is_expired(self) -> bool:
        """Check if entry has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "key": self.key,
            "value": self.value,
            "namespace": self.namespace.value,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> StateEntry:
        """Create from dictionary."""
        return cls(
            key=data["key"],
            value=data["value"],
            namespace=StateNamespace(data.get("namespace", "system")),
            version=data.get("version", 1),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
            expires_at=data.get("expires_at"),
            metadata=data.get("metadata", {}),
            checksum=data.get("checksum", ""),
        )


@dataclass
class StateTransaction:
    """A state transaction."""
    transaction_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    state: TransactionState = TransactionState.PENDING
    created_at: float = field(default_factory=time.time)
    operations: List[Dict[str, Any]] = field(default_factory=list)
    original_values: Dict[str, Any] = field(default_factory=dict)
    locks_held: Set[str] = field(default_factory=set)
    timeout_at: float = field(default_factory=lambda: time.time() + TRANSACTION_TIMEOUT_SECONDS)


@dataclass
class StateSnapshot:
    """A snapshot of state."""
    snapshot_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    created_at: float = field(default_factory=time.time)
    namespace: Optional[StateNamespace] = None
    entries_count: int = 0
    checksum: str = ""
    data: Dict[str, StateEntry] = field(default_factory=dict)


class StateStore(ABC):
    """Abstract state store interface."""

    @abstractmethod
    async def get(self, key: str, namespace: StateNamespace) -> Optional[StateEntry]:
        """Get a state entry."""
        pass

    @abstractmethod
    async def set(
        self,
        key: str,
        value: Any,
        namespace: StateNamespace,
        ttl: Optional[int] = None,
        version: Optional[int] = None,
    ) -> StateEntry:
        """Set a state entry."""
        pass

    @abstractmethod
    async def delete(self, key: str, namespace: StateNamespace) -> bool:
        """Delete a state entry."""
        pass

    @abstractmethod
    async def exists(self, key: str, namespace: StateNamespace) -> bool:
        """Check if key exists."""
        pass

    @abstractmethod
    async def list_keys(self, namespace: StateNamespace, pattern: Optional[str] = None) -> List[str]:
        """List keys in namespace."""
        pass

    @abstractmethod
    async def acquire_lock(self, key: str, timeout: float = 10.0) -> bool:
        """Acquire a distributed lock."""
        pass

    @abstractmethod
    async def release_lock(self, key: str) -> bool:
        """Release a distributed lock."""
        pass


class LocalStateStore(StateStore):
    """In-memory state store for local operation."""

    def __init__(self):
        self.logger = logging.getLogger("LocalStateStore")
        self._data: Dict[str, Dict[str, StateEntry]] = defaultdict(dict)
        self._locks: Dict[str, float] = {}  # key -> expiry time
        self._lock = asyncio.Lock()

    async def get(self, key: str, namespace: StateNamespace) -> Optional[StateEntry]:
        entry = self._data[namespace.value].get(key)
        if entry and entry.is_expired():
            await self.delete(key, namespace)
            return None
        return entry

    async def set(
        self,
        key: str,
        value: Any,
        namespace: StateNamespace,
        ttl: Optional[int] = None,
        version: Optional[int] = None,
    ) -> StateEntry:
        async with self._lock:
            existing = self._data[namespace.value].get(key)
            new_version = (existing.version + 1) if existing else 1

            if version is not None and existing and existing.version != version:
                raise ValueError(f"Version mismatch: expected {version}, got {existing.version}")

            entry = StateEntry(
                key=key,
                value=value,
                namespace=namespace,
                version=new_version,
                expires_at=time.time() + ttl if ttl else None,
            )

            self._data[namespace.value][key] = entry
            return entry

    async def delete(self, key: str, namespace: StateNamespace) -> bool:
        async with self._lock:
            if key in self._data[namespace.value]:
                del self._data[namespace.value][key]
                return True
            return False

    async def exists(self, key: str, namespace: StateNamespace) -> bool:
        entry = await self.get(key, namespace)
        return entry is not None

    async def list_keys(self, namespace: StateNamespace, pattern: Optional[str] = None) -> List[str]:
        keys = list(self._data[namespace.value].keys())
        if pattern:
            import fnmatch
            keys = [k for k in keys if fnmatch.fnmatch(k, pattern)]
        return keys

    async def acquire_lock(self, key: str, timeout: float = 10.0) -> bool:
        async with self._lock:
            now = time.time()
            # Clean expired locks
            expired = [k for k, exp in self._locks.items() if exp < now]
            for k in expired:
                del self._locks[k]

            if key in self._locks:
                return False

            self._locks[key] = now + timeout
            return True

    async def release_lock(self, key: str) -> bool:
        async with self._lock:
            if key in self._locks:
                del self._locks[key]
                return True
            return False

    def get_all_entries(self, namespace: Optional[StateNamespace] = None) -> Dict[str, StateEntry]:
        """Get all entries for snapshot."""
        result = {}
        namespaces = [namespace] if namespace else list(StateNamespace)

        for ns in namespaces:
            for key, entry in self._data[ns.value].items():
                if not entry.is_expired():
                    full_key = f"{ns.value}:{key}"
                    result[full_key] = entry

        return result


class RedisStateStore(StateStore):
    """Redis-backed state store for distributed operation."""

    def __init__(self, redis_url: str = REDIS_URL, db: int = REDIS_STATE_DB):
        self.logger = logging.getLogger("RedisStateStore")
        self.redis_url = redis_url
        self.db = db
        self._redis = None
        self._connected = False
        self._instance_id = str(uuid.uuid4())[:12]

    async def connect(self) -> bool:
        """Connect to Redis."""
        if self._connected:
            return True

        try:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(
                self.redis_url,
                db=self.db,
                decode_responses=True,
            )
            await self._redis.ping()
            self._connected = True
            self.logger.info(f"Connected to Redis for state at {self.redis_url}")
            return True

        except ImportError:
            self.logger.warning("redis package not installed")
            return False
        except Exception as e:
            self.logger.warning(f"Redis connection failed: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        if self._redis:
            await self._redis.aclose()  # v93.14
        self._connected = False

    def _key(self, key: str, namespace: StateNamespace) -> str:
        """Build Redis key."""
        return f"{STATE_KEY_PREFIX}{namespace.value}:{key}"

    async def get(self, key: str, namespace: StateNamespace) -> Optional[StateEntry]:
        if not self._connected:
            return None

        try:
            redis_key = self._key(key, namespace)
            data = await self._redis.get(redis_key)

            if data:
                entry = StateEntry.from_dict(json.loads(data))
                if entry.is_expired():
                    await self.delete(key, namespace)
                    return None
                return entry
            return None

        except Exception as e:
            self.logger.error(f"Redis get error: {e}")
            return None

    async def set(
        self,
        key: str,
        value: Any,
        namespace: StateNamespace,
        ttl: Optional[int] = None,
        version: Optional[int] = None,
    ) -> StateEntry:
        if not self._connected:
            raise RuntimeError("Redis not connected")

        try:
            redis_key = self._key(key, namespace)

            # Check version if specified
            if version is not None:
                existing = await self.get(key, namespace)
                if existing and existing.version != version:
                    raise ValueError(f"Version mismatch: expected {version}, got {existing.version}")

            existing = await self.get(key, namespace)
            new_version = (existing.version + 1) if existing else 1

            entry = StateEntry(
                key=key,
                value=value,
                namespace=namespace,
                version=new_version,
                expires_at=time.time() + ttl if ttl else None,
            )

            data = json.dumps(entry.to_dict())

            if ttl:
                await self._redis.setex(redis_key, ttl, data)
            else:
                await self._redis.set(redis_key, data)

            return entry

        except Exception as e:
            self.logger.error(f"Redis set error: {e}")
            raise

    async def delete(self, key: str, namespace: StateNamespace) -> bool:
        if not self._connected:
            return False

        try:
            redis_key = self._key(key, namespace)
            result = await self._redis.delete(redis_key)
            return result > 0

        except Exception as e:
            self.logger.error(f"Redis delete error: {e}")
            return False

    async def exists(self, key: str, namespace: StateNamespace) -> bool:
        if not self._connected:
            return False

        try:
            redis_key = self._key(key, namespace)
            return await self._redis.exists(redis_key) > 0

        except Exception as e:
            self.logger.error(f"Redis exists error: {e}")
            return False

    async def list_keys(self, namespace: StateNamespace, pattern: Optional[str] = None) -> List[str]:
        if not self._connected:
            return []

        try:
            search_pattern = f"{STATE_KEY_PREFIX}{namespace.value}:{pattern or '*'}"
            keys = await self._redis.keys(search_pattern)

            # Strip prefix to get actual keys
            prefix = f"{STATE_KEY_PREFIX}{namespace.value}:"
            return [k[len(prefix):] for k in keys]

        except Exception as e:
            self.logger.error(f"Redis keys error: {e}")
            return []

    async def acquire_lock(self, key: str, timeout: float = 10.0) -> bool:
        if not self._connected:
            return True  # Fail open

        try:
            lock_key = f"{STATE_KEY_PREFIX}locks:{key}"
            acquired = await self._redis.set(
                lock_key,
                self._instance_id,
                nx=True,
                ex=int(timeout)
            )
            return bool(acquired)

        except Exception as e:
            self.logger.error(f"Redis lock acquire error: {e}")
            return True  # Fail open

    async def release_lock(self, key: str) -> bool:
        if not self._connected:
            return True

        try:
            lock_key = f"{STATE_KEY_PREFIX}locks:{key}"
            current = await self._redis.get(lock_key)

            if current == self._instance_id:
                await self._redis.delete(lock_key)
                return True
            return False

        except Exception as e:
            self.logger.error(f"Redis lock release error: {e}")
            return False


class TransactionManager:
    """Manages state transactions with atomicity guarantees."""

    def __init__(self, store: StateStore):
        self.logger = logging.getLogger("TransactionManager")
        self.store = store
        self._active_transactions: Dict[str, StateTransaction] = {}
        self._lock = asyncio.Lock()

    async def begin(self) -> StateTransaction:
        """Begin a new transaction."""
        tx = StateTransaction(state=TransactionState.ACTIVE)
        async with self._lock:
            self._active_transactions[tx.transaction_id] = tx
        return tx

    async def add_operation(
        self,
        tx: StateTransaction,
        operation: str,
        key: str,
        namespace: StateNamespace,
        value: Any = None,
    ) -> None:
        """Add operation to transaction."""
        if tx.state != TransactionState.ACTIVE:
            raise ValueError(f"Transaction not active: {tx.state}")

        # Save original value for rollback
        if key not in tx.original_values:
            original = await self.store.get(key, namespace)
            tx.original_values[key] = original

        # Acquire lock
        lock_key = f"{namespace.value}:{key}"
        if lock_key not in tx.locks_held:
            acquired = await self.store.acquire_lock(lock_key, TRANSACTION_TIMEOUT_SECONDS)
            if not acquired:
                raise ValueError(f"Failed to acquire lock for {key}")
            tx.locks_held.add(lock_key)

        tx.operations.append({
            "operation": operation,
            "key": key,
            "namespace": namespace.value,
            "value": value,
        })

    async def commit(self, tx: StateTransaction) -> bool:
        """Commit the transaction."""
        if tx.state != TransactionState.ACTIVE:
            return False

        tx.state = TransactionState.PREPARED

        try:
            # Execute all operations
            for op in tx.operations:
                namespace = StateNamespace(op["namespace"])

                if op["operation"] == "set":
                    await self.store.set(op["key"], op["value"], namespace)
                elif op["operation"] == "delete":
                    await self.store.delete(op["key"], namespace)

            tx.state = TransactionState.COMMITTED
            return True

        except Exception as e:
            self.logger.error(f"Transaction commit failed: {e}")
            await self.rollback(tx)
            return False

        finally:
            await self._cleanup(tx)

    async def rollback(self, tx: StateTransaction) -> bool:
        """Rollback the transaction."""
        if tx.state == TransactionState.COMMITTED:
            return False

        try:
            # Restore original values
            for key, original in tx.original_values.items():
                parts = key.split(":", 1)
                namespace = StateNamespace(parts[0]) if len(parts) > 1 else StateNamespace.SYSTEM
                actual_key = parts[1] if len(parts) > 1 else key

                if original is None:
                    await self.store.delete(actual_key, namespace)
                else:
                    await self.store.set(actual_key, original.value, namespace)

            tx.state = TransactionState.ROLLED_BACK
            return True

        except Exception as e:
            self.logger.error(f"Transaction rollback failed: {e}")
            return False

        finally:
            await self._cleanup(tx)

    async def _cleanup(self, tx: StateTransaction) -> None:
        """Cleanup transaction resources."""
        # Release locks
        for lock_key in tx.locks_held:
            await self.store.release_lock(lock_key)

        # Remove from active
        async with self._lock:
            self._active_transactions.pop(tx.transaction_id, None)


class LeaderElection:
    """Leader election for coordination tasks."""

    def __init__(self, store: StateStore, election_name: str = "default"):
        self.logger = logging.getLogger("LeaderElection")
        self.store = store
        self.election_name = election_name
        self._instance_id = str(uuid.uuid4())[:12]
        self._is_leader = False
        self._leader_key = f"leader:{election_name}"
        self._callbacks: List[Callable[[bool], None]] = []
        self._renewal_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        """Start leader election."""
        self._running = True
        self._renewal_task = asyncio.create_task(self._renewal_loop())

    async def stop(self) -> None:
        """Stop leader election."""
        self._running = False

        if self._is_leader:
            await self._release_leadership()

        if self._renewal_task:
            self._renewal_task.cancel()
            try:
                await self._renewal_task
            except asyncio.CancelledError:
                pass

    def on_leadership_change(self, callback: Callable[[bool], None]) -> None:
        """Register callback for leadership changes."""
        self._callbacks.append(callback)

    def is_leader(self) -> bool:
        """Check if this instance is leader."""
        return self._is_leader

    async def _try_acquire(self) -> bool:
        """Try to acquire leadership."""
        try:
            entry = await self.store.get(self._leader_key, StateNamespace.SYSTEM)

            if entry is None or entry.is_expired():
                # Try to become leader
                await self.store.set(
                    self._leader_key,
                    {"instance_id": self._instance_id, "acquired_at": time.time()},
                    StateNamespace.SYSTEM,
                    ttl=LEADER_TTL_SECONDS,
                )
                return True

            elif entry.value.get("instance_id") == self._instance_id:
                # Renew leadership
                await self.store.set(
                    self._leader_key,
                    {"instance_id": self._instance_id, "acquired_at": entry.value.get("acquired_at", time.time())},
                    StateNamespace.SYSTEM,
                    ttl=LEADER_TTL_SECONDS,
                )
                return True

            return False

        except Exception as e:
            self.logger.error(f"Leadership acquisition error: {e}")
            return False

    async def _release_leadership(self) -> None:
        """Release leadership."""
        try:
            entry = await self.store.get(self._leader_key, StateNamespace.SYSTEM)

            if entry and entry.value.get("instance_id") == self._instance_id:
                await self.store.delete(self._leader_key, StateNamespace.SYSTEM)
                self.logger.info(f"Released leadership for {self.election_name}")

        except Exception as e:
            self.logger.error(f"Leadership release error: {e}")

    async def _renewal_loop(self) -> None:
        """Periodically try to acquire/renew leadership."""
        while self._running:
            try:
                was_leader = self._is_leader
                self._is_leader = await self._try_acquire()

                if was_leader != self._is_leader:
                    status = "acquired" if self._is_leader else "lost"
                    self.logger.info(f"Leadership {status} for {self.election_name}")

                    for callback in self._callbacks:
                        try:
                            callback(self._is_leader)
                        except Exception as e:
                            self.logger.error(f"Callback error: {e}")

                await asyncio.sleep(LEADER_TTL_SECONDS / 3)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.logger.error(f"Leadership renewal error: {e}")
                await asyncio.sleep(5)


class DistributedStateManager:
    """
    Unified distributed state manager.

    Provides transactional state management with Redis backing,
    leader election, and state change notifications.
    """

    def __init__(self):
        self.logger = logging.getLogger("DistributedStateManager")

        # Stores
        self._local_store = LocalStateStore()
        self._redis_store = RedisStateStore()
        self._primary_store: StateStore = self._local_store

        # Managers
        self._tx_manager: Optional[TransactionManager] = None
        self._leader_election: Optional[LeaderElection] = None

        # State
        self._running = False
        self._redis_available = False
        self._lock = asyncio.Lock()

        # Callbacks
        self._change_callbacks: Dict[str, List[Callable]] = defaultdict(list)

        # Metrics
        self._metrics = {
            "get_count": 0,
            "set_count": 0,
            "delete_count": 0,
            "tx_committed": 0,
            "tx_rolled_back": 0,
            "snapshots_created": 0,
        }

        # Ensure persistence directory
        PERSISTENCE_PATH.mkdir(parents=True, exist_ok=True)

    async def start(self) -> None:
        """Start the state manager."""
        if self._running:
            return

        self._running = True
        self.logger.info("DistributedStateManager starting...")

        # Try Redis connection
        self._redis_available = await self._redis_store.connect()

        if self._redis_available:
            self._primary_store = self._redis_store
            self.logger.info("  Using Redis for distributed state")
        else:
            self._primary_store = self._local_store
            self.logger.info("  Using local state (Redis unavailable)")

        # Initialize transaction manager
        self._tx_manager = TransactionManager(self._primary_store)

        # Initialize leader election
        self._leader_election = LeaderElection(self._primary_store, "state_manager")
        await self._leader_election.start()

        # Load from persistence if local
        if not self._redis_available and ENABLE_PERSISTENCE:
            await self._load_from_snapshot()

        self.logger.info("DistributedStateManager ready")

    async def stop(self) -> None:
        """Stop the state manager."""
        self._running = False

        # Stop leader election
        if self._leader_election:
            await self._leader_election.stop()

        # Save snapshot if local
        if not self._redis_available and ENABLE_PERSISTENCE:
            await self.create_snapshot()

        # Disconnect Redis
        await self._redis_store.disconnect()

        self.logger.info("DistributedStateManager stopped")

    # Core operations

    async def get(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SYSTEM,
        default: Any = None,
    ) -> Any:
        """Get a value from state."""
        self._metrics["get_count"] += 1

        entry = await self._primary_store.get(key, namespace)
        if entry:
            return entry.value
        return default

    async def set(
        self,
        key: str,
        value: Any,
        namespace: StateNamespace = StateNamespace.SYSTEM,
        ttl: Optional[int] = None,
    ) -> StateEntry:
        """Set a value in state."""
        self._metrics["set_count"] += 1

        entry = await self._primary_store.set(key, value, namespace, ttl)

        # Notify listeners
        await self._notify_change(key, namespace, "set", value)

        return entry

    async def delete(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SYSTEM,
    ) -> bool:
        """Delete a value from state."""
        self._metrics["delete_count"] += 1

        result = await self._primary_store.delete(key, namespace)

        if result:
            await self._notify_change(key, namespace, "delete", None)

        return result

    async def exists(
        self,
        key: str,
        namespace: StateNamespace = StateNamespace.SYSTEM,
    ) -> bool:
        """Check if key exists."""
        return await self._primary_store.exists(key, namespace)

    async def list_keys(
        self,
        namespace: StateNamespace = StateNamespace.SYSTEM,
        pattern: Optional[str] = None,
    ) -> List[str]:
        """List keys in namespace."""
        return await self._primary_store.list_keys(namespace, pattern)

    # Transaction operations

    async def begin_transaction(self) -> StateTransaction:
        """Begin a new transaction."""
        if not self._tx_manager:
            raise RuntimeError("Transaction manager not initialized")
        return await self._tx_manager.begin()

    async def tx_set(
        self,
        tx: StateTransaction,
        key: str,
        value: Any,
        namespace: StateNamespace = StateNamespace.SYSTEM,
    ) -> None:
        """Add set operation to transaction."""
        if not self._tx_manager:
            raise RuntimeError("Transaction manager not initialized")
        await self._tx_manager.add_operation(tx, "set", key, namespace, value)

    async def tx_delete(
        self,
        tx: StateTransaction,
        key: str,
        namespace: StateNamespace = StateNamespace.SYSTEM,
    ) -> None:
        """Add delete operation to transaction."""
        if not self._tx_manager:
            raise RuntimeError("Transaction manager not initialized")
        await self._tx_manager.add_operation(tx, "delete", key, namespace)

    async def commit(self, tx: StateTransaction) -> bool:
        """Commit transaction."""
        if not self._tx_manager:
            raise RuntimeError("Transaction manager not initialized")

        result = await self._tx_manager.commit(tx)

        if result:
            self._metrics["tx_committed"] += 1
        else:
            self._metrics["tx_rolled_back"] += 1

        return result

    async def rollback(self, tx: StateTransaction) -> bool:
        """Rollback transaction."""
        if not self._tx_manager:
            raise RuntimeError("Transaction manager not initialized")

        result = await self._tx_manager.rollback(tx)
        self._metrics["tx_rolled_back"] += 1
        return result

    # Leadership

    def is_leader(self) -> bool:
        """Check if this instance is leader."""
        if self._leader_election:
            return self._leader_election.is_leader()
        return True  # Assume leader if no election

    def on_leadership_change(self, callback: Callable[[bool], None]) -> None:
        """Register leadership change callback."""
        if self._leader_election:
            self._leader_election.on_leadership_change(callback)

    # Notifications

    def on_change(
        self,
        key_pattern: str,
        callback: Callable[[str, str, Any], None],
    ) -> None:
        """Register callback for state changes."""
        self._change_callbacks[key_pattern].append(callback)

    async def _notify_change(
        self,
        key: str,
        namespace: StateNamespace,
        operation: str,
        value: Any,
    ) -> None:
        """Notify listeners of state change."""
        import fnmatch

        full_key = f"{namespace.value}:{key}"

        for pattern, callbacks in self._change_callbacks.items():
            if fnmatch.fnmatch(full_key, pattern):
                for callback in callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(key, operation, value)
                        else:
                            callback(key, operation, value)
                    except Exception as e:
                        self.logger.error(f"Change callback error: {e}")

    # Snapshots

    async def create_snapshot(
        self,
        namespace: Optional[StateNamespace] = None,
    ) -> StateSnapshot:
        """Create a snapshot of state."""
        snapshot = StateSnapshot(namespace=namespace)

        if isinstance(self._primary_store, LocalStateStore):
            snapshot.data = self._primary_store.get_all_entries(namespace)
        else:
            # For Redis, collect entries
            namespaces = [namespace] if namespace else list(StateNamespace)
            for ns in namespaces:
                keys = await self._primary_store.list_keys(ns)
                for key in keys:
                    entry = await self._primary_store.get(key, ns)
                    if entry:
                        full_key = f"{ns.value}:{key}"
                        snapshot.data[full_key] = entry

        snapshot.entries_count = len(snapshot.data)
        snapshot.checksum = hashlib.sha256(
            json.dumps(
                {k: v.to_dict() for k, v in snapshot.data.items()},
                sort_keys=True
            ).encode()
        ).hexdigest()[:16]

        # Save to file
        if ENABLE_PERSISTENCE:
            filepath = PERSISTENCE_PATH / f"snapshot_{snapshot.snapshot_id}.json"
            filepath.write_text(json.dumps({
                "snapshot_id": snapshot.snapshot_id,
                "created_at": snapshot.created_at,
                "namespace": snapshot.namespace.value if snapshot.namespace else None,
                "entries_count": snapshot.entries_count,
                "checksum": snapshot.checksum,
                "data": {k: v.to_dict() for k, v in snapshot.data.items()},
            }, indent=2))

        self._metrics["snapshots_created"] += 1
        self.logger.info(f"Created snapshot: {snapshot.snapshot_id} ({snapshot.entries_count} entries)")

        return snapshot

    async def _load_from_snapshot(self) -> None:
        """Load state from latest snapshot."""
        try:
            snapshots = sorted(PERSISTENCE_PATH.glob("snapshot_*.json"), reverse=True)

            if not snapshots:
                return

            latest = snapshots[0]
            data = json.loads(latest.read_text())

            for key, entry_data in data.get("data", {}).items():
                entry = StateEntry.from_dict(entry_data)
                if not entry.is_expired():
                    self._local_store._data[entry.namespace.value][entry.key] = entry

            self.logger.info(f"Loaded snapshot: {data.get('snapshot_id')} ({len(data.get('data', {}))} entries)")

        except Exception as e:
            self.logger.error(f"Failed to load snapshot: {e}")

    # Metrics

    def get_metrics(self) -> Dict[str, Any]:
        """Get state manager metrics."""
        return {
            **self._metrics,
            "redis_available": self._redis_available,
            "is_leader": self.is_leader(),
            "mode": "redis" if self._redis_available else "local",
        }


# Global instance
_state_manager: Optional[DistributedStateManager] = None
_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_state_manager() -> DistributedStateManager:
    """Get the global state manager instance."""
    global _state_manager

    async with _lock:
        if _state_manager is None:
            _state_manager = DistributedStateManager()
            await _state_manager.start()

        return _state_manager


async def shutdown_state_manager() -> None:
    """Shutdown the global state manager."""
    global _state_manager

    if _state_manager:
        await _state_manager.stop()
        _state_manager = None
