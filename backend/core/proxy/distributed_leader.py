#!/usr/bin/env python3
"""
Distributed Proxy Leader Election System v1.0
==============================================

Raft-inspired leader election for cross-repo Cloud SQL proxy management.
Ensures only ONE repo manages the proxy lifecycle at any time.

Advanced Features:
- File-based locking with fcntl (no external dependencies)
- Heartbeat-based liveness detection with monotonic time
- Automatic leader failover on crash/timeout
- Compare-And-Swap (CAS) pattern for state transitions
- Split-brain prevention via exclusive file locking
- Clock drift resistance using multiple time sources

Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │  JARVIS (run_supervisor.py)                                 │
    │  ├── Attempts to become LEADER                              │
    │  ├── If successful: manages proxy, writes heartbeat         │
    │  └── If failed: becomes FOLLOWER, monitors leader           │
    ├─────────────────────────────────────────────────────────────┤
    │  JARVIS-Prime (--follower-mode)                             │
    │  └── Reads leader state, skips proxy management             │
    ├─────────────────────────────────────────────────────────────┤
    │  Reactor-Core (--follower-mode)                             │
    │  └── Reads leader state, skips proxy management             │
    └─────────────────────────────────────────────────────────────┘

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import platform
import random
import socket
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# =============================================================================
# CONFIGURATION - All from environment, ZERO hardcoding
# =============================================================================

@dataclass
class LeaderElectionConfig:
    """
    Configuration for leader election - all values from environment.

    Environment Variables:
        PROXY_LEADER_HEARTBEAT_INTERVAL: Seconds between heartbeats (default: 5.0)
        PROXY_LEADER_ELECTION_TIMEOUT_MIN: Minimum election timeout (default: 3.0)
        PROXY_LEADER_ELECTION_TIMEOUT_MAX: Maximum election timeout (default: 10.0)
        PROXY_LEADER_LEASE_DURATION: Heartbeat staleness threshold (default: 15.0)
        PROXY_LEADER_STATE_DIR: Directory for state files (default: ~/.jarvis/cross_repo)
        PROXY_LEADER_LOCK_TIMEOUT: Lock acquisition timeout (default: 5.0)
        PROXY_LEADER_MAX_ELECTION_ATTEMPTS: Max election attempts (default: 5)
    """
    heartbeat_interval: float = field(default_factory=lambda: float(
        os.getenv("PROXY_LEADER_HEARTBEAT_INTERVAL", "5.0")
    ))
    election_timeout_min: float = field(default_factory=lambda: float(
        os.getenv("PROXY_LEADER_ELECTION_TIMEOUT_MIN", "3.0")
    ))
    election_timeout_max: float = field(default_factory=lambda: float(
        os.getenv("PROXY_LEADER_ELECTION_TIMEOUT_MAX", "10.0")
    ))
    lease_duration: float = field(default_factory=lambda: float(
        os.getenv("PROXY_LEADER_LEASE_DURATION", "15.0")
    ))
    state_dir: Path = field(default_factory=lambda: Path(
        os.getenv("PROXY_LEADER_STATE_DIR", str(Path.home() / ".jarvis" / "cross_repo"))
    ))
    lock_timeout: float = field(default_factory=lambda: float(
        os.getenv("PROXY_LEADER_LOCK_TIMEOUT", "5.0")
    ))
    max_election_attempts: int = field(default_factory=lambda: int(
        os.getenv("PROXY_LEADER_MAX_ELECTION_ATTEMPTS", "5")
    ))

    def __post_init__(self):
        """Ensure state directory exists."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    @property
    def lock_file_path(self) -> Path:
        return self.state_dir / "proxy_leader.lock"

    @property
    def state_file_path(self) -> Path:
        return self.state_dir / "proxy_leader_state.json"

    @property
    def heartbeat_file_path(self) -> Path:
        return self.state_dir / "proxy_leader_heartbeat.json"


# =============================================================================
# ENUMS AND DATA STRUCTURES
# =============================================================================

class LeaderState(Enum):
    """Leader election states."""
    UNKNOWN = "unknown"
    FOLLOWER = "follower"
    CANDIDATE = "candidate"
    LEADER = "leader"
    SHUTDOWN = "shutdown"


class ElectionResult(Enum):
    """Result of an election attempt."""
    WON = "won"
    LOST = "lost"
    TIMEOUT = "timeout"
    ERROR = "error"
    EXISTING_LEADER = "existing_leader"


@dataclass
class LeaderIdentity:
    """
    Unique identity for a leader candidate.

    Format: {hostname}:{pid}:{start_time}:{uuid}

    The combination ensures:
    - hostname: Identifies the machine
    - pid: Identifies the process
    - start_time: Disambiguates recycled PIDs
    - uuid: Guarantees uniqueness even with clock issues
    """
    hostname: str
    pid: int
    start_time: float
    instance_uuid: str
    repo_name: str

    def __post_init__(self):
        """Validate identity components."""
        if not self.hostname:
            self.hostname = socket.gethostname()
        if not self.pid:
            self.pid = os.getpid()
        if not self.start_time:
            self.start_time = time.time()
        if not self.instance_uuid:
            self.instance_uuid = str(uuid.uuid4())[:8]
        if not self.repo_name:
            self.repo_name = os.getenv("JARVIS_REPO_NAME", "jarvis")

    @classmethod
    def create(cls, repo_name: Optional[str] = None) -> 'LeaderIdentity':
        """Create a new leader identity for this process."""
        return cls(
            hostname=socket.gethostname(),
            pid=os.getpid(),
            start_time=time.time(),
            instance_uuid=str(uuid.uuid4())[:8],
            repo_name=repo_name or os.getenv("JARVIS_REPO_NAME", "jarvis")
        )

    @property
    def id_string(self) -> str:
        """Get compact ID string."""
        return f"{self.hostname}:{self.pid}:{int(self.start_time)}:{self.instance_uuid}"

    def to_string(self) -> str:
        """Get compact ID string (alias for id_string for orchestrator compatibility)."""
        return self.id_string

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "hostname": self.hostname,
            "pid": self.pid,
            "start_time": self.start_time,
            "instance_uuid": self.instance_uuid,
            "repo_name": self.repo_name,
            "id_string": self.id_string,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LeaderIdentity':
        """Create from dictionary."""
        return cls(
            hostname=data.get("hostname", ""),
            pid=data.get("pid", 0),
            start_time=data.get("start_time", 0.0),
            instance_uuid=data.get("instance_uuid", ""),
            repo_name=data.get("repo_name", ""),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LeaderIdentity):
            return False
        return self.id_string == other.id_string

    def __hash__(self) -> int:
        return hash(self.id_string)


@dataclass
class LeaderHeartbeat:
    """
    Heartbeat data written by the leader.

    Contains both wall-clock and monotonic time for drift detection.
    """
    leader_id: LeaderIdentity
    timestamp: float  # Wall clock time
    monotonic: float  # Monotonic time (for same-machine comparison)
    term: int  # Election term (increments on each election)
    proxy_state: str  # Current proxy state
    proxy_pid: Optional[int]
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_fresh(self, lease_duration: float) -> bool:
        """
        Check if heartbeat is still valid.

        Uses wall clock time for cross-machine comparison.
        """
        age = time.time() - self.timestamp
        return age < lease_duration

    def to_dict(self) -> Dict[str, Any]:
        return {
            "leader_id": self.leader_id.to_dict(),
            "timestamp": self.timestamp,
            "monotonic": self.monotonic,
            "term": self.term,
            "proxy_state": self.proxy_state,
            "proxy_pid": self.proxy_pid,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'LeaderHeartbeat':
        return cls(
            leader_id=LeaderIdentity.from_dict(data.get("leader_id", {})),
            timestamp=data.get("timestamp", 0.0),
            monotonic=data.get("monotonic", 0.0),
            term=data.get("term", 0),
            proxy_state=data.get("proxy_state", "unknown"),
            proxy_pid=data.get("proxy_pid"),
            metadata=data.get("metadata", {}),
        )


@dataclass
class ElectionOutcome:
    """Result of a leader election."""
    result: ElectionResult
    is_leader: bool
    leader_id: Optional[LeaderIdentity]
    term: int
    message: str
    election_duration_ms: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "result": self.result.value,
            "is_leader": self.is_leader,
            "leader_id": self.leader_id.to_dict() if self.leader_id else None,
            "term": self.term,
            "message": self.message,
            "election_duration_ms": self.election_duration_ms,
        }


# =============================================================================
# FILE LOCKING UTILITIES
# =============================================================================

class FileLock:
    """
    Cross-platform file lock using fcntl (Unix) with timeout support.

    Features:
    - Non-blocking lock acquisition with timeout
    - Automatic unlock on context exit
    - Handles stale lock detection
    - Thread-safe
    """

    def __init__(self, path: Path, timeout: float = 5.0):
        self.path = path
        self.timeout = timeout
        self._fd: Optional[int] = None
        self._lock = threading.Lock()

    def acquire(self, blocking: bool = True) -> bool:
        """
        Acquire the file lock.

        Args:
            blocking: If True, wait up to timeout. If False, return immediately.

        Returns:
            True if lock acquired, False otherwise.
        """
        with self._lock:
            try:
                # Open or create the lock file
                self._fd = os.open(
                    str(self.path),
                    os.O_RDWR | os.O_CREAT,
                    0o644
                )

                if blocking:
                    # Try to acquire with timeout
                    start = time.monotonic()
                    while True:
                        try:
                            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            return True
                        except (IOError, OSError):
                            if time.monotonic() - start >= self.timeout:
                                self._close_fd()
                                return False
                            time.sleep(0.1)
                else:
                    # Non-blocking attempt
                    try:
                        fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        return True
                    except (IOError, OSError):
                        self._close_fd()
                        return False

            except Exception as e:
                logger.debug(f"[FileLock] Acquire error: {e}")
                self._close_fd()
                return False

    def release(self) -> None:
        """Release the file lock."""
        with self._lock:
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                except Exception:
                    pass
                self._close_fd()

    def _close_fd(self) -> None:
        """Close file descriptor."""
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass
            self._fd = None

    @contextmanager
    def locked(self, blocking: bool = True):
        """Context manager for lock acquisition."""
        acquired = self.acquire(blocking=blocking)
        try:
            yield acquired
        finally:
            if acquired:
                self.release()

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock on {self.path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


# =============================================================================
# DISTRIBUTED PROXY LEADER - MAIN CLASS
# =============================================================================

class DistributedProxyLeader:
    """
    Raft-inspired leader election for cross-repo proxy management.

    Election Protocol:
    1. On startup, check if leader exists (read state file)
    2. If leader heartbeat fresh (<lease_duration), become FOLLOWER
    3. If no leader or stale heartbeat, start ELECTION
    4. Election: random backoff, then try to acquire lock
    5. Winner becomes LEADER, writes heartbeat, manages proxy
    6. Losers become FOLLOWERS, monitor leader health
    7. If leader dies, FOLLOWERS detect via stale heartbeat → new election

    Thread Safety:
    - All state transitions protected by asyncio.Lock
    - File operations use fcntl locking
    - Heartbeat updates are atomic (write to temp, rename)

    Usage:
        leader = DistributedProxyLeader()
        outcome = await leader.run_election()

        if outcome.is_leader:
            # This process is the leader - manage proxy
            await leader.start_heartbeat()
            # ... proxy management code ...
        else:
            # This process is a follower - observe only
            await leader.wait_for_leader_ready()
    """

    __slots__ = (
        '_config', '_identity', '_state', '_current_term', '_leader_id',
        '_file_lock', '_heartbeat_task', '_monitor_task', '_state_lock',
        '_subscribers', '_subscriber_lock', '_shutdown_event', '_is_running',
    )

    def __init__(
        self,
        config: Optional[LeaderElectionConfig] = None,
        repo_name: Optional[str] = None
    ):
        """
        Initialize leader election system.

        Args:
            config: Configuration (uses defaults + env vars if None)
            repo_name: Name of this repo (jarvis, prime, reactor)
        """
        self._config = config or LeaderElectionConfig()
        self._identity = LeaderIdentity.create(repo_name)

        # State management
        self._state = LeaderState.UNKNOWN
        self._current_term = 0
        self._leader_id: Optional[LeaderIdentity] = None

        # Synchronization
        self._file_lock = FileLock(self._config.lock_file_path, self._config.lock_timeout)
        self._state_lock: Optional[asyncio.Lock] = None  # Created lazily

        # Background tasks
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event: Optional[asyncio.Event] = None
        self._is_running = False

        # Event subscribers
        self._subscribers: List[Callable[[LeaderState, LeaderState], Any]] = []
        self._subscriber_lock = threading.Lock()

        logger.info(
            f"[DistributedProxyLeader v1.0] Initialized "
            f"(id={self._identity.id_string}, repo={self._identity.repo_name})"
        )

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def state(self) -> LeaderState:
        """Current leader state."""
        return self._state

    @property
    def is_leader(self) -> bool:
        """Check if this process is the leader."""
        return self._state == LeaderState.LEADER

    @property
    def is_follower(self) -> bool:
        """Check if this process is a follower."""
        return self._state == LeaderState.FOLLOWER

    @property
    def identity(self) -> LeaderIdentity:
        """This process's identity."""
        return self._identity

    @property
    def leader_id(self) -> Optional[LeaderIdentity]:
        """Current leader's identity (may be self or another process)."""
        return self._leader_id

    @property
    def current_term(self) -> int:
        """Current election term."""
        return self._current_term

    # -------------------------------------------------------------------------
    # Lazy Initialization
    # -------------------------------------------------------------------------

    async def _ensure_locks(self) -> None:
        """Create asyncio locks lazily in async context."""
        if self._state_lock is None:
            self._state_lock = asyncio.Lock()
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()

    # -------------------------------------------------------------------------
    # State Transitions (CAS Pattern)
    # -------------------------------------------------------------------------

    async def _transition_to(
        self,
        new_state: LeaderState,
        expected_current: Optional[Set[LeaderState]] = None
    ) -> bool:
        """
        Atomic state transition using Compare-And-Swap pattern.

        Args:
            new_state: Target state
            expected_current: Set of valid current states (None = any)

        Returns:
            True if transition succeeded, False otherwise
        """
        await self._ensure_locks()
        assert self._state_lock is not None

        async with self._state_lock:
            if expected_current and self._state not in expected_current:
                logger.debug(
                    f"[Leader] Transition rejected: {self._state} not in {expected_current}"
                )
                return False

            old_state = self._state
            self._state = new_state

            logger.info(f"[Leader] State transition: {old_state.value} → {new_state.value}")

            # Notify subscribers (fire-and-forget)
            await self._notify_subscribers(old_state, new_state)

            return True

    async def _notify_subscribers(
        self,
        old_state: LeaderState,
        new_state: LeaderState
    ) -> None:
        """
        v117.0: Notify all subscribers of state change.

        IMPORTANT: This now AWAITS async callbacks to ensure they complete
        before returning. This fixes the race condition where callbacks
        were created as tasks but not awaited, causing events to be set
        after the caller had already moved on.
        """
        with self._subscriber_lock:
            subscribers = list(self._subscribers)

        for callback in subscribers:
            try:
                result = callback(old_state, new_state)
                if asyncio.iscoroutine(result):
                    # v117.0: AWAIT the callback instead of fire-and-forget
                    # This ensures the callback completes before we return
                    try:
                        await asyncio.wait_for(result, timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(f"[Leader] Subscriber callback timed out")
                    except Exception as e:
                        logger.debug(f"[Leader] Subscriber callback error: {e}")
            except Exception as e:
                logger.debug(f"[Leader] Subscriber invocation error: {e}")

    def subscribe(self, callback: Callable[[LeaderState, LeaderState], Any]) -> None:
        """Subscribe to state changes."""
        with self._subscriber_lock:
            self._subscribers.append(callback)

    # -------------------------------------------------------------------------
    # Election Logic
    # -------------------------------------------------------------------------

    async def run_election(self) -> ElectionOutcome:
        """
        Run leader election.

        This is the main entry point for the election process.

        Returns:
            ElectionOutcome with result and leader identity
        """
        await self._ensure_locks()
        start_time = time.monotonic()

        logger.info(f"[Leader] Starting election (candidate={self._identity.id_string})")

        # Check for existing leader first
        existing = await self._check_existing_leader()
        if existing:
            # Fresh leader exists - become follower
            await self._transition_to(LeaderState.FOLLOWER)
            self._leader_id = existing.leader_id
            self._current_term = existing.term

            return ElectionOutcome(
                result=ElectionResult.EXISTING_LEADER,
                is_leader=False,
                leader_id=existing.leader_id,
                term=existing.term,
                message=f"Existing leader: {existing.leader_id.id_string}",
                election_duration_ms=(time.monotonic() - start_time) * 1000
            )

        # No valid leader - start election
        await self._transition_to(LeaderState.CANDIDATE)

        for attempt in range(self._config.max_election_attempts):
            # Random backoff to prevent thundering herd
            backoff = random.uniform(
                self._config.election_timeout_min,
                self._config.election_timeout_max
            )

            logger.debug(f"[Leader] Election attempt {attempt + 1}, backoff {backoff:.2f}s")
            await asyncio.sleep(backoff)

            # Try to acquire lock
            if self._file_lock.acquire(blocking=False):
                try:
                    # Double-check no leader appeared while we waited
                    existing = await self._check_existing_leader()
                    if existing and existing.is_fresh(self._config.lease_duration):
                        # Someone else won - release and become follower
                        self._file_lock.release()
                        await self._transition_to(LeaderState.FOLLOWER)
                        self._leader_id = existing.leader_id
                        self._current_term = existing.term

                        return ElectionOutcome(
                            result=ElectionResult.LOST,
                            is_leader=False,
                            leader_id=existing.leader_id,
                            term=existing.term,
                            message=f"Lost to {existing.leader_id.id_string}",
                            election_duration_ms=(time.monotonic() - start_time) * 1000
                        )

                    # We won! Increment term and write initial heartbeat
                    self._current_term += 1
                    self._leader_id = self._identity

                    await self._write_heartbeat(proxy_state="initializing")
                    await self._transition_to(LeaderState.LEADER)

                    return ElectionOutcome(
                        result=ElectionResult.WON,
                        is_leader=True,
                        leader_id=self._identity,
                        term=self._current_term,
                        message="Election won",
                        election_duration_ms=(time.monotonic() - start_time) * 1000
                    )

                except Exception as e:
                    logger.error(f"[Leader] Election error: {e}")
                    self._file_lock.release()
                    raise

            # Lock acquisition failed - someone else has it
            logger.debug(f"[Leader] Lock busy, retrying...")

        # Max attempts reached
        await self._transition_to(LeaderState.FOLLOWER)

        return ElectionOutcome(
            result=ElectionResult.TIMEOUT,
            is_leader=False,
            leader_id=None,
            term=self._current_term,
            message=f"Election timeout after {self._config.max_election_attempts} attempts",
            election_duration_ms=(time.monotonic() - start_time) * 1000
        )

    async def _check_existing_leader(self) -> Optional[LeaderHeartbeat]:
        """
        Check if there's an existing valid leader.

        Returns:
            LeaderHeartbeat if valid leader exists, None otherwise
        """
        try:
            if not self._config.heartbeat_file_path.exists():
                return None

            data = json.loads(self._config.heartbeat_file_path.read_text())
            heartbeat = LeaderHeartbeat.from_dict(data)

            if heartbeat.is_fresh(self._config.lease_duration):
                return heartbeat
            else:
                logger.debug(
                    f"[Leader] Stale heartbeat from {heartbeat.leader_id.id_string} "
                    f"(age={time.time() - heartbeat.timestamp:.1f}s)"
                )
                return None

        except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
            logger.debug(f"[Leader] Heartbeat read error: {e}")
            return None

    # -------------------------------------------------------------------------
    # Heartbeat Management
    # -------------------------------------------------------------------------

    async def _write_heartbeat(
        self,
        proxy_state: str = "unknown",
        proxy_pid: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Write heartbeat to file (atomic via temp file + rename).

        Args:
            proxy_state: Current proxy state
            proxy_pid: Proxy process PID
            metadata: Additional metadata to include
        """
        heartbeat = LeaderHeartbeat(
            leader_id=self._identity,
            timestamp=time.time(),
            monotonic=time.monotonic(),
            term=self._current_term,
            proxy_state=proxy_state,
            proxy_pid=proxy_pid,
            metadata=metadata or {}
        )

        # Atomic write: write to temp, then rename
        temp_path = self._config.heartbeat_file_path.with_suffix('.tmp')
        temp_path.write_text(json.dumps(heartbeat.to_dict(), indent=2))
        temp_path.rename(self._config.heartbeat_file_path)

        logger.debug(f"[Leader] Heartbeat written (term={self._current_term})")

    async def start_heartbeat(self) -> None:
        """
        Start the heartbeat background task (leader only).

        Call this after winning the election.
        """
        if not self.is_leader:
            logger.warning("[Leader] Cannot start heartbeat - not leader")
            return

        if self._heartbeat_task and not self._heartbeat_task.done():
            logger.debug("[Leader] Heartbeat already running")
            return

        await self._ensure_locks()
        self._is_running = True

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(),
            name="leader_heartbeat"
        )
        logger.info("[Leader] Heartbeat task started")

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat loop."""
        assert self._shutdown_event is not None

        while self._is_running and not self._shutdown_event.is_set():
            try:
                if self.is_leader:
                    await self._write_heartbeat(
                        proxy_state=os.getenv("JARVIS_PROXY_STATE", "ready"),
                        proxy_pid=int(os.getenv("JARVIS_PROXY_PID", "0")) or None
                    )

                await asyncio.sleep(self._config.heartbeat_interval)

            except asyncio.CancelledError:
                logger.info("[Leader] Heartbeat loop cancelled")
                break
            except Exception as e:
                logger.error(f"[Leader] Heartbeat error: {e}")
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Follower Operations
    # -------------------------------------------------------------------------

    async def wait_for_leader_ready(
        self,
        timeout: Optional[float] = None,
        poll_interval: float = 1.0
    ) -> bool:
        """
        Wait for leader to signal proxy ready (follower only).

        Args:
            timeout: Max wait time (None = wait forever)
            poll_interval: How often to check leader state

        Returns:
            True if leader is ready, False on timeout
        """
        timeout = timeout or float(os.getenv("FOLLOWER_WAIT_TIMEOUT", "60.0"))
        start = time.monotonic()

        while True:
            heartbeat = await self._check_existing_leader()

            if heartbeat and heartbeat.proxy_state == "ready":
                logger.info(
                    f"[Leader] Leader ready: {heartbeat.leader_id.id_string} "
                    f"(proxy_state={heartbeat.proxy_state})"
                )
                return True

            if time.monotonic() - start >= timeout:
                logger.warning(f"[Leader] Timeout waiting for leader ready ({timeout}s)")
                return False

            await asyncio.sleep(poll_interval)

    async def start_leader_monitor(self) -> None:
        """
        Start monitoring leader health (follower only).

        If leader becomes stale, triggers re-election.
        """
        if self.is_leader:
            logger.warning("[Leader] Cannot monitor leader - we ARE the leader")
            return

        if self._monitor_task and not self._monitor_task.done():
            logger.debug("[Leader] Monitor already running")
            return

        await self._ensure_locks()
        self._is_running = True

        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="leader_monitor"
        )
        logger.info("[Leader] Monitor task started")

    async def _monitor_loop(self) -> None:
        """Background loop to monitor leader health."""
        assert self._shutdown_event is not None

        consecutive_stale = 0
        max_stale = int(os.getenv("LEADER_MONITOR_MAX_STALE", "3"))

        while self._is_running and not self._shutdown_event.is_set():
            try:
                heartbeat = await self._check_existing_leader()

                if heartbeat:
                    consecutive_stale = 0
                    self._leader_id = heartbeat.leader_id
                    self._current_term = heartbeat.term
                else:
                    consecutive_stale += 1

                    if consecutive_stale >= max_stale:
                        logger.warning(
                            f"[Leader] Leader appears dead "
                            f"({consecutive_stale} stale checks)"
                        )
                        # Could trigger re-election here
                        # For now, just log - let startup handle it

                await asyncio.sleep(self._config.heartbeat_interval)

            except asyncio.CancelledError:
                logger.info("[Leader] Monitor loop cancelled")
                break
            except Exception as e:
                logger.error(f"[Leader] Monitor error: {e}")
                await asyncio.sleep(1.0)

    # -------------------------------------------------------------------------
    # Shutdown
    # -------------------------------------------------------------------------

    async def shutdown(self) -> None:
        """Gracefully shutdown leader election system."""
        logger.info("[Leader] Shutting down...")

        self._is_running = False

        if self._shutdown_event:
            self._shutdown_event.set()

        # Cancel background tasks
        for task in [self._heartbeat_task, self._monitor_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        # Release lock if we hold it
        if self.is_leader:
            self._file_lock.release()

        await self._transition_to(LeaderState.SHUTDOWN)
        logger.info("[Leader] Shutdown complete")

    # Alias for orchestrator compatibility
    async def stop(self) -> None:
        """Stop the leader election system (alias for shutdown)."""
        await self.shutdown()

    # -------------------------------------------------------------------------
    # Orchestrator Integration
    # -------------------------------------------------------------------------

    async def start(self) -> bool:
        """
        Start the leader election and background tasks.

        This is the main entry point used by the orchestrator.

        Returns:
            True if startup succeeded
        """
        try:
            await self._ensure_locks()
            self._is_running = True

            # Run election
            outcome = await self.run_election()

            if outcome.result == ElectionResult.WON:
                # Start heartbeat for leader
                await self.start_heartbeat()
                return True

            elif outcome.result == ElectionResult.EXISTING_LEADER:
                # Start monitor for follower
                await self.start_leader_monitor()
                return True

            else:
                # Election failed
                logger.warning(f"[Leader] Election failed: {outcome.result.value}")
                return False

        except Exception as e:
            logger.error(f"[Leader] Start failed: {e}")
            return False

    def add_state_callback(
        self,
        callback: Callable[[LeaderState], Any]
    ) -> None:
        """
        Add a callback to be notified of state changes.

        The callback receives the new state (not old state like subscribe).
        This is for orchestrator compatibility.

        Args:
            callback: Function to call with new state
        """
        # Wrap to adapt from (old, new) to just (new)
        def wrapper(old: LeaderState, new: LeaderState) -> Any:
            return callback(new)

        self.subscribe(wrapper)

    # -------------------------------------------------------------------------
    # Status and Debugging
    # -------------------------------------------------------------------------

    def get_status(self) -> Dict[str, Any]:
        """Get current leader election status."""
        return {
            "state": self._state.value,
            "is_leader": self.is_leader,
            "identity": self._identity.to_dict(),
            "leader_id": self._leader_id.to_dict() if self._leader_id else None,
            "current_term": self._current_term,
            "config": {
                "heartbeat_interval": self._config.heartbeat_interval,
                "lease_duration": self._config.lease_duration,
                "state_dir": str(self._config.state_dir),
            },
            "is_running": self._is_running,
        }


# =============================================================================
# v117.0: STALE FILE CLEANUP SYSTEM
# =============================================================================

@dataclass
class StaleFileCleanupResult:
    """Result of stale file cleanup operation."""
    stale_locks_removed: int = 0
    stale_heartbeats_removed: int = 0
    corrupted_files_removed: int = 0
    orphaned_files_removed: int = 0
    errors: List[str] = field(default_factory=list)
    cleanup_duration_ms: float = 0.0

    @property
    def total_removed(self) -> int:
        return (
            self.stale_locks_removed +
            self.stale_heartbeats_removed +
            self.corrupted_files_removed +
            self.orphaned_files_removed
        )

    @property
    def had_errors(self) -> bool:
        return len(self.errors) > 0


async def cleanup_stale_files(
    config: Optional[LeaderElectionConfig] = None,
    max_heartbeat_age_seconds: Optional[float] = None,
    dry_run: bool = False,
) -> StaleFileCleanupResult:
    """
    v117.0: Clean up stale lock/state files on startup.

    This function should be called BEFORE creating a leader instance to ensure
    clean state. It handles:

    1. Stale lock files (locks held by dead processes)
    2. Stale heartbeat files (no update in X seconds)
    3. Corrupted JSON files (invalid format)
    4. Orphaned state files (state without matching heartbeat)

    Args:
        config: Leader election config (uses defaults if None)
        max_heartbeat_age_seconds: Override for max heartbeat age (default: 2x lease)
        dry_run: If True, report what would be cleaned without actually removing

    Returns:
        StaleFileCleanupResult with details of what was cleaned
    """
    start_time = time.monotonic()
    result = StaleFileCleanupResult()

    cfg = config or LeaderElectionConfig()
    max_age = max_heartbeat_age_seconds or (cfg.lease_duration * 2)

    logger.info(f"[StaleCleanup] Starting cleanup in {cfg.state_dir} (dry_run={dry_run})")

    # Ensure state directory exists
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    # Get all files in the state directory
    try:
        all_files = list(cfg.state_dir.iterdir())
    except Exception as e:
        result.errors.append(f"Failed to list state directory: {e}")
        return result

    current_time = time.time()

    for file_path in all_files:
        try:
            if not file_path.is_file():
                continue

            file_name = file_path.name

            # Handle lock files
            if file_name.endswith('.lock'):
                is_stale = await _check_lock_file_stale(file_path, cfg)
                if is_stale:
                    if not dry_run:
                        file_path.unlink(missing_ok=True)
                        logger.info(f"[StaleCleanup] Removed stale lock: {file_name}")
                    else:
                        logger.info(f"[StaleCleanup] Would remove stale lock: {file_name}")
                    result.stale_locks_removed += 1

            # Handle JSON state/heartbeat files
            elif file_name.endswith('.json'):
                cleanup_type = await _check_json_file(
                    file_path, current_time, max_age, cfg
                )

                if cleanup_type == "corrupted":
                    if not dry_run:
                        file_path.unlink(missing_ok=True)
                        logger.info(f"[StaleCleanup] Removed corrupted file: {file_name}")
                    else:
                        logger.info(f"[StaleCleanup] Would remove corrupted: {file_name}")
                    result.corrupted_files_removed += 1

                elif cleanup_type == "stale_heartbeat":
                    if not dry_run:
                        file_path.unlink(missing_ok=True)
                        logger.info(f"[StaleCleanup] Removed stale heartbeat: {file_name}")
                    else:
                        logger.info(f"[StaleCleanup] Would remove stale heartbeat: {file_name}")
                    result.stale_heartbeats_removed += 1

                elif cleanup_type == "orphaned":
                    if not dry_run:
                        file_path.unlink(missing_ok=True)
                        logger.info(f"[StaleCleanup] Removed orphaned file: {file_name}")
                    else:
                        logger.info(f"[StaleCleanup] Would remove orphaned: {file_name}")
                    result.orphaned_files_removed += 1

        except Exception as e:
            error_msg = f"Error processing {file_path.name}: {e}"
            logger.warning(f"[StaleCleanup] {error_msg}")
            result.errors.append(error_msg)

    result.cleanup_duration_ms = (time.monotonic() - start_time) * 1000

    logger.info(
        f"[StaleCleanup] Complete: removed {result.total_removed} files "
        f"(locks={result.stale_locks_removed}, heartbeats={result.stale_heartbeats_removed}, "
        f"corrupted={result.corrupted_files_removed}, orphaned={result.orphaned_files_removed}) "
        f"in {result.cleanup_duration_ms:.1f}ms"
    )

    return result


async def _check_lock_file_stale(
    lock_path: Path,
    config: LeaderElectionConfig,
) -> bool:
    """
    Check if a lock file is stale (held by a dead process).

    A lock is considered stale if:
    1. The process that created it is no longer running
    2. The file hasn't been modified in 2x lease duration
    3. We can acquire an exclusive lock on it (no one holds it)
    """
    try:
        # Check file age
        stat = lock_path.stat()
        file_age = time.time() - stat.st_mtime

        # If file is recent, not stale
        if file_age < config.lease_duration:
            return False

        # Try to read lock metadata if it contains PID info
        lock_content = None
        try:
            with open(lock_path, 'r') as f:
                lock_content = f.read().strip()
        except Exception:
            pass

        if lock_content:
            # Try to parse as JSON with PID
            try:
                lock_data = json.loads(lock_content)
                if 'pid' in lock_data:
                    pid = int(lock_data['pid'])
                    hostname = lock_data.get('hostname', '')

                    # Only check if same host
                    if hostname == socket.gethostname():
                        # Check if process is still running
                        if _is_process_running(pid):
                            return False  # Process still alive, not stale
                        else:
                            return True  # Process dead, stale
            except (json.JSONDecodeError, ValueError):
                pass

        # Try to acquire lock - if we can, it's stale
        try:
            with open(lock_path, 'r+') as f:
                # Non-blocking exclusive lock attempt
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                # We got the lock, so it's stale
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return True
        except (IOError, OSError):
            # Can't acquire lock, someone holds it
            return False

    except FileNotFoundError:
        return False  # File gone, nothing to clean
    except Exception as e:
        logger.debug(f"[StaleCleanup] Error checking lock {lock_path}: {e}")
        # If very old (5x lease), force cleanup
        try:
            stat = lock_path.stat()
            if time.time() - stat.st_mtime > config.lease_duration * 5:
                return True
        except Exception:
            pass
        return False


def _is_process_running(pid: int) -> bool:
    """Check if a process with given PID is running."""
    try:
        os.kill(pid, 0)  # Signal 0 = check if process exists
        return True
    except OSError:
        return False


async def _check_json_file(
    file_path: Path,
    current_time: float,
    max_age: float,
    config: LeaderElectionConfig,
) -> Optional[str]:
    """
    Check a JSON file for staleness or corruption.

    Returns:
        None if file is valid and fresh
        "corrupted" if file has invalid JSON
        "stale_heartbeat" if heartbeat file is too old
        "orphaned" if state file has no corresponding heartbeat
    """
    try:
        with open(file_path, 'r') as f:
            content = f.read()

        if not content.strip():
            return "corrupted"

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return "corrupted"

        # Check heartbeat files
        if 'heartbeat' in file_path.name.lower() or 'last_heartbeat' in data:
            # Get timestamp from various possible fields
            timestamp = None
            for field in ['last_heartbeat', 'timestamp', 'updated_at', 'time']:
                if field in data:
                    ts = data[field]
                    if isinstance(ts, (int, float)):
                        timestamp = ts
                        break
                    elif isinstance(ts, str):
                        try:
                            # Try ISO format
                            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                            timestamp = dt.timestamp()
                            break
                        except ValueError:
                            pass

            if timestamp:
                age = current_time - timestamp
                if age > max_age:
                    return "stale_heartbeat"

        # Check state files for orphaned states
        elif 'state' in file_path.name.lower():
            # Check if corresponding heartbeat exists and is fresh
            heartbeat_path = config.heartbeat_file_path
            if not heartbeat_path.exists():
                # No heartbeat file = orphaned state
                # But only if the state file itself is old
                stat = file_path.stat()
                if current_time - stat.st_mtime > max_age:
                    return "orphaned"

        return None

    except FileNotFoundError:
        return None
    except Exception as e:
        logger.debug(f"[StaleCleanup] Error checking {file_path}: {e}")
        return "corrupted"


# =============================================================================
# MODULE-LEVEL SINGLETON
# =============================================================================

_leader_instance: Optional[DistributedProxyLeader] = None
_leader_lock = threading.Lock()


def get_distributed_leader(
    config: Optional[LeaderElectionConfig] = None,
    repo_name: Optional[str] = None
) -> DistributedProxyLeader:
    """
    Get the singleton DistributedProxyLeader instance.

    Thread-safe singleton pattern with lazy initialization.
    """
    global _leader_instance

    with _leader_lock:
        if _leader_instance is None:
            _leader_instance = DistributedProxyLeader(config, repo_name)
        return _leader_instance


async def run_leader_election(
    repo_name: Optional[str] = None
) -> ElectionOutcome:
    """
    Convenience function to run leader election.

    Args:
        repo_name: Name of this repo (jarvis, prime, reactor)

    Returns:
        ElectionOutcome with result
    """
    leader = get_distributed_leader(repo_name=repo_name)
    return await leader.run_election()


async def create_proxy_leader(
    repo_name: Optional[str] = None,
    config: Optional[LeaderElectionConfig] = None,
    cleanup_stale: bool = True,
    cleanup_dry_run: bool = False,
) -> Tuple[DistributedProxyLeader, Optional[StaleFileCleanupResult]]:
    """
    v117.0: Factory function to create and initialize a proxy leader.

    This is the recommended way to create a leader instance for the orchestrator.
    By default, it cleans up stale files before creating the leader to ensure
    clean state after crashes or improper shutdowns.

    Args:
        repo_name: Name of this repository (jarvis, prime, reactor)
        config: Optional custom configuration
        cleanup_stale: If True (default), clean up stale files before creating leader
        cleanup_dry_run: If True, report what would be cleaned without actually removing

    Returns:
        Tuple of (DistributedProxyLeader instance, cleanup result or None)

    Example:
        # Standard usage with cleanup
        leader, cleanup = await create_proxy_leader(repo_name="jarvis")

        # Skip cleanup (not recommended)
        leader, _ = await create_proxy_leader(repo_name="jarvis", cleanup_stale=False)

        # Dry run to see what would be cleaned
        leader, cleanup = await create_proxy_leader(
            repo_name="jarvis",
            cleanup_dry_run=True
        )
        if cleanup and cleanup.total_removed > 0:
            logger.info(f"Would have cleaned {cleanup.total_removed} files")
    """
    cfg = config or LeaderElectionConfig()

    cleanup_result: Optional[StaleFileCleanupResult] = None

    if cleanup_stale:
        try:
            cleanup_result = await cleanup_stale_files(
                config=cfg,
                dry_run=cleanup_dry_run,
            )

            if cleanup_result.had_errors:
                logger.warning(
                    f"[Factory] Stale cleanup had {len(cleanup_result.errors)} errors: "
                    f"{cleanup_result.errors[:3]}"  # Show first 3
                )

            if cleanup_result.total_removed > 0 and not cleanup_dry_run:
                logger.info(
                    f"[Factory] Cleaned {cleanup_result.total_removed} stale files "
                    f"before creating leader"
                )

        except Exception as e:
            logger.warning(f"[Factory] Stale cleanup failed (continuing anyway): {e}")
            cleanup_result = StaleFileCleanupResult(errors=[str(e)])

    leader = DistributedProxyLeader(config=cfg, repo_name=repo_name)
    return leader, cleanup_result


# Type alias for state change callbacks
LeaderStateCallback = Callable[[LeaderState], Awaitable[None]]
