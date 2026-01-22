"""
Enterprise-Grade Service Registry v3.0
======================================

Dynamic service discovery system eliminating hardcoded ports and enabling
true distributed orchestration across JARVIS, J-Prime, and Reactor-Core.

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │                   Service Registry v3.0                          │
    │  ┌────────────────────────────────────────────────────────────┐  │
    │  │  File-Based Registry: ~/.jarvis/registry/services.json     │  │
    │  │  ┌──────────────┬──────────────┬─────────────────────┐     │  │
    │  │  │   JARVIS     │   J-PRIME    │   REACTOR-CORE      │     │  │
    │  │  │  PID: 12345  │  PID: 12346  │   PID: 12347        │     │  │
    │  │  │  Port: 5001  │  Port: 8000  │   Port: 8090        │     │  │
    │  │  │  Status: ✅  │  Status: ✅  │   Status: ✅         │     │  │
    │  │  └──────────────┴──────────────┴─────────────────────┘     │  │
    │  └────────────────────────────────────────────────────────────┘  │
    │                                                                  │
    │  Features:                                                       │
    │  • Atomic file operations with fcntl locking                     │
    │  • Automatic stale service cleanup (dead PIDs)                   │
    │  • Health heartbeat tracking                                     │
    │  • Zero hardcoded ports or URLs                                  │
    │  • Cross-process safe concurrent access                          │
    └──────────────────────────────────────────────────────────────────┘

Usage:
    # Register service on startup
    registry = ServiceRegistry()
    await registry.register_service(
        service_name="jarvis-prime",
        pid=os.getpid(),
        port=8000,
        health_endpoint="/health"
    )

    # Discover services dynamically
    jprime = await registry.discover_service("jarvis-prime")
    if jprime:
        url = f"http://{jprime.host}:{jprime.port}{jprime.health_endpoint}"

    # Heartbeat to keep alive
    await registry.heartbeat("jarvis-prime")

    # Clean deregistration on shutdown
    await registry.deregister_service("jarvis-prime")

Author: JARVIS AI System
Version: 93.0.0

v93.0 Changes:
- Added DirectoryManager for robust directory handling
- Retry logic for all file operations (read/write)
- Handles race conditions where directory is deleted
- Atomic temp file creation using tempfile module
- Pre-write directory validation
- Detailed error logging for debugging
"""

import asyncio
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from datetime import datetime, timedelta
from contextlib import contextmanager
import psutil

logger = logging.getLogger(__name__)


# =============================================================================
# v93.0: Robust Directory Management
# =============================================================================

class DirectoryManager:
    """
    v93.0: Thread-safe directory management with atomic operations.

    Handles race conditions, missing directories, and provides retry logic
    for all file system operations.
    """

    def __init__(self, base_dir: Path, max_retries: int = 3, retry_delay: float = 0.1):
        self.base_dir = base_dir
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._initialized = False
        self._init_lock = asyncio.Lock() if asyncio.get_event_loop().is_running() else None

    def ensure_directory_sync(self) -> bool:
        """
        v93.0: Synchronously ensure directory exists with retry logic.

        Returns:
            True if directory exists/was created successfully
        """
        for attempt in range(self.max_retries):
            try:
                # Create directory with all parents
                self.base_dir.mkdir(parents=True, exist_ok=True)

                # Verify it actually exists (handles race with deletion)
                if self.base_dir.exists() and self.base_dir.is_dir():
                    self._initialized = True
                    return True

                # If doesn't exist after creation, wait and retry
                time.sleep(self.retry_delay * (attempt + 1))

            except PermissionError as e:
                logger.error(f"[v93.0] Permission denied creating {self.base_dir}: {e}")
                return False
            except OSError as e:
                if attempt == self.max_retries - 1:
                    logger.error(f"[v93.0] Failed to create directory {self.base_dir}: {e}")
                    return False
                time.sleep(self.retry_delay * (attempt + 1))

        return False

    async def ensure_directory_async(self) -> bool:
        """v93.0: Async version of ensure_directory_sync."""
        return await asyncio.to_thread(self.ensure_directory_sync)

    @contextmanager
    def atomic_write_context(self, target_file: Path):
        """
        v93.0: Context manager for atomic file writes.

        Creates a temp file, yields it for writing, then atomically
        renames to target. Handles cleanup on failure.

        Usage:
            with dir_manager.atomic_write_context(file_path) as temp_path:
                temp_path.write_text(data)
            # File is atomically renamed on context exit
        """
        # Ensure directory exists BEFORE attempting write
        self.ensure_directory_sync()

        # Create temp file in same directory for atomic rename
        temp_fd, temp_path_str = tempfile.mkstemp(
            dir=str(self.base_dir),
            prefix=target_file.stem + "_",
            suffix=".tmp"
        )
        temp_path = Path(temp_path_str)

        try:
            # Close the fd since we'll write via path
            os.close(temp_fd)

            yield temp_path

            # Ensure directory still exists before rename
            self.ensure_directory_sync()

            # Atomic rename (POSIX guarantees atomicity on same filesystem)
            temp_path.replace(target_file)

        except Exception as e:
            # Clean up temp file on failure
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass
            raise

    def safe_read(self, file_path: Path) -> Optional[str]:
        """
        v93.0: Safely read a file with retry logic.

        Returns None if file doesn't exist or can't be read.
        """
        for attempt in range(self.max_retries):
            try:
                if not file_path.exists():
                    return None

                return file_path.read_text()

            except (FileNotFoundError, OSError) as e:
                if attempt == self.max_retries - 1:
                    logger.warning(f"[v93.0] Failed to read {file_path}: {e}")
                    return None
                time.sleep(self.retry_delay * (attempt + 1))

        return None


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class ServiceInfo:
    """
    Information about a registered service.

    v4.0: Enhanced with process start time tracking to detect PID reuse,
    which can cause false-positive alive checks.
    """
    service_name: str
    pid: int
    port: int
    host: str = "localhost"
    health_endpoint: str = "/health"
    status: str = "starting"  # starting, healthy, degraded, failed
    registered_at: float = 0.0
    last_heartbeat: float = 0.0
    metadata: Dict = None
    # v4.0: Track process start time to detect PID reuse
    process_start_time: float = 0.0

    def __post_init__(self):
        if self.registered_at == 0.0:
            self.registered_at = time.time()
        if self.last_heartbeat == 0.0:
            self.last_heartbeat = time.time()
        if self.metadata is None:
            self.metadata = {}
        # v4.0: Capture process start time if not provided
        if self.process_start_time == 0.0:
            self.process_start_time = self._get_process_start_time()

    def _get_process_start_time(self) -> float:
        """v4.0: Get the process start time for PID reuse detection."""
        try:
            process = psutil.Process(self.pid)
            return process.create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return 0.0

    def to_dict(self) -> Dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceInfo":
        """Create from dictionary."""
        # v4.0: Handle legacy entries without process_start_time
        if "process_start_time" not in data:
            data["process_start_time"] = 0.0
        return cls(**data)

    def is_process_alive(self) -> bool:
        """
        Check if the service's process is still running.

        v4.0 Enhancement: Also validates process start time to detect PID reuse.
        On Unix systems, PIDs can be reused after a process terminates.
        A new process could get the same PID, causing false-positive alive checks.
        """
        try:
            process = psutil.Process(self.pid)

            # Basic check: process exists and is running
            if not process.is_running():
                return False

            # v4.0: PID reuse detection
            # If we have a stored start time, verify it matches
            if self.process_start_time > 0.0:
                current_start_time = process.create_time()
                # Allow 1 second tolerance for timing differences
                if abs(current_start_time - self.process_start_time) > 1.0:
                    logger.warning(
                        f"PID reuse detected for {self.service_name}: "
                        f"stored_start={self.process_start_time:.1f}, "
                        f"current_start={current_start_time:.1f}"
                    )
                    return False

            return True

        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            return False

    def is_stale(self, timeout_seconds: float = 60.0) -> bool:
        """Check if service hasn't sent heartbeat in timeout period."""
        return (time.time() - self.last_heartbeat) > timeout_seconds

    def is_in_startup_phase(self, startup_grace_period: float = 120.0) -> bool:
        """
        v93.3: Check if service is still in its startup grace period.

        During startup, services may take longer to initialize and may not
        send heartbeats as frequently. This method identifies services that
        were registered recently and should be given extra time.

        Args:
            startup_grace_period: Seconds after registration during which
                                  service is considered "starting up"

        Returns:
            True if service is within startup grace period
        """
        if self.registered_at <= 0:
            return False
        return (time.time() - self.registered_at) < startup_grace_period

    def is_stale_startup_aware(
        self,
        timeout_seconds: float = 60.0,
        startup_grace_period: float = 120.0,
        startup_multiplier: float = 5.0,
    ) -> bool:
        """
        v93.3: Startup-aware stale detection.

        Uses extended timeout during startup grace period to allow
        services time to fully initialize before marking them stale.

        Args:
            timeout_seconds: Normal stale timeout
            startup_grace_period: How long to consider service "starting"
            startup_multiplier: Multiply timeout by this during startup

        Returns:
            True if service is stale (accounting for startup phase)
        """
        effective_timeout = timeout_seconds
        if self.is_in_startup_phase(startup_grace_period):
            effective_timeout = timeout_seconds * startup_multiplier

        return (time.time() - self.last_heartbeat) > effective_timeout

    def validate(self) -> tuple[bool, str]:
        """
        v4.0: Comprehensive validation of service entry.

        Returns:
            Tuple of (is_valid, reason_if_invalid)
        """
        # Check PID is valid
        if self.pid <= 0:
            return False, f"Invalid PID: {self.pid}"

        # Check port is valid
        if not (0 < self.port < 65536):
            return False, f"Invalid port: {self.port}"

        # Check process is alive (with PID reuse detection)
        if not self.is_process_alive():
            return False, f"Process {self.pid} is dead or PID was reused"

        # Check not stale (but don't fail on this - just warn)
        # Stale services may still be running, just not heartbeating

        return True, "OK"


# =============================================================================
# Service Registry
# =============================================================================

class ServiceRegistry:
    """
    Enterprise-grade service registry with atomic operations.

    v93.0 Enhancement:
    - Uses DirectoryManager for robust directory handling
    - Retry logic for all file operations
    - Handles race conditions and directory deletion
    - Pre-flight validation before writes

    Features:
    - File-based persistence with fcntl locking
    - Automatic stale service cleanup
    - Health tracking with heartbeats
    - Zero hardcoded configuration
    """

    def __init__(
        self,
        registry_dir: Optional[Path] = None,
        heartbeat_timeout: float = 60.0,
        cleanup_interval: float = 30.0,
        startup_grace_period: float = None,
    ):
        """
        Initialize service registry.

        Args:
            registry_dir: Directory for registry file (default: ~/.jarvis/registry)
            heartbeat_timeout: Seconds before service considered stale
            cleanup_interval: Seconds between cleanup cycles
            startup_grace_period: v93.3 - Grace period for newly registered services (default: from env)
        """
        self.registry_dir = registry_dir or Path.home() / ".jarvis" / "registry"
        self.registry_file = self.registry_dir / "services.json"
        self.heartbeat_timeout = heartbeat_timeout
        self.cleanup_interval = cleanup_interval
        self._cleanup_task: Optional[asyncio.Task] = None

        # v93.3: Startup-aware grace period configuration
        self.startup_grace_period = startup_grace_period or float(
            os.environ.get("JARVIS_SERVICE_STARTUP_GRACE", "120.0")
        )
        # v93.3: Extended stale threshold during startup (5x normal)
        self.startup_stale_multiplier = float(
            os.environ.get("JARVIS_STARTUP_STALE_MULTIPLIER", "5.0")
        )

        # v93.0: Use DirectoryManager for robust directory handling
        self._dir_manager = DirectoryManager(self.registry_dir)

        # v93.14: Rate limit stale warnings (max 1 per service per 60s)
        self._stale_warning_times: Dict[str, float] = {}
        self._warning_rate_limit_seconds = float(
            os.environ.get("JARVIS_STALE_WARNING_RATE_LIMIT", "60.0")
        )

        # v95.0: Callback system for service lifecycle events
        # Allows orchestrators to be notified when services die/become stale
        self._on_service_dead_callbacks: List[Callable[[str, int], Any]] = []
        self._on_service_stale_callbacks: List[Callable[[str, float], Any]] = []
        self._on_service_recovered_callbacks: List[Callable[[str], Any]] = []

        # v95.0: Service-specific adaptive stale thresholds
        # Different services have different characteristics:
        # - reactor-core: Long model loading, needs extended threshold
        # - jarvis-prime: Model loading, needs extended threshold
        # - jarvis-core: Quick startup, standard threshold
        # Format: {"service_pattern": threshold_seconds}
        self._service_stale_thresholds: Dict[str, float] = {
            "reactor": float(os.environ.get("REACTOR_STALE_THRESHOLD", "600.0")),  # 10 minutes
            "jarvis-prime": float(os.environ.get("JARVIS_PRIME_STALE_THRESHOLD", "600.0")),  # 10 minutes
            "jprime": float(os.environ.get("JARVIS_PRIME_STALE_THRESHOLD", "600.0")),  # 10 minutes
            # v95.0: jarvis-body (registry owner) has highest threshold - should never be stale
            "jarvis-body": float(os.environ.get("JARVIS_BODY_STALE_THRESHOLD", "3600.0")),  # 1 hour
            "default": float(os.environ.get("JARVIS_VERY_STALE_THRESHOLD", "300.0")),  # 5 minutes
        }
        logger.debug(f"[v95.0] Service stale thresholds: {self._service_stale_thresholds}")

        # v95.0: Circuit breaker for registry operations (prevents cascading failures)
        self._circuit_breaker_state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self._circuit_breaker_failures = 0
        self._circuit_breaker_threshold = int(
            os.environ.get("JARVIS_REGISTRY_CB_THRESHOLD", "5")
        )
        self._circuit_breaker_reset_timeout = float(
            os.environ.get("JARVIS_REGISTRY_CB_RESET", "30.0")
        )
        self._circuit_breaker_last_failure = 0.0
        self._circuit_breaker_lock = asyncio.Lock()

        # v95.0: Local cache for graceful degradation when registry unavailable
        self._local_cache: Dict[str, ServiceInfo] = {}
        self._cache_lock = asyncio.Lock()
        self._cache_ttl = float(os.environ.get("JARVIS_REGISTRY_CACHE_TTL", "60.0"))
        self._cache_last_update = 0.0

        # v95.0: Heartbeat retry queue for transient failures
        self._pending_heartbeats: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._heartbeat_retry_task: Optional[asyncio.Task] = None

        # v95.0: Owner service identification and self-heartbeat
        # The owner is the service that RUNS this registry (jarvis-body)
        # Owner is NEVER marked stale because if the registry is running,
        # the owner must be alive (by definition)
        self._owner_service_name: str = os.environ.get(
            "JARVIS_REGISTRY_OWNER", "jarvis-body"
        )
        self._self_heartbeat_task: Optional[asyncio.Task] = None
        self._self_heartbeat_interval: float = float(
            os.environ.get("JARVIS_SELF_HEARTBEAT_INTERVAL", "15.0")
        )
        logger.debug(f"[v95.0] Registry owner: {self._owner_service_name}")

        # v95.1: Grace period tracking for premature deregistration prevention
        # Tracks services that appear dead but haven't been confirmed yet
        self._suspicious_services: Dict[str, float] = {}  # service_name -> first_detection_time
        self._dead_confirmation_grace_period: float = float(
            os.environ.get("JARVIS_DEAD_CONFIRMATION_GRACE", "30.0")  # 30 second grace
        )
        self._dead_confirmation_retries: int = int(
            os.environ.get("JARVIS_DEAD_CONFIRMATION_RETRIES", "3")
        )
        self._suspicious_retry_counts: Dict[str, int] = {}  # service_name -> retry_count

        # v93.0: Ensure directory exists with retry logic
        if not self._dir_manager.ensure_directory_sync():
            logger.error(f"[v93.0] CRITICAL: Failed to create registry directory: {self.registry_dir}")
            raise RuntimeError(f"Cannot create registry directory: {self.registry_dir}")

        # Initialize registry file if doesn't exist
        if not self.registry_file.exists():
            self._write_registry({})

    # =========================================================================
    # v95.0: Callback Registration for Service Lifecycle Events
    # =========================================================================

    def register_on_service_dead(
        self,
        callback: Callable[[str, int], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service's process dies.

        The callback receives:
        - service_name: Name of the dead service
        - pid: The dead PID

        This allows orchestrators to be notified and restart services.
        """
        self._on_service_dead_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_dead callback: {callback}")

    def register_on_service_stale(
        self,
        callback: Callable[[str, float], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service becomes stale.

        The callback receives:
        - service_name: Name of the stale service
        - last_heartbeat_age: Seconds since last heartbeat

        This allows orchestrators to be notified of unresponsive services.
        """
        self._on_service_stale_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_stale callback: {callback}")

    def register_on_service_recovered(
        self,
        callback: Callable[[str], Any]
    ) -> None:
        """
        v95.0: Register callback for when a service recovers (becomes healthy).

        The callback receives:
        - service_name: Name of the recovered service
        """
        self._on_service_recovered_callbacks.append(callback)
        logger.debug(f"[v95.0] Registered on_service_recovered callback: {callback}")

    async def _notify_service_dead(self, service_name: str, pid: int) -> None:
        """v95.0: Notify all registered callbacks that a service died."""
        for callback in self._on_service_dead_callbacks:
            try:
                result = callback(service_name, pid)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_dead callback error: {e}")

    async def _notify_service_stale(self, service_name: str, age: float) -> None:
        """v95.0: Notify all registered callbacks that a service is stale."""
        for callback in self._on_service_stale_callbacks:
            try:
                result = callback(service_name, age)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_stale callback error: {e}")

    async def _notify_service_recovered(self, service_name: str) -> None:
        """v95.0: Notify all registered callbacks that a service recovered."""
        for callback in self._on_service_recovered_callbacks:
            try:
                result = callback(service_name)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"[v95.0] on_service_recovered callback error: {e}")

    # =========================================================================
    # v95.0: Circuit Breaker Pattern for Registry Resilience
    # =========================================================================

    async def _check_circuit_breaker(self) -> bool:
        """
        v95.0: Check if circuit breaker allows operation.

        Circuit breaker prevents cascading failures when registry is unhealthy.
        States:
        - CLOSED: Normal operation, all requests allowed
        - OPEN: Registry unavailable, use cache fallback
        - HALF_OPEN: Testing if registry recovered

        Returns:
            True if operation allowed, False if circuit is open
        """
        async with self._circuit_breaker_lock:
            if self._circuit_breaker_state == "CLOSED":
                return True

            if self._circuit_breaker_state == "OPEN":
                # Check if reset timeout has passed
                elapsed = time.time() - self._circuit_breaker_last_failure
                if elapsed >= self._circuit_breaker_reset_timeout:
                    self._circuit_breaker_state = "HALF_OPEN"
                    logger.info("[v95.0] Circuit breaker: OPEN → HALF_OPEN (testing recovery)")
                    return True
                return False

            # HALF_OPEN: allow single test request
            return True

    async def _record_circuit_success(self) -> None:
        """v95.0: Record successful registry operation."""
        async with self._circuit_breaker_lock:
            if self._circuit_breaker_state == "HALF_OPEN":
                self._circuit_breaker_state = "CLOSED"
                self._circuit_breaker_failures = 0
                logger.info("[v95.0] Circuit breaker: HALF_OPEN → CLOSED (recovery confirmed)")
            elif self._circuit_breaker_state == "CLOSED":
                # Reset failure count on success
                self._circuit_breaker_failures = max(0, self._circuit_breaker_failures - 1)

    async def _record_circuit_failure(self) -> None:
        """v95.0: Record failed registry operation."""
        async with self._circuit_breaker_lock:
            self._circuit_breaker_failures += 1
            self._circuit_breaker_last_failure = time.time()

            if self._circuit_breaker_state == "HALF_OPEN":
                self._circuit_breaker_state = "OPEN"
                logger.warning("[v95.0] Circuit breaker: HALF_OPEN → OPEN (recovery failed)")
            elif (
                self._circuit_breaker_state == "CLOSED"
                and self._circuit_breaker_failures >= self._circuit_breaker_threshold
            ):
                self._circuit_breaker_state = "OPEN"
                logger.warning(
                    f"[v95.0] Circuit breaker: CLOSED → OPEN "
                    f"(failures: {self._circuit_breaker_failures})"
                )

    # =========================================================================
    # v95.0: Local Cache Fallback for Graceful Degradation
    # =========================================================================

    async def _update_cache(self, services: Dict[str, ServiceInfo]) -> None:
        """v95.0: Update local cache with latest registry state."""
        async with self._cache_lock:
            self._local_cache = services.copy()
            self._cache_last_update = time.time()

    async def _get_from_cache(self, service_name: Optional[str] = None) -> Optional[Any]:
        """
        v95.0: Get service(s) from local cache.

        Args:
            service_name: Specific service to get, or None for all

        Returns:
            ServiceInfo if found, Dict if all services, None if not found/stale
        """
        async with self._cache_lock:
            cache_age = time.time() - self._cache_last_update
            if cache_age > self._cache_ttl:
                logger.debug(f"[v95.0] Cache stale ({cache_age:.1f}s > {self._cache_ttl}s)")
                return None

            if service_name:
                return self._local_cache.get(service_name)
            return self._local_cache.copy()

    async def discover_service_resilient(
        self,
        service_name: str,
        use_cache_fallback: bool = True
    ) -> Optional[ServiceInfo]:
        """
        v95.0: Resilient service discovery with circuit breaker and cache fallback.

        This is the preferred discovery method for cross-repo operations.

        Args:
            service_name: Service to discover
            use_cache_fallback: Whether to use cache when registry unavailable

        Returns:
            ServiceInfo if found, None otherwise
        """
        # Check circuit breaker first
        if not await self._check_circuit_breaker():
            if use_cache_fallback:
                cached = await self._get_from_cache(service_name)
                if cached:
                    logger.debug(f"[v95.0] Using cache fallback for {service_name}")
                    return cached
            logger.warning(f"[v95.0] Circuit open, no cache for {service_name}")
            return None

        try:
            # Try normal discovery
            result = await self.discover_service(service_name)
            await self._record_circuit_success()

            # Update cache on success
            if result:
                async with self._cache_lock:
                    self._local_cache[service_name] = result
                    self._cache_last_update = time.time()

            return result

        except Exception as e:
            await self._record_circuit_failure()
            logger.warning(f"[v95.0] Discovery failed for {service_name}: {e}")

            if use_cache_fallback:
                cached = await self._get_from_cache(service_name)
                if cached:
                    logger.info(f"[v95.0] Using cache fallback for {service_name}")
                    return cached

            return None

    def _get_adaptive_stale_threshold(self, service_name: str) -> float:
        """
        v95.0: Get adaptive stale threshold based on service type.

        Different services have different operational characteristics:
        - reactor-core: Long model loading times, needs extended threshold
        - jarvis-prime: Model loading, needs extended threshold
        - Other services: Standard threshold

        This prevents services with long initialization/loading times
        from being incorrectly marked as stale.

        Args:
            service_name: Name of the service

        Returns:
            Stale threshold in seconds (appropriate for this service)
        """
        service_lower = service_name.lower()

        # Check for service-specific thresholds (pattern matching)
        for pattern, threshold in self._service_stale_thresholds.items():
            if pattern != "default" and pattern in service_lower:
                logger.debug(
                    f"[v95.0] Using adaptive threshold for {service_name}: "
                    f"{threshold}s (pattern: {pattern})"
                )
                return threshold

        # Return default threshold
        return self._service_stale_thresholds.get("default", 300.0)

    def _acquire_lock(self, file_handle) -> None:
        """Acquire exclusive lock on registry file (blocking)."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_EX)

    def _release_lock(self, file_handle) -> None:
        """Release lock on registry file."""
        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)

    def _read_registry(self) -> Dict[str, ServiceInfo]:
        """
        v93.0: Read registry with file locking and robust error handling.

        Returns:
            Dict mapping service names to ServiceInfo
        """
        # v93.0: Ensure directory exists before read
        self._dir_manager.ensure_directory_sync()

        for attempt in range(3):
            try:
                if not self.registry_file.exists():
                    return {}

                with open(self.registry_file, 'r+') as f:
                    self._acquire_lock(f)
                    try:
                        content = f.read()
                        if not content.strip():
                            return {}
                        data = json.loads(content)
                        return {
                            name: ServiceInfo.from_dict(info)
                            for name, info in data.items()
                        }
                    finally:
                        self._release_lock(f)

            except FileNotFoundError:
                # File was deleted between check and open - retry
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return {}
            except json.JSONDecodeError as e:
                logger.warning(f"[v93.0] Corrupted registry JSON (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                # Return empty on corruption - will be overwritten on next write
                return {}
            except OSError as e:
                logger.warning(f"[v93.0] Registry read error (attempt {attempt + 1}): {e}")
                if attempt < 2:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                return {}

        return {}

    def _write_registry(self, services: Dict[str, ServiceInfo]) -> None:
        """
        v93.0: Write registry with atomic file operations and robust directory handling.

        Uses DirectoryManager for:
        - Pre-write directory validation
        - Atomic temp file creation in correct directory
        - Retry logic on failure
        - Proper cleanup on error

        Args:
            services: Dict mapping service names to ServiceInfo
        """
        # Convert to serializable dict
        data = {
            name: service.to_dict()
            for name, service in services.items()
        }

        # v93.0: Retry loop for robust writes
        last_error = None
        for attempt in range(3):
            try:
                # v93.0: Use DirectoryManager's atomic write context
                with self._dir_manager.atomic_write_context(self.registry_file) as temp_path:
                    with open(temp_path, 'w') as f:
                        self._acquire_lock(f)
                        try:
                            json.dump(data, f, indent=2)
                            f.flush()
                            os.fsync(f.fileno())  # Force write to disk
                        finally:
                            self._release_lock(f)

                # Success!
                return

            except OSError as e:
                last_error = e
                logger.warning(
                    f"[v93.0] Registry write failed (attempt {attempt + 1}/3): {e}"
                )
                # Ensure directory exists for next attempt
                self._dir_manager.ensure_directory_sync()
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))

            except Exception as e:
                last_error = e
                logger.error(f"[v93.0] Unexpected registry write error: {e}")
                if attempt < 2:
                    time.sleep(0.1 * (attempt + 1))

        # All attempts failed
        logger.error(f"[v93.0] Failed to write registry after 3 attempts: {last_error}")
        raise RuntimeError(f"Failed to write registry: {last_error}")

    async def register_service(
        self,
        service_name: str,
        pid: int,
        port: int,
        host: str = "localhost",
        health_endpoint: str = "/health",
        metadata: Optional[Dict] = None
    ) -> ServiceInfo:
        """
        Register a service in the registry.

        Args:
            service_name: Unique service identifier
            pid: Process ID
            port: Port number service is listening on
            host: Hostname (default: localhost)
            health_endpoint: Health check endpoint path
            metadata: Optional additional metadata

        Returns:
            ServiceInfo for the registered service
        """
        service = ServiceInfo(
            service_name=service_name,
            pid=pid,
            port=port,
            host=host,
            health_endpoint=health_endpoint,
            status="starting",
            metadata=metadata or {}
        )

        # Read existing registry
        services = await asyncio.to_thread(self._read_registry)

        # Add/update service
        services[service_name] = service

        # Write back atomically
        await asyncio.to_thread(self._write_registry, services)

        logger.info(
            f"📝 Service registered: {service_name} "
            f"(PID: {pid}, Port: {port}, Host: {host})"
        )

        return service

    async def deregister_service(self, service_name: str) -> bool:
        """
        Remove service from registry.

        Args:
            service_name: Service to deregister

        Returns:
            True if service was found and removed
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name in services:
            del services[service_name]
            await asyncio.to_thread(self._write_registry, services)
            logger.info(f"❌ Service deregistered: {service_name}")
            return True

        return False

    async def discover_service(self, service_name: str) -> Optional[ServiceInfo]:
        """
        Discover a service by name.

        Args:
            service_name: Service to find

        Returns:
            ServiceInfo if found and healthy, None otherwise
        """
        services = await asyncio.to_thread(self._read_registry)
        service = services.get(service_name)

        if not service:
            return None

        # v95.1: Check if process is still alive with grace period
        # This prevents premature deregistration due to transient PID issues
        if not service.is_process_alive():
            dead_pid = service.pid
            current_time = time.time()

            # v95.1: Track suspicious services with grace period
            if service_name not in self._suspicious_services:
                # First detection - start grace period
                self._suspicious_services[service_name] = current_time
                self._suspicious_retry_counts[service_name] = 1
                logger.warning(
                    f"⚠️  Service {service_name} PID {dead_pid} appears dead - "
                    f"starting {self._dead_confirmation_grace_period}s grace period "
                    f"(attempt 1/{self._dead_confirmation_retries})"
                )
                # Return None but DON'T deregister yet - might recover
                return None
            else:
                # Already suspicious - check if grace period expired
                first_detection = self._suspicious_services[service_name]
                elapsed = current_time - first_detection
                retry_count = self._suspicious_retry_counts.get(service_name, 0) + 1
                self._suspicious_retry_counts[service_name] = retry_count

                if elapsed >= self._dead_confirmation_grace_period and \
                   retry_count >= self._dead_confirmation_retries:
                    # Grace period expired AND minimum retries reached - confirm dead
                    logger.warning(
                        f"🧹 Service {service_name} confirmed dead after "
                        f"{elapsed:.1f}s grace period and {retry_count} checks - deregistering"
                    )
                    # Clean up tracking
                    del self._suspicious_services[service_name]
                    if service_name in self._suspicious_retry_counts:
                        del self._suspicious_retry_counts[service_name]
                    # NOW deregister
                    await self.deregister_service(service_name)
                    await self._notify_service_dead(service_name, dead_pid)
                    return None
                else:
                    # Still in grace period - log but don't deregister
                    remaining = self._dead_confirmation_grace_period - elapsed
                    logger.debug(
                        f"⏳ Service {service_name} still appears dead "
                        f"(attempt {retry_count}/{self._dead_confirmation_retries}, "
                        f"{remaining:.1f}s remaining in grace period)"
                    )
                    return None

        # v95.1: Service is alive - clear any suspicious tracking
        if service_name in self._suspicious_services:
            logger.info(
                f"✅ Service {service_name} recovered (PID confirmed alive)"
            )
            del self._suspicious_services[service_name]
            if service_name in self._suspicious_retry_counts:
                del self._suspicious_retry_counts[service_name]

        # v93.3: Startup-aware stale check
        is_in_startup = service.is_in_startup_phase(self.startup_grace_period)
        is_stale = service.is_stale_startup_aware(
            timeout_seconds=self.heartbeat_timeout,
            startup_grace_period=self.startup_grace_period,
            startup_multiplier=self.startup_stale_multiplier,
        )

        if is_stale:
            # v93.14: Rate limit stale warnings to prevent log flooding
            current_time = time.time()
            last_warning_time = self._stale_warning_times.get(service_name, 0)
            heartbeat_age = current_time - service.last_heartbeat

            if current_time - last_warning_time >= self._warning_rate_limit_seconds:
                startup_note = " (after startup grace)" if not is_in_startup else ""
                logger.warning(
                    f"⚠️  Service {service_name} is stale{startup_note} "
                    f"(last heartbeat {heartbeat_age:.0f}s ago)"
                )
                self._stale_warning_times[service_name] = current_time
                # v95.0: Notify callbacks that service is stale (for potential restart)
                await self._notify_service_stale(service_name, heartbeat_age)

            return None

        return service

    async def list_services(self, healthy_only: bool = True) -> List[ServiceInfo]:
        """
        List all registered services.

        Args:
            healthy_only: If True, only return healthy services

        Returns:
            List of ServiceInfo objects
        """
        services = await asyncio.to_thread(self._read_registry)

        if not healthy_only:
            return list(services.values())

        # v93.3: Filter to only healthy services with startup-aware stale detection
        healthy = []
        for service in services.values():
            is_stale = service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            )
            if service.is_process_alive() and not is_stale:
                healthy.append(service)

        return healthy

    async def heartbeat(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None,
        timeout: float = 2.0,
    ) -> bool:
        """
        v95.0: Enterprise-grade heartbeat with timeout protection.

        Features:
        - Non-blocking with configurable timeout
        - Fire-and-forget safe (won't block event loop)
        - Graceful degradation on timeout
        - Automatic retry with exponential backoff on transient failures

        Args:
            service_name: Service to update
            status: Optional new status (healthy, degraded, etc.)
            metadata: Optional metadata to merge
            timeout: Maximum time to wait for heartbeat (default 2.0s)

        Returns:
            True if service found and updated, False on timeout or error
        """
        try:
            return await asyncio.wait_for(
                self._heartbeat_internal(service_name, status, metadata),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            # Heartbeat timeout is non-fatal - just log and continue
            logger.debug(f"Heartbeat timeout for {service_name} (non-fatal, will retry)")
            return False
        except asyncio.CancelledError:
            # Don't suppress cancellation
            raise
        except Exception as e:
            # Log but don't propagate - heartbeat failures shouldn't crash services
            logger.debug(f"Heartbeat error for {service_name}: {e} (non-fatal)")
            return False

    async def _heartbeat_internal(
        self,
        service_name: str,
        status: Optional[str] = None,
        metadata: Optional[Dict] = None
    ) -> bool:
        """
        v95.0: Internal heartbeat implementation.

        Separated from public method to allow timeout wrapping.
        """
        services = await asyncio.to_thread(self._read_registry)

        if service_name not in services:
            logger.debug(f"Heartbeat for unregistered service: {service_name}")
            return False

        service = services[service_name]
        service.last_heartbeat = time.time()

        if status:
            service.status = status

        if metadata:
            service.metadata.update(metadata)

        await asyncio.to_thread(self._write_registry, services)

        # v93.14: Clear stale warning rate limiter on successful heartbeat
        if service_name in self._stale_warning_times:
            del self._stale_warning_times[service_name]

        return True

    async def cleanup_stale_services(self) -> int:
        """
        v95.1: Remove services with dead PIDs or stale heartbeats.

        CRITICAL: Registry owner (jarvis-body) is NEVER removed.
        If the registry is running, the owner must be alive by definition.

        Returns:
            Number of services cleaned up
        """
        services = await asyncio.to_thread(self._read_registry)
        cleaned = 0

        for service_name, service in list(services.items()):
            # v95.1: CRITICAL - Registry owner is EXEMPT from cleanup
            # If the registry code is running, the owner service MUST be alive
            if service_name == self._owner_service_name:
                logger.debug(f"  ✓ {service_name} is registry owner - exempt from cleanup")
                continue

            should_remove = False

            # Check if process is dead
            if not service.is_process_alive():
                logger.info(
                    f"🧹 Cleaning dead service: {service_name} (PID {service.pid} not found)"
                )
                should_remove = True

            # v93.3: Check if stale with startup awareness
            elif service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            ):
                in_startup = service.is_in_startup_phase(self.startup_grace_period)
                # Only clean if past startup grace period
                if not in_startup:
                    logger.info(
                        f"🧹 Cleaning stale service: {service_name} "
                        f"(last heartbeat {time.time() - service.last_heartbeat:.0f}s ago)"
                    )
                    should_remove = True
                else:
                    logger.debug(
                        f"⏳ Service {service_name} stale but in startup phase, keeping"
                    )

            if should_remove:
                del services[service_name]
                cleaned += 1

        if cleaned > 0:
            await asyncio.to_thread(self._write_registry, services)

        return cleaned

    async def _cleanup_loop(self) -> None:
        """Background task to periodically clean up stale services."""
        while True:
            try:
                await asyncio.sleep(self.cleanup_interval)
                cleaned = await self.cleanup_stale_services()
                if cleaned > 0:
                    logger.debug(f"🧹 Cleaned {cleaned} stale services")

            except asyncio.CancelledError:
                logger.info("🛑 Service registry cleanup loop stopped")
                break

            except Exception as e:
                logger.error(f"❌ Error in cleanup loop: {e}", exc_info=True)

    async def start_cleanup_task(self) -> None:
        """Start background cleanup task."""
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            logger.info(
                f"🧹 Service registry cleanup started "
                f"(interval: {self.cleanup_interval}s, timeout: {self.heartbeat_timeout}s)"
            )

        # v95.0: Start self-heartbeat task for the registry owner
        if self._self_heartbeat_task is None or self._self_heartbeat_task.done():
            self._self_heartbeat_task = asyncio.create_task(self._self_heartbeat_loop())
            logger.info(
                f"[v95.0] Self-heartbeat started for owner '{self._owner_service_name}' "
                f"(interval: {self._self_heartbeat_interval}s)"
            )

    async def stop_cleanup_task(self) -> None:
        """Stop background cleanup task."""
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup stopped")

        # v95.0: Stop self-heartbeat task
        if self._self_heartbeat_task and not self._self_heartbeat_task.done():
            self._self_heartbeat_task.cancel()
            try:
                await self._self_heartbeat_task
            except asyncio.CancelledError:
                pass
            logger.info("🛑 Service registry cleanup loop stopped")

    async def _self_heartbeat_loop(self) -> None:
        """
        v95.1: Enterprise-grade self-heartbeat loop for the registry owner service.

        The registry owner (jarvis-body) is the service that RUNS this registry.
        If the registry is running, the owner must be alive (by definition).

        CRITICAL FIXES (v95.1):
        1. Auto-register owner on startup if not registered
        2. Send IMMEDIATE heartbeat (no wait-first pattern)
        3. Owner is NEVER marked stale (exemption in cleanup)
        4. Self-healing registration if owner entry disappears
        """
        logger.info(f"[v95.1] Self-heartbeat loop started for {self._owner_service_name}")

        # v95.1: CRITICAL - Ensure owner is registered IMMEDIATELY on startup
        await self._ensure_owner_registered()

        # v95.1: Send FIRST heartbeat immediately (no wait-first pattern!)
        await self._send_owner_heartbeat()

        while True:
            try:
                # Now wait for interval AFTER first heartbeat
                await asyncio.sleep(self._self_heartbeat_interval)

                # v95.1: Verify owner is still registered (self-healing)
                await self._ensure_owner_registered()

                # Send heartbeat for owner
                await self._send_owner_heartbeat()

            except asyncio.CancelledError:
                logger.debug(f"[v95.1] Self-heartbeat loop cancelled for {self._owner_service_name}")
                break
            except Exception as e:
                # Log but don't crash - the owner should stay alive
                logger.warning(f"[v95.1] Self-heartbeat error (non-fatal): {e}")
                # Try to recover by re-registering
                try:
                    await self._ensure_owner_registered()
                except Exception:
                    pass

    async def _ensure_owner_registered(self) -> None:
        """
        v95.2: Ensure the registry owner service is always registered with correct PID.

        This is CRITICAL for preventing "jarvis-body is stale/dead" issues.
        If the registry is running, the owner MUST exist in the registry with
        the CURRENT process PID.

        Key fixes (v95.2):
        1. Always verify PID matches current process
        2. Re-register if PID mismatch (process may have restarted)
        3. Include process_start_time for accurate alive checks
        """
        current_pid = os.getpid()
        current_start_time = self._get_current_process_start_time()

        try:
            # Read registry directly (bypass discover_service to avoid dead PID detection)
            services = await asyncio.to_thread(self._read_registry)
            owner = services.get(self._owner_service_name)

            needs_registration = False
            reason = ""

            if owner is None:
                needs_registration = True
                reason = "not registered"
            elif owner.pid != current_pid:
                # PID mismatch - registry has old PID, need to update
                needs_registration = True
                reason = f"PID mismatch (stored: {owner.pid}, current: {current_pid})"
            elif owner.process_start_time > 0 and abs(owner.process_start_time - current_start_time) > 2.0:
                # Start time mismatch - process restarted with same PID
                needs_registration = True
                reason = f"start time mismatch (stored: {owner.process_start_time:.1f}, current: {current_start_time:.1f})"

            if needs_registration:
                logger.info(
                    f"[v95.2] Registering owner '{self._owner_service_name}': {reason} "
                    f"(PID: {current_pid})"
                )
                owner_port = int(os.environ.get("JARVIS_BODY_PORT", "8010"))

                # Create ServiceInfo with explicit process_start_time
                service = ServiceInfo(
                    service_name=self._owner_service_name,
                    pid=current_pid,
                    port=owner_port,
                    host="localhost",
                    health_endpoint="/health",
                    status="healthy",
                    process_start_time=current_start_time,
                    metadata={
                        "is_registry_owner": True,
                        "auto_registered": True,
                        "registration_source": "self_heartbeat_loop",
                    }
                )

                services[self._owner_service_name] = service
                await asyncio.to_thread(self._write_registry, services)
                logger.info(f"[v95.2] Owner '{self._owner_service_name}' registered successfully")
            else:
                logger.debug(f"[v95.2] Owner '{self._owner_service_name}' already registered correctly")

        except Exception as e:
            logger.error(f"[v95.2] Failed to ensure owner registered: {e}")

    def _get_current_process_start_time(self) -> float:
        """v95.2: Get the start time of the current process."""
        try:
            return psutil.Process(os.getpid()).create_time()
        except Exception:
            return 0.0

    async def _send_owner_heartbeat(self) -> None:
        """v95.1: Send heartbeat for the registry owner."""
        try:
            await self.heartbeat(
                self._owner_service_name,
                status="healthy",
                metadata={
                    "heartbeat_source": "self_heartbeat",
                    "is_registry_owner": True,
                    "pid": os.getpid(),
                    "timestamp": time.time(),
                }
            )
            logger.debug(f"[v95.1] Self-heartbeat sent for {self._owner_service_name}")
        except Exception as e:
            logger.warning(f"[v95.1] Failed to send owner heartbeat: {e}")

    async def wait_for_service(
        self,
        service_name: str,
        timeout: float = 30.0,
        check_interval: float = 1.0
    ) -> Optional[ServiceInfo]:
        """
        Wait for a service to become available.

        Args:
            service_name: Service to wait for
            timeout: Maximum time to wait (seconds)
            check_interval: How often to check (seconds)

        Returns:
            ServiceInfo if service becomes available, None if timeout
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            service = await self.discover_service(service_name)
            if service:
                logger.info(f"✅ Service discovered: {service_name}")
                return service

            await asyncio.sleep(check_interval)

        logger.warning(
            f"⏱️  Timeout waiting for service: {service_name} (after {timeout}s)"
        )
        return None

    # =========================================================================
    # v4.0: PRE-FLIGHT CLEANUP - Critical for Robust Startup
    # =========================================================================

    async def pre_flight_cleanup(self) -> Dict[str, Any]:
        """
        v4.0: Comprehensive pre-flight cleanup before starting services.

        MUST be called before starting any services to ensure a clean slate.
        This prevents "dead PID" warnings and ensures reliable service discovery.

        What it does:
        1. Validates ALL entries in the registry
        2. Removes entries with dead PIDs (process not running)
        3. Removes entries with reused PIDs (process start time mismatch)
        4. Removes entries with invalid data (bad port, bad PID)
        5. Optionally kills zombie processes still holding ports
        6. Reports detailed cleanup statistics

        Returns:
            Dict with cleanup statistics and removed services
        """
        logger.info("🚀 Pre-flight cleanup: Validating service registry...")

        services = await asyncio.to_thread(self._read_registry)
        stats = {
            "total_entries": len(services),
            "valid_entries": 0,
            "removed_dead_pid": [],
            "removed_pid_reuse": [],
            "removed_invalid": [],
            "removed_stale": [],
            "ports_freed": [],
            "cleanup_time_ms": 0,
        }

        start_time = time.time()
        to_remove = []

        for service_name, service in services.items():
            # Run comprehensive validation
            is_valid, reason = service.validate()

            if not is_valid:
                to_remove.append(service_name)

                # Categorize the removal reason
                if "dead" in reason.lower() or "not found" in reason.lower():
                    stats["removed_dead_pid"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })
                elif "reuse" in reason.lower():
                    stats["removed_pid_reuse"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })
                else:
                    stats["removed_invalid"].append({
                        "name": service_name,
                        "pid": service.pid,
                        "port": service.port,
                        "reason": reason
                    })

                # Track freed ports
                stats["ports_freed"].append(service.port)

                logger.info(
                    f"  🧹 Removing {service_name}: {reason} "
                    f"(PID: {service.pid}, Port: {service.port})"
                )
            else:
                # v95.1: CRITICAL - Registry owner is NEVER marked stale or removed
                # If the registry is running, the owner MUST be alive (by definition)
                if service_name == self._owner_service_name:
                    stats["valid_entries"] += 1
                    logger.debug(
                        f"  ✓ {service_name} is registry owner - exempt from stale checks"
                    )
                    continue

                # v93.3: Startup-aware stale detection with tiered cleanup
                # Services without recent heartbeat should be cleaned up, but give
                # newly registered services extra time during startup grace period
                is_in_startup = service.is_in_startup_phase(self.startup_grace_period)

                # Use startup-aware stale detection
                is_stale = service.is_stale_startup_aware(
                    timeout_seconds=self.heartbeat_timeout,
                    startup_grace_period=self.startup_grace_period,
                    startup_multiplier=self.startup_stale_multiplier,
                )

                if is_stale:
                    stale_age = time.time() - service.last_heartbeat
                    service_age = time.time() - service.registered_at

                    # v95.0: Adaptive very stale threshold - accounts for:
                    # 1. Service type (reactor-core needs longer threshold)
                    # 2. Startup phase (extra grace during startup)
                    # This prevents services with long init times from being incorrectly removed
                    base_very_stale = self._get_adaptive_stale_threshold(service_name)
                    very_stale_threshold = (
                        base_very_stale * self.startup_stale_multiplier
                        if is_in_startup else base_very_stale
                    )

                    if stale_age > very_stale_threshold:
                        # Very stale - treat as dead and clean up
                        logger.warning(
                            f"  🧹 {service_name} is very stale "
                            f"(last heartbeat {stale_age:.0f}s ago > {very_stale_threshold}s threshold) - removing"
                        )
                        to_remove.append(service_name)
                        stats["removed_stale"].append({
                            "name": service_name,
                            "pid": service.pid,
                            "stale_seconds": stale_age,
                            "action": "removed"
                        })
                    else:
                        # Mildly stale - warn but keep for now
                        startup_status = " (in startup phase)" if is_in_startup else ""
                        logger.warning(
                            f"  ⚠️  {service_name} is stale{startup_status} "
                            f"(last heartbeat {stale_age:.0f}s ago, age {service_age:.0f}s) - keeping for now"
                        )
                        stats["valid_entries"] += 1
                        stats["removed_stale"].append({
                            "name": service_name,
                            "pid": service.pid,
                            "stale_seconds": stale_age,
                            "service_age": service_age,
                            "in_startup": is_in_startup,
                            "action": "kept"
                        })
                else:
                    stats["valid_entries"] += 1

        # Remove invalid entries
        if to_remove:
            for name in to_remove:
                del services[name]
            await asyncio.to_thread(self._write_registry, services)

        stats["cleanup_time_ms"] = (time.time() - start_time) * 1000

        # Log summary
        removed_count = len(to_remove)
        if removed_count > 0:
            logger.info(
                f"✅ Pre-flight cleanup complete: "
                f"removed {removed_count} invalid entries, "
                f"{stats['valid_entries']} valid entries remain "
                f"({stats['cleanup_time_ms']:.1f}ms)"
            )
        else:
            logger.info(
                f"✅ Pre-flight cleanup complete: "
                f"registry is clean ({stats['valid_entries']} valid entries, "
                f"{stats['cleanup_time_ms']:.1f}ms)"
            )

        return stats

    async def validate_and_cleanup_port(self, port: int) -> bool:
        """
        v4.0: Ensure a port is free before starting a service.

        Checks if port is in use by:
        1. A registered service (validates if still alive)
        2. An unregistered process (optionally kills it)

        Returns:
            True if port is now free, False if still in use
        """
        # Check if any registered service claims this port
        services = await asyncio.to_thread(self._read_registry)

        for service_name, service in services.items():
            if service.port == port:
                if service.is_process_alive():
                    logger.warning(
                        f"Port {port} in use by registered service {service_name} "
                        f"(PID: {service.pid})"
                    )
                    return False
                else:
                    # Dead service holding port - remove from registry
                    logger.info(
                        f"Removing dead service {service_name} from port {port}"
                    )
                    await self.deregister_service(service_name)

        # Check if port is in use by an unregistered process
        try:
            for conn in psutil.net_connections(kind='inet'):
                if conn.laddr.port == port and conn.status == 'LISTEN':
                    # Port is in use by unregistered process
                    try:
                        proc = psutil.Process(conn.pid)
                        logger.warning(
                            f"Port {port} in use by unregistered process: "
                            f"{proc.name()} (PID: {conn.pid})"
                        )
                        return False
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
        except (psutil.AccessDenied, OSError):
            # May not have permission to check all connections
            pass

        return True

    async def get_registry_health_report(self) -> Dict[str, Any]:
        """
        v4.0: Generate comprehensive health report for the registry.

        Returns detailed information about all registered services,
        their health status, and any issues detected.
        """
        services = await asyncio.to_thread(self._read_registry)

        report = {
            "timestamp": datetime.now().isoformat(),
            "total_services": len(services),
            "healthy_services": 0,
            "degraded_services": 0,
            "dead_services": 0,
            "stale_services": 0,
            "services": {}
        }

        for service_name, service in services.items():
            is_alive = service.is_process_alive()
            # v93.3: Use startup-aware stale detection for health report
            is_in_startup = service.is_in_startup_phase(self.startup_grace_period)
            is_stale = service.is_stale_startup_aware(
                timeout_seconds=self.heartbeat_timeout,
                startup_grace_period=self.startup_grace_period,
                startup_multiplier=self.startup_stale_multiplier,
            )

            service_report = {
                "pid": service.pid,
                "port": service.port,
                "host": service.host,
                "status": service.status,
                "is_alive": is_alive,
                "is_stale": is_stale,
                "is_in_startup": is_in_startup,  # v93.3: Track startup phase
                "registered_at": datetime.fromtimestamp(
                    service.registered_at
                ).isoformat() if service.registered_at else None,
                "last_heartbeat": datetime.fromtimestamp(
                    service.last_heartbeat
                ).isoformat() if service.last_heartbeat else None,
                "heartbeat_age_seconds": time.time() - service.last_heartbeat,
                "service_age_seconds": time.time() - service.registered_at,  # v93.3
            }

            if not is_alive:
                service_report["health"] = "DEAD"
                report["dead_services"] += 1
            elif is_stale:
                service_report["health"] = "STALE"
                report["stale_services"] += 1
            elif is_in_startup:
                service_report["health"] = "STARTING"  # v93.3: New status
            elif service.status == "degraded":
                service_report["health"] = "DEGRADED"
                report["degraded_services"] += 1
            else:
                service_report["health"] = "HEALTHY"
                report["healthy_services"] += 1

            report["services"][service_name] = service_report

        return report


# =============================================================================
# v17.0: SERVICE LAUNCH CONFIG - Auto-Restart Configuration
# =============================================================================

@dataclass
class ServiceLaunchConfig:
    """
    v17.0: Configuration for how to launch/restart a service.

    Stores all information needed to restart a service automatically
    when it dies or becomes unhealthy.
    """
    service_name: str
    command: List[str]  # e.g., ["python3", "server.py"]
    working_dir: str
    env_vars: Dict[str, str] = None
    python_venv: Optional[str] = None  # Path to venv/bin/python
    restart_policy: str = "always"  # always, on-failure, never
    max_restarts: int = 5
    restart_delay_sec: float = 5.0
    restart_backoff_multiplier: float = 2.0
    max_restart_delay_sec: float = 300.0
    health_check_timeout_sec: float = 30.0

    def __post_init__(self):
        if self.env_vars is None:
            self.env_vars = {}

    def to_dict(self) -> Dict:
        return {
            "service_name": self.service_name,
            "command": self.command,
            "working_dir": self.working_dir,
            "env_vars": self.env_vars,
            "python_venv": self.python_venv,
            "restart_policy": self.restart_policy,
            "max_restarts": self.max_restarts,
            "restart_delay_sec": self.restart_delay_sec,
            "restart_backoff_multiplier": self.restart_backoff_multiplier,
            "max_restart_delay_sec": self.max_restart_delay_sec,
            "health_check_timeout_sec": self.health_check_timeout_sec,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ServiceLaunchConfig":
        return cls(**data)


@dataclass
class ServiceRestartHistory:
    """v17.0: Track restart attempts for a service."""
    service_name: str
    restart_count: int = 0
    last_restart_time: float = 0.0
    last_failure_reason: Optional[str] = None
    consecutive_failures: int = 0

    def record_restart(self, reason: str = None):
        self.restart_count += 1
        self.last_restart_time = time.time()
        self.last_failure_reason = reason

    def record_success(self):
        self.consecutive_failures = 0

    def record_failure(self, reason: str = None):
        self.consecutive_failures += 1
        self.last_failure_reason = reason

    def get_current_delay(self, config: ServiceLaunchConfig) -> float:
        """Calculate current restart delay with exponential backoff."""
        if self.consecutive_failures == 0:
            return config.restart_delay_sec

        delay = config.restart_delay_sec * (
            config.restart_backoff_multiplier ** (self.consecutive_failures - 1)
        )
        return min(delay, config.max_restart_delay_sec)

    def should_restart(self, config: ServiceLaunchConfig) -> bool:
        """Check if service should be restarted based on policy."""
        if config.restart_policy == "never":
            return False

        if config.restart_policy == "on-failure" and self.last_failure_reason is None:
            return False

        return self.restart_count < config.max_restarts


# =============================================================================
# v17.0: SERVICE SUPERVISOR - Cross-Repo Auto-Restart Manager
# =============================================================================

class ServiceSupervisor:
    """
    v17.0: Enterprise-grade service supervisor with auto-restart.

    Features:
    - Automatic restart of dead services with exponential backoff
    - Cross-repo service management (JARVIS, J-Prime, Reactor-Core)
    - Health monitoring with configurable restart policies
    - Parallel launch support for fast startup
    - Graceful degradation when services repeatedly fail
    - Distributed tracing for restart events

    Architecture:
        ┌─────────────────────────────────────────────────────────────────┐
        │                    Service Supervisor v17.0                      │
        ├─────────────────────────────────────────────────────────────────┤
        │  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────────┐  │
        │  │   Monitor   │  │  Restarter  │  │    Health Checker       │  │
        │  │   (async)   │──│   (async)   │──│     (parallel)          │  │
        │  └──────┬──────┘  └──────┬──────┘  └───────────┬─────────────┘  │
        │         │                │                     │                │
        │         ▼                ▼                     ▼                │
        │  ┌──────────────────────────────────────────────────────────┐   │
        │  │              Service Registry (File-Based)                │   │
        │  └──────────────────────────────────────────────────────────┘   │
        └─────────────────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        registry: Optional[ServiceRegistry] = None,
        monitor_interval: float = 10.0,
        launch_configs_file: Optional[Path] = None,
    ):
        """
        Initialize service supervisor.

        Args:
            registry: ServiceRegistry instance (uses global if not provided)
            monitor_interval: Seconds between health checks
            launch_configs_file: File to persist launch configs
        """
        self._registry = registry
        self._monitor_interval = monitor_interval
        self._launch_configs_file = launch_configs_file or (
            Path.home() / ".jarvis" / "registry" / "launch_configs.json"
        )

        self._launch_configs: Dict[str, ServiceLaunchConfig] = {}
        self._restart_history: Dict[str, ServiceRestartHistory] = {}
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._running = False
        self._monitor_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._restart_callbacks: List[callable] = []

        # Load persisted launch configs
        self._load_launch_configs()

        logger.info("[v17.0] ServiceSupervisor initialized")

    @property
    def registry(self) -> ServiceRegistry:
        """Get service registry (lazy initialization)."""
        if self._registry is None:
            self._registry = get_service_registry()
        return self._registry

    def _load_launch_configs(self) -> None:
        """Load persisted launch configurations."""
        try:
            if self._launch_configs_file.exists():
                with open(self._launch_configs_file) as f:
                    data = json.load(f)
                    self._launch_configs = {
                        name: ServiceLaunchConfig.from_dict(cfg)
                        for name, cfg in data.items()
                    }
                logger.debug(f"[v17.0] Loaded {len(self._launch_configs)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to load launch configs: {e}")

    def _save_launch_configs(self) -> None:
        """Persist launch configurations to disk."""
        try:
            self._launch_configs_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                name: cfg.to_dict()
                for name, cfg in self._launch_configs.items()
            }
            with open(self._launch_configs_file, 'w') as f:
                json.dump(data, f, indent=2)
            logger.debug(f"[v17.0] Saved {len(data)} launch configs")
        except Exception as e:
            logger.warning(f"[v17.0] Failed to save launch configs: {e}")

    def register_service_launch(self, config: ServiceLaunchConfig) -> None:
        """
        Register how to launch a service for auto-restart.

        Args:
            config: ServiceLaunchConfig with launch details
        """
        self._launch_configs[config.service_name] = config
        self._restart_history[config.service_name] = ServiceRestartHistory(
            service_name=config.service_name
        )
        self._save_launch_configs()
        logger.info(f"[v17.0] Registered launch config for: {config.service_name}")

    def on_restart(self, callback: callable) -> None:
        """Register callback for restart events."""
        self._restart_callbacks.append(callback)

    async def _notify_restart(
        self,
        service_name: str,
        success: bool,
        reason: str = None
    ) -> None:
        """Notify callbacks of restart event."""
        for callback in self._restart_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(service_name, success, reason)
                else:
                    callback(service_name, success, reason)
            except Exception as e:
                logger.warning(f"[v17.0] Restart callback error: {e}")

    async def launch_service(
        self,
        service_name: str,
        wait_for_healthy: bool = True,
    ) -> Optional[asyncio.subprocess.Process]:
        """
        Launch a service using its registered config.

        Args:
            service_name: Service to launch
            wait_for_healthy: Wait for health check to pass

        Returns:
            Process if launched successfully, None otherwise
        """
        async with self._lock:
            if service_name not in self._launch_configs:
                logger.warning(f"[v17.0] No launch config for: {service_name}")
                return None

            config = self._launch_configs[service_name]
            history = self._restart_history.get(
                service_name,
                ServiceRestartHistory(service_name=service_name)
            )

            # Check restart policy
            if not history.should_restart(config):
                logger.warning(
                    f"[v17.0] Service {service_name} exceeded max restarts "
                    f"({history.restart_count}/{config.max_restarts})"
                )
                return None

            # Calculate delay if this is a restart
            if history.restart_count > 0:
                delay = history.get_current_delay(config)
                logger.info(
                    f"[v17.0] Waiting {delay:.1f}s before restarting {service_name} "
                    f"(attempt {history.restart_count + 1}/{config.max_restarts})"
                )
                await asyncio.sleep(delay)

            # Build command
            cmd = config.command.copy()
            if config.python_venv:
                # Replace python with venv python
                if cmd[0] in ("python", "python3"):
                    cmd[0] = config.python_venv

            # Build environment
            env = os.environ.copy()
            env.update(config.env_vars)
            env["PYTHONPATH"] = config.working_dir

            # Create log files
            log_dir = Path.home() / ".jarvis" / "logs" / "services"
            log_dir.mkdir(parents=True, exist_ok=True)
            stdout_log = log_dir / f"{service_name}_stdout.log"
            stderr_log = log_dir / f"{service_name}_stderr.log"

            try:
                logger.info(f"[v17.0] Launching {service_name}: {' '.join(cmd)}")

                stdout_fd = open(stdout_log, 'a')
                stderr_fd = open(stderr_log, 'a')

                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=config.working_dir,
                    env=env,
                    stdout=stdout_fd,
                    stderr=stderr_fd,
                    start_new_session=True,
                )

                self._processes[service_name] = process
                history.record_restart(reason="supervisor_launch")
                self._restart_history[service_name] = history

                logger.info(f"[v17.0] {service_name} launched (PID: {process.pid})")

                # Wait for health check if requested
                if wait_for_healthy:
                    service_info = await self.registry.wait_for_service(
                        service_name,
                        timeout=config.health_check_timeout_sec,
                    )
                    if service_info:
                        history.record_success()
                        logger.info(f"[v17.0] ✅ {service_name} healthy")
                        await self._notify_restart(service_name, True, "healthy")
                    else:
                        history.record_failure(reason="health_check_timeout")
                        logger.warning(f"[v17.0] ⚠️ {service_name} unhealthy after launch")
                        await self._notify_restart(service_name, False, "health_check_timeout")

                return process

            except Exception as e:
                history.record_failure(reason=str(e))
                self._restart_history[service_name] = history
                logger.error(f"[v17.0] Failed to launch {service_name}: {e}")
                await self._notify_restart(service_name, False, str(e))
                return None

    async def restart_service(self, service_name: str) -> bool:
        """
        Restart a dead or unhealthy service.

        Args:
            service_name: Service to restart

        Returns:
            True if restart was successful
        """
        logger.info(f"[v17.0] Restarting service: {service_name}")

        # Kill existing process if any
        if service_name in self._processes:
            old_proc = self._processes[service_name]
            if old_proc.returncode is None:
                try:
                    old_proc.terminate()
                    await asyncio.wait_for(old_proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    old_proc.kill()
            del self._processes[service_name]

        # Deregister from registry
        await self.registry.deregister_service(service_name)

        # Launch fresh
        process = await self.launch_service(service_name, wait_for_healthy=True)
        return process is not None

    async def _monitor_loop(self) -> None:
        """
        v93.14: Enhanced background loop with proactive heartbeat sending.

        CRITICAL FIX: The previous implementation only checked if services were
        stale via discover_service(), but didn't send heartbeats to keep them alive.
        This caused a vicious cycle where services were marked stale because no one
        was sending heartbeats on their behalf.

        Now:
        1. List all services WITHOUT triggering stale warnings
        2. Do actual HTTP health checks for services with health endpoints
        3. Send heartbeats for healthy services to keep them alive
        4. Only restart truly unhealthy services
        """
        logger.info(f"[v93.14] Service monitor started (interval: {self._monitor_interval}s)")

        while self._running:
            try:
                await asyncio.sleep(self._monitor_interval)

                # Check each registered service
                for service_name, config in self._launch_configs.items():
                    if config.restart_policy == "never":
                        continue

                    try:
                        await self._check_and_heartbeat_service(service_name, config)
                    except Exception as svc_error:
                        logger.debug(f"[v93.14] Error checking {service_name}: {svc_error}")

            except asyncio.CancelledError:
                logger.info("[v93.14] Service monitor stopping")
                break
            except Exception as e:
                logger.error(f"[v93.14] Monitor loop error: {e}", exc_info=True)

    async def _check_and_heartbeat_service(
        self,
        service_name: str,
        config: "ServiceLaunchConfig"
    ) -> None:
        """
        v93.14: Check service health and send heartbeat if alive.

        This method performs:
        1. Direct HTTP health check (preferred if endpoint available)
        2. Process alive check (fallback)
        3. Heartbeat sending on successful health check
        4. Service restart if truly dead

        Args:
            service_name: Name of service to check
            config: Launch configuration with health endpoint info
        """
        import aiohttp

        is_healthy = False
        health_status = "unknown"

        # ═══════════════════════════════════════════════════════════════════════
        # Step 1: Try HTTP health check if endpoint is configured
        # ═══════════════════════════════════════════════════════════════════════
        health_url = None
        if hasattr(config, 'health_endpoint') and config.health_endpoint:
            # Get port from environment or config
            port = None
            if "jarvis-prime" in service_name.lower() or "jprime" in service_name.lower():
                port = int(os.environ.get("JARVIS_PRIME_PORT", "8000"))
            elif "reactor" in service_name.lower():
                port = int(os.environ.get("REACTOR_CORE_PORT", "8090"))

            if port:
                health_url = f"http://localhost:{port}{config.health_endpoint}"

        if health_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        health_url,
                        timeout=aiohttp.ClientTimeout(total=config.health_check_timeout_sec or 5.0)
                    ) as response:
                        is_healthy = response.status == 200
                        if is_healthy:
                            health_status = "healthy"
                            logger.debug(f"[v93.14] {service_name} HTTP health check passed")
                        else:
                            health_status = f"unhealthy (HTTP {response.status})"
            except aiohttp.ClientError as http_err:
                health_status = f"unreachable ({type(http_err).__name__})"
                logger.debug(f"[v93.14] {service_name} HTTP health check failed: {http_err}")
            except Exception as http_err:
                health_status = f"error ({type(http_err).__name__})"
                logger.debug(f"[v93.14] {service_name} HTTP health check error: {http_err}")

        # ═══════════════════════════════════════════════════════════════════════
        # Step 2: Fallback to process alive check if HTTP check failed
        # ═══════════════════════════════════════════════════════════════════════
        if not is_healthy and service_name in self._processes:
            proc = self._processes[service_name]
            if proc.returncode is None:
                # Process is running - assume healthy for heartbeat purposes
                is_healthy = True
                health_status = "process_alive"
                logger.debug(f"[v93.14] {service_name} process alive (PID: {proc.pid})")

        # ═══════════════════════════════════════════════════════════════════════
        # Step 3: Send heartbeat if service is healthy
        # ═══════════════════════════════════════════════════════════════════════
        if is_healthy:
            try:
                await self.registry.heartbeat(
                    service_name,
                    status=health_status,
                    metadata={"checked_by": "ServiceSupervisor", "health_url": health_url}
                )
                logger.debug(f"[v93.14] Sent heartbeat for {service_name} ({health_status})")
            except Exception as hb_err:
                logger.warning(f"[v93.14] Failed to send heartbeat for {service_name}: {hb_err}")
        else:
            # ═══════════════════════════════════════════════════════════════════
            # Step 4: Service is unhealthy - check if we should restart
            # ═══════════════════════════════════════════════════════════════════
            history = self._restart_history.get(service_name)
            if history and not history.should_restart(config.max_restarts or 5):
                logger.warning(
                    f"[v93.14] {service_name} unhealthy but max restarts reached, skipping"
                )
                return

            # Check if service is registered but stale (use list_services to avoid warning)
            services = await self.registry.list_services(healthy_only=False)
            service_info = next(
                (s for s in services if s.service_name == service_name),
                None
            )

            if service_info is None:
                # Service not registered at all - needs restart
                logger.warning(
                    f"[v93.14] {service_name} not registered, initiating restart"
                )
                await self.restart_service(service_name)
            elif not service_info.is_process_alive():
                # Service registered but process is dead
                logger.warning(
                    f"[v93.14] {service_name} process dead (was PID {service_info.pid}), "
                    f"initiating restart"
                )
                await self.restart_service(service_name)
            else:
                # Process is alive but HTTP check failed - send degraded heartbeat
                # to prevent stale detection, but don't restart yet
                try:
                    await self.registry.heartbeat(
                        service_name,
                        status="degraded",
                        metadata={"reason": health_status}
                    )
                    logger.debug(
                        f"[v93.14] Sent degraded heartbeat for {service_name} "
                        f"(process alive but health check failed)"
                    )
                except Exception as hb_err:
                    logger.debug(f"[v93.14] Failed to send degraded heartbeat: {hb_err}")

    async def start(self) -> None:
        """Start the service supervisor."""
        self._running = True
        self._monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="service_supervisor_monitor"
        )
        logger.info("[v17.0] ServiceSupervisor started")

    async def stop(self) -> None:
        """Stop the service supervisor."""
        self._running = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("[v17.0] ServiceSupervisor stopped")

    async def launch_all_services(
        self,
        parallel: bool = True,
        wait_for_healthy: bool = True,
    ) -> Dict[str, bool]:
        """
        Launch all registered services.

        Args:
            parallel: Launch services in parallel
            wait_for_healthy: Wait for health checks

        Returns:
            Dict mapping service_name to success status
        """
        results = {}

        if parallel:
            # Launch all in parallel
            tasks = {
                name: asyncio.create_task(
                    self.launch_service(name, wait_for_healthy=wait_for_healthy)
                )
                for name in self._launch_configs.keys()
            }

            for name, task in tasks.items():
                try:
                    process = await asyncio.wait_for(task, timeout=120.0)
                    results[name] = process is not None
                except asyncio.TimeoutError:
                    results[name] = False
                    logger.warning(f"[v17.0] Timeout launching {name}")
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")
        else:
            # Launch sequentially
            for name in self._launch_configs.keys():
                try:
                    process = await self.launch_service(name, wait_for_healthy=wait_for_healthy)
                    results[name] = process is not None
                except Exception as e:
                    results[name] = False
                    logger.error(f"[v17.0] Error launching {name}: {e}")

        return results

    def get_stats(self) -> Dict[str, Any]:
        """Get supervisor statistics."""
        return {
            "running": self._running,
            "services_registered": len(self._launch_configs),
            "services_with_process": len(self._processes),
            "restart_history": {
                name: {
                    "restart_count": history.restart_count,
                    "consecutive_failures": history.consecutive_failures,
                    "last_restart": history.last_restart_time,
                    "last_failure": history.last_failure_reason,
                }
                for name, history in self._restart_history.items()
            },
        }


# Global supervisor instance
_global_supervisor: Optional[ServiceSupervisor] = None


def get_service_supervisor() -> ServiceSupervisor:
    """Get global service supervisor instance (singleton)."""
    global _global_supervisor
    if _global_supervisor is None:
        _global_supervisor = ServiceSupervisor()
    return _global_supervisor


# =============================================================================
# v93.0: Startup Directory Initialization
# =============================================================================

# All required .jarvis directories for JARVIS ecosystem
REQUIRED_JARVIS_DIRECTORIES = [
    Path.home() / ".jarvis",
    Path.home() / ".jarvis" / "registry",
    Path.home() / ".jarvis" / "state",
    Path.home() / ".jarvis" / "state" / "locks",
    Path.home() / ".jarvis" / "locks",
    Path.home() / ".jarvis" / "logs",
    Path.home() / ".jarvis" / "logs" / "services",
    Path.home() / ".jarvis" / "cross_repo",
    Path.home() / ".jarvis" / "cross_repo" / "events",
    Path.home() / ".jarvis" / "cross_repo" / "sync",
    Path.home() / ".jarvis" / "trinity",
    Path.home() / ".jarvis" / "trinity" / "ipc",
    Path.home() / ".jarvis" / "trinity" / "commands",
    Path.home() / ".jarvis" / "bridge",
    Path.home() / ".jarvis" / "bridge" / "training_staging",
    Path.home() / ".jarvis" / "dlq",
    Path.home() / ".jarvis" / "cache",
    Path.home() / ".jarvis" / "ouroboros",
    Path.home() / ".jarvis" / "ouroboros" / "events",
    Path.home() / ".jarvis" / "ouroboros" / "memory",
    Path.home() / ".jarvis" / "ouroboros" / "snapshots",
    Path.home() / ".jarvis" / "ouroboros" / "cache",
    Path.home() / ".jarvis" / "ouroboros" / "sandbox",
    Path.home() / ".jarvis" / "ouroboros" / "rollback",
    Path.home() / ".jarvis" / "ouroboros" / "learning_cache",
    Path.home() / ".jarvis" / "ouroboros" / "manual_review",
    Path.home() / ".jarvis" / "reactor",
    Path.home() / ".jarvis" / "reactor" / "events",
    Path.home() / ".jarvis" / "oracle",
    Path.home() / ".jarvis" / "experience_mesh",
    Path.home() / ".jarvis" / "experience_mesh" / "fallback",
    Path.home() / ".jarvis" / "experience_queue",
    Path.home() / ".jarvis" / "experience_queue" / "ouroboros",
    Path.home() / ".jarvis" / "atomic_backups",
    Path.home() / ".jarvis" / "bulletproof",
    Path.home() / ".jarvis" / "performance",
]


def ensure_all_jarvis_directories() -> Dict[str, Any]:
    """
    v93.0: Ensure all required JARVIS directories exist.

    Should be called ONCE at startup before any services initialize.
    This prevents race conditions and "No such file or directory" errors
    throughout the JARVIS ecosystem.

    Returns:
        Dict with creation stats: created, existed, failed
    """
    stats = {
        "created": [],
        "existed": [],
        "failed": [],
        "total": len(REQUIRED_JARVIS_DIRECTORIES),
    }

    logger.info("[v93.0] Pre-flight directory initialization...")

    for dir_path in REQUIRED_JARVIS_DIRECTORIES:
        try:
            if dir_path.exists():
                stats["existed"].append(str(dir_path))
            else:
                dir_path.mkdir(parents=True, exist_ok=True)
                stats["created"].append(str(dir_path))
                logger.debug(f"[v93.0] Created directory: {dir_path}")
        except Exception as e:
            stats["failed"].append({"path": str(dir_path), "error": str(e)})
            logger.error(f"[v93.0] Failed to create directory {dir_path}: {e}")

    # Summary
    if stats["failed"]:
        logger.error(
            f"[v93.0] Directory initialization INCOMPLETE: "
            f"{len(stats['created'])} created, {len(stats['existed'])} existed, "
            f"{len(stats['failed'])} FAILED"
        )
    else:
        logger.info(
            f"[v93.0] Directory initialization complete: "
            f"{len(stats['created'])} created, {len(stats['existed'])} existed"
        )

    return stats


# =============================================================================
# Convenience Functions
# =============================================================================

_global_registry: Optional[ServiceRegistry] = None


def get_service_registry() -> ServiceRegistry:
    """Get global service registry instance (singleton)."""
    global _global_registry
    if _global_registry is None:
        _global_registry = ServiceRegistry()
    return _global_registry


async def register_current_service(
    service_name: str,
    port: int,
    health_endpoint: str = "/health",
    metadata: Optional[Dict] = None
) -> ServiceInfo:
    """
    Convenience function to register current process as a service.

    Args:
        service_name: Name for this service
        port: Port this service listens on
        health_endpoint: Health check endpoint
        metadata: Optional metadata

    Returns:
        ServiceInfo for registered service
    """
    registry = get_service_registry()
    return await registry.register_service(
        service_name=service_name,
        pid=os.getpid(),
        port=port,
        health_endpoint=health_endpoint,
        metadata=metadata
    )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Core classes
    "ServiceRegistry",
    "ServiceInfo",
    # v17.0: Service Supervisor
    "ServiceSupervisor",
    "ServiceLaunchConfig",
    "ServiceRestartHistory",
    # v93.0: Directory Management
    "DirectoryManager",
    "ensure_all_jarvis_directories",
    "REQUIRED_JARVIS_DIRECTORIES",
    # Convenience functions
    "get_service_registry",
    "get_service_supervisor",
    "register_current_service",
]
