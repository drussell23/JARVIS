"""
Distributed Lock Manager for Cross-Repo Coordination v3.0
===========================================================

Production-grade distributed lock manager for coordinating operations across
JARVIS, JARVIS-Prime, and Reactor-Core repositories.

Features:
- File-based locks with automatic expiration
- Stale lock detection and cleanup
- Lock timeout and retry logic
- Deadlock prevention with TTL (time-to-live)
- Lock health monitoring
- Zero-config operation with sensible defaults
- Async-first API
- v2.0: UUID-based temp files (fixes race conditions)
- v2.0: Per-lock-name async semaphores (prevents concurrent write collisions)
- v2.0: Exponential backoff with jitter (reduces contention)
- v2.0: Orphaned temp file cleanup (prevents disk clutter)
- v2.0: Cross-repo coordination improvements
- v3.0: Redis backend support for distributed environments
- v3.0: Automatic backend selection (Redis → File fallback)
- v3.0: Cross-repo lock bridge for JARVIS-Prime and Reactor-Core
- v3.0: Fencing tokens for monotonic ordering
- v3.0: Lock lease extension (keepalive)

Problem Solved:
    Before: Process crashes while holding lock → other processes blocked forever
    After: Locks auto-expire after TTL → stale locks cleaned up automatically

    Before (v2.0): Only file-based locks, limited to single machine
    After (v3.0): Redis + File hybrid, works across VMs, GCP instances, Docker

Example Usage:
    ```python
    lock_manager = DistributedLockManager()

    # Acquire lock with auto-expiration
    async with lock_manager.acquire("vbia_events", timeout=5.0, ttl=10.0) as acquired:
        if acquired:
            # Perform critical operation
            await update_vbia_events()
        else:
            logger.warning("Could not acquire lock")
    ```

Architecture:
    v3.0 Unified Cross-Repo Lock System:

    ┌─────────────────────────────────────────────────────────────────────────┐
    │                      UNIFIED LOCK MANAGER v3.0                          │
    │                                                                         │
    │  ┌───────────────────┐       ┌───────────────────┐                      │
    │  │  Redis Backend    │  OR   │   File Backend    │                      │
    │  │  (Distributed)    │ ←──→  │   (Local/NFS)     │                      │
    │  │                   │       │                   │                      │
    │  │  - Fencing tokens │       │  - JSON metadata  │                      │
    │  │  - Keepalive      │       │  - PID validation │                      │
    │  │  - SETNX + TTL    │       │  - Atomic rename  │                      │
    │  └───────────────────┘       └───────────────────┘                      │
    │           ↑                           ↑                                 │
    │           │   Automatic Fallback      │                                 │
    │           └───────────┬───────────────┘                                 │
    │                       │                                                 │
    │  ┌────────────────────┴────────────────────┐                            │
    │  │         Lock Coordinator                │                            │
    │  │  - Backend selection                    │                            │
    │  │  - Cross-repo coordination              │                            │
    │  │  - Health monitoring                    │                            │
    │  └────────────────────┬────────────────────┘                            │
    │                       │                                                 │
    │  ┌────────────┬───────┴───────┬────────────┐                            │
    │  │  JARVIS    │  JARVIS-Prime │ Reactor    │                            │
    │  │  (Body)    │  (Mind)       │ Core       │                            │
    │  └────────────┴───────────────┴────────────┘                            │
    └─────────────────────────────────────────────────────────────────────────┘

    File-based locks (fallback):
    ~/.jarvis/cross_repo/locks/
    ├── vbia_events.dlm.lock     (Lock file with metadata)
    │   {
    │     "acquired_at": 1736895345.123,
    │     "expires_at": 1736895355.123,
    │     "owner": "jarvis-12345-1736895300.5",
    │     "token": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    │     "fencing_token": 42,
    │     "backend": "file"
    │   }
    ├── prime_state.dlm.lock
    └── reactor_training.dlm.lock

Author: JARVIS AI System
Version: 3.0.0

v2.0.0 CRITICAL FIX (Race Condition Resolution):
    Before: Multiple async tasks used same temp file path `{lock}.tmp.{pid}`
            This caused "Temp file size mismatch" and "No such file" errors
            when concurrent tasks stepped on each other's temp files.
    After: Each write uses UUID-based temp file + per-lock semaphore
           Eliminates all inter-task race conditions.

v3.0.0 CROSS-REPO UNIFICATION:
    Before: JARVIS used file locks, Reactor-Core used Redis locks, no coordination.
    After: Unified lock manager with automatic backend selection:
           - Redis available → Use Redis (distributed across machines)
           - Redis unavailable → Fall back to file locks (single machine)
           - Both repos can import this module for consistent locking
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import random
import socket
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

import aiofiles
import aiofiles.os

logger = logging.getLogger(__name__)


# =============================================================================
# v3.5: Env-var helpers + async executor bridge for blocking syscalls
# =============================================================================

def _env_float(name: str, default: float) -> float:
    """Read a float env var with fallback."""
    try:
        return float(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    """Read an int env var with fallback."""
    try:
        return int(os.environ.get(name, str(default)))
    except (ValueError, TypeError):
        return default


# v3.5: Cleanup budget limits — prevents event loop starvation from
# blocking syscalls (os.kill, psutil.Process) during initialization.
DLM_CLEANUP_TEMP_TIMEOUT = _env_float("DLM_CLEANUP_TEMP_TIMEOUT", 5.0)
DLM_CLEANUP_STALE_TIMEOUT = _env_float("DLM_CLEANUP_STALE_TIMEOUT", 10.0)
DLM_CLEANUP_MAX_FILES = _env_int("DLM_CLEANUP_MAX_FILES", 200)
DLM_BLOCKING_OP_TIMEOUT = _env_float("DLM_BLOCKING_OP_TIMEOUT", 5.0)
DLM_REDIS_CONNECT_TIMEOUT = _env_float("DLM_REDIS_CONNECT_TIMEOUT", 5.0)
DLM_REDIS_SOCKET_TIMEOUT = _env_float("DLM_REDIS_SOCKET_TIMEOUT", 5.0)


async def _run_blocking(func, *args, timeout: float = DLM_BLOCKING_OP_TIMEOUT):
    """Run a synchronous blocking function in an executor with timeout.

    Prevents os.kill(), psutil.Process(), os.fsync(), etc. from blocking
    the asyncio event loop. Returns None on timeout instead of raising.
    """
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, func, *args),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[v3.5] Blocking op timed out ({timeout}s): "
            f"{getattr(func, '__name__', str(func))}"
        )
        return None
    except Exception as e:
        logger.debug(f"[v3.5] Blocking op error: {e}")
        return None


# =============================================================================
# v3.0: Backend Configuration
# =============================================================================

# Redis configuration from environment
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)
REDIS_LOCK_DB = int(os.getenv("REDIS_LOCK_DB", "1"))  # Separate DB for locks

# Lock configuration from environment
DEFAULT_LOCK_TTL = float(os.getenv("DISTRIBUTED_LOCK_TTL", "10.0"))
DEFAULT_ACQUIRE_TIMEOUT = float(os.getenv("DISTRIBUTED_LOCK_TIMEOUT", "5.0"))
DEFAULT_RETRY_INTERVAL = float(os.getenv("DISTRIBUTED_LOCK_RETRY_INTERVAL", "0.1"))
KEEPALIVE_INTERVAL = float(os.getenv("DISTRIBUTED_LOCK_KEEPALIVE", "3.0"))

# Cross-repo lock key prefixes
LOCK_PREFIX = "jarvis:lock:"
TRINITY_LOCK_PREFIX = f"{LOCK_PREFIX}trinity:"


class LockBackend(Enum):
    """v3.0: Available lock backends."""
    REDIS = "redis"
    FILE = "file"
    AUTO = "auto"  # Automatic selection: Redis if available, else file


class LockState(Enum):
    """v3.0: States of a distributed lock."""
    RELEASED = "released"
    ACQUIRING = "acquiring"
    ACQUIRED = "acquired"
    EXTENDING = "extending"
    FAILED = "failed"


# =============================================================================
# v3.0: Redis Client Wrapper
# =============================================================================

class AsyncRedisLockClient:
    """
    v3.0: Async Redis client wrapper for distributed locks.

    Falls back gracefully when Redis is unavailable.
    Thread-safe singleton pattern.
    """

    _instance: Optional["AsyncRedisLockClient"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "AsyncRedisLockClient":
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.host = REDIS_HOST
        self.port = REDIS_PORT
        self.password = REDIS_PASSWORD
        self.db = REDIS_LOCK_DB
        self._client = None
        self._available = False
        self._connect_lock = asyncio.Lock()
        self._last_check = 0.0
        self._check_interval = 30.0  # Re-check availability every 30s
        self._initialized = True

    async def connect(self) -> bool:
        """Connect to Redis. Returns True if successful."""
        async with self._connect_lock:
            if self._client is not None and self._available:
                return True

            # Rate limit connection attempts
            now = time.time()
            if now - self._last_check < self._check_interval and not self._available:
                return False
            self._last_check = now

            try:
                # Try importing redis-py async (preferred) or aioredis (legacy)
                redis_module = None
                try:
                    import redis.asyncio as redis_module  # type: ignore
                except ImportError:
                    try:
                        import aioredis as redis_module  # type: ignore
                    except ImportError:
                        logger.debug("[v3.0] Redis client not available (redis/aioredis not installed)")
                        self._available = False
                        return False

                url = f"redis://{self.host}:{self.port}/{self.db}"
                # v3.5: Explicit socket timeouts prevent hanging on unreachable Redis
                self._client = await redis_module.from_url(
                    url,
                    password=self.password,
                    encoding="utf-8",
                    decode_responses=True,
                    socket_timeout=DLM_REDIS_SOCKET_TIMEOUT,
                    socket_connect_timeout=DLM_REDIS_CONNECT_TIMEOUT,
                )

                # Test connection with timeout
                await asyncio.wait_for(
                    self._client.ping(),
                    timeout=DLM_REDIS_CONNECT_TIMEOUT,
                )
                self._available = True
                logger.info(f"[v3.0] Connected to Redis at {self.host}:{self.port}")
                return True

            except Exception as e:
                logger.debug(f"[v3.0] Redis connection failed: {e}")
                self._available = False
                self._client = None
                return False

    async def disconnect(self) -> None:
        """Disconnect from Redis."""
        async with self._connect_lock:
            if self._client:
                try:
                    await self._client.close()
                except Exception:
                    pass
                self._client = None
                self._available = False

    @property
    def is_available(self) -> bool:
        return self._available and self._client is not None

    async def set_nx(self, key: str, value: str, ttl_seconds: float) -> bool:
        """Set if not exists with TTL (SETNX + EXPIRE)."""
        if not self.is_available or self._client is None:
            return False

        try:
            result = await self._client.set(key, value, nx=True, ex=int(ttl_seconds))
            return result is not None
        except Exception as e:
            logger.debug(f"[v3.0] Redis SETNX failed: {e}")
            self._available = False
            return False

    async def get(self, key: str) -> Optional[str]:
        """Get value by key."""
        if not self.is_available or self._client is None:
            return None

        try:
            result = await self._client.get(key)
            return str(result) if result else None
        except Exception as e:
            logger.debug(f"[v3.0] Redis GET failed: {e}")
            return None

    async def delete(self, key: str) -> bool:
        """Delete key."""
        if not self.is_available or self._client is None:
            return False

        try:
            result = await self._client.delete(key)
            return int(result) > 0
        except Exception as e:
            logger.debug(f"[v3.0] Redis DELETE failed: {e}")
            return False

    async def expire(self, key: str, ttl_seconds: int) -> bool:
        """Set TTL on key."""
        if not self.is_available or self._client is None:
            return False

        try:
            result = await self._client.expire(key, ttl_seconds)
            return bool(result)
        except Exception as e:
            logger.debug(f"[v3.0] Redis EXPIRE failed: {e}")
            return False

    async def incr(self, key: str) -> int:
        """Increment and return new value (for fencing tokens)."""
        if not self.is_available or self._client is None:
            return 0

        try:
            result = await self._client.incr(key)
            return int(result)
        except Exception as e:
            logger.debug(f"[v3.0] Redis INCR failed: {e}")
            return 0


# Global Redis client instance
_redis_client: Optional[AsyncRedisLockClient] = None


def get_redis_client() -> AsyncRedisLockClient:
    """Get the global Redis client instance."""
    global _redis_client
    if _redis_client is None:
        _redis_client = AsyncRedisLockClient()
    return _redis_client


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class LockConfig:
    """
    Configuration for distributed lock manager.

    v96.2: Lock files now use `.dlm.lock` extension to avoid collision with
    flock-based locks from other systems (e.g., AtomicFileWriter in unified_loop_manager.py).

    v3.0: Added backend selection, Redis configuration, and cross-repo settings.

    Lock File Naming:
    - DistributedLockManager: {lock_name}.dlm.lock (JSON metadata)
    - Other systems (flock): {name}.lock (empty flock files)

    This prevents "corrupted lock file" errors when both systems share the same lock directory.
    """
    # Lock directory
    lock_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "cross_repo" / "locks")

    # v96.2: Lock file extension - distinct from flock-based locks
    lock_extension: str = ".dlm.lock"

    # Default lock timeout (how long to wait for lock acquisition)
    default_timeout_seconds: float = DEFAULT_ACQUIRE_TIMEOUT

    # Default lock TTL (how long lock is valid before auto-expiration)
    default_ttl_seconds: float = DEFAULT_LOCK_TTL

    # Retry settings
    retry_delay_seconds: float = DEFAULT_RETRY_INTERVAL

    # Stale lock cleanup settings
    cleanup_enabled: bool = True
    cleanup_interval_seconds: float = 30.0

    # Lock health check
    health_check_enabled: bool = True

    # v3.0: Backend selection
    backend: LockBackend = LockBackend.AUTO  # AUTO tries Redis first, falls back to file

    # v3.0: Redis configuration
    redis_host: str = REDIS_HOST
    redis_port: int = REDIS_PORT
    redis_db: int = REDIS_LOCK_DB
    redis_password: Optional[str] = REDIS_PASSWORD

    # v3.0: Cross-repo identification
    repo_source: str = field(default_factory=lambda: os.getenv("JARVIS_REPO_SOURCE", "jarvis"))

    # v3.0: Keepalive settings (for long-running operations)
    keepalive_enabled: bool = True
    keepalive_interval_seconds: float = KEEPALIVE_INTERVAL

    # v3.0: Fencing token counter file (for file-based backend)
    fencing_counter_file: Path = field(
        default_factory=lambda: Path.home() / ".jarvis" / "cross_repo" / "locks" / ".fencing_counter"
    )


# =============================================================================
# Lock Metadata
# =============================================================================

@dataclass
class LockMetadata:
    """
    v3.0: Enhanced metadata stored in lock file.

    CRITICAL FIX for Problem 19 (Startup lock not released on crash):
    - Added process_start_time for PID reuse detection
    - Added process_name and process_cmdline for identity validation
    - Added machine_id for distributed environments
    - Enhanced stale detection with multiple signals

    v3.0 Additions:
    - backend: Which backend was used to acquire the lock (redis/file)
    - fencing_token: Monotonically increasing token for ordering
    - repo_source: Which repo acquired the lock (jarvis/jarvis-prime/reactor-core)
    - extensions: Number of times the lock TTL was extended

    Lock File Format (v3.0):
    {
        "acquired_at": 1736895345.123,
        "expires_at": 1736895355.123,
        "owner": "jarvis-12345-1736895300.5",
        "token": "f47ac10b-58cc-4372-a567...",
        "lock_name": "vbia_events",
        "process_start_time": 1736895300.5,
        "process_name": "python3",
        "process_cmdline": "python3 -m ...",
        "machine_id": "darwin-hostname",
        "backend": "redis",                   # v3.0
        "fencing_token": 42,                  # v3.0
        "repo_source": "jarvis",              # v3.0
        "extensions": 0                       # v3.0
    }
    """
    acquired_at: float  # Timestamp when lock was acquired
    expires_at: float  # Timestamp when lock expires (acquired_at + TTL)
    owner: str  # Process identifier (e.g., "jarvis-{pid}-{start_time}")
    token: str  # Unique token for this lock instance
    lock_name: str  # Name of the locked resource

    # v96.0: Enhanced process tracking for PID reuse detection
    process_start_time: float = 0.0    # Process creation timestamp
    process_name: str = ""              # Process name (e.g., "python3")
    process_cmdline: str = ""           # Command line (truncated)
    machine_id: str = ""                # Machine identifier

    # v3.0: Cross-repo and backend tracking
    backend: str = "file"               # Which backend: "redis" or "file"
    fencing_token: int = 0              # Monotonically increasing token
    repo_source: str = "jarvis"         # Which repo: jarvis, jarvis-prime, reactor-core
    extensions: int = 0                 # Number of TTL extensions

    def is_expired(self) -> bool:
        """Check if lock has expired."""
        return time.time() >= self.expires_at

    def is_stale(self) -> bool:
        """Check if lock is stale (expired for significant time)."""
        return time.time() >= self.expires_at + 5.0

    def time_remaining(self) -> float:
        """Get remaining time before expiration (negative if expired)."""
        return self.expires_at - time.time()

    def get_pid(self) -> int:
        """
        v96.0: Extract PID from owner string.

        Supports both old format "jarvis-{pid}" and new format "jarvis-{pid}-{start_time}".
        """
        try:
            parts = self.owner.split("-")
            if len(parts) >= 2:
                return int(parts[1])
        except (ValueError, IndexError):
            pass
        return 0

    def get_owner_start_time(self) -> float:
        """
        v96.0: Extract process start time from owner string.

        Returns 0.0 if using old format without start time.
        """
        try:
            parts = self.owner.split("-")
            if len(parts) >= 3:
                return float(parts[2])
        except (ValueError, IndexError):
            pass
        # Fall back to stored process_start_time
        return self.process_start_time


# =============================================================================
# Distributed Lock Manager
# =============================================================================

class DistributedLockManager:
    """
    v3.0: Production-grade distributed lock manager for cross-repo coordination.

    Features:
    - Automatic lock expiration (TTL-based)
    - Stale lock detection and cleanup
    - Deadlock prevention
    - Lock renewal support
    - Process-safe across multiple repos
    - v93.0: Aggressive pre-cleanup on initialization
    - v93.0: PID validation for stale lock detection
    - v93.0: Faster acquisition with optimistic locking
    - v96.0: Process start time tracking for PID reuse detection (Problem 19 fix)
    - v96.0: Process fingerprint in lock metadata
    - v96.0: Machine ID tracking for distributed environments
    - v96.0: Enhanced owner ID format: "jarvis-{pid}-{start_time}"
    - v3.0: Redis backend support with automatic fallback
    - v3.0: Fencing tokens for monotonic ordering
    - v3.0: Lock keepalive for long-running operations
    - v3.0: Cross-repo identification (jarvis/jarvis-prime/reactor-core)

    CRITICAL FIX (v96.0) - Problem 19: Startup lock not released on crash
    ════════════════════════════════════════════════════════════════════════
    Before: Process crash → PID reused → stale lock appears valid → deadlock
    After: Process start time validated → PID reuse detected → stale lock cleaned

    v3.0 CROSS-REPO UNIFICATION:
    ════════════════════════════════════════════════════════════════════════
    Before: Each repo had its own locking mechanism
    After: Unified lock manager with Redis (distributed) + File (fallback)
    """

    def __init__(self, config: Optional[LockConfig] = None):
        """Initialize distributed lock manager."""
        self.config = config or LockConfig()
        self._cleanup_task: Optional[asyncio.Task] = None

        # v96.0: Enhanced owner ID with process start time for PID reuse detection
        self._process_start_time = self._get_own_process_start_time()
        self._owner_id = f"{self.config.repo_source}-{os.getpid()}-{self._process_start_time:.1f}"
        self._process_name = self._get_own_process_name()
        self._process_cmdline = self._get_own_process_cmdline()
        self._machine_id = self._get_machine_id()

        self._initialized = False
        self._init_lock = asyncio.Lock()

        # v2.0: Per-lock-name semaphores to prevent concurrent write collisions
        # Within the same process, only one task can write to a specific lock at a time
        self._lock_semaphores: Dict[str, asyncio.Semaphore] = {}
        self._semaphore_lock = asyncio.Lock()

        # v2.0: Counter for unique temp file naming (backup to UUID)
        self._write_counter = 0

        # v3.0: Backend selection state
        self._active_backend: LockBackend = LockBackend.FILE
        self._redis_client: Optional[AsyncRedisLockClient] = None
        self._redis_available = False

        # v3.2: Redis availability caching — prevents log spam and wasted connect attempts
        self._redis_last_check_time: float = 0.0
        self._redis_retry_interval: float = float(
            os.environ.get("JARVIS_REDIS_RETRY_INTERVAL", "300")
        )

        # v3.0: Fencing token counter (monotonically increasing)
        self._fencing_token_counter = 0
        self._fencing_lock = asyncio.Lock()

        # v3.0: Active keepalive tasks (lock_name -> task)
        self._keepalive_tasks: Dict[str, asyncio.Task] = {}

        # v3.0: Callbacks for lock events
        self._on_lock_acquired: List[Callable[[str, LockMetadata], None]] = []
        self._on_lock_released: List[Callable[[str], None]] = []
        self._on_lock_failed: List[Callable[[str, str], None]] = []

        # v3.2: Keepalive exhaustion tracking
        self._on_keepalive_failed: List[Callable[[str, str], None]] = []
        self._expired_locks: set = set()
        self._lock_events: Dict[str, asyncio.Event] = {}

        # v3.4: Keepalive stopping flag — set synchronously BEFORE any yield in
        # the finally block of acquire_unified(). The keepalive loop checks this
        # flag before writing metadata, preventing stale token writes during the
        # window between cancellation signal and actual task termination.
        self._keepalive_stopping: set = set()

        logger.info(
            f"Distributed Lock Manager v3.2 initialized "
            f"(owner: {self._owner_id}, backend: {self.config.backend.value})"
        )

    async def _ensure_directory(self, directory: Path) -> None:
        """Ensure a directory exists (self-healing against runtime cleanup races)."""
        try:
            await aiofiles.os.makedirs(directory, exist_ok=True)
        except FileExistsError:
            return
        except OSError as e:
            if e.errno == errno.EEXIST:
                return
            raise

    async def _ensure_lock_storage_ready(self) -> None:
        """Ensure lock storage paths exist before lock I/O operations."""
        await self._ensure_directory(self.config.lock_dir)
        counter_parent = self.config.fencing_counter_file.parent
        if counter_parent != self.config.lock_dir:
            await self._ensure_directory(counter_parent)

    # -----------------------------------------------------------------
    # v3.2: Keepalive exhaustion public API
    # -----------------------------------------------------------------

    def on_keepalive_failed(self, callback: Callable[[str, str], None]) -> None:
        """v3.2: Register a callback fired when keepalive exhausts retries.

        Args:
            callback: Receives (lock_name: str, reason: str)
        """
        self._on_keepalive_failed.append(callback)

    def is_lock_expired(self, lock_name: str) -> bool:
        """v3.2: Check if a lock's keepalive has failed and the lock was auto-released.

        Callers inside ``async with acquire()`` can poll this to detect stale state.
        """
        return lock_name in self._expired_locks

    def get_lock_expired_event(self, lock_name: str) -> asyncio.Event:
        """v3.2: Get an asyncio.Event that is set when a lock's keepalive fails.

        Callers can ``await event.wait()`` or check ``event.is_set()``
        to detect keepalive exhaustion without polling.
        """
        if lock_name not in self._lock_events:
            self._lock_events[lock_name] = asyncio.Event()
        return self._lock_events[lock_name]

    async def _handle_keepalive_exhaustion(
        self,
        lock_name: str,
        token: str,
        backend: str,
        max_failures: int,
    ) -> None:
        """v3.2: Handle keepalive exhaustion — auto-release lock and notify.

        Called when ``_keepalive_loop`` exhausts its retry budget.  Steps:
        1. Auto-release the lock (prevents race conditions with other holders)
        2. Mark lock as expired for polling via ``is_lock_expired()``
        3. Set asyncio.Event for waiting callers
        4. Fire ``_on_keepalive_failed`` callbacks
        """
        # 1. Auto-release
        try:
            if backend == "redis" and self._redis_client:
                await self._release_redis(lock_name, token)
            else:
                lock_file = self.config.lock_dir / f"{lock_name}{self.config.lock_extension}"
                await self._release_lock(lock_file, token)
            logger.info(f"[v3.2] Lock '{lock_name}' auto-released after keepalive failure")
        except Exception as release_err:
            logger.error(f"[v3.2] Failed to auto-release '{lock_name}': {release_err}")

        # 2. Mark expired
        self._expired_locks.add(lock_name)

        # 3. Set event
        event = self._lock_events.get(lock_name)
        if event is not None:
            event.set()

        # 4. Fire callbacks
        reason = f"keepalive_exhausted_after_{max_failures}_failures"
        for cb in self._on_keepalive_failed:
            try:
                result = cb(lock_name, reason)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as cb_err:
                logger.warning(f"[v3.2] Keepalive callback error: {cb_err}")

    def _get_own_process_start_time(self) -> float:
        """v96.0: Get our own process start time for owner ID."""
        try:
            import psutil
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return time.time()  # Fallback to current time

    def _get_own_process_name(self) -> str:
        """v96.0: Get our own process name for lock metadata."""
        try:
            import psutil
            return psutil.Process(os.getpid()).name()
        except Exception:
            return "unknown"

    def _get_own_process_cmdline(self) -> str:
        """v96.0: Get our own command line for lock metadata (truncated)."""
        try:
            import psutil
            cmdline = psutil.Process(os.getpid()).cmdline()
            return " ".join(cmdline)[:500] if cmdline else ""
        except Exception:
            return ""

    def _get_machine_id(self) -> str:
        """v96.0: Get machine identifier for distributed environments.

        v3.5: Uses a thread with timeout to prevent hanging on misconfigured
        DNS/NSS where socket.gethostname() blocks indefinitely.
        """
        import concurrent.futures
        import platform

        def _resolve():
            return f"{platform.system().lower()}-{socket.gethostname()}"

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_resolve)
                return future.result(timeout=2.0)
        except (concurrent.futures.TimeoutError, Exception):
            return f"{os.uname().sysname.lower()}-unknown" if hasattr(os, "uname") else "unknown"

    async def _get_lock_semaphore(self, lock_name: str) -> asyncio.Semaphore:
        """
        v2.0: Get or create a semaphore for a specific lock name.

        This ensures only one async task within this process can write
        to a particular lock file at a time, preventing the race condition
        where multiple tasks use the same temp file path.
        """
        async with self._semaphore_lock:
            if lock_name not in self._lock_semaphores:
                self._lock_semaphores[lock_name] = asyncio.Semaphore(1)
            return self._lock_semaphores[lock_name]

    def _generate_temp_file_path(self, lock_file: Path, token: str) -> Path:
        """
        v2.0: Generate a truly unique temp file path.

        Uses UUID + PID + counter to guarantee uniqueness even with:
        - Multiple async tasks in same process
        - Multiple processes with same PID (after restart)
        - Rapid consecutive writes

        Format: {lock_file}.tmp.{pid}.{counter}.{token_prefix}
        Example: vbia_state.dlm.lock.tmp.12345.1.f47ac10b
        """
        self._write_counter += 1
        token_prefix = token[:8] if len(token) >= 8 else token
        return lock_file.parent / f"{lock_file.name}.tmp.{os.getpid()}.{self._write_counter}.{token_prefix}"

    async def initialize(self) -> None:
        """
        v2.0: Initialize lock manager with aggressive pre-cleanup.

        This method ensures any stale locks from crashed processes are
        cleaned up BEFORE we start operations, preventing the common
        "Lock acquisition timeout" issue.

        v2.0 Enhancements:
        - Cleans up orphaned temp files from crashed/interrupted writes
        - Validates lock directory permissions
        """
        async with self._init_lock:
            if self._initialized:
                return

            # Create/repair lock storage directories.
            try:
                await self._ensure_lock_storage_ready()
                logger.info(f"Lock directory initialized: {self.config.lock_dir}")
            except Exception as e:
                logger.error(f"Failed to create lock directory: {e}")
                raise

            # v3.5: Wrap cleanup operations with timeouts to prevent hanging
            # during startup. Both methods contain blocking syscalls that are
            # now offloaded to executors, but we still cap total wall-clock time.
            try:
                temp_cleaned = await asyncio.wait_for(
                    self._cleanup_orphaned_temp_files(),
                    timeout=DLM_CLEANUP_TEMP_TIMEOUT,
                )
                if temp_cleaned > 0:
                    logger.info(f"[v2.0] Cleaned {temp_cleaned} orphaned temp file(s) at startup")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v3.5] Temp file cleanup timed out ({DLM_CLEANUP_TEMP_TIMEOUT}s) — "
                    "continuing with possibly orphaned temp files"
                )

            try:
                cleaned = await asyncio.wait_for(
                    self._aggressive_stale_cleanup(),
                    timeout=DLM_CLEANUP_STALE_TIMEOUT,
                )
                if cleaned > 0:
                    logger.info(f"[v93.0] Pre-cleaned {cleaned} stale lock(s) at startup")
            except asyncio.TimeoutError:
                logger.warning(
                    f"[v3.5] Stale lock cleanup timed out ({DLM_CLEANUP_STALE_TIMEOUT}s) — "
                    "continuing with possibly stale locks (cleanup loop will handle them)"
                )

            # Start cleanup task
            if self.config.cleanup_enabled:
                self._cleanup_task = asyncio.create_task(
                    self._cleanup_loop(),
                    name="lock_cleanup_loop"
                )
                logger.info("Stale lock cleanup task started")

            self._initialized = True

    async def _cleanup_orphaned_temp_files(self) -> int:
        """
        v2.0: Clean up orphaned temp files from crashed/interrupted writes.

        Temp files have the pattern: *.tmp.{pid}.{counter}.{token}
        We clean up any that:
        1. Are older than 60 seconds (stale)
        2. OR belong to a dead process

        Returns:
            Number of temp files cleaned
        """
        cleaned = 0

        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return 0

            # Find all temp files
            all_files = await aiofiles.os.listdir(self.config.lock_dir)
            temp_files = [f for f in all_files if '.tmp.' in f]

            # v3.5: Cap iteration count — if there are 200+ temp files,
            # something is seriously wrong; the cleanup loop will handle the rest.
            if len(temp_files) > DLM_CLEANUP_MAX_FILES:
                logger.warning(
                    f"[v3.5] {len(temp_files)} orphaned temp files exceeds cap "
                    f"({DLM_CLEANUP_MAX_FILES}) — processing oldest {DLM_CLEANUP_MAX_FILES} only"
                )
                # Sort by name (which includes PID/counter) and take first N
                temp_files = temp_files[:DLM_CLEANUP_MAX_FILES]

            current_time = time.time()

            for temp_name in temp_files:
                temp_path = self.config.lock_dir / temp_name

                try:
                    stat_info = await aiofiles.os.stat(temp_path)
                    age_seconds = current_time - stat_info.st_mtime

                    should_remove = False
                    reason = ""

                    # Check 1: File is older than 60 seconds (definitely stale)
                    if age_seconds > 60:
                        should_remove = True
                        reason = f"stale ({age_seconds:.0f}s old)"

                    # Check 2: Try to extract PID and check if process is alive
                    # v3.5: os.kill() offloaded to executor via _run_blocking
                    else:
                        # Format: {name}.tmp.{pid}.{counter}.{token}
                        parts = temp_name.split('.tmp.')
                        if len(parts) >= 2:
                            remainder = parts[1]  # {pid}.{counter}.{token}
                            pid_parts = remainder.split('.')
                            if len(pid_parts) >= 1:
                                try:
                                    temp_pid = int(pid_parts[0])

                                    def _check_pid(p=temp_pid):
                                        try:
                                            os.kill(p, 0)
                                            return True  # alive
                                        except ProcessLookupError:
                                            return False  # dead
                                        except PermissionError:
                                            return True  # exists, can't signal
                                        except OSError:
                                            return False

                                    pid_alive = await _run_blocking(
                                        _check_pid, timeout=2.0
                                    )
                                    if pid_alive is None:
                                        # Timeout checking PID — skip this file
                                        pass
                                    elif not pid_alive:
                                        should_remove = True
                                        reason = f"owner process {temp_pid} dead"
                                    elif age_seconds > 10:
                                        should_remove = True
                                        reason = f"stuck write ({age_seconds:.0f}s)"
                                except ValueError:
                                    if age_seconds > 30:
                                        should_remove = True
                                        reason = "unparseable and old"

                    if should_remove:
                        await aiofiles.os.remove(temp_path)
                        logger.debug(f"[v2.0] Cleaned temp file: {temp_name} ({reason})")
                        cleaned += 1

                except FileNotFoundError:
                    pass  # Already gone
                except Exception as e:
                    logger.debug(f"[v2.0] Error checking temp file {temp_name}: {e}")

        except Exception as e:
            logger.error(f"[v2.0] Error in temp file cleanup: {e}")

        return cleaned

    async def shutdown(self) -> None:
        """Shutdown lock manager and cleanup resources.

        v3.3: Now cancels ALL active keepalive tasks, not just _cleanup_task.
        Previously, orphaned keepalive tasks could survive shutdown/crash and
        extend stale lock TTLs, causing token mismatch warnings on restart
        when a new instance acquires the same lock with a different token.
        """
        # v3.3: Cancel all keepalive tasks FIRST — prevents stale TTL extension
        if self._keepalive_tasks:
            task_names = list(self._keepalive_tasks.keys())
            logger.info(
                f"Cancelling {len(task_names)} active keepalive task(s): "
                f"{', '.join(task_names)}"
            )
            for lock_name, task in list(self._keepalive_tasks.items()):
                if not task.done():
                    task.cancel()
            # Await all cancellations with timeout to avoid hanging on shutdown
            if self._keepalive_tasks:
                await asyncio.gather(
                    *(t for t in self._keepalive_tasks.values() if not t.done()),
                    return_exceptions=True,
                )
            self._keepalive_tasks.clear()

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass

        # v3.3: Clear expiration tracking state
        self._expired_locks.clear()
        self._lock_events.clear()
        self._keepalive_stopping.clear()

        logger.info("Distributed Lock Manager shut down")

    # =========================================================================
    # Lock Acquisition
    # =========================================================================

    @asynccontextmanager
    async def acquire(
        self,
        lock_name: str,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None,
        enable_keepalive: Optional[bool] = None,
    ) -> AsyncIterator[bool]:
        """
        Acquire distributed lock with automatic expiration and keepalive.

        v3.1: Now delegates to acquire_unified() for keepalive support,
        preserving the backward-compatible bool yield signature.

        Args:
            lock_name: Name of the lock (e.g., "vbia_events", "prime_state")
            timeout: Max time to wait for lock acquisition (seconds)
            ttl: Lock time-to-live - auto-expires after this duration (seconds)
            enable_keepalive: Override keepalive behavior for this acquisition.
                If None, uses DISTRIBUTED_LOCK_ACQUIRE_KEEPALIVE_DEFAULT.

        Yields:
            bool: True if lock acquired, False if timeout

        Example:
            async with lock_manager.acquire("my_resource", timeout=5.0, ttl=10.0) as acquired:
                if acquired:
                    # Critical section - you have the lock
                    await do_work()
                else:
                    # Could not acquire lock
                    logger.warning("Lock acquisition failed")
        """
        timeout = timeout or self.config.default_timeout_seconds
        ttl = ttl or self.config.default_ttl_seconds
        if enable_keepalive is None:
            enable_keepalive = os.environ.get(
                "DISTRIBUTED_LOCK_ACQUIRE_KEEPALIVE_DEFAULT",
                "true",
            ).strip().lower() == "true"

        try:
            async with self.acquire_unified(
                lock_name=lock_name,
                timeout=timeout,
                ttl=ttl,
                enable_keepalive=enable_keepalive,
            ) as (acquired, _metadata):
                yield acquired
        except Exception as e:
            logger.warning(
                f"Lock acquire delegation failed for '{lock_name}': {e}"
            )
            yield False

    async def _try_acquire_lock(
        self,
        lock_file: Path,
        lock_name: str,
        token: str,
        ttl: float
    ) -> bool:
        """
        v93.0: Try to acquire lock atomically with dead process detection.

        Enhancements:
        - Immediately removes locks from expired TTL
        - Immediately removes locks from dead processes (no waiting!)
        - Optimistic locking with verification

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            # Self-heal lock directory if another component cleaned it up.
            await self._ensure_directory(lock_file.parent)

            # Check if lock file exists
            if await aiofiles.os.path.exists(lock_file):
                # Read existing lock metadata
                existing_lock = await self._read_lock_metadata(lock_file)

                if existing_lock:
                    should_remove = False
                    remove_reason = ""

                    # Check 1: Lock is expired
                    if existing_lock.is_expired():
                        should_remove = True
                        remove_reason = f"expired {-existing_lock.time_remaining():.1f}s ago"

                    # v93.0: Check 2: Owner process is dead - DON'T WAIT!
                    # v3.5: Now async — blocking syscalls offloaded to executor
                    elif not await self._is_process_alive(existing_lock.owner):
                        should_remove = True
                        remove_reason = f"owner process {existing_lock.owner} is dead"

                    if should_remove:
                        logger.info(
                            f"[v93.0] Removing lock: {lock_name} ({remove_reason})"
                        )
                        await self._remove_lock_file(lock_file)
                    else:
                        # Lock is still valid AND owner is alive
                        logger.debug(
                            f"Lock held by another process: {lock_name} "
                            f"(owner: {existing_lock.owner}, expires in {existing_lock.time_remaining():.1f}s)"
                        )
                        return False

            # v96.0: Create new lock with enhanced metadata for PID reuse detection
            now = time.time()
            metadata = LockMetadata(
                acquired_at=now,
                expires_at=now + ttl,
                owner=self._owner_id,
                token=token,
                lock_name=lock_name,
                # v96.0: Process fingerprint for identity validation
                process_start_time=self._process_start_time,
                process_name=self._process_name,
                process_cmdline=self._process_cmdline,
                machine_id=self._machine_id,
            )

            # Write lock file atomically (with fsync for durability)
            await self._write_lock_metadata(lock_file, metadata)

            # v93.0: Verify we actually got the lock with exponential backoff
            # This handles slow filesystems (NFS, cloud storage) that may have delays
            verification_delays = [0.005, 0.01, 0.02, 0.05]  # Exponential backoff
            for delay in verification_delays:
                await asyncio.sleep(delay)
                verify_lock = await self._read_lock_metadata(lock_file)

                if verify_lock is None:
                    # Lock file disappeared - race condition, we lost
                    logger.debug(f"Lock file disappeared during verification: {lock_name}")
                    return False

                if verify_lock.token == token:
                    # We successfully own the lock
                    return True

                # Someone else's token - continue checking in case of read timing issue
                continue

            # After all retries, still not our token - we lost the race
            logger.debug(f"Lock race condition detected: {lock_name} (lost to another process)")
            return False

        except Exception as e:
            logger.error(f"Error acquiring lock {lock_name}: {e}")
            return False

    async def _release_lock(self, lock_file: Path, token: str) -> None:
        """
        Release lock if we own it.
        
        v2.1.0: Improved logging - only warns for actual race conditions,
        not for natural lock expiration (which is expected behavior).

        Args:
            lock_file: Path to lock file
            token: Our lock token
        """
        try:
            # Read current lock
            current_lock = await self._read_lock_metadata(lock_file)

            if current_lock is None:
                # v2.1.0: Lock file doesn't exist - this is normal if:
                # - Lock expired and was cleaned up by background task
                # - Lock was already released
                # This is NOT a warning condition - it's expected behavior
                logger.debug(
                    f"Lock already released or expired: {lock_file.name} "
                    f"(token: {token[:8]}... - no action needed)"
                )
            elif current_lock.token == token:
                # We own this lock, remove it
                await self._remove_lock_file(lock_file)
            else:
                # v3.4: Someone else owns the lock. This is expected during rapid
                # acquire/release cycles (e.g., heartbeat loops) where the next
                # iteration acquires a new token before the previous finally block
                # completes. Only warn if this is genuinely unexpected.
                # NOTE: lock_file.stem strips only the LAST dot-extension, so
                # "heartbeat.dlm.lock" → stem="heartbeat.dlm". We need to strip
                # the full extension (".dlm.lock") from the filename instead.
                fname = lock_file.name  # "heartbeat.dlm.lock"
                ext = self.config.lock_extension  # ".dlm.lock"
                lock_name = fname[:-len(ext)] if fname.endswith(ext) else lock_file.stem
                if lock_name in self._keepalive_stopping:
                    # Expected: we're in the finally block releasing, and another
                    # iteration already acquired. This is normal — downgrade to DEBUG.
                    logger.debug(
                        f"Lock superseded during release: {lock_file.name} "
                        f"(our token: {token[:8]}..., new owner: {current_lock.token[:8]}..., "
                        f"time remaining: {current_lock.time_remaining():.1f}s)"
                    )
                else:
                    logger.warning(
                        f"Cannot release lock - owned by different token: {lock_file.name} "
                        f"(our token: {token[:8]}..., current owner: {current_lock.token[:8]}..., "
                        f"time remaining: {current_lock.time_remaining():.1f}s)"
                    )
        except Exception as e:
            logger.error(f"Error releasing lock {lock_file.name}: {e}")

    # =========================================================================
    # Lock Metadata I/O
    # =========================================================================

    async def _read_lock_metadata(self, lock_file: Path) -> Optional[LockMetadata]:
        """
        v96.1: Read and parse lock metadata from file with robust error handling.

        Handles both old format (without v96.0 fields) and new format (with process fingerprint).

        v96.1 Enhancements:
        - Pre-check for empty files before JSON parsing
        - Better error categorization for diagnostics
        - More resilient against filesystem race conditions
        """
        try:
            # v96.1: Check file size before reading to detect empty/corrupted files early
            try:
                file_size = (await aiofiles.os.stat(lock_file)).st_size
                if file_size == 0:
                    logger.debug(f"Empty lock file detected: {lock_file} (will remove)")
                    await self._remove_lock_file(lock_file)
                    return None
            except FileNotFoundError:
                return None
            except OSError:
                pass  # Fall through to normal read, will handle errors there

            async with aiofiles.open(lock_file, 'r') as f:
                data = await f.read()

                # v96.1: Additional empty check after read (race condition protection)
                if not data or not data.strip():
                    logger.debug(f"Lock file has no content: {lock_file} (will remove)")
                    await self._remove_lock_file(lock_file)
                    return None

                metadata_dict = json.loads(data)

                # v96.0: Handle legacy lock files without new fields
                # Set default values for missing fields to maintain backward compatibility
                if "process_start_time" not in metadata_dict:
                    metadata_dict["process_start_time"] = 0.0
                if "process_name" not in metadata_dict:
                    metadata_dict["process_name"] = ""
                if "process_cmdline" not in metadata_dict:
                    metadata_dict["process_cmdline"] = ""
                if "machine_id" not in metadata_dict:
                    metadata_dict["machine_id"] = ""

                return LockMetadata(**metadata_dict)
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            logger.error(f"Corrupted lock file: {lock_file} (will remove)")
            await self._remove_lock_file(lock_file)
            return None
        except TypeError as e:
            # v96.0: Handle unknown fields from newer lock format gracefully
            logger.debug(f"Lock file has unknown fields: {lock_file} - {e}")
            try:
                # Try reading with only known fields
                async with aiofiles.open(lock_file, 'r') as f:
                    data = await f.read()
                    metadata_dict = json.loads(data)
                    # Extract only the fields we know about
                    return LockMetadata(
                        acquired_at=metadata_dict.get("acquired_at", 0.0),
                        expires_at=metadata_dict.get("expires_at", 0.0),
                        owner=metadata_dict.get("owner", ""),
                        token=metadata_dict.get("token", ""),
                        lock_name=metadata_dict.get("lock_name", ""),
                        process_start_time=metadata_dict.get("process_start_time", 0.0),
                        process_name=metadata_dict.get("process_name", ""),
                        process_cmdline=metadata_dict.get("process_cmdline", ""),
                        machine_id=metadata_dict.get("machine_id", ""),
                    )
            except Exception:
                return None
        except Exception as e:
            logger.error(f"Error reading lock metadata {lock_file}: {e}")
            return None

    async def _write_lock_metadata(self, lock_file: Path, metadata: LockMetadata) -> None:
        """
        v2.0: Write lock metadata to file atomically with enhanced durability.

        CRITICAL FIX (v2.0): Uses UUID-based temp files + per-lock semaphores
        to prevent race conditions when multiple async tasks try to write
        to the same lock concurrently.

        Previous bug: All tasks used temp_file = {lock}.tmp.{pid}
        Fix: Each task uses temp_file = {lock}.tmp.{pid}.{counter}.{token}

        v1.1: Ensures parent directory exists before writing.
        v93.0: Added fsync to ensure filesystem consistency before verification.
        v96.1: Added content verification after write to detect silent corruption.
        v2.0: Per-lock semaphore + unique temp file path per write attempt.
        """
        max_retries = 3
        last_error: Optional[Exception] = None

        # v2.0: Get semaphore for this specific lock to serialize writes
        # This prevents multiple async tasks from stepping on each other's temp files
        lock_semaphore = await self._get_lock_semaphore(metadata.lock_name)

        async with lock_semaphore:
            for attempt in range(max_retries):
                # v2.0: Generate truly unique temp file path for this attempt
                temp_file = self._generate_temp_file_path(lock_file, metadata.token)

                try:
                    # Ensure directory exists (resilient to runtime cleanup races).
                    lock_dir = lock_file.parent
                    await self._ensure_directory(lock_dir)

                    # v96.1: Serialize the content first to catch any serialization errors
                    content = json.dumps(asdict(metadata), indent=2)
                    content_bytes = content.encode('utf-8')
                    expected_size = len(content_bytes)

                    if not content or expected_size < 10:
                        raise ValueError(f"Invalid metadata serialization: {content[:50]}")

                    try:
                        # v2.0: Write content in binary mode for exact size control
                        async with aiofiles.open(temp_file, 'wb') as f:
                            await f.write(content_bytes)
                            await f.flush()
                            # v93.0: Force write to disk before rename
                            # v3.5: Offload to executor to avoid blocking event loop
                            fd = f.fileno()
                            await _run_blocking(os.fsync, fd, timeout=2.0)

                        # v96.1: Verify temp file was written correctly before rename
                        try:
                            temp_stat = await aiofiles.os.stat(temp_file)
                            temp_size = temp_stat.st_size
                            if temp_size != expected_size:
                                raise IOError(
                                    f"Temp file size mismatch: expected {expected_size}, got {temp_size}"
                                )
                        except FileNotFoundError:
                            # Another process/task may have already renamed it - rare but possible
                            raise IOError(f"Temp file disappeared: {temp_file}")

                        # Atomic rename
                        await aiofiles.os.rename(temp_file, lock_file)

                    except Exception:
                        # Clean up temp file on failure
                        try:
                            await aiofiles.os.remove(temp_file)
                        except (FileNotFoundError, OSError):
                            pass
                        raise

                    # v93.0: Sync directory to ensure rename is durable
                    # v3.5: Offloaded to executor to avoid blocking event loop
                    def _sync_dir(d=str(lock_dir)):
                        try:
                            fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
                            try:
                                os.fsync(fd)
                            finally:
                                os.close(fd)
                        except (OSError, AttributeError):
                            pass  # O_DIRECTORY not supported on some platforms

                    await _run_blocking(_sync_dir, timeout=2.0)

                    # v96.1: Verify final file was written correctly
                    final_size = (await aiofiles.os.stat(lock_file)).st_size
                    if final_size == 0:
                        raise IOError("Lock file is empty after write - filesystem issue detected")

                    # Success - exit retry loop
                    return

                except Exception as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        # v2.0: Exponential backoff with jitter to reduce contention
                        base_delay = 0.01 * (2 ** attempt)  # 0.01, 0.02, 0.04
                        jitter = random.uniform(0, base_delay * 0.5)  # Up to 50% jitter
                        delay = base_delay + jitter
                        missing_path = (
                            isinstance(e, FileNotFoundError)
                            or (isinstance(e, OSError) and e.errno == errno.ENOENT)
                        )
                        if missing_path:
                            try:
                                await self._ensure_directory(lock_file.parent)
                            except Exception:
                                pass
                            logger.warning(
                                f"Lock write attempt {attempt + 1} failed due to missing path for "
                                f"{lock_file.name}; repaired directory and retrying in {delay:.3f}s..."
                            )
                        else:
                            logger.warning(
                                f"Lock write attempt {attempt + 1} failed: {e}, retrying in {delay:.3f}s..."
                            )
                        await asyncio.sleep(delay)
                    continue

            # All retries failed
            logger.error(f"Error writing lock metadata {lock_file} after {max_retries} attempts: {last_error}")
            raise last_error or IOError(f"Failed to write lock file: {lock_file}")

    async def _remove_lock_file(self, lock_file: Path) -> None:
        """
        Remove lock file safely with race condition handling.

        v1.1: Made robust against TOCTOU race conditions - if file disappears
        between exists check and remove, we treat it as successful removal.
        """
        try:
            await aiofiles.os.remove(lock_file)
        except FileNotFoundError:
            # File already gone - treat as successful removal (race condition handled)
            logger.debug(f"Lock file already removed (race condition OK): {lock_file}")
        except OSError as e:
            # Check if it's "No such file or directory" error
            import errno
            if e.errno == errno.ENOENT:
                logger.debug(f"Lock file already removed: {lock_file}")
            else:
                logger.error(f"Error removing lock file {lock_file}: {e}")

    # =========================================================================
    # v96.0: Advanced Cleanup with PID Reuse Detection
    # =========================================================================

    @staticmethod
    def _is_process_alive_sync(
        owner_id: str,
        lock_metadata_dict: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """v3.5: Synchronous process liveness check — runs in executor thread.

        All blocking syscalls (os.kill, psutil.Process) are contained here
        so the asyncio event loop is never blocked.

        Args:
            owner_id: Lock owner identifier (format: "jarvis-{pid}-{start_time}")
            lock_metadata_dict: Optional dict with process_start_time, process_name

        Returns:
            True if process is alive AND matches stored identity
        """
        import psutil

        try:
            # Phase 1: PID Extraction
            if "-" not in owner_id:
                return True  # Can't validate, assume alive (conservative)

            parts = owner_id.split("-")
            try:
                pid = int(parts[1]) if len(parts) >= 2 else 0
            except ValueError:
                return False

            if pid <= 0:
                return False

            stored_start_time = 0.0
            if len(parts) >= 3:
                try:
                    stored_start_time = float(parts[2])
                except ValueError:
                    pass

            if stored_start_time == 0.0 and lock_metadata_dict:
                stored_start_time = lock_metadata_dict.get("process_start_time", 0.0)

            # Phase 2: Process Existence Check (blocking syscall)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                pass  # Exists, can't signal
            except OSError as e:
                import errno
                if e.errno == errno.ESRCH:
                    return False
                elif e.errno == errno.EPERM:
                    pass
                else:
                    return False

            # Phase 3: PID Reuse Detection (blocking psutil calls)
            if stored_start_time > 0.0:
                try:
                    process = psutil.Process(pid)
                    current_start_time = process.create_time()

                    time_diff = abs(current_start_time - stored_start_time)
                    if time_diff > 1.0:
                        return False  # PID was reused

                    stored_name = (lock_metadata_dict or {}).get("process_name", "")
                    if stored_name:
                        current_name = process.name()
                        if current_name != stored_name:
                            return False  # Different process

                except psutil.NoSuchProcess:
                    return False
                except (psutil.AccessDenied, Exception):
                    pass

            return True

        except Exception:
            return False  # Assume dead to prevent stale lock accumulation

    async def _is_process_alive(
        self,
        owner_id: str,
        lock_metadata: Optional[LockMetadata] = None,
    ) -> bool:
        """v3.5: Async wrapper for process liveness check.

        Offloads all blocking syscalls (os.kill, psutil.Process) to a thread
        executor so the asyncio event loop is never blocked during DLM cleanup.
        """
        # Build a plain dict from metadata to pass to the sync function
        # (LockMetadata dataclass can't be pickled across thread boundaries safely)
        meta_dict = None
        if lock_metadata:
            meta_dict = {
                "process_start_time": lock_metadata.process_start_time,
                "process_name": lock_metadata.process_name,
            }

        result = await _run_blocking(
            self._is_process_alive_sync,
            owner_id,
            meta_dict,
            timeout=DLM_BLOCKING_OP_TIMEOUT,
        )
        # _run_blocking returns None on timeout — treat as dead (conservative)
        return result if result is not None else False

    @staticmethod
    def _validate_lock_owner_identity_sync(
        pid: int,
        stored_start_time: float,
        process_name: str,
    ) -> Tuple[bool, str]:
        """v3.5: Synchronous identity validation — runs in executor thread.

        All blocking psutil calls are contained here.
        """
        import psutil

        if pid <= 0:
            return False, "invalid_pid"

        try:
            process = psutil.Process(pid)
            if not process.is_running():
                return False, "process_not_running"

            if process.status() == psutil.STATUS_ZOMBIE:
                return False, "zombie_process"

        except psutil.NoSuchProcess:
            return False, "process_not_found"
        except psutil.AccessDenied:
            return True, "access_denied_assumed_valid"

        # Validate start time
        if stored_start_time > 0.0:
            try:
                current_start_time = process.create_time()
                time_diff = abs(current_start_time - stored_start_time)
                if time_diff > 1.0:
                    return False, f"pid_reused:time_diff={time_diff:.1f}s"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        # Validate process name
        if process_name:
            try:
                current_name = process.name()
                if current_name != process_name:
                    return False, f"name_mismatch:{process_name}!={current_name}"
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass

        return True, "identity_verified"

    async def _validate_lock_owner_identity(
        self,
        metadata: LockMetadata,
    ) -> Tuple[bool, str]:
        """v3.5: Async identity validation — offloads blocking psutil to executor.

        Returns:
            Tuple of (is_valid: bool, reason: str)
        """
        pid = metadata.get_pid()
        if pid <= 0:
            return False, "invalid_pid"

        # Distributed: different machine can't validate PID — check TTL only
        if metadata.machine_id and metadata.machine_id != self._machine_id:
            if metadata.is_expired():
                return False, "expired_remote_lock"
            return True, "remote_lock_not_expired"

        stored_start_time = metadata.get_owner_start_time()

        result = await _run_blocking(
            self._validate_lock_owner_identity_sync,
            pid,
            stored_start_time,
            metadata.process_name,
            timeout=DLM_BLOCKING_OP_TIMEOUT,
        )

        if result is None:
            # Timeout — assume dead to prevent stale lock accumulation
            return False, "validation_timeout"
        return result

    async def _aggressive_stale_cleanup(self) -> int:
        """
        v93.0: Aggressive cleanup of stale locks at startup.

        This is more aggressive than the normal cleanup:
        1. Cleans ALL expired locks (not just stale ones)
        2. Validates PIDs for locks that aren't expired yet
        3. Removes locks from dead processes immediately

        This prevents the "Lock acquisition timeout" issue when starting
        after a crash.

        Returns:
            Number of locks cleaned
        """
        cleaned_count = 0

        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return 0

            # v96.2: Only process our .dlm.lock files, ignore other lock types
            ext = self.config.lock_extension
            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith(ext)
            ]

            # v3.5: Cap iteration count — prevents unbounded cleanup time
            if len(lock_files) > DLM_CLEANUP_MAX_FILES:
                logger.warning(
                    f"[v3.5] {len(lock_files)} lock files exceeds cap "
                    f"({DLM_CLEANUP_MAX_FILES}) — processing first {DLM_CLEANUP_MAX_FILES}"
                )
                lock_files = lock_files[:DLM_CLEANUP_MAX_FILES]

            for lock_file_name in lock_files:
                lock_file = self.config.lock_dir / lock_file_name
                metadata = await self._read_lock_metadata(lock_file)

                if not metadata:
                    # Corrupted lock file - remove it
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1
                    continue

                should_clean = False
                reason = ""

                # Check 1: Lock is expired (pure computation — no blocking)
                if metadata.is_expired():
                    should_clean = True
                    reason = f"expired {-metadata.time_remaining():.1f}s ago"

                # Check 2: Owner process is dead (even if not expired yet)
                # v3.5: Now async — blocking syscalls offloaded to executor
                elif not await self._is_process_alive(metadata.owner, metadata):
                    should_clean = True
                    reason = f"owner process dead (was {metadata.owner})"

                # Check 3: v96.0 Enhanced - Validate lock owner identity
                # v3.5: Now async — blocking psutil calls offloaded to executor
                else:
                    is_valid, validation_reason = await self._validate_lock_owner_identity(metadata)
                    if not is_valid:
                        should_clean = True
                        reason = f"identity validation failed: {validation_reason}"
                    # Also check for our own stale lock from previous run
                    elif metadata.owner == self._owner_id:
                        if metadata.acquired_at < time.time() - 60:
                            should_clean = True
                            reason = "own stale lock from previous run"

                if should_clean:
                    logger.info(
                        f"[v93.0] Cleaning lock: {metadata.lock_name} ({reason})"
                    )
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1

        except Exception as e:
            logger.error(f"[v93.0] Error in aggressive cleanup: {e}")

        return cleaned_count

    async def _cleanup_loop(self) -> None:
        """Background task to clean up stale locks."""
        logger.info("Stale lock cleanup loop started")

        while True:
            try:
                await asyncio.sleep(self.config.cleanup_interval_seconds)
                await self._cleanup_stale_locks()
            except asyncio.CancelledError:
                logger.info("Stale lock cleanup loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}", exc_info=True)

    async def _cleanup_stale_locks(self) -> None:
        """
        v93.0: Enhanced cleanup with PID validation.

        Cleans locks that are:
        1. Past their TTL (expired/stale)
        2. Owned by dead processes (PID validation)
        """
        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return

            # v96.2: Only process our .dlm.lock files, ignore other lock types
            ext = self.config.lock_extension
            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith(ext)
            ]

            cleaned_count = 0

            for lock_file_name in lock_files:
                lock_file = self.config.lock_dir / lock_file_name
                metadata = await self._read_lock_metadata(lock_file)

                if not metadata:
                    continue

                should_clean = False

                # Check TTL expiration
                if metadata.is_stale():
                    logger.warning(
                        f"Cleaning stale lock: {metadata.lock_name} "
                        f"(owner: {metadata.owner}, expired {-metadata.time_remaining():.1f}s ago)"
                    )
                    should_clean = True

                # v96.0: Enhanced - Check if owner process is dead with PID reuse detection
                # v3.5: Now async — blocking syscalls offloaded to executor
                elif not await self._is_process_alive(metadata.owner, metadata):
                    logger.warning(
                        f"Cleaning orphaned lock: {metadata.lock_name} "
                        f"(owner process {metadata.owner} is dead)"
                    )
                    should_clean = True

                # v96.0: Also validate lock owner identity comprehensively
                # v3.5: Now async — blocking psutil calls offloaded to executor
                else:
                    is_valid, validation_reason = await self._validate_lock_owner_identity(metadata)
                    if not is_valid:
                        logger.warning(
                            f"Cleaning invalid lock: {metadata.lock_name} "
                            f"(identity validation failed: {validation_reason})"
                        )
                        should_clean = True

                if should_clean:
                    await self._remove_lock_file(lock_file)
                    cleaned_count += 1

            if cleaned_count > 0:
                logger.info(f"Cleaned up {cleaned_count} stale/orphaned lock(s)")

        except Exception as e:
            logger.error(f"Error during stale lock cleanup: {e}", exc_info=True)

    # =========================================================================
    # Monitoring & Health
    # =========================================================================

    async def get_lock_status(self, lock_name: str) -> Optional[dict]:
        """
        Get current status of a lock.

        Returns:
            dict with lock status or None if lock not held
        """
        # v96.2: Use .dlm.lock extension
        lock_file = self.config.lock_dir / f"{lock_name}{self.config.lock_extension}"
        metadata = await self._read_lock_metadata(lock_file)

        if not metadata:
            return None

        # v96.0: Include identity validation in status
        # v3.5: Now async — blocking calls offloaded to executor
        is_valid, validation_reason = await self._validate_lock_owner_identity(metadata)
        is_alive = await self._is_process_alive(metadata.owner, metadata)

        return {
            "lock_name": metadata.lock_name,
            "owner": metadata.owner,
            "acquired_at": metadata.acquired_at,
            "expires_at": metadata.expires_at,
            "time_remaining": metadata.time_remaining(),
            "is_expired": metadata.is_expired(),
            "is_stale": metadata.is_stale(),
            # v96.0: Enhanced validation fields
            "owner_alive": is_alive,
            "identity_valid": is_valid,
            "validation_reason": validation_reason,
            "process_name": metadata.process_name,
            "machine_id": metadata.machine_id,
        }

    async def list_all_locks(self) -> list[dict]:
        """List all current locks with their status."""
        locks = []

        try:
            if not await aiofiles.os.path.exists(self.config.lock_dir):
                return locks

            # v96.2: Only list our .dlm.lock files, ignore other lock types (e.g., flock .lock files)
            ext = self.config.lock_extension
            lock_files = [
                f for f in await aiofiles.os.listdir(self.config.lock_dir)
                if f.endswith(ext)
            ]

            for lock_file_name in lock_files:
                # v96.2: Remove our extension to get the lock name
                lock_name = lock_file_name[:-len(ext)]
                status = await self.get_lock_status(lock_name)
                if status:
                    locks.append(status)

            return locks

        except Exception as e:
            logger.error(f"Error listing locks: {e}")
            return locks

    # =========================================================================
    # v3.0: Redis Backend Support
    # =========================================================================

    async def _select_backend(self) -> LockBackend:
        """
        v3.0: Select the best available lock backend.

        Priority: Redis (if available) > File (fallback)
        """
        if self.config.backend == LockBackend.FILE:
            return LockBackend.FILE

        if self.config.backend == LockBackend.REDIS:
            self._redis_client = get_redis_client()
            if await self._redis_client.connect():
                self._redis_available = True
                return LockBackend.REDIS
            logger.warning("[v3.0] Redis backend requested but unavailable")
            return LockBackend.REDIS

        # AUTO mode: Try Redis first, fall back to file
        if self.config.backend == LockBackend.AUTO:
            # v3.2: Skip Redis check if we recently found it unavailable
            now = time.time()
            if (
                self._redis_last_check_time > 0
                and (now - self._redis_last_check_time) < self._redis_retry_interval
            ):
                return LockBackend.FILE

            self._redis_client = get_redis_client()
            if await self._redis_client.connect():
                self._redis_available = True
                self._redis_last_check_time = 0.0  # Reset on success
                logger.info("[v3.2] Using Redis backend (distributed mode)")
                return LockBackend.REDIS
            else:
                self._redis_last_check_time = now
                logger.info(
                    f"[v3.2] Redis unavailable, using file backend "
                    f"(will retry in {self._redis_retry_interval:.0f}s)"
                )
                return LockBackend.FILE

        return LockBackend.FILE

    async def _get_next_fencing_token(self, lock_name: str) -> int:
        """v3.0: Get the next monotonically increasing fencing token."""
        async with self._fencing_lock:
            if self._redis_available and self._redis_client:
                token = await self._redis_client.incr(f"{TRINITY_LOCK_PREFIX}fencing:{lock_name}")
                if token > 0:
                    return token

            # Fall back to file-based counter
            self._fencing_token_counter += 1
            return self._fencing_token_counter

    async def _try_acquire_redis(
        self,
        lock_name: str,
        token: str,
        ttl: float,
    ) -> Optional[LockMetadata]:
        """v3.0: Try to acquire lock via Redis backend."""
        if not self._redis_available or not self._redis_client:
            return None

        redis_key = f"{TRINITY_LOCK_PREFIX}{lock_name}"
        fencing_token = await self._get_next_fencing_token(lock_name)
        now = time.time()

        metadata = LockMetadata(
            acquired_at=now,
            expires_at=now + ttl,
            owner=self._owner_id,
            token=token,
            lock_name=lock_name,
            process_start_time=self._process_start_time,
            process_name=self._process_name,
            process_cmdline=self._process_cmdline,
            machine_id=self._machine_id,
            backend="redis",
            fencing_token=fencing_token,
            repo_source=self.config.repo_source,
        )

        value = json.dumps(asdict(metadata))

        if await self._redis_client.set_nx(redis_key, value, ttl):
            logger.debug(f"[v3.0] Redis lock acquired: {lock_name} (fencing={fencing_token})")
            return metadata

        # Check if existing lock is stale
        existing_value = await self._redis_client.get(redis_key)
        if existing_value:
            try:
                existing = LockMetadata(**json.loads(existing_value))
                if existing.is_expired() or not await self._is_process_alive(existing.owner, existing):
                    if await self._redis_client.delete(redis_key):
                        if await self._redis_client.set_nx(redis_key, value, ttl):
                            logger.info(f"[v3.0] Redis lock acquired after stale cleanup: {lock_name}")
                            return metadata
            except (json.JSONDecodeError, TypeError):
                await self._redis_client.delete(redis_key)
                if await self._redis_client.set_nx(redis_key, value, ttl):
                    return metadata

        return None

    async def _release_redis(self, lock_name: str, token: str) -> bool:
        """v3.0: Release lock from Redis backend."""
        if not self._redis_available or not self._redis_client:
            return False

        redis_key = f"{TRINITY_LOCK_PREFIX}{lock_name}"
        value = await self._redis_client.get(redis_key)

        if not value:
            return True

        try:
            metadata = LockMetadata(**json.loads(value))
            if metadata.token == token:
                return await self._redis_client.delete(redis_key)
            return False
        except (json.JSONDecodeError, TypeError):
            return await self._redis_client.delete(redis_key)

    @asynccontextmanager
    async def acquire_unified(
        self,
        lock_name: str,
        timeout: Optional[float] = None,
        ttl: Optional[float] = None,
        enable_keepalive: bool = True,
    ) -> AsyncIterator[Tuple[bool, Optional[LockMetadata]]]:
        """
        v3.0: Acquire lock using best available backend with optional keepalive.

        Yields:
            Tuple of (acquired: bool, metadata: Optional[LockMetadata])
        """
        timeout = timeout or self.config.default_timeout_seconds
        ttl = ttl or self.config.default_ttl_seconds
        token = str(uuid4())

        if not self._initialized:
            await self.initialize()

        if self._active_backend == LockBackend.FILE:
            self._active_backend = await self._select_backend()

        acquired = False
        metadata: Optional[LockMetadata] = None
        keepalive_task: Optional[asyncio.Task] = None

        start_time = time.time()
        attempt = 0

        try:
            while time.time() - start_time < timeout:
                # Try Redis first if available
                if self._redis_available and self._redis_client:
                    metadata = await self._try_acquire_redis(lock_name, token, ttl)
                    if metadata:
                        acquired = True
                        break

                # Fall back to file-based lock
                lock_file = self.config.lock_dir / f"{lock_name}{self.config.lock_extension}"
                if await self._try_acquire_lock(lock_file, lock_name, token, ttl):
                    acquired = True
                    fencing_token = await self._get_next_fencing_token(lock_name)
                    now = time.time()
                    metadata = LockMetadata(
                        acquired_at=now,
                        expires_at=now + ttl,
                        owner=self._owner_id,
                        token=token,
                        lock_name=lock_name,
                        process_start_time=self._process_start_time,
                        process_name=self._process_name,
                        process_cmdline=self._process_cmdline,
                        machine_id=self._machine_id,
                        backend="file",
                        fencing_token=fencing_token,
                        repo_source=self.config.repo_source,
                    )
                    break

                attempt += 1
                base_delay = self.config.retry_delay_seconds
                jitter = random.uniform(0, base_delay * 0.5)
                await asyncio.sleep(base_delay + jitter)

            if acquired and metadata:
                logger.debug(
                    f"[v3.0] Lock acquired: {lock_name} "
                    f"(backend={metadata.backend}, fencing={metadata.fencing_token})"
                )

                # Start keepalive if enabled
                if enable_keepalive and self.config.keepalive_enabled:
                    # v3.2: Create event before starting keepalive
                    self._lock_events[lock_name] = asyncio.Event()
                    keepalive_task = asyncio.create_task(
                        self._keepalive_loop(lock_name, token, ttl, metadata.backend),
                        name=f"keepalive_{lock_name}"
                    )
                    self._keepalive_tasks[lock_name] = keepalive_task

                # v3.2: Fire lock acquired callbacks
                for _acq_cb in self._on_lock_acquired:
                    try:
                        _acq_result = _acq_cb(lock_name, metadata)
                        if asyncio.iscoroutine(_acq_result):
                            await _acq_result
                    except Exception:
                        pass
            else:
                logger.warning(f"[v3.0] Lock acquisition timeout: {lock_name}")
                # v3.2: Fire lock failed callbacks
                for _fail_cb in self._on_lock_failed:
                    try:
                        _fail_result = _fail_cb(lock_name, "acquisition_timeout")
                        if asyncio.iscoroutine(_fail_result):
                            await _fail_result
                    except Exception:
                        pass

            yield acquired, metadata

        finally:
            # v3.4: Signal keepalive to stop BEFORE any yield. This is synchronous
            # (no await), so no other coroutine can run between the flag set and
            # the keepalive's next check. Prevents stale token writes during the
            # race window between cancellation and actual task termination.
            self._keepalive_stopping.add(lock_name)

            if keepalive_task and not keepalive_task.done():
                keepalive_task.cancel()
                try:
                    await asyncio.wait_for(keepalive_task, timeout=3.0)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass

            if lock_name in self._keepalive_tasks:
                del self._keepalive_tasks[lock_name]

            # v3.2: Clean up expiration tracking
            self._expired_locks.discard(lock_name)
            self._lock_events.pop(lock_name, None)

            if acquired:
                if metadata and metadata.backend == "redis":
                    await self._release_redis(lock_name, token)
                else:
                    lock_file = self.config.lock_dir / f"{lock_name}{self.config.lock_extension}"
                    await self._release_lock(lock_file, token)
                logger.debug(f"[v3.0] Lock released: {lock_name}")

                # v3.2: Fire lock released callbacks
                for _rel_cb in self._on_lock_released:
                    try:
                        _rel_result = _rel_cb(lock_name)
                        if asyncio.iscoroutine(_rel_result):
                            await _rel_result
                    except Exception:
                        pass

            # v3.4: Clean up stopping flag after release
            self._keepalive_stopping.discard(lock_name)

    async def _keepalive_loop(
        self,
        lock_name: str,
        token: str,
        ttl: float,
        backend: str,
    ) -> None:
        """v3.1: Background task to extend lock TTL periodically.

        Resilient keepalive with retry counter — tolerates transient failures
        instead of silently dying on the first error.
        """
        interval = self.config.keepalive_interval_seconds
        max_failures = int(os.environ.get(
            "DISTRIBUTED_LOCK_KEEPALIVE_MAX_FAILURES", "3"
        ))
        consecutive_failures = 0

        while True:
            try:
                await asyncio.sleep(interval)
                extended = False

                if backend == "redis" and self._redis_client:
                    redis_key = f"{TRINITY_LOCK_PREFIX}{lock_name}"
                    value = await self._redis_client.get(redis_key)
                    if value:
                        try:
                            meta = LockMetadata(**json.loads(value))
                            if meta.token == token:
                                await self._redis_client.expire(redis_key, int(ttl))
                                extended = True
                                logger.debug(f"[v3.1] Redis lock TTL extended: {lock_name}")
                            else:
                                # Token mismatch — lock was stolen
                                logger.warning(
                                    f"[v3.1] Keepalive token mismatch for '{lock_name}' — stopping"
                                )
                                break
                        except (json.JSONDecodeError, TypeError):
                            consecutive_failures += 1
                    else:
                        # Key gone — lock expired or was deleted
                        logger.warning(
                            f"[v3.1] Redis key gone for '{lock_name}' — stopping keepalive"
                        )
                        break
                else:
                    # v3.4: Check stopping flag before reading/writing metadata.
                    # If the parent context manager's finally block has signaled us
                    # to stop, bail out immediately — writing would overwrite a
                    # newer token that may have been acquired during our yield.
                    if lock_name in self._keepalive_stopping:
                        logger.debug(
                            f"[v3.4] Keepalive for '{lock_name}' stopping (flag set) — exiting"
                        )
                        break

                    lock_file = self.config.lock_dir / f"{lock_name}{self.config.lock_extension}"
                    metadata = await self._read_lock_metadata(lock_file)
                    if metadata and metadata.token == token:
                        # v3.4: Re-check stopping flag after async read — the flag
                        # could have been set while we awaited the file read.
                        if lock_name in self._keepalive_stopping:
                            logger.debug(
                                f"[v3.4] Keepalive for '{lock_name}' stopping after read — skipping write"
                            )
                            break
                        metadata.expires_at = time.time() + ttl
                        metadata.extensions += 1
                        await self._write_lock_metadata(lock_file, metadata)
                        extended = True
                        logger.debug(f"[v3.1] File lock TTL extended: {lock_name}")
                    elif metadata and metadata.token != token:
                        # Token mismatch — lock was stolen
                        logger.warning(
                            f"[v3.1] Keepalive token mismatch for '{lock_name}' — stopping"
                        )
                        break
                    else:
                        # Lock file gone
                        logger.warning(
                            f"[v3.1] Lock file gone for '{lock_name}' — stopping keepalive"
                        )
                        break

                if extended:
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    logger.warning(
                        f"[v3.1] Keepalive extension failed for '{lock_name}' "
                        f"({consecutive_failures}/{max_failures})"
                    )
                    if consecutive_failures >= max_failures:
                        logger.error(
                            f"[v3.2] Keepalive gave up for '{lock_name}' after "
                            f"{max_failures} consecutive failures — auto-releasing"
                        )
                        await self._handle_keepalive_exhaustion(
                            lock_name, token, backend, max_failures
                        )
                        break

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    f"[v3.1] Keepalive error for '{lock_name}': {e} "
                    f"({consecutive_failures}/{max_failures})"
                )
                if consecutive_failures >= max_failures:
                    logger.error(
                        f"[v3.2] Keepalive gave up for '{lock_name}' after "
                        f"{max_failures} consecutive failures — auto-releasing"
                    )
                    await self._handle_keepalive_exhaustion(
                        lock_name, token, backend, max_failures
                    )
                    break

    def get_active_backend(self) -> LockBackend:
        """v3.0: Get the currently active lock backend."""
        return self._active_backend

    def is_redis_available(self) -> bool:
        """v3.0: Check if Redis backend is available."""
        return self._redis_available

    async def get_cross_repo_status(self) -> Dict[str, Any]:
        """v3.0: Get comprehensive cross-repo lock status."""
        status: Dict[str, Any] = {
            "backend": self._active_backend.value,
            "redis_available": self._redis_available,
            "repo_source": self.config.repo_source,
            "owner_id": self._owner_id,
            "machine_id": self._machine_id,
            "active_keepalives": list(self._keepalive_tasks.keys()),
            "locks": [],
        }

        file_locks = await self.list_all_locks()
        for lock in file_locks:
            lock["backend"] = "file"
        status["locks"].extend(file_locks)

        return status


# =============================================================================
# Global Instance (Singleton Pattern)
# =============================================================================

_lock_manager_instance: Optional[DistributedLockManager] = None


async def get_lock_manager(config: Optional[LockConfig] = None) -> DistributedLockManager:
    """
    Get or create global lock manager instance.

    v3.0: Supports optional config for cross-repo initialization.
    """
    global _lock_manager_instance

    if _lock_manager_instance is None:
        _lock_manager_instance = DistributedLockManager(config)
        await _lock_manager_instance.initialize()

    return _lock_manager_instance


async def shutdown_lock_manager() -> None:
    """Shutdown global lock manager instance."""
    global _lock_manager_instance

    if _lock_manager_instance:
        await _lock_manager_instance.shutdown()
        _lock_manager_instance = None


# =============================================================================
# v3.0: Cross-Repo Convenience Functions
# =============================================================================

@asynccontextmanager
async def acquire_cross_repo_lock(
    lock_name: str,
    repo_source: str = "jarvis",
    timeout: float = 5.0,
    ttl: float = 10.0,
) -> AsyncIterator[Tuple[bool, Optional[LockMetadata]]]:
    """
    v3.0: Convenience function for cross-repo lock acquisition.

    Makes it easy for JARVIS-Prime and Reactor-Core to acquire locks consistently.

    Example (from JARVIS-Prime):
        from backend.core.distributed_lock_manager import acquire_cross_repo_lock

        async with acquire_cross_repo_lock("training_job", "jarvis-prime") as (acquired, meta):
            if acquired:
                await run_training()
    """
    config = LockConfig(repo_source=repo_source)
    manager = await get_lock_manager(config)

    async with manager.acquire_unified(lock_name, timeout, ttl) as result:
        yield result


def get_trinity_lock_prefix() -> str:
    """v3.0: Get the Trinity lock prefix for Redis keys."""
    return TRINITY_LOCK_PREFIX
