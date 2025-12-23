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
