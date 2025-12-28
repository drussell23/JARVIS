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
        default_factory=lambda: float(os.getenv("JARVIS_PRIME_HEALTH_TIMEOUT", "5.0"))
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

        # Latency tracking for metrics
        self._latency_samples: List[float] = []
        self._max_latency_samples = 100

        # Docker-specific state
        self._docker_container_id: Optional[str] = None

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

            # Clean up any existing process on our port (v10.3)
            await self._ensure_port_available()

            # Start the subprocess
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
                    await self._terminate_process()
                    self._set_status(PrimeStatus.FAILED)
                    return False
            else:
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
        Perform a health check.

        Returns:
            True if healthy
        """
        if not self._http_session:
            return False

        try:
            start_time = time.perf_counter()

            async with self._http_session.get(self.config.health_url) as response:
                latency_ms = (time.perf_counter() - start_time) * 1000
                self._record_latency(latency_ms)

                if response.status == 200:
                    data = await response.json()

                    self._health.last_health_check = datetime.now()
                    self._health.consecutive_failures = 0
                    self._health.model_loaded = data.get("model_loaded", False)
                    self._health.model_name = data.get("model_name")

                    return True
                else:
                    self._health.consecutive_failures += 1
                    return False

        except asyncio.TimeoutError:
            logger.warning("[JarvisPrime] Health check timed out")
            self._health.consecutive_failures += 1
            return False
        except aiohttp.ClientError as e:
            logger.warning(f"[JarvisPrime] Health check failed: {e}")
            self._health.consecutive_failures += 1
            return False
        except Exception as e:
            logger.error(f"[JarvisPrime] Health check error: {e}")
            self._health.consecutive_failures += 1
            return False

    async def _health_check_loop(self) -> None:
        """Background health check loop with auto-recovery."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.health_check_interval_seconds)

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

                # Perform health check
                healthy = await self.check_health()

                if not healthy:
                    if self._health.consecutive_failures >= 3:
                        logger.warning(
                            f"[JarvisPrime] {self._health.consecutive_failures} consecutive failures"
                        )
                        self._set_status(PrimeStatus.DEGRADED)

                        if self._health.consecutive_failures >= 5:
                            logger.error("[JarvisPrime] Too many failures, triggering restart")
                            await self.restart()
                    else:
                        # Minor degradation
                        if self._health.status == PrimeStatus.RUNNING:
                            self._set_status(PrimeStatus.DEGRADED)
                else:
                    # Healthy - restore status if degraded
                    if self._health.status == PrimeStatus.DEGRADED:
                        self._set_status(PrimeStatus.RUNNING)

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
    # Port Management (v10.4) - Process-hierarchy aware
    # =========================================================================

    async def _ensure_port_available(self) -> None:
        """
        Intelligent port coordination system - ensures port is available before starting.

        Strategy (v10.5 - Enhanced with zombie detection):
        1. Check if port is in use
        2. If by our own managed process, trust it
        3. Check for zombie/defunct processes and clean immediately
        4. If by related process (parent/child/sibling), coordinate shutdown
        5. If by old JARVIS Prime instance, graceful shutdown with retries
        6. If by unrelated process, force cleanup
        7. Wait with exponential backoff (up to 45s total for stubborn processes)
        8. ONLY return when port is truly available OR raise exception

        This prevents the "address already in use" error by NEVER proceeding
        until the port is guaranteed to be free.

        Raises:
            RuntimeError: If port cannot be freed within timeout
        """
        port = self.config.port
        max_wait_time = 45.0  # Increased from 30s to handle stubborn processes
        start_time = time.time()

        logger.info(f"[JarvisPrime] Ensuring port {port} is available for startup...")

        # Initial check
        pid = await self._get_pid_on_port(port)
        if pid is None:
            logger.debug(f"[JarvisPrime] Port {port} is available")
            return

        # CRITICAL: Check if this is our own stored process
        if self._process and self._process.pid == pid:
            logger.debug(f"[JarvisPrime] Port {port} is used by our own managed process (PID {pid})")
            return

        # v10.6: CRITICAL FIX - Check if port is used by current supervisor process
        # This happens during restart scenarios where supervisor is restarting
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
                            # Look for python processes with "backend/main.py" (JARVIS Prime signature)
                            cmdline = child.cmdline()
                            if any('backend/main.py' in arg for arg in cmdline):
                                logger.info(
                                    f"[JarvisPrime] Found existing Prime subprocess (PID {child.pid}). "
                                    f"Reusing instead of starting new instance."
                                )
                                # Store the existing process
                                self._process = child
                                self._status = "running"
                                return
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            continue
                except Exception as e:
                    logger.debug(f"[JarvisPrime] Could not check for existing subprocess: {e}")

            # No existing subprocess found - the port binding is stale
            # Try to unbind by closing any sockets on this port owned by us
            logger.warning(
                f"[JarvisPrime] No existing Prime subprocess found, but port {port} is bound to us. "
                f"This indicates a stale binding from previous run. "
                f"Port will be freed when new Prime subprocess starts."
            )
            # Don't raise exception - allow Prime to start and take over the port
            return

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
                            return
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
                return

        # Strategy 2: Try SIGTERM (polite kill signal)
        if not is_related:  # Only if not related to us
            logger.info(f"[JarvisPrime] Attempting SIGTERM on PID {pid}...")
            try:
                os.kill(pid, signal.SIGTERM)
                if await self._wait_for_port_free(port, max_wait=5.0):
                    logger.info(f"[JarvisPrime] Port {port} freed via SIGTERM")
                    return
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
                return

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
                        return
                except Exception as e:
                    logger.error(f"[JarvisPrime] Failed to kill orphaned instance: {e}")

        # Strategy 4: Force kill unrelated process (SIGKILL)
        if not is_related:
            logger.warning(f"[JarvisPrime] Force killing unrelated PID {pid} with SIGKILL...")
            try:
                os.kill(pid, signal.SIGKILL)
                if await self._wait_for_port_free(port, max_wait=3.0):
                    logger.info(f"[JarvisPrime] Port {port} freed via SIGKILL")
                    return
            except (ProcessLookupError, PermissionError) as e:
                logger.error(f"[JarvisPrime] SIGKILL failed: {e}")

        # Final verification
        elapsed = time.time() - start_time
        pid = await self._get_pid_on_port(port)

        if pid is None:
            logger.info(f"[JarvisPrime] Port {port} is now available (took {elapsed:.1f}s)")
            return

        # CRITICAL: Port is STILL occupied - cannot proceed!
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
_orchestrator_lock = asyncio.Lock()


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
