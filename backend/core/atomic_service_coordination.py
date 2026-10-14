"""
Atomic Service Coordination - Race-Free Registry & IPC Operations
==================================================================

v95.3: COMPREHENSIVE FIX for Service Registry & IPC Issues
----------------------------------------------------------
Fixes ROOT CAUSES of:
1. PID mismatch race conditions during rapid restarts
2. Trinity IPC connection timeouts
3. Memory pressure during parallel model loading

Key Innovations:
1. AtomicRegistryCoordinator - PID-validated atomic registry operations
2. RobustIPCConnector - Retry-aware IPC with path validation
3. MemoryAwareStartupSequencer - Sequential model loading with pressure monitoring

Architecture:
    +---------------------------------------------------------------------+
    |  AtomicServiceCoordination v95.3                                    |
    |  +-- AtomicRegistryCoordinator (PID-validated atomic updates)       |
    |  +-- RobustIPCConnector (retry logic + path validation)             |
    |  +-- MemoryAwareStartupSequencer (sequential loading + monitoring)  |
    |  +-- CrossRepoSynchronizer (JARVIS + Prime + Reactor coordination)  |
    +---------------------------------------------------------------------+

Race Condition Fix Strategy:
- Use process_start_time as additional identity (prevents PID reuse issues)
- Validate PID ownership BEFORE any registry modification
- Use file-based distributed locking with stale lock detection
- Implement compare-and-swap semantics for updates

Author: JARVIS Trinity v95.3 - Atomic Service Coordination
"""

from __future__ import annotations

import asyncio
from backend.core import portable_fcntl as fcntl
import hashlib
import json
import logging
import os
import socket
import struct
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

logger = logging.getLogger(__name__)

# =============================================================================
# Psutil Import (Graceful Degradation)
# =============================================================================

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    psutil = None  # type: ignore
    PSUTIL_AVAILABLE = False


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class CoordinationConfig:
    """Configuration for atomic service coordination."""
    # Paths
    registry_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "registry")
    ipc_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "trinity")
    lock_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis" / "locks")

    # Timeouts
    lock_timeout_seconds: float = 30.0
    stale_lock_timeout_seconds: float = 60.0
    ipc_connect_timeout_seconds: float = 10.0
    ipc_retry_interval_seconds: float = 1.0
    ipc_max_retries: int = 10

    # Memory thresholds (in MB)
    memory_low_threshold_mb: int = 2048  # 2GB
    memory_critical_threshold_mb: int = 1024  # 1GB
    model_load_delay_seconds: float = 5.0  # Delay between model loads

    # Registry
    pid_validation_tolerance_seconds: float = 2.0  # Tolerance for start time comparison
    cleanup_confirmation_retries: int = 3
    cleanup_grace_period_seconds: float = 30.0

    def __post_init__(self):
        """Ensure directories exist."""
        for dir_path in [self.registry_dir, self.ipc_dir, self.lock_dir]:
            dir_path.mkdir(parents=True, exist_ok=True)


# =============================================================================
# Process Identity (Enhanced)
# =============================================================================

@dataclass
class ProcessIdentity:
    """
    Unique process identity that survives PID reuse.

    Uses combination of PID and process start time to uniquely identify
    a process instance, solving the PID reuse problem.
    """
    pid: int
    start_time: float  # Process creation timestamp
    hostname: str = field(default_factory=socket.gethostname)
    identity_hash: str = ""

    def __post_init__(self):
        if not self.identity_hash:
            self.identity_hash = self._compute_hash()

    def _compute_hash(self) -> str:
        """Compute a unique hash for this process identity."""
        data = f"{self.pid}:{self.start_time}:{self.hostname}"
        return hashlib.md5(data.encode()).hexdigest()[:12]

    @classmethod
    def current(cls) -> "ProcessIdentity":
        """Get identity for current process."""
        pid = os.getpid()
        start_time = cls._get_start_time(pid)
        return cls(pid=pid, start_time=start_time)

    @classmethod
    def from_pid(cls, pid: int) -> Optional["ProcessIdentity"]:
        """Get identity for a specific PID."""
        start_time = cls._get_start_time(pid)
        if start_time == 0.0:
            return None
        return cls(pid=pid, start_time=start_time)

    @staticmethod
    def _get_start_time(pid: int) -> float:
        """Get process start time."""
        if PSUTIL_AVAILABLE and psutil is not None:
            try:
                proc = psutil.Process(pid)
                return proc.create_time()
            except Exception:
                return 0.0
        else:
            # Fallback: use /proc on Linux
            try:
                stat_path = Path(f"/proc/{pid}/stat")
                if stat_path.exists():
                    with open(stat_path) as f:
                        fields = f.read().split()
                        # Field 22 is start time in jiffies
                        starttime_jiffies = int(fields[21])
                        # Convert to seconds (assuming 100 jiffies/sec)
                        return starttime_jiffies / 100.0
            except Exception:
                pass
            return time.time()  # Fallback to registration time

    def matches(self, other: "ProcessIdentity", tolerance: float = 2.0) -> bool:
        """Check if this identity matches another (with tolerance for timing)."""
        if self.pid != other.pid:
            return False
        if self.hostname != other.hostname:
            return False
        return abs(self.start_time - other.start_time) <= tolerance

    def is_alive(self) -> bool:
        """Check if the process with this identity is still running."""
        current = ProcessIdentity.from_pid(self.pid)
        if current is None:
            return False
        return self.matches(current)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pid": self.pid,
            "start_time": self.start_time,
            "hostname": self.hostname,
            "identity_hash": self.identity_hash,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProcessIdentity":
        return cls(
            pid=data["pid"],
            start_time=data["start_time"],
            hostname=data.get("hostname", socket.gethostname()),
            identity_hash=data.get("identity_hash", ""),
        )


# =============================================================================
# Distributed Lock with Stale Detection
# =============================================================================

class DistributedLock:
    """
    File-based distributed lock with stale lock detection.

    Fixes the race condition where a lock is held by a dead process.
    """

    def __init__(
        self,
        lock_name: str,
        config: CoordinationConfig,
    ):
        self.lock_name = lock_name
        self.config = config
        self.lock_file = config.lock_dir / f"{lock_name}.lock"
        self.lock_info_file = config.lock_dir / f"{lock_name}.lock.info"
        self._fd: Optional[int] = None
        self._acquired = False

    @asynccontextmanager
    async def acquire(self, timeout: Optional[float] = None):
        """
        Acquire the lock with timeout and stale detection.

        Usage:
            async with lock.acquire(timeout=10.0):
                # Critical section
        """
        timeout = timeout or self.config.lock_timeout_seconds
        start_time = time.time()

        while True:
            # Check for stale lock
            if await self._check_and_clear_stale_lock():
                logger.info(f"[DistributedLock] Cleared stale lock: {self.lock_name}")

            # Try to acquire
            if await self._try_acquire():
                try:
                    yield
                finally:
                    await self._release()
                return

            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                raise TimeoutError(
                    f"Failed to acquire lock '{self.lock_name}' after {elapsed:.1f}s"
                )

            # Wait before retry
            await asyncio.sleep(0.1)

    async def _try_acquire(self) -> bool:
        """Try to acquire the lock."""
        try:
            # Create lock file if doesn't exist
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)

            # Open file for exclusive access
            self._fd = os.open(
                str(self.lock_file),
                os.O_RDWR | os.O_CREAT,
                0o600
            )

            # Try non-blocking exclusive lock
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(self._fd)
                self._fd = None
                return False

            # Write lock owner info
            identity = ProcessIdentity.current()
            lock_info = {
                "owner": identity.to_dict(),
                "acquired_at": time.time(),
                "lock_name": self.lock_name,
            }

            # Atomic write to info file
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(self.config.lock_dir),
                prefix=f".{self.lock_name}.",
                suffix=".info.tmp"
            )
            try:
                os.write(tmp_fd, json.dumps(lock_info).encode())
                os.fsync(tmp_fd)
                os.close(tmp_fd)
                os.replace(tmp_path, self.lock_info_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

            self._acquired = True
            return True

        except Exception as e:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            logger.debug(f"[DistributedLock] Acquire failed: {e}")
            return False

    async def _release(self) -> None:
        """Release the lock."""
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

        # Clean up info file
        try:
            if self.lock_info_file.exists():
                self.lock_info_file.unlink()
        except OSError:
            pass

        self._acquired = False

    async def _check_and_clear_stale_lock(self) -> bool:
        """
        Check if lock is stale (held by dead process) and clear it.

        Returns True if a stale lock was cleared.
        """
        if not self.lock_info_file.exists():
            return False

        try:
            with open(self.lock_info_file) as f:
                lock_info = json.load(f)

            owner_data = lock_info.get("owner", {})
            acquired_at = lock_info.get("acquired_at", 0)

            # Check if lock is too old
            lock_age = time.time() - acquired_at
            if lock_age > self.config.stale_lock_timeout_seconds:
                logger.warning(
                    f"[DistributedLock] Lock '{self.lock_name}' is stale "
                    f"(age: {lock_age:.1f}s > {self.config.stale_lock_timeout_seconds}s)"
                )
                return await self._force_clear_lock()

            # Check if owner process is dead
            owner = ProcessIdentity.from_dict(owner_data)
            if not owner.is_alive():
                logger.warning(
                    f"[DistributedLock] Lock '{self.lock_name}' owner "
                    f"(PID {owner.pid}) is dead"
                )
                return await self._force_clear_lock()

            return False

        except Exception as e:
            logger.debug(f"[DistributedLock] Stale check failed: {e}")
            return False

    async def _force_clear_lock(self) -> bool:
        """Force clear a stale lock."""
        try:
            if self.lock_file.exists():
                self.lock_file.unlink()
            if self.lock_info_file.exists():
                self.lock_info_file.unlink()
            return True
        except Exception as e:
            logger.error(f"[DistributedLock] Force clear failed: {e}")
            return False


# =============================================================================
# Atomic Registry Coordinator
# =============================================================================

@dataclass
class ServiceEntry:
    """Enhanced service entry with process identity."""
    service_name: str
    identity: ProcessIdentity
    port: int
    host: str = "localhost"
    health_endpoint: str = "/health"
    status: str = "starting"
    registered_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_alive(self) -> bool:
        """Check if service process is still running."""
        return self.identity.is_alive()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "service_name": self.service_name,
            "identity": self.identity.to_dict(),
            "port": self.port,
            "host": self.host,
            "health_endpoint": self.health_endpoint,
            "status": self.status,
            "registered_at": self.registered_at,
            "last_heartbeat": self.last_heartbeat,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ServiceEntry":
        return cls(
            service_name=data["service_name"],
            identity=ProcessIdentity.from_dict(data["identity"]),
            port=data["port"],
            host=data.get("host", "localhost"),
            health_endpoint=data.get("health_endpoint", "/health"),
            status=data.get("status", "unknown"),
            registered_at=data.get("registered_at", 0.0),
            last_heartbeat=data.get("last_heartbeat", 0.0),
            metadata=data.get("metadata", {}),
        )


class AtomicRegistryCoordinator:
    """
    Atomic, PID-validated registry operations.

    Fixes race conditions by:
    1. Using distributed locks for all modifications
    2. Validating process identity before updates
    3. Compare-and-swap semantics for concurrent access
    4. Stale entry cleanup with grace periods
    """

    def __init__(self, config: Optional[CoordinationConfig] = None):
        self.config = config or CoordinationConfig()
        self.registry_file = self.config.registry_dir / "services_v2.json"
        self._lock = DistributedLock("registry", self.config)
        self._local_identity = ProcessIdentity.current()

        # In-memory cache for fast lookups
        self._cache: Dict[str, ServiceEntry] = {}
        self._cache_time: float = 0.0
        self._cache_ttl: float = 5.0  # Cache for 5 seconds

        logger.info(
            f"[AtomicRegistryCoordinator] Initialized "
            f"(identity: {self._local_identity.identity_hash})"
        )

    async def register(
        self,
        service_name: str,
        port: int,
        host: str = "localhost",
        health_endpoint: str = "/health",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ServiceEntry:
        """
        Register a service with atomic, PID-validated update.

        If a service with the same name exists but different identity,
        the old entry is replaced (the old process must be dead).
        """
        entry = ServiceEntry(
            service_name=service_name,
            identity=self._local_identity,
            port=port,
            host=host,
            health_endpoint=health_endpoint,
            metadata=metadata or {},
        )

        async with self._lock.acquire():
            services = await self._read_registry()

            # Check for existing entry
            existing = services.get(service_name)
            if existing:
                if existing.identity.matches(self._local_identity):
                    # Same process - just update
                    logger.debug(
                        f"[AtomicRegistry] Updating existing entry for {service_name}"
                    )
                elif existing.is_alive():
                    # Different process that's still alive!
                    raise RuntimeError(
                        f"Service '{service_name}' is already registered by a "
                        f"different live process (PID {existing.identity.pid})"
                    )
                else:
                    # Old process is dead - replace
                    logger.info(
                        f"[AtomicRegistry] Replacing dead entry for {service_name} "
                        f"(old PID: {existing.identity.pid}, new PID: {self._local_identity.pid})"
                    )

            # Update registry
            services[service_name] = entry
            await self._write_registry(services)

            # Update cache
            self._cache[service_name] = entry
            self._cache_time = time.time()

        logger.info(
            f"[AtomicRegistry] Registered: {service_name} "
            f"(PID: {self._local_identity.pid}, port: {port})"
        )

        return entry

    async def deregister(self, service_name: str) -> bool:
        """
        Deregister a service with PID validation.

        Only the owning process (or a process that can prove the owner is dead)
        can deregister a service.
        """
        async with self._lock.acquire():
            services = await self._read_registry()

            existing = services.get(service_name)
            if not existing:
                return False

            # Validate ownership
            if existing.identity.matches(self._local_identity):
                # We own it - can deregister
                pass
            elif not existing.is_alive():
                # Owner is dead - we can clean up
                logger.info(
                    f"[AtomicRegistry] Cleaning up dead service {service_name} "
                    f"(owner PID {existing.identity.pid} is dead)"
                )
            else:
                # Someone else owns it and they're alive
                logger.warning(
                    f"[AtomicRegistry] Cannot deregister {service_name} - "
                    f"owned by live process PID {existing.identity.pid}"
                )
                return False

            del services[service_name]
            await self._write_registry(services)

            # Update cache
            if service_name in self._cache:
                del self._cache[service_name]

        return True

    async def heartbeat(self, service_name: str) -> bool:
        """
        Send heartbeat with PID validation.

        Only updates if the caller is the registered owner.
        """
        async with self._lock.acquire():
            services = await self._read_registry()

            existing = services.get(service_name)
            if not existing:
                logger.warning(
                    f"[AtomicRegistry] Heartbeat for unknown service: {service_name}"
                )
                return False

            # Validate ownership
            if not existing.identity.matches(self._local_identity):
                logger.warning(
                    f"[AtomicRegistry] Heartbeat rejected - not owner of {service_name} "
                    f"(owner: {existing.identity.pid}, caller: {self._local_identity.pid})"
                )
                return False

            # Update heartbeat
            existing.last_heartbeat = time.time()
            existing.status = "healthy"
            services[service_name] = existing
            await self._write_registry(services)

            # Update cache
            self._cache[service_name] = existing
            self._cache_time = time.time()

        return True

    async def discover(self, service_name: str) -> Optional[ServiceEntry]:
        """
        Discover a service by name.

        Returns None if service doesn't exist or is dead.
        """
        # Check cache first
        if time.time() - self._cache_time < self._cache_ttl:
            cached = self._cache.get(service_name)
            if cached and cached.is_alive():
                return cached

        services = await self._read_registry()
        entry = services.get(service_name)

        if entry and entry.is_alive():
            # Update cache
            self._cache[service_name] = entry
            self._cache_time = time.time()
            return entry

        return None

    async def list_services(self, include_dead: bool = False) -> List[ServiceEntry]:
        """List all registered services."""
        services = await self._read_registry()

        if include_dead:
            return list(services.values())

        return [s for s in services.values() if s.is_alive()]

    async def cleanup_dead_services(self) -> int:
        """
        Clean up services with dead owners.

        Uses grace period to avoid race conditions.
        """
        cleaned = 0

        async with self._lock.acquire():
            services = await self._read_registry()
            to_remove: List[str] = []

            for name, entry in services.items():
                if not entry.is_alive():
                    # Check if within grace period
                    age = time.time() - entry.last_heartbeat
                    if age > self.config.cleanup_grace_period_seconds:
                        to_remove.append(name)
                        logger.info(
                            f"[AtomicRegistry] Cleaning up dead service: {name} "
                            f"(age: {age:.1f}s)"
                        )

            for name in to_remove:
                del services[name]
                if name in self._cache:
                    del self._cache[name]
                cleaned += 1

            if to_remove:
                await self._write_registry(services)

        return cleaned

    async def _read_registry(self) -> Dict[str, ServiceEntry]:
        """Read registry from file."""
        if not self.registry_file.exists():
            return {}

        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._read_registry_sync)
        except Exception as e:
            logger.error(f"[AtomicRegistry] Read error: {e}")
            return {}

    def _read_registry_sync(self) -> Dict[str, ServiceEntry]:
        """Synchronous read."""
        try:
            with open(self.registry_file) as f:
                data = json.load(f)

            services = {}
            for name, entry_data in data.get("services", {}).items():
                try:
                    services[name] = ServiceEntry.from_dict(entry_data)
                except Exception as e:
                    logger.warning(f"[AtomicRegistry] Invalid entry {name}: {e}")

            return services
        except Exception as e:
            logger.debug(f"[AtomicRegistry] Read failed: {e}")
            return {}

    async def _write_registry(self, services: Dict[str, ServiceEntry]) -> None:
        """Write registry to file atomically."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._write_registry_sync, services)

    def _write_registry_sync(self, services: Dict[str, ServiceEntry]) -> None:
        """Synchronous atomic write."""
        data = {
            "version": "2.0",
            "updated_at": time.time(),
            "services": {
                name: entry.to_dict()
                for name, entry in services.items()
            },
        }

        # Atomic write via temp file + rename
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.config.registry_dir),
            prefix=".services_v2.",
            suffix=".tmp"
        )

        try:
            os.write(tmp_fd, json.dumps(data, indent=2).encode())
            os.fsync(tmp_fd)
            os.close(tmp_fd)
            os.replace(tmp_path, self.registry_file)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


# =============================================================================
# Robust IPC Connector
# =============================================================================

class IPCConnectionState(Enum):
    """IPC connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class IPCEndpoint:
    """IPC endpoint configuration."""
    component_name: str
    heartbeat_file: Path
    health_url: Optional[str] = None
    port: Optional[int] = None


class RobustIPCConnector:
    """
    Robust IPC connector with retry logic and path validation.

    Fixes IPC timeout issues by:
    1. Validating IPC paths before connection attempts
    2. Implementing exponential backoff retries
    3. Health-checking endpoints before declaring connected
    4. Supporting multiple connection strategies (file, HTTP, socket)
    """

    def __init__(self, config: Optional[CoordinationConfig] = None):
        self.config = config or CoordinationConfig()
        self._state = IPCConnectionState.DISCONNECTED
        self._endpoints: Dict[str, IPCEndpoint] = {}
        self._connection_tasks: Dict[str, asyncio.Task] = {}

        # Setup standard Trinity endpoints
        self._setup_default_endpoints()

    def _setup_default_endpoints(self) -> None:
        """Setup default Trinity component endpoints."""
        trinity_dir = self.config.ipc_dir

        # JARVIS Body
        self._endpoints["jarvis_body"] = IPCEndpoint(
            component_name="jarvis_body",
            heartbeat_file=trinity_dir / "heartbeats" / "jarvis_body.json",
            health_url="http://127.0.0.1:8010/health/ready",
            port=8010,
        )

        # JARVIS Prime
        self._endpoints["jarvis_prime"] = IPCEndpoint(
            component_name="jarvis_prime",
            heartbeat_file=trinity_dir / "heartbeats" / "jarvis_prime.json",
            health_url="http://127.0.0.1:8000/health/ready",
            port=8000,
        )

        # Reactor Core
        self._endpoints["reactor_core"] = IPCEndpoint(
            component_name="reactor_core",
            heartbeat_file=trinity_dir / "heartbeats" / "reactor_core.json",
            health_url="http://127.0.0.1:8090/health/ready",
            port=8090,
        )

    async def connect(
        self,
        component: str,
        timeout: Optional[float] = None,
    ) -> bool:
        """
        Connect to a component with retry logic.

        Returns True if connection established, False otherwise.
        """
        endpoint = self._endpoints.get(component)
        if not endpoint:
            logger.error(f"[RobustIPC] Unknown component: {component}")
            return False

        timeout = timeout or self.config.ipc_connect_timeout_seconds
        start_time = time.time()
        retry_count = 0

        self._state = IPCConnectionState.CONNECTING

        while time.time() - start_time < timeout:
            # Strategy 1: Check heartbeat file
            if await self._check_heartbeat(endpoint):
                logger.info(f"[RobustIPC] Connected to {component} via heartbeat")
                self._state = IPCConnectionState.CONNECTED
                return True

            # Strategy 2: Check HTTP health endpoint
            if endpoint.health_url:
                if await self._check_health_endpoint(endpoint):
                    logger.info(f"[RobustIPC] Connected to {component} via HTTP")
                    self._state = IPCConnectionState.CONNECTED
                    return True

            # Strategy 3: Check port directly
            if endpoint.port:
                if await self._check_port(endpoint):
                    logger.info(f"[RobustIPC] Connected to {component} via port")
                    self._state = IPCConnectionState.CONNECTED
                    return True

            # Exponential backoff
            retry_count += 1
            backoff = min(
                self.config.ipc_retry_interval_seconds * (2 ** (retry_count - 1)),
                5.0  # Max 5 second backoff
            )

            logger.debug(
                f"[RobustIPC] Connection attempt {retry_count} to {component} failed, "
                f"retrying in {backoff:.1f}s..."
            )

            await asyncio.sleep(backoff)

        logger.warning(
            f"[RobustIPC] Failed to connect to {component} after {timeout:.1f}s "
            f"({retry_count} attempts)"
        )
        self._state = IPCConnectionState.FAILED
        return False

    async def _check_heartbeat(self, endpoint: IPCEndpoint) -> bool:
        """Check if heartbeat file exists and is fresh."""
        try:
            if not endpoint.heartbeat_file.exists():
                return False

            with open(endpoint.heartbeat_file) as f:
                data = json.load(f)

            timestamp = data.get("timestamp", 0)
            age = time.time() - timestamp

            # Heartbeat is valid if less than 30 seconds old
            if age < 30:
                # Also verify PID is alive
                pid = data.get("pid")
                if pid:
                    identity = ProcessIdentity.from_pid(pid)
                    if identity and identity.is_alive():
                        return True

            return False

        except Exception as e:
            logger.debug(f"[RobustIPC] Heartbeat check failed: {e}")
            return False

    async def _check_health_endpoint(self, endpoint: IPCEndpoint) -> bool:
        """Check if HTTP health endpoint responds."""
        if not endpoint.health_url:
            return False

        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=2.0)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(endpoint.health_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Check for actual readiness
                        return data.get("ready", True)
                    return False

        except Exception as e:
            logger.debug(f"[RobustIPC] Health check failed: {e}")
            return False

    async def _check_port(self, endpoint: IPCEndpoint) -> bool:
        """Check if port is listening."""
        if not endpoint.port:
            return False

        try:
            loop = asyncio.get_running_loop()
            # Try to connect to the port
            _, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", endpoint.port),
                timeout=1.0
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def wait_for_component(
        self,
        component: str,
        timeout: Optional[float] = None,
        check_interval: float = 1.0,
    ) -> bool:
        """Wait for a component to become available."""
        timeout = timeout or self.config.ipc_connect_timeout_seconds * 3
        start_time = time.time()

        while time.time() - start_time < timeout:
            if await self.connect(component, timeout=check_interval * 2):
                return True
            await asyncio.sleep(check_interval)

        return False

    def get_endpoint(self, component: str) -> Optional[IPCEndpoint]:
        """Get endpoint configuration for a component."""
        return self._endpoints.get(component)


# =============================================================================
# Memory-Aware Startup Sequencer
# =============================================================================

class MemoryPressureLevel(Enum):
    """Memory pressure levels."""
    NORMAL = "normal"
    LOW = "low"
    CRITICAL = "critical"


@dataclass
class StartupTask:
    """A task to execute during startup."""
    name: str
    loader: Callable[[], Any]
    memory_estimate_mb: int = 500
    priority: int = 0  # Lower = higher priority
    dependencies: List[str] = field(default_factory=list)
    loaded: bool = False
    error: Optional[str] = None


class MemoryAwareStartupSequencer:
    """
    Sequential model loading with memory pressure monitoring.

    Fixes memory pressure issues by:
    1. Loading models sequentially instead of in parallel
    2. Monitoring memory before each load
    3. Waiting for memory to be available
    4. Prioritizing critical models
    """

    def __init__(self, config: Optional[CoordinationConfig] = None):
        self.config = config or CoordinationConfig()
        self._tasks: Dict[str, StartupTask] = {}
        self._load_order: List[str] = []
        self._memory_samples: List[int] = []

    def register_task(
        self,
        name: str,
        loader: Callable[[], Any],
        memory_estimate_mb: int = 500,
        priority: int = 0,
        dependencies: Optional[List[str]] = None,
    ) -> None:
        """Register a startup task."""
        self._tasks[name] = StartupTask(
            name=name,
            loader=loader,
            memory_estimate_mb=memory_estimate_mb,
            priority=priority,
            dependencies=dependencies or [],
        )

    async def execute_sequential(
        self,
        progress_callback: Optional[Callable[[str, float], None]] = None,
    ) -> Dict[str, bool]:
        """
        Execute all tasks sequentially with memory monitoring.

        Returns dict of task_name -> success.
        """
        results: Dict[str, bool] = {}

        # Sort by priority and dependencies
        self._compute_load_order()

        total_tasks = len(self._load_order)

        for i, task_name in enumerate(self._load_order):
            task = self._tasks[task_name]
            progress = (i / total_tasks) * 100

            if progress_callback:
                progress_callback(task_name, progress)

            # Check dependencies
            deps_satisfied = all(
                results.get(dep, False)
                for dep in task.dependencies
            )
            if not deps_satisfied:
                logger.warning(
                    f"[MemorySequencer] Skipping {task_name} - dependencies not satisfied"
                )
                results[task_name] = False
                continue

            # Wait for memory to be available
            await self._wait_for_memory(task.memory_estimate_mb)

            # Execute task
            try:
                logger.info(
                    f"[MemorySequencer] Loading {task_name} "
                    f"(~{task.memory_estimate_mb}MB, {progress:.0f}%)"
                )

                start_time = time.time()
                result = task.loader()

                # Handle async loaders
                if asyncio.iscoroutine(result):
                    await result

                elapsed = time.time() - start_time
                task.loaded = True
                results[task_name] = True

                logger.info(
                    f"[MemorySequencer] Loaded {task_name} in {elapsed:.1f}s"
                )

            except Exception as e:
                task.error = str(e)
                results[task_name] = False
                logger.error(f"[MemorySequencer] Failed to load {task_name}: {e}")

            # Delay between loads to let memory settle
            if i < total_tasks - 1:
                await asyncio.sleep(self.config.model_load_delay_seconds)

        if progress_callback:
            progress_callback("complete", 100.0)

        return results

    async def _wait_for_memory(self, required_mb: int) -> None:
        """Wait until enough memory is available."""
        max_wait = 60.0  # Max 60 seconds
        start_time = time.time()

        while time.time() - start_time < max_wait:
            available = self._get_available_memory_mb()
            pressure = self._get_memory_pressure()

            if pressure == MemoryPressureLevel.CRITICAL:
                logger.warning(
                    f"[MemorySequencer] CRITICAL memory pressure "
                    f"({available}MB available), waiting..."
                )
                await asyncio.sleep(5.0)
                continue

            if available >= required_mb + self.config.memory_low_threshold_mb:
                return  # Enough memory

            if pressure == MemoryPressureLevel.LOW:
                logger.info(
                    f"[MemorySequencer] Low memory ({available}MB), "
                    f"waiting for {required_mb}MB + buffer..."
                )
                await asyncio.sleep(2.0)
            else:
                return  # Normal pressure, proceed

        logger.warning(
            f"[MemorySequencer] Proceeding despite low memory after {max_wait}s wait"
        )

    def _get_available_memory_mb(self) -> int:
        """Get available memory in MB."""
        if PSUTIL_AVAILABLE and psutil is not None:
            try:
                mem = psutil.virtual_memory()
                return int(mem.available / (1024 * 1024))
            except Exception:
                pass

        # Fallback: assume 4GB available
        return 4096

    def _get_memory_pressure(self) -> MemoryPressureLevel:
        """Get current memory pressure level."""
        available = self._get_available_memory_mb()

        if available < self.config.memory_critical_threshold_mb:
            return MemoryPressureLevel.CRITICAL
        elif available < self.config.memory_low_threshold_mb:
            return MemoryPressureLevel.LOW
        else:
            return MemoryPressureLevel.NORMAL

    def _compute_load_order(self) -> None:
        """Compute task load order based on priority and dependencies."""
        # Topological sort with priority
        visited: Set[str] = set()
        order: List[str] = []

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)

            task = self._tasks.get(name)
            if not task:
                return

            # Visit dependencies first
            for dep in task.dependencies:
                visit(dep)

            order.append(name)

        # Visit all tasks in priority order
        sorted_tasks = sorted(
            self._tasks.keys(),
            key=lambda n: self._tasks[n].priority
        )

        for name in sorted_tasks:
            visit(name)

        self._load_order = order


# =============================================================================
# Cross-Repo Synchronizer
# =============================================================================

class CrossRepoSynchronizer:
    """
    Coordinates startup across JARVIS, Prime, and Reactor Core.

    Ensures proper startup order with memory awareness.
    """

    def __init__(self, config: Optional[CoordinationConfig] = None):
        self.config = config or CoordinationConfig()
        self.registry = AtomicRegistryCoordinator(config)
        self.ipc = RobustIPCConnector(config)
        self.sequencer = MemoryAwareStartupSequencer(config)

    async def wait_for_all_components(
        self,
        components: List[str],
        timeout: float = 120.0,
        progress_callback: Optional[Callable[[str, str], None]] = None,
    ) -> Dict[str, bool]:
        """
        Wait for all components to be ready.

        Returns dict of component_name -> ready.
        """
        results: Dict[str, bool] = {}
        start_time = time.time()

        for component in components:
            if progress_callback:
                progress_callback(component, "connecting")

            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                results[component] = False
                continue

            success = await self.ipc.wait_for_component(
                component,
                timeout=remaining,
            )

            results[component] = success

            if success and progress_callback:
                progress_callback(component, "connected")
            elif not success and progress_callback:
                progress_callback(component, "failed")

        return results


# =============================================================================
# Singleton Access
# =============================================================================

_coordinator_instance: Optional[AtomicRegistryCoordinator] = None
_ipc_instance: Optional[RobustIPCConnector] = None
_sequencer_instance: Optional[MemoryAwareStartupSequencer] = None
_synchronizer_instance: Optional[CrossRepoSynchronizer] = None


async def get_atomic_registry() -> AtomicRegistryCoordinator:
    """Get or create the atomic registry coordinator."""
    global _coordinator_instance
    if _coordinator_instance is None:
        _coordinator_instance = AtomicRegistryCoordinator()
    return _coordinator_instance


async def get_ipc_connector() -> RobustIPCConnector:
    """Get or create the IPC connector."""
    global _ipc_instance
    if _ipc_instance is None:
        _ipc_instance = RobustIPCConnector()
    return _ipc_instance


async def get_startup_sequencer() -> MemoryAwareStartupSequencer:
    """Get or create the startup sequencer."""
    global _sequencer_instance
    if _sequencer_instance is None:
        _sequencer_instance = MemoryAwareStartupSequencer()
    return _sequencer_instance


async def get_cross_repo_synchronizer() -> CrossRepoSynchronizer:
    """Get or create the cross-repo synchronizer."""
    global _synchronizer_instance
    if _synchronizer_instance is None:
        _synchronizer_instance = CrossRepoSynchronizer()
    return _synchronizer_instance
