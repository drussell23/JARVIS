"""
JARVIS-Prime Orchestrator - Tier 0 Local Brain Subprocess Manager
==================================================================

Manages JARVIS-Prime as a critical microservice subprocess within the
JARVIS supervisor lifecycle. This ensures the local brain is always
available before routing any commands.

Architecture:
    ┌─────────────────────────────────────────────────────────────────┐
    │  JARVISSupervisor                                               │
    │  ├── JarvisPrimeOrchestrator (this file)                        │
    │  │   ├── Subprocess Management (start, stop, restart)           │
    │  │   ├── Health Monitoring (periodic checks)                    │
    │  │   ├── Auto-Recovery (respawn on failure)                     │
    │  │   └── Graceful Shutdown (wait for in-flight requests)        │
    │  └── ... other components                                        │
    └─────────────────────────────────────────────────────────────────┘

Integration:
    from backend.core.supervisor.jarvis_prime_orchestrator import (
        JarvisPrimeOrchestrator,
        get_jarvis_prime_orchestrator,
    )

Author: JARVIS v5.0 Living OS
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

# v11.1: Import IntelligentPortManager for async, parallel port coordination
try:
    from backend.core.supervisor.intelligent_port_manager import (
        IntelligentPortManager,
        ProcessInfo as PortProcessInfo,
        ProcessType,
    )
    INTELLIGENT_PORT_MANAGER_AVAILABLE = True
except ImportError:
    INTELLIGENT_PORT_MANAGER_AVAILABLE = False

from backend.core.async_safety import LazyAsyncLock

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class JarvisPrimeConfig:
    """Configuration for JARVIS-Prime subprocess management."""

    # JARVIS-Prime location
    prime_repo_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            str(Path.home() / "Documents" / "repos" / "jarvis-prime")
        ))
    )

    # Server settings
    host: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_HOST", "127.0.0.1")
    )
    port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8000"))
    )

    # Model settings
    models_dir: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_MODELS_DIR", "./models")
    )
    initial_model: Optional[str] = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_INITIAL_MODEL")
    )

    # Startup settings
    startup_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_STARTUP_TIMEOUT", "30.0"))
    )
    health_check_interval_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_HEALTH_INTERVAL", "10.0"))
    )
    health_check_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT", "10.0"))  # v10.8: Increased from 5s
    )

    # v10.8: Adaptive Health System Configuration
    health_check_timeout_min_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT_MIN", "5.0"))
    )
    health_check_timeout_max_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT_MAX", "30.0"))
    )
    startup_grace_period_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_STARTUP_GRACE", "60.0"))
    )
    health_recovery_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_RECOVERY_THRESHOLD", "3"))
    )
    degradation_warning_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_DEGRADE_WARN", "2"))
    )
    degradation_critical_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_DEGRADE_CRIT", "5"))
    )
    restart_failure_threshold: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_RESTART_THRESHOLD", "8"))
    )

    # Recovery settings
    max_restart_attempts: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_MAX_RESTARTS", "3"))
    )
    restart_backoff_base_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_RESTART_BACKOFF", "2.0"))
    )
    restart_backoff_max_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_RESTART_MAX_BACKOFF", "60.0"))
    )

    # Feature flags
    enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    auto_start: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_AUTO_START", "true").lower() == "true"
    )
    debug_mode: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DEBUG", "false").lower() == "true"
    )

    # Docker settings
    use_docker: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_USE_DOCKER", "false").lower() == "true"
    )
    docker_image: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_IMAGE", "jarvis-prime:latest")
    )
    docker_container_name: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_CONTAINER_NAME", "jarvis-prime")
    )
    docker_memory_limit: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_MEMORY", "10g")
    )
    docker_cpus: str = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_DOCKER_CPUS", "4")
    )
    docker_volumes: Dict[str, str] = field(default_factory=dict)  # host:container mappings

    # v11.0: Intelligent Instance Adoption - reuse existing healthy instances
    adopt_existing_instances: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ADOPT_EXISTING", "true").lower() == "true"
    )
    adoption_health_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_ADOPT_TIMEOUT", "5.0"))
    )
    require_model_match_for_adoption: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ADOPT_MODEL_MATCH", "false").lower() == "true"
    )

    # v11.0: Dynamic Port Fallback - try alternative ports if primary is unavailable
    enable_port_fallback: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_PORT_FALLBACK", "true").lower() == "true"
    )
    fallback_port_range_start: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_FALLBACK_PORT_START", "8003"))
    )
    fallback_port_range_end: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_FALLBACK_PORT_END", "8010"))
    )
    max_port_fallback_attempts: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_MAX_PORT_ATTEMPTS", "5"))
    )

    # v11.0: Instance Ownership Tracking
    instance_ownership_file: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_OWNERSHIP_FILE",
            str(Path.home() / ".jarvis" / "prime_instance.json")
        ))
    )

    @property
    def server_url(self) -> str:
        """Get the full server URL."""
        return f"http://{self.host}:{self.port}"

    @property
    def health_url(self) -> str:
        """Get the health check URL."""
        return f"{self.server_url}/health"

    @property
    def completions_url(self) -> str:
        """Get the chat completions URL (OpenAI-compatible)."""
        return f"{self.server_url}/v1/chat/completions"


class PrimeStatus(str, Enum):
    """JARVIS-Prime subprocess status."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass
class PrimeHealth:
    """Health information for JARVIS-Prime."""
    status: PrimeStatus
    pid: Optional[int] = None
    uptime_seconds: float = 0.0
    last_health_check: Optional[datetime] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    average_latency_ms: float = 0.0
    model_loaded: bool = False
    model_name: Optional[str] = None
    error_message: Optional[str] = None

    def is_healthy(self) -> bool:
        """Check if JARVIS-Prime is healthy."""
        return self.status == PrimeStatus.RUNNING and self.consecutive_failures == 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "status": self.status.value,
            "pid": self.pid,
            "uptime_seconds": self.uptime_seconds,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "consecutive_failures": self.consecutive_failures,
            "total_requests": self.total_requests,
            "average_latency_ms": self.average_latency_ms,
            "model_loaded": self.model_loaded,
            "model_name": self.model_name,
            "error_message": self.error_message,
        }


# =============================================================================
# JARVIS-Prime Orchestrator
# =============================================================================

class JarvisPrimeOrchestrator:
    """
    Manages JARVIS-Prime as a critical microservice subprocess.

    Features:
    - Subprocess lifecycle management (start, stop, restart)
    - Health monitoring with auto-recovery
    - Exponential backoff on failures
    - Graceful shutdown with in-flight request handling
    - Integration with supervisor narrator
    - Lazy initialization (doesn't start until needed)

    Example:
        >>> orchestrator = get_jarvis_prime_orchestrator()
        >>> await orchestrator.start()
        >>> health = orchestrator.get_health()
        >>> if health.is_healthy():
        ...     # Route to JARVIS-Prime
        >>> await orchestrator.stop()
    """

    def __init__(
        self,
        config: Optional[JarvisPrimeConfig] = None,
        narrator_callback: Optional[Callable[[str], asyncio.coroutine]] = None,
    ):
        """
        Initialize the JARVIS-Prime orchestrator.

        Args:
            config: Configuration for JARVIS-Prime
            narrator_callback: Optional callback for voice announcements
        """
        self.config = config or JarvisPrimeConfig()
        self._narrator_callback = narrator_callback

        # Process management
        self._process: Optional[asyncio.subprocess.Process] = None
        self._start_time: Optional[datetime] = None
        self._restart_count: int = 0

        # Health tracking
        self._health = PrimeHealth(
            status=PrimeStatus.DISABLED if not self.config.enabled else PrimeStatus.STOPPED
        )
        self._health_check_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None

        # State management
        self._started = False
        self._stopping = False
        self._shutdown_event = asyncio.Event()

        # Callbacks
        self._on_status_change: List[Callable[[PrimeStatus], None]] = []
        self._on_health_update: List[Callable[[PrimeHealth], None]] = []

        # ROOT CAUSE FIX: Lock to prevent concurrent start/restart attempts
        self._start_lock = asyncio.Lock()

        # Latency tracking for metrics
        self._latency_samples: List[float] = []
        self._max_latency_samples = 100

        # Docker-specific state
        self._docker_container_id: Optional[str] = None

        # v10.8: Adaptive Health System State
        self._current_timeout: float = self.config.health_check_timeout_seconds
        self._consecutive_successes: int = 0
        self._in_startup_grace: bool = True
        self._startup_complete_time: Optional[datetime] = None
        self._last_failure_reason: Optional[str] = None
        self._health_check_interval_multiplier: float = 1.0  # Dynamically adjusted

        # v11.0: Instance Adoption & Port Fallback State
        self._adopted_instance: bool = False  # True if we adopted an existing instance
        self._effective_port: int = self.config.port  # May differ from config if using fallback
        self._adoption_info: Optional[Dict[str, Any]] = None  # Info about adopted instance
        self._original_port: int = self.config.port  # Remember original port for logging

        # v11.1: Intelligent Port Manager - parallel, async port coordination
        self._port_manager: Optional[IntelligentPortManager] = None
        if INTELLIGENT_PORT_MANAGER_AVAILABLE:
            self._port_manager = IntelligentPortManager(
                host=self.config.host,
                primary_port=self.config.port,
                fallback_port_start=self.config.fallback_port_range_start,
                fallback_port_end=self.config.fallback_port_range_end,
                max_cleanup_time_seconds=float(os.getenv("JARVIS_PRIME_CLEANUP_TIMEOUT", "10.0")),
                adopt_existing_instances=self.config.adopt_existing_instances,
                health_probe_timeout=self.config.adoption_health_timeout_seconds,
            )
            logger.info("[JarvisPrime] v11.1 IntelligentPortManager initialized")

        logger.info(
            f"[JarvisPrime] Orchestrator initialized "
            f"(enabled={self.config.enabled}, auto_start={self.config.auto_start})"
        )

    # =========================================================================
    # Lifecycle Methods
    # =========================================================================

    async def start(self) -> bool:
        """
        Start JARVIS-Prime subprocess.

        Returns:
            True if started successfully, False otherwise
        """
        # ROOT CAUSE FIX: Prevent concurrent start attempts with lock
        async with self._start_lock:
            if not self.config.enabled:
                logger.info("[JarvisPrime] Disabled via configuration")
                self._health.status = PrimeStatus.DISABLED
                return False

            if self._started and self._process and self._process.returncode is None:
                logger.debug("[JarvisPrime] Already running")
                return True

            try:
                await self._announce("Initializing local brain.")

                # Update status
                self._set_status(PrimeStatus.STARTING)

                # Validate JARVIS-Prime repo exists
                if not self._validate_repo():
                    self._health.error_message = f"JARVIS-Prime repo not found at {self.config.prime_repo_path}"
                    self._set_status(PrimeStatus.FAILED)
                    return False

                # Create HTTP session for health checks
                if self._http_session is None:
                    self._http_session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout_seconds)
                    )

                # ROOT CAUSE FIX: Retry logic to handle race conditions
                max_spawn_attempts = int(os.getenv("JARVIS_PRIME_SPAWN_RETRIES", "3"))
                spawn_retry_delay = float(os.getenv("JARVIS_PRIME_SPAWN_RETRY_DELAY", "2.0"))

                for attempt in range(max_spawn_attempts):
                    try:
                        # ROOT CAUSE FIX: Clean up port and check if we reused existing process
                        reused_existing = await self._ensure_port_available()

                        # CRITICAL: Skip spawn if we reused an existing healthy process
                        if reused_existing:
                            logger.info(f"[JarvisPrime] Reused existing process, skipping spawn")
                            success = True  # Treat as successful spawn
                        else:
                            # Start new subprocess
                            success = await self._spawn_process()

                        if success:
                            # Wait for it to become healthy
                            healthy = await self._wait_for_healthy()

                            if healthy:
                                self._started = True
                                self._restart_count = 0
                                self._start_time = datetime.now()
                                self._set_status(PrimeStatus.RUNNING)

                                # Start health monitoring
                                self._health_check_task = asyncio.create_task(
                                    self._health_check_loop()
                                )

                                await self._announce("Local brain online and ready.")
                                logger.info(f"[JarvisPrime] Started successfully (PID: {self._process.pid})")
                                return True
                            else:
                                # Process spawned but not healthy
                                await self._terminate_process()

                                if attempt < max_spawn_attempts - 1:
                                    logger.warning(
                                        f"[JarvisPrime] Process unhealthy (attempt {attempt + 1}/{max_spawn_attempts}), "
                                        f"retrying in {spawn_retry_delay}s..."
                                    )
                                    await asyncio.sleep(spawn_retry_delay)
                                    continue
                                else:
                                    self._set_status(PrimeStatus.FAILED)
                                    return False
                        else:
                            # Spawn failed
                            if attempt < max_spawn_attempts - 1:
                                logger.warning(
                                    f"[JarvisPrime] Spawn failed (attempt {attempt + 1}/{max_spawn_attempts}), "
                                    f"retrying in {spawn_retry_delay}s..."
                                )
                                await asyncio.sleep(spawn_retry_delay)
                                continue
                            else:
                                self._set_status(PrimeStatus.FAILED)
                                return False

                    except RuntimeError as e:
                        # Port cleanup failed - this is fatal
                        if "Port" in str(e) and "cannot be freed" in str(e):
                            logger.error(f"[JarvisPrime] Fatal port cleanup error: {e}")
                            self._set_status(PrimeStatus.FAILED)
                            self._health.error_message = str(e)
                            return False

                        # Other runtime errors - retry
                        if attempt < max_spawn_attempts - 1:
                            logger.warning(
                                f"[JarvisPrime] Start error (attempt {attempt + 1}/{max_spawn_attempts}): {e}, "
                                f"retrying in {spawn_retry_delay}s..."
                            )
                            await asyncio.sleep(spawn_retry_delay)
                            continue
                        else:
                            raise

                # All attempts exhausted
                self._set_status(PrimeStatus.FAILED)
                return False

            except Exception as e:
                logger.error(f"[JarvisPrime] Start failed: {e}")
                self._health.error_message = str(e)
                self._set_status(PrimeStatus.FAILED)
                return False

    async def stop(self) -> None:
        """Gracefully stop JARVIS-Prime subprocess."""
        if self._stopping:
            return

        self._stopping = True
        self._shutdown_event.set()

        try:
            await self._announce("Shutting down local brain.")

            # Stop health check loop
            if self._health_check_task:
                self._health_check_task.cancel()
                try:
                    await self._health_check_task
                except asyncio.CancelledError:
                    pass
                self._health_check_task = None

            # Terminate process
            await self._terminate_process()

            # Close HTTP session
            if self._http_session:
                await self._http_session.close()
                self._http_session = None

            self._started = False
            self._set_status(PrimeStatus.STOPPED)

            logger.info("[JarvisPrime] Stopped gracefully")

        finally:
            self._stopping = False

    async def restart(self) -> bool:
        """
        Restart JARVIS-Prime subprocess.

        Returns:
            True if restarted successfully
        """
        self._set_status(PrimeStatus.RESTARTING)
        self._restart_count += 1

        await self._announce("Restarting local brain.")

        # Calculate backoff
        backoff = min(
            self.config.restart_backoff_base_seconds * (2 ** (self._restart_count - 1)),
            self.config.restart_backoff_max_seconds
        )

        if self._restart_count > 1:
            logger.info(f"[JarvisPrime] Restart attempt {self._restart_count}, waiting {backoff:.1f}s")
            await asyncio.sleep(backoff)

        if self._restart_count > self.config.max_restart_attempts:
            logger.error(f"[JarvisPrime] Max restart attempts ({self.config.max_restart_attempts}) exceeded")
            self._set_status(PrimeStatus.FAILED)
            self._health.error_message = "Max restart attempts exceeded"
            return False

        # Stop and restart
        await self._terminate_process()
        return await self.start()

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    def get_health(self) -> PrimeHealth:
        """Get current health status."""
        # Update uptime
        if self._start_time and self._health.status == PrimeStatus.RUNNING:
            self._health.uptime_seconds = (datetime.now() - self._start_time).total_seconds()

        # Update PID
        if self._process:
            self._health.pid = self._process.pid

        return self._health

    async def check_health(self) -> bool:
        """
        v10.8: Perform an adaptive health check with intelligent timeout.

        Features:
        - Adaptive timeout that scales based on failures
        - Classifies failure reasons (timeout, connection refused, error)
        - Tracks consecutive successes for hysteresis recovery

        Returns:
            True if healthy
        """
        if not self._http_session:
            self._last_failure_reason = "no_session"
            return False

        try:
            start_time = time.perf_counter()

            # v10.8: Use adaptive timeout with aiohttp
            adaptive_timeout = aiohttp.ClientTimeout(total=self._current_timeout)

            async with self._http_session.get(
                self.config.health_url,
                timeout=adaptive_timeout
            ) as response:
                latency_ms = (time.perf_counter() - start_time) * 1000
                self._record_latency(latency_ms)

                if response.status == 200:
                    data = await response.json()

                    self._health.last_health_check = datetime.now()
                    self._health.consecutive_failures = 0
                    self._health.model_loaded = data.get("model_loaded", False)
                    self._health.model_name = data.get("model_name")

                    # v10.8: Track consecutive successes for hysteresis
                    self._consecutive_successes += 1
                    self._last_failure_reason = None

                    # v10.8: Gradually reduce timeout on success (with floor)
                    self._current_timeout = max(
                        self.config.health_check_timeout_min_seconds,
                        self._current_timeout * 0.9  # Reduce by 10% per success
                    )

                    # v10.8: Reduce interval multiplier on success
                    self._health_check_interval_multiplier = max(
                        1.0,
                        self._health_check_interval_multiplier * 0.8
                    )

                    return True
                else:
                    self._health.consecutive_failures += 1
                    self._consecutive_successes = 0
                    self._last_failure_reason = f"http_{response.status}"
                    return False

        except asyncio.TimeoutError:
            elapsed = time.perf_counter() - start_time
            self._health.consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = "timeout"

            # v10.8: Increase timeout for next attempt (with ceiling)
            self._current_timeout = min(
                self.config.health_check_timeout_max_seconds,
                self._current_timeout * 1.5  # Increase by 50% on timeout
            )

            # v10.8: Only warn if outside startup grace period
            if not self._is_in_startup_grace():
                logger.warning(
                    f"[JarvisPrime] Health check timed out after {elapsed:.1f}s "
                    f"(adaptive timeout: {self._current_timeout:.1f}s, "
                    f"failures: {self._health.consecutive_failures})"
                )
            else:
                logger.debug(
                    f"[JarvisPrime] Health check timeout during startup grace "
                    f"(timeout: {self._current_timeout:.1f}s)"
                )

            return False

        except aiohttp.ClientConnectorError as e:
            self._health.consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = "connection_refused"

            if not self._is_in_startup_grace():
                logger.warning(f"[JarvisPrime] Health check connection refused: {e}")
            else:
                logger.debug(f"[JarvisPrime] Connection refused during startup grace")

            return False

        except aiohttp.ClientError as e:
            self._health.consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = f"client_error:{type(e).__name__}"

            if not self._is_in_startup_grace():
                logger.warning(f"[JarvisPrime] Health check failed: {e}")
            return False

        except Exception as e:
            self._health.consecutive_failures += 1
            self._consecutive_successes = 0
            self._last_failure_reason = f"error:{type(e).__name__}"
            logger.error(f"[JarvisPrime] Health check error: {e}")
            return False

    def _is_in_startup_grace(self) -> bool:
        """v10.8: Check if we're still in the startup grace period."""
        if not self._in_startup_grace:
            return False

        if self._startup_complete_time is None:
            return True  # Still starting

        elapsed = (datetime.now() - self._startup_complete_time).total_seconds()
        if elapsed >= self.config.startup_grace_period_seconds:
            self._in_startup_grace = False
            logger.debug(f"[JarvisPrime] Startup grace period ended after {elapsed:.1f}s")
            return False

        return True

    async def _health_check_loop(self) -> None:
        """
        v10.8: Adaptive background health check loop with intelligent recovery.

        Features:
        - Startup grace period (no degradation during initial warmup)
        - Hysteresis for recovery (require multiple successes before RUNNING)
        - Configurable degradation thresholds
        - Adaptive check interval that slows down during issues
        - Better failure classification and logging
        """
        # v10.8: Mark startup complete time when health loop starts
        self._startup_complete_time = datetime.now()
        self._in_startup_grace = True

        while not self._shutdown_event.is_set():
            try:
                # v10.8: Use adaptive interval based on health state
                adaptive_interval = (
                    self.config.health_check_interval_seconds *
                    self._health_check_interval_multiplier
                )
                await asyncio.sleep(adaptive_interval)

                if self._shutdown_event.is_set():
                    break

                # Check if process is still running
                if self._process and self._process.returncode is not None:
                    logger.warning(
                        f"[JarvisPrime] Process exited unexpectedly "
                        f"(code: {self._process.returncode})"
                    )
                    self._set_status(PrimeStatus.DEGRADED)

                    # Auto-recovery
                    if not self._stopping:
                        await self.restart()
                    continue

                # Perform adaptive health check
                healthy = await self.check_health()

                if not healthy:
                    # v10.8: Increase interval on failures (back off)
                    self._health_check_interval_multiplier = min(
                        4.0,  # Max 4x the base interval
                        self._health_check_interval_multiplier * 1.2
                    )

                    # v10.8: Skip degradation during startup grace
                    if self._is_in_startup_grace():
                        logger.debug(
                            f"[JarvisPrime] Ignoring failure during startup grace "
                            f"(failures: {self._health.consecutive_failures})"
                        )
                        continue

                    # v10.8: Progressive degradation based on configurable thresholds
                    failures = self._health.consecutive_failures

                    if failures >= self.config.restart_failure_threshold:
                        logger.error(
                            f"[JarvisPrime] Critical failure threshold reached "
                            f"({failures} failures, reason: {self._last_failure_reason}). "
                            f"Triggering restart..."
                        )
                        await self.restart()

                    elif failures >= self.config.degradation_critical_threshold:
                        if self._health.status != PrimeStatus.DEGRADED:
                            logger.warning(
                                f"[JarvisPrime] Critical degradation: {failures} consecutive failures "
                                f"(reason: {self._last_failure_reason}, "
                                f"timeout: {self._current_timeout:.1f}s)"
                            )
                            self._set_status(PrimeStatus.DEGRADED)

                    elif failures >= self.config.degradation_warning_threshold:
                        if self._health.status == PrimeStatus.RUNNING:
                            logger.info(
                                f"[JarvisPrime] Warning: {failures} consecutive failures "
                                f"(reason: {self._last_failure_reason}). "
                                f"Increasing timeout to {self._current_timeout:.1f}s"
                            )
                            # Stay RUNNING but log warning

                else:
                    # Healthy check
                    # v10.8: Require hysteresis for recovery from DEGRADED
                    if self._health.status == PrimeStatus.DEGRADED:
                        if self._consecutive_successes >= self.config.health_recovery_threshold:
                            logger.info(
                                f"[JarvisPrime] Recovered after {self._consecutive_successes} "
                                f"consecutive successful health checks"
                            )
                            self._set_status(PrimeStatus.RUNNING)
                        else:
                            logger.debug(
                                f"[JarvisPrime] Recovery in progress "
                                f"({self._consecutive_successes}/{self.config.health_recovery_threshold})"
                            )

                # Notify callbacks
                for callback in self._on_health_update:
                    try:
                        callback(self._health)
                    except Exception as e:
                        logger.debug(f"[JarvisPrime] Health callback error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[JarvisPrime] Health loop error: {e}")

    # =========================================================================
    # Process Management
    # =========================================================================

    def _validate_repo(self) -> bool:
        """Validate JARVIS-Prime repository exists."""
        repo_path = self.config.prime_repo_path

        if not repo_path.exists():
            logger.error(f"[JarvisPrime] Repository not found: {repo_path}")
            return False

        server_path = repo_path / "jarvis_prime" / "server.py"
        if not server_path.exists():
            logger.error(f"[JarvisPrime] server.py not found: {server_path}")
            return False

        return True

    # =========================================================================
    # v11.0: Intelligent Instance Adoption & Port Fallback
    # =========================================================================

    async def _try_adopt_existing_instance(self, port: int, pid: int) -> bool:
        """
        Attempt to adopt an existing JARVIS Prime instance on the given port.

        This is the ROOT CAUSE FIX for port conflicts: instead of killing a healthy
        instance, we adopt it and reuse it. This is more efficient (no model reload)
        and prevents unnecessary conflicts.

        Args:
            port: Port where the instance is running
            pid: PID of the process on that port

        Returns:
            True if instance was successfully adopted, False otherwise
        """
        if not self.config.adopt_existing_instances:
            logger.debug(f"[JarvisPrime] Instance adoption disabled by config")
            return False

        logger.info(
            f"[JarvisPrime] 🔍 Checking if PID {pid} on port {port} is an adoptable JARVIS Prime instance..."
        )

        try:
            # Step 1: Verify it's a JARVIS Prime instance via health endpoint
            health_info = await self._probe_instance_health(port)

            if health_info is None:
                logger.debug(f"[JarvisPrime] Instance on port {port} did not respond to health check")
                return False

            # Step 2: Check model compatibility if required
            if self.config.require_model_match_for_adoption:
                expected_model = self.config.initial_model
                actual_model = health_info.get("model", health_info.get("model_name"))

                if expected_model and actual_model:
                    # Compare just the filename/basename for flexibility
                    expected_name = Path(expected_model).name if expected_model else None
                    actual_name = Path(actual_model).name if actual_model else None

                    if expected_name != actual_name:
                        logger.warning(
                            f"[JarvisPrime] Model mismatch: expected '{expected_name}', "
                            f"found '{actual_name}'. Skipping adoption."
                        )
                        return False

            # Step 3: Create adoption wrapper
            logger.info(
                f"[JarvisPrime] ✅ Adopting healthy JARVIS Prime instance on port {port} (PID {pid})"
            )

            # Create a mock process wrapper for the adopted instance
            class AdoptedProcess:
                """Wrapper for an adopted external process."""

                def __init__(self, process_pid: int, process_port: int):
                    self.pid = process_pid
                    self.port = process_port
                    self.returncode = None
                    self._terminated = False

                def terminate(self):
                    """Terminate the adopted process gracefully."""
                    if self._terminated:
                        return
                    try:
                        os.kill(self.pid, signal.SIGTERM)
                        self._terminated = True
                    except (ProcessLookupError, PermissionError):
                        pass

                def kill(self):
                    """Force kill the adopted process."""
                    if self._terminated:
                        return
                    try:
                        os.kill(self.pid, signal.SIGKILL)
                        self._terminated = True
                    except (ProcessLookupError, PermissionError):
                        pass

                async def wait(self):
                    """Wait for process to exit."""
                    max_wait = 10
                    waited = 0
                    while waited < max_wait:
                        try:
                            os.kill(self.pid, 0)  # Check if process exists
                            await asyncio.sleep(0.1)
                            waited += 0.1
                        except ProcessLookupError:
                            self.returncode = 0
                            return
                        except PermissionError:
                            # Process exists but we can't signal it
                            await asyncio.sleep(0.1)
                            waited += 0.1
                    self.returncode = -1  # Timeout

            # Store adoption info
            self._process = AdoptedProcess(pid, port)
            self._adopted_instance = True
            self._effective_port = port
            self._adoption_info = {
                "pid": pid,
                "port": port,
                "health": health_info,
                "adopted_at": datetime.now().isoformat(),
                "original_owner": "external",
            }

            # Update health status
            self._health = PrimeHealth(
                status=PrimeStatus.RUNNING,
                pid=pid,
                model_loaded=health_info.get("model_loaded", True),
                model_name=health_info.get("model", health_info.get("model_name")),
                last_health_check=datetime.now(),
            )
            self._start_time = datetime.now()
            self._started = True

            # Save ownership info
            await self._save_instance_ownership(port, pid, adopted=True)

            logger.info(
                f"[JarvisPrime] 🎉 Successfully adopted JARVIS Prime instance: "
                f"PID={pid}, Port={port}, Model={health_info.get('model', 'unknown')}"
            )

            return True

        except Exception as e:
            logger.debug(f"[JarvisPrime] Instance adoption failed: {e}")
            return False

    async def _probe_instance_health(self, port: int) -> Optional[Dict[str, Any]]:
        """
        Probe an instance's health endpoint to verify it's a JARVIS Prime server.

        Args:
            port: Port to probe

        Returns:
            Health info dict if healthy JARVIS Prime, None otherwise
        """
        health_url = f"http://{self.config.host}:{port}/health"
        timeout = aiohttp.ClientTimeout(total=self.config.adoption_health_timeout_seconds)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(health_url) as resp:
                    if resp.status == 200:
                        data = await resp.json()

                        # Verify it looks like a JARVIS Prime response
                        # JARVIS Prime health endpoint typically returns:
                        # {"status": "ok", "model": "...", "model_loaded": true, ...}
                        if isinstance(data, dict):
                            status = data.get("status", "").lower()
                            if status in ("ok", "healthy", "running"):
                                logger.debug(
                                    f"[JarvisPrime] Health probe on port {port} succeeded: {data}"
                                )
                                return data

                        logger.debug(
                            f"[JarvisPrime] Health probe response not recognized as JARVIS Prime: {data}"
                        )
                        return None

                    logger.debug(f"[JarvisPrime] Health probe returned status {resp.status}")
                    return None

        except aiohttp.ClientConnectorError:
            logger.debug(f"[JarvisPrime] No HTTP server responding on port {port}")
            return None
        except asyncio.TimeoutError:
            logger.debug(f"[JarvisPrime] Health probe timed out on port {port}")
            return None
        except Exception as e:
            logger.debug(f"[JarvisPrime] Health probe failed: {e}")
            return None

    async def _find_available_fallback_port(self) -> Optional[int]:
        """
        Find an available port in the fallback range.

        Returns:
            Available port number, or None if no port available
        """
        if not self.config.enable_port_fallback:
            return None

        logger.info(
            f"[JarvisPrime] 🔍 Searching for available port in range "
            f"{self.config.fallback_port_range_start}-{self.config.fallback_port_range_end}..."
        )

        import socket

        for port in range(
            self.config.fallback_port_range_start,
            self.config.fallback_port_range_end + 1
        ):
            # Check if port is in use
            pid = await self._get_pid_on_port(port)
            if pid is None:
                # Double-check with socket bind
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind((self.config.host, port))
                    sock.close()
                    logger.info(f"[JarvisPrime] ✅ Found available fallback port: {port}")
                    return port
                except OSError:
                    continue

        logger.warning(f"[JarvisPrime] No available ports in fallback range")
        return None

    async def _save_instance_ownership(
        self,
        port: int,
        pid: int,
        adopted: bool = False
    ):
        """
        Save instance ownership information for cross-session coordination.

        Args:
            port: Port the instance is running on
            pid: Process ID
            adopted: Whether this is an adopted instance
        """
        import json

        ownership_file = self.config.instance_ownership_file

        try:
            ownership_file.parent.mkdir(parents=True, exist_ok=True)

            ownership_data = {
                "port": port,
                "pid": pid,
                "adopted": adopted,
                "supervisor_pid": os.getpid(),
                "started_at": datetime.now().isoformat(),
                "host": self.config.host,
            }

            ownership_file.write_text(json.dumps(ownership_data, indent=2))
            logger.debug(f"[JarvisPrime] Saved ownership info to {ownership_file}")

        except Exception as e:
            logger.debug(f"[JarvisPrime] Could not save ownership info: {e}")

    async def _load_instance_ownership(self) -> Optional[Dict[str, Any]]:
        """
        Load instance ownership information from previous session.

        Returns:
            Ownership data dict if available, None otherwise
        """
        import json

        ownership_file = self.config.instance_ownership_file

        try:
            if ownership_file.exists():
                data = json.loads(ownership_file.read_text())
                logger.debug(f"[JarvisPrime] Loaded ownership info: {data}")
                return data
        except Exception as e:
            logger.debug(f"[JarvisPrime] Could not load ownership info: {e}")

        return None

    async def _clear_instance_ownership(self):
        """Clear instance ownership information."""
        try:
            if self.config.instance_ownership_file.exists():
                self.config.instance_ownership_file.unlink()
        except Exception:
            pass

    # =========================================================================
    # Port Management (v10.4 → v11.0) - Process-hierarchy aware with adoption
    # =========================================================================

    async def _ensure_port_available(self) -> bool:
        """
        Intelligent port coordination system - ensures port is available before starting.

        Strategy (v11.1 - Enhanced with IntelligentPortManager):
        1. Use IntelligentPortManager for fast, parallel port coordination
        2. Classifies processes BEFORE attempting cleanup (no wasted time)
        3. Runs cleanup strategies in PARALLEL with first-success wins
        4. Switches to fallback port IMMEDIATELY when process is unkillable
        5. Falls back to legacy implementation if IntelligentPortManager unavailable

        Key improvements over v11.0:
        - 10 second max cleanup instead of 21+ seconds
        - Parallel signal cascade instead of sequential
        - Early detection of unkillable processes
        - Immediate fallback without waiting for all cleanup to fail

        Raises:
            RuntimeError: If port cannot be freed within timeout

        Returns:
            bool: True if reused existing process, False if port is available for new process
        """
        # v11.1: Use IntelligentPortManager when available
        if self._port_manager is not None:
            return await self._ensure_port_available_v11_1()

        # Fallback to legacy implementation
        return await self._ensure_port_available_legacy()

    async def _ensure_port_available_v11_1(self) -> bool:
        """
        v11.1: Use IntelligentPortManager for fast, parallel port coordination.

        This is ~2x faster than the legacy implementation because:
        1. Process classification happens in < 500ms
        2. Cleanup strategies run in parallel
        3. Fallback is used immediately for unkillable processes
        """
        try:
            port, adopted_info = await self._port_manager.ensure_port_available()

            # Update effective port
            self._effective_port = port
            if port != self._original_port:
                self.config.port = port
                logger.info(
                    f"[JarvisPrime] v11.1 Using port {port} instead of {self._original_port}"
                )

            # Handle adoption
            if adopted_info is not None and adopted_info.should_adopt:
                logger.info(
                    f"[JarvisPrime] v11.1 Adopting existing JARVIS Prime "
                    f"(PID {adopted_info.pid} on port {port})"
                )

                # Create adoption wrapper for the existing process
                self._adopted_instance = True
                self._adoption_info = {
                    "pid": adopted_info.pid,
                    "port": port,
                    "adopted_at": datetime.now().isoformat(),
                    "process_type": adopted_info.process_type.value,
                }

                # Create mock process wrapper
                class AdoptedProcessWrapper:
                    def __init__(self, process_pid: int, process_port: int):
                        self.pid = process_pid
                        self.port = process_port
                        self.returncode = None
                        self._terminated = False

                    def terminate(self):
                        if self._terminated:
                            return
                        try:
                            os.kill(self.pid, signal.SIGTERM)
                            self._terminated = True
                        except (ProcessLookupError, PermissionError):
                            pass

                    def kill(self):
                        if self._terminated:
                            return
                        try:
                            os.kill(self.pid, signal.SIGKILL)
                            self._terminated = True
                        except (ProcessLookupError, PermissionError):
                            pass

                    async def wait(self):
                        max_wait = 10
                        waited = 0
                        while waited < max_wait:
                            try:
                                os.kill(self.pid, 0)
                                await asyncio.sleep(0.1)
                                waited += 0.1
                            except ProcessLookupError:
                                self.returncode = 0
                                return
                            except PermissionError:
                                await asyncio.sleep(0.1)
                                waited += 0.1
                        self.returncode = -1

                self._process = AdoptedProcessWrapper(adopted_info.pid, port)

                # Update health status
                self._health = PrimeHealth(
                    status=PrimeStatus.RUNNING,
                    pid=adopted_info.pid,
                    model_loaded=adopted_info.is_healthy,
                    last_health_check=datetime.now(),
                )
                self._start_time = datetime.now()
                self._started = True

                # Save ownership info
                await self._save_instance_ownership(port, adopted_info.pid, adopted=True)

                return True  # Adopted existing instance

            # Port is available (either freed or fallback)
            return False

        except RuntimeError as e:
            # Port manager couldn't find any available port
            logger.error(f"[JarvisPrime] v11.1 Port management failed: {e}")
            self._health.error_message = str(e)
            raise

    async def _ensure_port_available_legacy(self) -> bool:
        """
        Legacy port coordination (v11.0) - used as fallback when IntelligentPortManager unavailable.
        """
        port = self.config.port
        self._effective_port = port  # Track effective port
        max_wait_time = 45.0  # Increased from 30s to handle stubborn processes
        start_time = time.time()

        logger.info(f"[JarvisPrime] Ensuring port {port} is available for startup (legacy mode)...")

        # Initial check
        pid = await self._get_pid_on_port(port)
        if pid is None:
            logger.debug(f"[JarvisPrime] Port {port} is available")
            return False  # Port available, no reuse

        # CRITICAL: Check if this is our own stored process
        if self._process and self._process.pid == pid:
            logger.debug(f"[JarvisPrime] Port {port} is used by our own managed process (PID {pid})")
            return True  # Already have this process, reusing

        # =========================================================================
        # v11.0: TRY INSTANCE ADOPTION FIRST (ROOT CAUSE FIX)
        # =========================================================================
        # Before trying to kill anything, check if the existing instance is a
        # healthy JARVIS Prime that we can simply adopt. This is MORE efficient
        # than killing and restarting!

        if self.config.adopt_existing_instances:
            logger.info(
                f"[JarvisPrime] 🔄 Port {port} is in use by PID {pid}. "
                f"Attempting to adopt existing instance..."
            )

            if await self._try_adopt_existing_instance(port, pid):
                logger.info(
                    f"[JarvisPrime] ✅ Successfully adopted existing JARVIS Prime instance! "
                    f"Skipping spawn - using PID {pid} on port {port}"
                )
                return True  # Adopted existing instance, no need to spawn

            logger.info(
                f"[JarvisPrime] Instance on port {port} is not adoptable. "
                f"Proceeding with cleanup..."
            )

        # v10.7: ENHANCED FIX - Check if port is used by current supervisor process
        # This happens during restart scenarios where supervisor is restarting Prime
        current_pid = os.getpid()
        if pid == current_pid:
            logger.info(
                f"[JarvisPrime] Port {port} is bound by current supervisor process (PID {pid}). "
                f"This is a restart scenario - checking for existing Prime subprocess..."
            )

            # Check if we have an existing JARVIS Prime subprocess we can reuse
            if PSUTIL_AVAILABLE:
                try:
                    current_process = psutil.Process(current_pid)
                    children = current_process.children(recursive=True)

                    for child in children:
                        try:
                            # Look for python processes with "jarvis_prime" in command (JARVIS Prime signature)
                            cmdline = child.cmdline()
                            cmdline_str = ' '.join(cmdline)

                            if ('jarvis_prime' in cmdline_str.lower() or
                                'jarvis-prime' in cmdline_str.lower() or
                                f'--port {port}' in cmdline_str or
                                f'--port={port}' in cmdline_str):

                                logger.info(
                                    f"[JarvisPrime] Found existing Prime subprocess (PID {child.pid}). "
                                    f"Reusing instead of starting new instance."
                                )

                                # Create a mock subprocess object that wraps the psutil.Process
                                class MockProcess:
                                    def __init__(self, proc):
                                        self.pid = proc.pid
                                        self.returncode = None
                                        self._proc = proc

                                    def terminate(self):
                                        try:
                                            self._proc.terminate()
                                        except Exception:
                                            pass

                                    def kill(self):
                                        try:
                                            self._proc.kill()
                                        except Exception:
                                            pass

                                    async def wait(self):
                                        # Wait for process to exit
                                        max_wait = 10
                                        waited = 0
                                        while waited < max_wait:
                                            try:
                                                if not self._proc.is_running():
                                                    self.returncode = 0
                                                    return
                                            except Exception:
                                                self.returncode = 0
                                                return
                                            await asyncio.sleep(0.1)
                                            waited += 0.1
                                        self.returncode = 0

                                # ROOT CAUSE FIX: Store the existing process and return True to skip spawn!
                                self._process = MockProcess(child)
                                logger.info(f"[JarvisPrime] ✅ Reusing existing subprocess PID {child.pid} on port {port}")
                                return True  # CRITICAL: Tell caller we reused, don't spawn new process!

                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue

                except Exception as e:
                    logger.debug(f"[JarvisPrime] Could not check for existing subprocess: {e}")

            # ROOT CAUSE FIX: No existing subprocess found - the port binding is stale
            # This is a CRITICAL failure case that needs aggressive cleanup!
            logger.warning(
                f"[JarvisPrime] No existing Prime subprocess found, but port {port} is bound. "
                f"This indicates a zombie/orphaned process. Attempting aggressive cleanup..."
            )

            # AGGRESSIVE FIX: Use fuser to find and kill ALL processes on the port
            import subprocess as sp
            try:
                # Find PIDs using fuser (more reliable than lsof for cleanup)
                result = sp.run(
                    ["lsof", "-ti", f":{port}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if result.stdout.strip():
                    pids = result.stdout.strip().split('\n')
                    logger.warning(f"[JarvisPrime] Found {len(pids)} processes on port {port}: {pids}")

                    for pid_str in pids:
                        try:
                            kill_pid = int(pid_str.strip())
                            logger.warning(f"[JarvisPrime] Force killing PID {kill_pid} on port {port}")
                            os.kill(kill_pid, signal.SIGKILL)
                        except (ValueError, ProcessLookupError, PermissionError) as e:
                            logger.debug(f"[JarvisPrime] Could not kill PID {pid_str}: {e}")

                    # Wait for processes to die
                    await asyncio.sleep(2.0)

                # Verify port is now free
                verify_pid = await self._get_pid_on_port(port)
                if verify_pid is None:
                    logger.info(f"[JarvisPrime] Port {port} successfully freed via aggressive cleanup")
                    return False  # Port freed, ready for new process
                else:
                    # CRITICAL: Port STILL not free - this is a hard failure
                    logger.error(
                        f"[JarvisPrime] CRITICAL: Port {port} still in use (PID {verify_pid}) after aggressive cleanup! "
                        f"Cannot proceed safely."
                    )
                    raise RuntimeError(
                        f"Port {port} cannot be freed (PID {verify_pid} won't die). "
                        f"Manual intervention required: sudo kill -9 {verify_pid}"
                    )

            except FileNotFoundError:
                logger.warning("[JarvisPrime] lsof not available - trying manual socket release")
                # Fallback: Try SO_REUSEADDR
                try:
                    import socket
                    temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        temp_sock.bind(('', port))
                        logger.debug(f"[JarvisPrime] Bound temporary socket to port {port}")
                    finally:
                        temp_sock.close()
                    await asyncio.sleep(0.5)

                    verify_pid = await self._get_pid_on_port(port)
                    if verify_pid is None:
                        logger.info(f"[JarvisPrime] Port {port} released via socket manipulation")
                        return False  # Port freed
                except Exception as e:
                    logger.error(f"[JarvisPrime] Socket release failed: {e}")
                    raise RuntimeError(f"Cannot free port {port} - no cleanup tools available")

            except Exception as e:
                logger.error(f"[JarvisPrime] Port cleanup error: {e}")
                raise RuntimeError(f"Port {port} cleanup failed: {e}")

        # v10.5: IMMEDIATE zombie/defunct process cleanup
        # Zombies can't be killed normally, so detect and reap them first
        if PSUTIL_AVAILABLE:
            try:
                proc = psutil.Process(pid)
                if proc.status() == psutil.STATUS_ZOMBIE:
                    logger.warning(f"[JarvisPrime] PID {pid} is zombie - attempting to reap...")
                    try:
                        # Zombies are already dead, just need to reap them
                        os.waitpid(pid, os.WNOHANG)
                        logger.info(f"[JarvisPrime] Reaped zombie process {pid}")
                        # Wait briefly for port to free
                        await asyncio.sleep(0.5)
                        pid_check = await self._get_pid_on_port(port)
                        if pid_check is None:
                            logger.info(f"[JarvisPrime] Port {port} freed after reaping zombie")
                            return False  # Port freed
                    except (OSError, ChildProcessError) as e:
                        logger.debug(f"[JarvisPrime] Zombie reap failed (may not be our child): {e}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Determine relationship to the process on the port
        is_related = await self._is_ancestor_process(pid)
        process_info = await self._get_process_info(pid)

        if is_related:
            logger.warning(
                f"[JarvisPrime] Port {port} is used by related process (PID {pid}). "
                f"Process: {process_info.get('name', 'unknown')}"
            )
            logger.info(
                f"[JarvisPrime] This appears to be an old JARVIS Prime instance or "
                f"related supervisor process. Attempting coordinated shutdown..."
            )
        else:
            logger.warning(
                f"[JarvisPrime] Port {port} is used by unrelated process (PID {pid}). "
                f"Process: {process_info.get('name', 'unknown')}. Attempting cleanup..."
            )

        # Strategy 1: Try graceful HTTP shutdown (works for JARVIS Prime instances)
        shutdown_success = await self._try_graceful_http_shutdown(port, pid)
        if shutdown_success:
            # Wait for port to free up with verification
            if await self._wait_for_port_free(port, max_wait=10.0):
                logger.info(f"[JarvisPrime] Port {port} freed via graceful shutdown")
                return False  # Port freed

        # Strategy 2: Try SIGTERM (polite kill signal)
        if not is_related:  # Only if not related to us
            logger.info(f"[JarvisPrime] Attempting SIGTERM on PID {pid}...")
            try:
                os.kill(pid, signal.SIGTERM)
                if await self._wait_for_port_free(port, max_wait=5.0):
                    logger.info(f"[JarvisPrime] Port {port} freed via SIGTERM")
                    return False  # Port freed
            except (ProcessLookupError, PermissionError) as e:
                logger.debug(f"[JarvisPrime] SIGTERM failed: {e}")

        # Strategy 3: If related process, try coordinated restart
        if is_related:
            logger.warning(
                f"[JarvisPrime] Related process (PID {pid}) won't release port {port}. "
                f"This may indicate a stuck old instance. Waiting longer..."
            )
            # Wait with exponential backoff for related processes
            if await self._wait_for_port_free(port, max_wait=20.0, exponential=True):
                logger.info(f"[JarvisPrime] Port {port} eventually freed by related process")
                return False  # Port freed

            # Last resort for related processes: Check if it's truly orphaned
            if await self._is_orphaned_instance(pid, process_info):
                logger.warning(
                    f"[JarvisPrime] PID {pid} appears to be an orphaned JARVIS Prime instance. "
                    f"Attempting force cleanup..."
                )
                try:
                    os.kill(pid, signal.SIGKILL)
                    if await self._wait_for_port_free(port, max_wait=3.0):
                        logger.info(f"[JarvisPrime] Port {port} freed by removing orphaned instance")
                        return False  # Port freed
                except Exception as e:
                    logger.error(f"[JarvisPrime] Failed to kill orphaned instance: {e}")

        # Strategy 4: Force kill unrelated process (SIGKILL)
        if not is_related:
            logger.warning(f"[JarvisPrime] Force killing unrelated PID {pid} with SIGKILL...")
            try:
                os.kill(pid, signal.SIGKILL)
                if await self._wait_for_port_free(port, max_wait=3.0):
                    logger.info(f"[JarvisPrime] Port {port} freed via SIGKILL")
                    return False  # Port freed
            except (ProcessLookupError, PermissionError) as e:
                logger.error(f"[JarvisPrime] SIGKILL failed: {e}")

        # Final verification
        elapsed = time.time() - start_time
        pid = await self._get_pid_on_port(port)

        if pid is None:
            logger.info(f"[JarvisPrime] Port {port} is now available (took {elapsed:.1f}s)")
            return False  # Port freed

        # =========================================================================
        # v11.0: PORT FALLBACK - Try alternative port if primary is occupied
        # =========================================================================
        # Before giving up completely, try to find an alternative port

        if self.config.enable_port_fallback:
            logger.warning(
                f"[JarvisPrime] ⚠️ Primary port {port} unavailable after {elapsed:.1f}s. "
                f"Searching for fallback port..."
            )

            fallback_port = await self._find_available_fallback_port()

            if fallback_port is not None:
                logger.info(
                    f"[JarvisPrime] 🔄 Switching to fallback port {fallback_port} "
                    f"(primary port {port} occupied by PID {pid})"
                )

                # Update effective port
                self._effective_port = fallback_port

                # Also update the config port for this session
                # (Note: this is a runtime override, not persisted)
                self.config.port = fallback_port

                logger.info(
                    f"[JarvisPrime] ✅ Port fallback successful: will use port {fallback_port} "
                    f"instead of {self._original_port}"
                )
                return False  # Port available (fallback), proceed with spawn

            logger.error(
                f"[JarvisPrime] ❌ No fallback ports available in range "
                f"{self.config.fallback_port_range_start}-{self.config.fallback_port_range_end}"
            )

        # CRITICAL: Port is STILL occupied AND no fallback available - cannot proceed!
        error_msg = (
            f"Port {port} is still in use by PID {pid} after {elapsed:.1f}s of cleanup attempts. "
            f"Process: {process_info.get('name', 'unknown')}. "
            f"Cannot start JARVIS Prime - port is not available. "
            f"Manual intervention required: kill PID {pid} or use different port."
        )
        logger.error(f"[JarvisPrime] {error_msg}")

        # Raise exception to prevent startup with occupied port
        raise RuntimeError(error_msg)

    async def _is_ancestor_process(self, pid: int) -> bool:
        """
        Intelligent process relationship checker - determines if killing a PID would harm us.

        Checks for:
        1. Same PID (ourselves)
        2. Parent/ancestor processes (killing them would kill us via signal propagation)
        3. Child processes we spawned (our responsibility to manage)
        4. Sibling processes in same process group (might be managed by same supervisor)

        This is a comprehensive safety check using psutil when available, falling back
        to ps command parsing for maximum compatibility.

        Args:
            pid: Process ID to check

        Returns:
            True if killing this PID is unsafe (would harm us or related processes)
            False if PID is unrelated and safe to kill
        """
        current_pid = os.getpid()

        # CRITICAL FIX: If checking our own PID, return True (unsafe to kill ourselves!)
        if pid == current_pid:
            logger.warning(
                f"[JarvisPrime] Port is in use by current process (PID {pid}). "
                f"This indicates a restart scenario - cannot kill ourselves!"
            )
            return True  # ✅ Never kill our own PID

        # Use psutil for comprehensive process tree analysis (preferred method)
        if PSUTIL_AVAILABLE:
            try:
                current_process = psutil.Process(current_pid)
                target_process = psutil.Process(pid)

                # Check 1: Is target our parent/ancestor?
                ancestors = []
                parent = current_process.parent()
                depth = 0
                while parent and depth < 20:  # Prevent infinite loops
                    ancestors.append(parent.pid)
                    if parent.pid == pid:
                        logger.debug(
                            f"[JarvisPrime] PID {pid} is ancestor at depth {depth+1} - unsafe to kill"
                        )
                        return True
                    parent = parent.parent()
                    depth += 1

                # Check 2: Is target our child/descendant?
                children = current_process.children(recursive=True)
                child_pids = [child.pid for child in children]
                if pid in child_pids:
                    logger.debug(
                        f"[JarvisPrime] PID {pid} is our child process - unsafe to kill "
                        f"(should be managed via proper cleanup)"
                    )
                    return True

                # Check 3: Is target in our process group?
                try:
                    current_pgid = os.getpgid(current_pid)
                    target_pgid = os.getpgid(pid)
                    if current_pgid == target_pgid and current_pgid != 0:
                        logger.debug(
                            f"[JarvisPrime] PID {pid} is in same process group {current_pgid} - "
                            f"potentially unsafe (might be sibling managed by same supervisor)"
                        )
                        # For process group members, check if they share a parent
                        if current_process.parent() and target_process.parent():
                            if current_process.parent().pid == target_process.parent().pid:
                                logger.warning(
                                    f"[JarvisPrime] PID {pid} is sibling process (same parent) - "
                                    f"letting supervisor handle cleanup"
                                )
                                return True
                except (OSError, psutil.AccessDenied):
                    pass  # Can't determine process group - continue to other checks

                # Check 4: Is target our parent's other child (sibling)?
                if current_process.parent():
                    siblings = current_process.parent().children()
                    sibling_pids = [sib.pid for sib in siblings if sib.pid != current_pid]
                    if pid in sibling_pids:
                        logger.debug(
                            f"[JarvisPrime] PID {pid} is sibling process - "
                            f"supervisor should handle coordination"
                        )
                        return True

                # All checks passed - target is unrelated and safe to kill
                logger.debug(f"[JarvisPrime] PID {pid} is unrelated process - safe to kill")
                return False

            except psutil.NoSuchProcess:
                logger.debug(f"[JarvisPrime] PID {pid} no longer exists")
                return False  # Process is gone, safe to "kill" (no-op)
            except psutil.AccessDenied:
                logger.warning(
                    f"[JarvisPrime] Access denied when checking PID {pid} - "
                    f"assuming unsafe to kill (might be system process)"
                )
                return True  # Can't verify, be safe
            except Exception as e:
                logger.warning(
                    f"[JarvisPrime] Error checking process relationship with psutil: {e}",
                    exc_info=True
                )
                # Fall through to ps-based fallback

        # Fallback: Use ps command for basic ancestry checking
        # (when psutil unavailable or failed)
        try:
            logger.debug("[JarvisPrime] Using ps fallback for process ancestry check")

            # Walk up the process tree using ps
            proc = await asyncio.create_subprocess_exec(
                "ps", "-o", "ppid=", "-p", str(current_pid),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            parent_pid = int(stdout.decode().strip())

            # Check up to 20 levels of ancestry
            checked_pids = set()
            while parent_pid > 1 and parent_pid not in checked_pids:
                if parent_pid == pid:
                    logger.debug(f"[JarvisPrime] PID {pid} is ancestor (ps fallback)")
                    return True

                checked_pids.add(parent_pid)

                # Get next parent
                proc = await asyncio.create_subprocess_exec(
                    "ps", "-o", "ppid=", "-p", str(parent_pid),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)

                try:
                    parent_pid = int(stdout.decode().strip())
                except (ValueError, AttributeError):
                    break

                if len(checked_pids) > 20:
                    break  # Prevent infinite loops

            # ps-based check found no ancestry relationship
            logger.debug(f"[JarvisPrime] PID {pid} not in ancestry chain (ps fallback)")
            return False

        except Exception as e:
            logger.warning(
                f"[JarvisPrime] Error checking process ancestry with ps: {e}",
                exc_info=True
            )
            # If we can't determine relationship, be safe and return True
            # This prevents accidentally killing processes we can't verify
            logger.warning(
                f"[JarvisPrime] Cannot verify PID {pid} safety - "
                f"assuming unsafe to kill as precaution"
            )
            return True

    async def _get_pid_on_port(self, port: int) -> Optional[int]:
        """
        Get the PID of the process using a specific port.

        Args:
            port: Port number to check

        Returns:
            PID if port is in use, None otherwise
        """
        try:
            # Use lsof to find process on port (macOS/Linux)
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-t", "-i", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            if stdout:
                # May return multiple PIDs, get the first one
                pids = stdout.decode().strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])
            return None

        except asyncio.TimeoutError:
            logger.debug(f"[JarvisPrime] lsof timed out checking port {port}")
            return None
        except FileNotFoundError:
            # lsof not available, try netstat or ss
            return await self._get_pid_on_port_fallback(port)
        except Exception as e:
            logger.debug(f"[JarvisPrime] Error checking port {port}: {e}")
            return None

    async def _get_pid_on_port_fallback(self, port: int) -> Optional[int]:
        """Fallback method to check port using netstat."""
        try:
            # Try netstat for Linux
            proc = await asyncio.create_subprocess_exec(
                "netstat", "-tlnp",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)

            for line in stdout.decode().split("\n"):
                if f":{port}" in line and "LISTEN" in line:
                    # Extract PID from netstat output
                    parts = line.split()
                    if len(parts) >= 7:
                        pid_prog = parts[6]
                        if "/" in pid_prog:
                            return int(pid_prog.split("/")[0])
            return None

        except Exception:
            # Can't determine - assume port is available
            return None

    async def _try_graceful_http_shutdown(self, port: int, pid: int) -> bool:
        """
        Attempt graceful HTTP shutdown of JARVIS Prime instance on given port.

        Args:
            port: Port number
            pid: Process ID (for logging)

        Returns:
            True if shutdown request succeeded, False otherwise
        """
        try:
            logger.info(f"[JarvisPrime] Attempting graceful HTTP shutdown of PID {pid} on port {port}...")
            async with aiohttp.ClientSession() as session:
                shutdown_url = f"http://{self.config.host}:{port}/admin/shutdown"
                async with session.post(shutdown_url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        logger.info(f"[JarvisPrime] Graceful shutdown request accepted by instance on port {port}")
                        return True
                    else:
                        logger.debug(f"[JarvisPrime] Shutdown request returned status {resp.status}")
                        return False
        except aiohttp.ClientConnectorError:
            logger.debug(f"[JarvisPrime] No HTTP server responding on port {port}")
            return False
        except asyncio.TimeoutError:
            logger.debug(f"[JarvisPrime] Shutdown request timed out on port {port}")
            return False
        except Exception as e:
            logger.debug(f"[JarvisPrime] Graceful shutdown failed: {e}")
            return False

    async def _wait_for_port_free(
        self,
        port: int,
        max_wait: float = 10.0,
        exponential: bool = False
    ) -> bool:
        """
        Wait for port to become available with intelligent retry logic.

        Args:
            port: Port number to monitor
            max_wait: Maximum time to wait in seconds
            exponential: Use exponential backoff (for related processes)

        Returns:
            True if port became free, False if timeout
        """
        start_time = time.time()
        attempt = 0

        while (time.time() - start_time) < max_wait:
            # Check if port is free
            pid = await self._get_pid_on_port(port)
            if pid is None:
                elapsed = time.time() - start_time
                logger.debug(f"[JarvisPrime] Port {port} became available after {elapsed:.1f}s")
                return True

            # Calculate wait time
            if exponential:
                # Exponential backoff: 0.5s, 1s, 2s, 4s, 8s, ...
                wait_time = min(0.5 * (2 ** attempt), 8.0)
            else:
                # Linear backoff: 0.5s each time
                wait_time = 0.5

            # Don't wait beyond max_wait
            remaining = max_wait - (time.time() - start_time)
            wait_time = min(wait_time, remaining)

            if wait_time > 0:
                logger.debug(
                    f"[JarvisPrime] Port {port} still in use by PID {pid}, "
                    f"waiting {wait_time:.1f}s (attempt {attempt + 1})..."
                )
                await asyncio.sleep(wait_time)

            attempt += 1

        # Timeout reached
        pid = await self._get_pid_on_port(port)
        logger.warning(
            f"[JarvisPrime] Timeout waiting for port {port} to free "
            f"(still in use by PID {pid} after {max_wait:.1f}s)"
        )
        return False

    async def _get_process_info(self, pid: int) -> Dict[str, Any]:
        """
        Get information about a process.

        Args:
            pid: Process ID

        Returns:
            Dictionary with process info (name, cmdline, etc.)
        """
        info = {
            "pid": pid,
            "name": "unknown",
            "cmdline": "unknown",
            "is_jarvis_prime": False,
        }

        if PSUTIL_AVAILABLE:
            try:
                proc = psutil.Process(pid)
                info["name"] = proc.name()
                info["cmdline"] = " ".join(proc.cmdline())

                # Check if it's a JARVIS Prime instance
                cmdline_lower = info["cmdline"].lower()
                if "jarvis" in cmdline_lower and "prime" in cmdline_lower:
                    info["is_jarvis_prime"] = True
                elif "jarvis-prime" in cmdline_lower:
                    info["is_jarvis_prime"] = True
                elif "8002" in cmdline_lower:  # Default JARVIS Prime port
                    info["is_jarvis_prime"] = True

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        else:
            # Fallback using ps
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ps", "-p", str(pid), "-o", "comm=",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
                if stdout:
                    info["name"] = stdout.decode().strip()
                    if "python" in info["name"].lower() or "jarvis" in info["name"].lower():
                        info["is_jarvis_prime"] = True
            except Exception:
                pass

        return info

    async def _is_orphaned_instance(self, pid: int, process_info: Dict[str, Any]) -> bool:
        """
        Determine if a process is an orphaned JARVIS Prime instance.

        An instance is considered orphaned if:
        1. It's a JARVIS Prime process (based on cmdline)
        2. It's been running for a while (>30s) without supervisor
        3. It's not responding to health checks
        4. It's in a zombie/stuck state

        Args:
            pid: Process ID
            process_info: Process information dict

        Returns:
            True if process appears to be orphaned JARVIS Prime instance
        """
        # Must be identified as JARVIS Prime
        if not process_info.get("is_jarvis_prime", False):
            return False

        logger.debug(f"[JarvisPrime] Checking if PID {pid} is orphaned JARVIS Prime instance...")

        # Check 1: Is it responding to health checks?
        try:
            async with aiohttp.ClientSession() as session:
                health_url = f"http://{self.config.host}:{self.config.port}/health"
                async with session.get(health_url, timeout=aiohttp.ClientTimeout(total=2)) as resp:
                    if resp.status == 200:
                        logger.debug(f"[JarvisPrime] Instance on port {self.config.port} is healthy - not orphaned")
                        return False  # It's healthy, not orphaned
        except Exception:
            logger.debug(f"[JarvisPrime] Instance on port {self.config.port} not responding to health check")

        # Check 2: Is it in zombie/defunct state?
        if PSUTIL_AVAILABLE:
            try:
                proc = psutil.Process(pid)
                status = proc.status()
                if status == psutil.STATUS_ZOMBIE:
                    logger.info(f"[JarvisPrime] PID {pid} is zombie process - definitely orphaned")
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # Check 3: Is the parent process missing/dead?
        if PSUTIL_AVAILABLE:
            try:
                proc = psutil.Process(pid)
                parent = proc.parent()
                if parent is None or parent.pid == 1:  # Parent is init/launchd = orphaned
                    logger.info(
                        f"[JarvisPrime] PID {pid} has no parent or parent is init - likely orphaned"
                    )
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        # If we can't determine health status and it's not responding, consider it orphaned
        logger.info(
            f"[JarvisPrime] PID {pid} appears to be JARVIS Prime but not responding - "
            f"treating as potentially orphaned"
        )
        return True

    async def _spawn_process(self) -> bool:
        """Spawn the JARVIS-Prime subprocess (or Docker container)."""
        if self.config.use_docker:
            return await self._spawn_docker_container()
        else:
            return await self._spawn_subprocess()

    async def _spawn_subprocess(self) -> bool:
        """Spawn the JARVIS-Prime subprocess."""
        try:
            # Build command
            cmd = [
                sys.executable,
                "-m", "jarvis_prime.server",
                "--host", self.config.host,
                "--port", str(self.config.port),
                "--models-dir", self.config.models_dir,
            ]

            if self.config.initial_model:
                cmd.extend(["--initial-model", self.config.initial_model])

            if self.config.debug_mode:
                cmd.append("--debug")

            # Set environment
            env = os.environ.copy()
            env["PYTHONPATH"] = str(self.config.prime_repo_path)

            logger.info(f"[JarvisPrime] Spawning process: {' '.join(cmd)}")

            # Spawn process
            self._process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.config.prime_repo_path),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Start log reader tasks (non-blocking)
            asyncio.create_task(self._read_stdout())
            asyncio.create_task(self._read_stderr())

            logger.info(f"[JarvisPrime] Process spawned (PID: {self._process.pid})")
            return True

        except Exception as e:
            logger.error(f"[JarvisPrime] Failed to spawn process: {e}")
            self._health.error_message = str(e)
            return False

    async def _spawn_docker_container(self) -> bool:
        """Spawn JARVIS-Prime as a Docker container."""
        try:
            # First, stop any existing container with the same name
            await self._stop_docker_container()

            # Build docker run command
            cmd = [
                "docker", "run",
                "-d",  # Detached mode
                "--name", self.config.docker_container_name,
                "-p", f"{self.config.port}:8000",
                "--memory", self.config.docker_memory_limit,
                "--cpus", self.config.docker_cpus,
                "-e", f"JARVIS_PRIME_HOST=0.0.0.0",
                "-e", f"JARVIS_PRIME_PORT=8000",
                "-e", f"LOG_LEVEL={'DEBUG' if self.config.debug_mode else 'INFO'}",
            ]

            # Add volume mounts
            models_dir = self.config.prime_repo_path / "models"
            telemetry_dir = self.config.prime_repo_path / "telemetry"

            cmd.extend(["-v", f"{models_dir}:/app/models"])
            cmd.extend(["-v", f"{telemetry_dir}:/app/telemetry"])

            # Add custom volume mappings
            for host_path, container_path in self.config.docker_volumes.items():
                cmd.extend(["-v", f"{host_path}:{container_path}"])

            # Add reactor-core watch directory if configured
            reactor_core_dir = os.getenv("REACTOR_CORE_OUTPUT")
            if reactor_core_dir:
                cmd.extend(["-v", f"{reactor_core_dir}:/app/reactor-core-output:ro"])

            # Add image name
            cmd.append(self.config.docker_image)

            logger.info(f"[JarvisPrime] Starting Docker container: {' '.join(cmd)}")

            # Run docker command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                error_msg = stderr.decode().strip()
                logger.error(f"[JarvisPrime] Docker start failed: {error_msg}")
                self._health.error_message = error_msg
                return False

            container_id = stdout.decode().strip()[:12]
            logger.info(f"[JarvisPrime] Docker container started: {container_id}")

            # Store container ID for later use
            self._docker_container_id = container_id

            # Start log streaming task
            asyncio.create_task(self._stream_docker_logs())

            return True

        except FileNotFoundError:
            logger.error("[JarvisPrime] Docker not found. Please install Docker.")
            self._health.error_message = "Docker not installed"
            return False
        except Exception as e:
            logger.error(f"[JarvisPrime] Docker spawn failed: {e}")
            self._health.error_message = str(e)
            return False

    async def _stop_docker_container(self) -> None:
        """Stop and remove the Docker container."""
        try:
            # Stop container
            stop_proc = await asyncio.create_subprocess_exec(
                "docker", "stop", self.config.docker_container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(stop_proc.wait(), timeout=10.0)
        except Exception:
            pass  # Container might not exist

        try:
            # Remove container
            rm_proc = await asyncio.create_subprocess_exec(
                "docker", "rm", self.config.docker_container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await rm_proc.wait()
        except Exception:
            pass  # Container might not exist

    async def _stream_docker_logs(self) -> None:
        """Stream Docker container logs."""
        if not self.config.use_docker:
            return

        try:
            process = await asyncio.create_subprocess_exec(
                "docker", "logs", "-f", self.config.docker_container_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

            async for line in process.stdout:
                text = line.decode().strip()
                if text:
                    if "error" in text.lower() or "exception" in text.lower():
                        logger.warning(f"[JarvisPrime:Docker] {text}")
                    else:
                        logger.debug(f"[JarvisPrime:Docker] {text}")

                if self._shutdown_event.is_set():
                    break

        except Exception as e:
            logger.debug(f"[JarvisPrime] Docker log streaming ended: {e}")

    async def _read_stdout(self) -> None:
        """Read stdout from subprocess (for logging)."""
        if not self._process or not self._process.stdout:
            return

        try:
            async for line in self._process.stdout:
                text = line.decode().strip()
                if text:
                    logger.debug(f"[JarvisPrime:OUT] {text}")
        except Exception as e:
            logger.debug(f"[JarvisPrime] stdout reader ended: {e}")

    async def _read_stderr(self) -> None:
        """Read stderr from subprocess (for logging)."""
        if not self._process or not self._process.stderr:
            return

        try:
            async for line in self._process.stderr:
                text = line.decode().strip()
                if text:
                    # Check for errors
                    if "error" in text.lower() or "exception" in text.lower():
                        logger.warning(f"[JarvisPrime:ERR] {text}")
                    else:
                        logger.debug(f"[JarvisPrime:ERR] {text}")
        except Exception as e:
            logger.debug(f"[JarvisPrime] stderr reader ended: {e}")

    async def _wait_for_healthy(self) -> bool:
        """Wait for JARVIS-Prime to become healthy."""
        start_time = time.perf_counter()
        check_interval = 0.5  # Start with fast checks

        while (time.perf_counter() - start_time) < self.config.startup_timeout_seconds:
            # Check if process died
            if self._process and self._process.returncode is not None:
                logger.error(f"[JarvisPrime] Process died during startup (code: {self._process.returncode})")
                return False

            # Try health check
            try:
                if self._http_session is None:
                    self._http_session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=2.0)
                    )

                async with self._http_session.get(self.config.health_url) as response:
                    if response.status == 200:
                        data = await response.json()
                        logger.info(f"[JarvisPrime] Health check passed: {data}")
                        return True
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass  # Expected during startup

            # Slow down checks over time
            await asyncio.sleep(check_interval)
            check_interval = min(check_interval * 1.5, 2.0)

        logger.error(f"[JarvisPrime] Startup timeout ({self.config.startup_timeout_seconds}s)")
        return False

    async def _terminate_process(self) -> None:
        """Terminate the JARVIS-Prime process/container gracefully."""
        if self.config.use_docker:
            await self._stop_docker_container()
            return

        if not self._process:
            return

        try:
            # Try SIGTERM first
            self._process.terminate()

            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.debug("[JarvisPrime] Process terminated gracefully")
            except asyncio.TimeoutError:
                # Force kill
                logger.warning("[JarvisPrime] Process didn't terminate, killing")
                self._process.kill()
                await self._process.wait()

        except ProcessLookupError:
            pass  # Already dead
        except Exception as e:
            logger.warning(f"[JarvisPrime] Error terminating process: {e}")
        finally:
            self._process = None

    # =========================================================================
    # Helpers
    # =========================================================================

    def _set_status(self, status: PrimeStatus) -> None:
        """Update status and notify callbacks."""
        old_status = self._health.status
        self._health.status = status

        if old_status != status:
            logger.info(f"[JarvisPrime] Status: {old_status.value} -> {status.value}")

            for callback in self._on_status_change:
                try:
                    callback(status)
                except Exception as e:
                    logger.debug(f"[JarvisPrime] Status callback error: {e}")

    def _record_latency(self, latency_ms: float) -> None:
        """Record a latency sample."""
        self._latency_samples.append(latency_ms)

        # Keep only recent samples
        if len(self._latency_samples) > self._max_latency_samples:
            self._latency_samples = self._latency_samples[-self._max_latency_samples:]

        # Update average
        self._health.average_latency_ms = sum(self._latency_samples) / len(self._latency_samples)

    async def _announce(self, message: str) -> None:
        """Announce via narrator if available."""
        if self._narrator_callback:
            try:
                await self._narrator_callback(message)
            except Exception as e:
                logger.debug(f"[JarvisPrime] Narrator error: {e}")

    # =========================================================================
    # Public API
    # =========================================================================

    def is_running(self) -> bool:
        """Check if JARVIS-Prime is running."""
        return self._health.status == PrimeStatus.RUNNING

    def is_available(self) -> bool:
        """Check if JARVIS-Prime is available for requests."""
        return self._health.status in (PrimeStatus.RUNNING, PrimeStatus.DEGRADED)

    def on_status_change(self, callback: Callable[[PrimeStatus], None]) -> None:
        """Register a status change callback."""
        self._on_status_change.append(callback)

    def on_health_update(self, callback: Callable[[PrimeHealth], None]) -> None:
        """Register a health update callback."""
        self._on_health_update.append(callback)

    def get_config(self) -> JarvisPrimeConfig:
        """Get the current configuration."""
        return self.config


# =============================================================================
# Singleton Access
# =============================================================================

_orchestrator_instance: Optional[JarvisPrimeOrchestrator] = None
_orchestrator_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


def get_jarvis_prime_orchestrator(
    config: Optional[JarvisPrimeConfig] = None,
    narrator_callback: Optional[Callable[[str], asyncio.coroutine]] = None,
) -> JarvisPrimeOrchestrator:
    """
    Get the global JARVIS-Prime orchestrator instance.

    Args:
        config: Optional configuration (only used on first call)
        narrator_callback: Optional narrator callback (only used on first call)

    Returns:
        The global orchestrator instance
    """
    global _orchestrator_instance

    if _orchestrator_instance is None:
        _orchestrator_instance = JarvisPrimeOrchestrator(
            config=config,
            narrator_callback=narrator_callback,
        )

    return _orchestrator_instance


async def get_jarvis_prime_orchestrator_async(
    config: Optional[JarvisPrimeConfig] = None,
    narrator_callback: Optional[Callable[[str], asyncio.coroutine]] = None,
    auto_start: bool = True,
) -> JarvisPrimeOrchestrator:
    """
    Get the global JARVIS-Prime orchestrator instance (async version).

    This version can optionally auto-start the orchestrator.

    Args:
        config: Optional configuration
        narrator_callback: Optional narrator callback
        auto_start: If True, start the orchestrator if not running

    Returns:
        The global orchestrator instance
    """
    global _orchestrator_instance

    async with _orchestrator_lock:
        if _orchestrator_instance is None:
            _orchestrator_instance = JarvisPrimeOrchestrator(
                config=config,
                narrator_callback=narrator_callback,
            )

        if auto_start and not _orchestrator_instance.is_running():
            if _orchestrator_instance.config.enabled and _orchestrator_instance.config.auto_start:
                await _orchestrator_instance.start()

    return _orchestrator_instance
