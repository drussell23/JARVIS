"""
Cross-Repo Startup Orchestrator v5.0 - Enterprise-Grade Process Lifecycle Manager
===================================================================================

Dynamic service discovery and self-healing process orchestration for JARVIS ecosystem.
Enables single-command startup of all Trinity components (JARVIS Body, J-Prime Mind, Reactor-Core Nerves).

Features (v5.0):
- 🔧 SINGLE SOURCE OF TRUTH: Ports from trinity_config.py (no hardcoding!)
- 🧹 Pre-flight Cleanup: Kills stale processes on legacy ports (8002, 8003)
- 🔍 Wrong-Binding Detection: Detects 127.0.0.1 vs 0.0.0.0 misconfigurations
- 🔄 Auto-Healing with exponential backoff (dead process detection & restart)
- 📡 Real-Time Output Streaming (stdout/stderr prefixed per service)
- 🎯 Process Lifecycle Management (spawn, monitor, graceful shutdown)
- 🛡️ Graceful Shutdown Handlers (SIGINT/SIGTERM cleanup)
- 🐍 Auto-detect venv Python for each repo
- 📁 Correct entry points: run_server.py, run_reactor.py
- ⚡ Pre-spawn validation (port check, dependency check)
- 📊 Service Health Monitoring with progressive backoff

Service Ports (v5.0 - from trinity_config.py):
- jarvis-prime: port 8000 (run_server.py --port 8000 --host 0.0.0.0)
- reactor-core: port 8090 (run_reactor.py --port 8090)
- jarvis-body: port 8010 (main JARVIS)

Legacy ports cleaned up automatically:
- jarvis-prime: 8001, 8002 (killed if found)
- reactor-core: 8003, 8004, 8005 (killed if found)

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │         Cross-Repo Orchestrator v5.0 - Process Manager           │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  Service Registry: ~/.jarvis/registry/services.json              │
    │  ┌────────────────┬──────────────┬─────────────────────┐        │
    │  │   JARVIS       │   J-PRIME    │   REACTOR-CORE      │        │
    │  │  PID: auto     │  PID: auto   │   PID: auto         │        │
    │  │  Port: 8010    │  Port: 8000  │   Port: 8090        │        │
    │  │  Status: ✅    │  Status: ✅  │   Status: ✅        │        │
    │  └────────────────┴──────────────┴─────────────────────┘        │
    │                                                                   │
    │  Process Lifecycle:                                               │
    │  0. Pre-flight Cleanup (kill stale legacy processes)             │
    │  1. Pre-Spawn Validation (venv detect, port check)               │
    │  2. Spawn (asyncio.create_subprocess_exec with venv Python)      │
    │  3. Monitor (PID tracking + progressive health checks)           │
    │  4. Stream Output (real-time with [SERVICE] prefix)              │
    │  5. Auto-Heal (restart on crash with exponential backoff)        │
    │  6. Graceful Shutdown (SIGTERM → wait → SIGKILL)                 │
    │                                                                   │
    └──────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 5.3.0

Changelog:
- v5.3: Made startup resilient to CancelledError (don't propagate during init)
- v5.2: Fixed loop.stop() antipattern, proper task cancellation on shutdown
- v5.1: Fixed sys.exit() antipattern in async shutdown handler
- v5.0: Added pre-flight cleanup, circuit breaker, trinity_config integration
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration (Zero Hardcoding - All Environment Driven)
# =============================================================================

# v5.0: Import from trinity_config as SINGLE SOURCE OF TRUTH
try:
    from backend.core.trinity_config import get_config as get_trinity_config
    _TRINITY_CONFIG_AVAILABLE = True
except ImportError:
    _TRINITY_CONFIG_AVAILABLE = False
    get_trinity_config = None


def _get_port_from_trinity(service: str, fallback: int) -> int:
    """
    Get port from trinity_config (single source of truth) with fallback.

    v5.0: This ensures all services use consistent ports from trinity_config.
    """
    if not _TRINITY_CONFIG_AVAILABLE:
        return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))

    try:
        config = get_trinity_config()
        if service == "jarvis_prime":
            return config.jarvis_prime_endpoint.port
        elif service == "reactor_core":
            return config.reactor_core_endpoint.port
        elif service == "jarvis":
            return config.jarvis_endpoint.port
    except Exception:
        pass

    return int(os.getenv(f"{service.upper()}_PORT", str(fallback)))


@dataclass
class OrchestratorConfig:
    """
    Enterprise configuration with zero hardcoding.

    v5.0: Ports sourced from trinity_config as SINGLE SOURCE OF TRUTH.
    """

    # Repository paths
    jarvis_prime_path: Path = field(default_factory=lambda: Path(
        os.getenv("JARVIS_PRIME_PATH", str(Path.home() / "Documents" / "repos" / "jarvis-prime"))
    ))
    reactor_core_path: Path = field(default_factory=lambda: Path(
        os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents" / "repos" / "reactor-core"))
    ))

    # Default ports - sourced from trinity_config (SINGLE SOURCE OF TRUTH)
    # Fallbacks: jarvis-prime=8000, reactor-core=8090
    jarvis_prime_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("jarvis_prime", 8000)
    )
    reactor_core_default_port: int = field(
        default_factory=lambda: _get_port_from_trinity("reactor_core", 8090)
    )

    # Legacy ports to clean up (processes on these should be killed)
    legacy_jarvis_prime_ports: List[int] = field(default_factory=lambda: [8001, 8002])
    legacy_reactor_core_ports: List[int] = field(default_factory=lambda: [8003, 8004, 8005])

    # Feature flags
    jarvis_prime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    reactor_core_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
    )

    # Auto-healing configuration
    auto_healing_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTO_HEALING_ENABLED", "true").lower() == "true"
    )
    max_restart_attempts: int = field(
        default_factory=lambda: int(os.getenv("MAX_RESTART_ATTEMPTS", "5"))
    )
    restart_backoff_base: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_BASE", "1.0"))
    )
    restart_backoff_max: float = field(
        default_factory=lambda: float(os.getenv("RESTART_BACKOFF_MAX", "60.0"))
    )

    # Health monitoring
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_INTERVAL", "5.0"))
    )
    health_check_timeout: float = field(
        default_factory=lambda: float(os.getenv("HEALTH_CHECK_TIMEOUT", "5.0"))
    )
    startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("SERVICE_STARTUP_TIMEOUT", "60.0"))
    )

    # Graceful shutdown
    shutdown_timeout: float = field(
        default_factory=lambda: float(os.getenv("SHUTDOWN_TIMEOUT", "10.0"))
    )

    # Output streaming
    stream_output: bool = field(
        default_factory=lambda: os.getenv("STREAM_CHILD_OUTPUT", "true").lower() == "true"
    )


# =============================================================================
# Data Models
# =============================================================================

class ServiceStatus(Enum):
    """Service lifecycle status."""
    PENDING = "pending"
    STARTING = "starting"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class ServiceDefinition:
    """
    Definition of a service to manage.

    v4.0: Enhanced with script_args for command-line argument passing.
    """
    name: str
    repo_path: Path
    script_name: str = "main.py"
    fallback_scripts: List[str] = field(default_factory=lambda: ["server.py", "run.py", "app.py"])
    default_port: int = 8000
    health_endpoint: str = "/health"
    startup_timeout: float = 60.0
    environment: Dict[str, str] = field(default_factory=dict)

    # v4.0: Command-line arguments to pass to the script
    # e.g., ["--port", "8000", "--host", "0.0.0.0"]
    script_args: List[str] = field(default_factory=list)

    # v3.1: Module-based entry points (e.g., "reactor_core.api.server")
    # When set, spawns with: python -m <module_path>
    module_path: Optional[str] = None

    # v3.1: Nested script paths to search (relative to repo_path)
    # e.g., ["reactor_core/api/server.py", "src/main.py"]
    nested_scripts: List[str] = field(default_factory=list)

    # v3.1: Use uvicorn for FastAPI apps
    use_uvicorn: bool = False
    uvicorn_app: Optional[str] = None  # e.g., "reactor_core.api.server:app"


@dataclass
class ManagedProcess:
    """Represents a managed child process with monitoring."""
    definition: ServiceDefinition
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    port: Optional[int] = None
    status: ServiceStatus = ServiceStatus.PENDING
    restart_count: int = 0
    last_restart: float = 0.0
    last_health_check: float = 0.0
    consecutive_failures: int = 0

    # Background tasks
    output_stream_task: Optional[asyncio.Task] = None
    health_monitor_task: Optional[asyncio.Task] = None

    @property
    def is_running(self) -> bool:
        """Check if process is running."""
        if self.process is None:
            return False
        return self.process.returncode is None

    def calculate_backoff(self, base: float = 1.0, max_backoff: float = 60.0) -> float:
        """Calculate exponential backoff for restart."""
        backoff = base * (2 ** self.restart_count)
        return min(backoff, max_backoff)


# =============================================================================
# Process Orchestrator
# =============================================================================

class ProcessOrchestrator:
    """
    Enterprise-grade process lifecycle manager.

    Features:
    - Spawn and manage child processes
    - Stream stdout/stderr with service prefixes
    - Auto-heal crashed services with exponential backoff
    - Graceful shutdown handling
    - Dynamic service discovery via registry
    """

    def __init__(self, config: Optional[OrchestratorConfig] = None):
        """Initialize orchestrator."""
        self.config = config or OrchestratorConfig()
        self.processes: Dict[str, ManagedProcess] = {}
        self._shutdown_event = asyncio.Event()
        self._running = False

        # Service registry (lazy loaded)
        self._registry = None

        # Signal handlers registered flag
        self._signals_registered = False

    # =========================================================================
    # Pre-Flight Cleanup (v5.0)
    # =========================================================================

    async def _kill_process_on_port(self, port: int) -> bool:
        """
        Kill any process listening on the specified port.

        v5.3: Uses lsof to find and kill stale processes.
        Resilient to CancelledError during startup.
        Returns True if a process was killed.
        """
        try:
            # Find process on port using lsof
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-ti", f":{port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            pids = stdout.decode().strip().split('\n')
            killed = False

            for pid_str in pids:
                if not pid_str:
                    continue
                try:
                    pid = int(pid_str)
                    os.kill(pid, signal.SIGTERM)
                    logger.info(f"    🔪 Killed stale process on port {port} (PID: {pid})")
                    killed = True
                except (ValueError, ProcessLookupError, PermissionError) as e:
                    logger.debug(f"    Could not kill PID {pid_str}: {e}")

            if killed:
                # Give process time to terminate - use shield to prevent cancellation
                try:
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    # Don't let cancellation interrupt cleanup
                    logger.debug(f"    Port {port} cleanup sleep interrupted, continuing...")

            return killed

        except asyncio.CancelledError:
            # v5.3: Don't propagate CancelledError during critical cleanup
            logger.debug(f"    Port cleanup cancelled for {port}, continuing startup...")
            return False
        except Exception as e:
            logger.debug(f"    Port cleanup failed for {port}: {e}")
            return False

    async def _cleanup_legacy_ports(self) -> Dict[str, List[int]]:
        """
        Clean up any processes on legacy ports before startup.

        v5.3: Ensures no stale processes from old configurations are blocking
        the correct ports. Resilient to CancelledError.
        Returns dict of service -> [killed_ports].
        """
        logger.info("  🧹 Pre-flight: Cleaning up legacy ports...")

        cleaned = {"jarvis-prime": [], "reactor-core": []}

        try:
            # Clean up legacy jarvis-prime ports
            for port in self.config.legacy_jarvis_prime_ports:
                if await self._kill_process_on_port(port):
                    cleaned["jarvis-prime"].append(port)

            # Clean up legacy reactor-core ports
            for port in self.config.legacy_reactor_core_ports:
                if await self._kill_process_on_port(port):
                    cleaned["reactor-core"].append(port)

            # Also check if something is running on CORRECT ports but with wrong host
            # (e.g., bound to 127.0.0.1 instead of 0.0.0.0)
            for service, port in [
                ("jarvis-prime", self.config.jarvis_prime_default_port),
                ("reactor-core", self.config.reactor_core_default_port),
            ]:
                if await self._check_wrong_binding(port):
                    logger.warning(f"    ⚠️ {service} on port {port} bound to 127.0.0.1, restarting...")
                    if await self._kill_process_on_port(port):
                        cleaned[service].append(port)

        except asyncio.CancelledError:
            # v5.3: Don't let cancellation interrupt startup - log and continue
            logger.warning("  ⚠️ Legacy port cleanup interrupted, continuing startup...")

        # Summary
        total_cleaned = sum(len(ports) for ports in cleaned.values())
        if total_cleaned > 0:
            logger.info(f"  ✅ Cleaned {total_cleaned} stale processes")
        else:
            logger.info(f"  ✅ No legacy processes found")

        return cleaned

    async def _check_wrong_binding(self, port: int) -> bool:
        """
        Check if a service on port is bound to 127.0.0.1 (should be 0.0.0.0).

        v5.3: Detects misconfigured services that won't accept external connections.
        Resilient to CancelledError.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "lsof", "-i", f":{port}", "-P", "-n",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()

            if not stdout:
                return False

            output = stdout.decode()
            # Check if bound to 127.0.0.1 (localhost only)
            if "127.0.0.1:" in output or "localhost:" in output:
                # Also check it's not bound to 0.0.0.0 (which would be correct)
                if "*:" not in output and "0.0.0.0:" not in output:
                    return True

        except Exception:
            pass

        return False

    @property
    def registry(self):
        """Lazy-load service registry."""
        if self._registry is None:
            try:
                from backend.core.service_registry import get_service_registry
                self._registry = get_service_registry()
            except ImportError:
                logger.warning("Service registry not available")
        return self._registry

    def add_service(self, definition: ServiceDefinition) -> None:
        """
        Add a service definition to the orchestrator.

        This allows dynamic addition of services beyond the default configuration.

        Args:
            definition: Service definition to add
        """
        if definition.name in self.processes:
            logger.warning(f"Service {definition.name} already exists, updating definition")

        self.processes[definition.name] = ManagedProcess(definition=definition)
        logger.debug(f"Added service: {definition.name}")

    def remove_service(self, name: str) -> bool:
        """
        Remove a service from the orchestrator.

        Args:
            name: Service name to remove

        Returns:
            True if service was removed
        """
        if name in self.processes:
            del self.processes[name]
            logger.debug(f"Removed service: {name}")
            return True
        return False

    def _get_service_definitions(self) -> List[ServiceDefinition]:
        """
        Get service definitions based on configuration.

        v4.0: Uses actual entry point scripts from each repo:
        - jarvis-prime: run_server.py (port 8000)
        - reactor-core: run_reactor.py (port 8090)
        """
        definitions = []

        if self.config.jarvis_prime_enabled:
            # jarvis-prime uses run_server.py as main entry point
            # Port is passed via --port CLI arg (not env var)
            definitions.append(ServiceDefinition(
                name="jarvis-prime",
                repo_path=self.config.jarvis_prime_path,
                script_name="run_server.py",  # Primary entry point
                fallback_scripts=["main.py", "server.py", "app.py"],
                default_port=self.config.jarvis_prime_default_port,
                health_endpoint="/health",
                startup_timeout=self.config.startup_timeout,
                # Pass port via command-line (run_server.py uses argparse)
                script_args=[
                    "--port", str(self.config.jarvis_prime_default_port),
                    "--host", "0.0.0.0",
                ],
                environment={
                    "PYTHONPATH": str(self.config.jarvis_prime_path),
                },
            ))

        if self.config.reactor_core_enabled:
            # reactor-core uses run_reactor.py as main entry point
            # Port is passed via --port CLI arg
            definitions.append(ServiceDefinition(
                name="reactor-core",
                repo_path=self.config.reactor_core_path,
                script_name="run_reactor.py",  # Primary entry point
                fallback_scripts=["run_supervisor.py", "main.py", "server.py"],
                default_port=self.config.reactor_core_default_port,
                health_endpoint="/health",
                startup_timeout=self.config.startup_timeout,
                # Don't use uvicorn - run_reactor.py handles its own server
                use_uvicorn=False,
                uvicorn_app=None,
                # Pass port via command-line
                script_args=[
                    "--port", str(self.config.reactor_core_default_port),
                ],
                environment={
                    "PYTHONPATH": str(self.config.reactor_core_path),
                    "REACTOR_PORT": str(self.config.reactor_core_default_port),
                },
            ))

        return definitions

    def _find_script(self, definition: ServiceDefinition) -> Optional[Path]:
        """
        Find the startup script for a service.

        v3.1: Enhanced discovery with nested script paths and module detection.

        Search order:
        1. Module path (returns None but module_path is used directly in spawn)
        2. Uvicorn app (returns None but uvicorn is used in spawn)
        3. Nested scripts (e.g., "reactor_core/api/server.py")
        4. Root scripts (main.py, server.py, etc.)
        """
        repo_path = definition.repo_path

        if not repo_path.exists():
            logger.warning(f"Repository not found: {repo_path}")
            return None

        # v3.1: If module_path or uvicorn_app is set, we don't need a script file
        # The spawn method will handle these directly
        if definition.module_path or definition.uvicorn_app:
            logger.debug(f"Service {definition.name} uses module/uvicorn entry point")
            return Path("__module__")  # Sentinel value

        # v3.1: Try nested scripts first (more specific paths)
        for nested in definition.nested_scripts:
            script_path = repo_path / nested
            if script_path.exists():
                logger.debug(f"Found nested script: {script_path}")
                return script_path

        # Try main script in root
        script_path = repo_path / definition.script_name
        if script_path.exists():
            return script_path

        # Try fallback scripts in root
        for fallback in definition.fallback_scripts:
            script_path = repo_path / fallback
            if script_path.exists():
                return script_path

        logger.warning(
            f"No startup script found in {repo_path} "
            f"(tried: {definition.script_name}, {definition.fallback_scripts})"
        )
        return None

    # =========================================================================
    # Output Streaming
    # =========================================================================

    async def _stream_output(
        self,
        managed: ManagedProcess,
        stream: asyncio.StreamReader,
        stream_type: str = "stdout"
    ) -> None:
        """
        Stream process output with service prefix.

        Example output:
            [J-PRIME] Loading model...
            [J-PRIME] Model loaded in 2.3s
            [REACTOR] Initializing pipeline...
        """
        prefix = f"[{managed.definition.name.upper().replace('-', '_')}]"
        is_stderr = stream_type == "stderr"

        try:
            while True:
                line = await stream.readline()
                if not line:
                    break

                decoded = line.decode('utf-8', errors='replace').rstrip()
                if decoded:
                    log_func = logger.warning if is_stderr else logger.info
                    log_func(f"{prefix} {decoded}")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Output streaming error for {managed.definition.name}: {e}")

    async def _start_output_streaming(self, managed: ManagedProcess) -> None:
        """Start streaming stdout and stderr for a process."""
        if not self.config.stream_output:
            return

        if managed.process is None:
            return

        async def stream_both():
            tasks = []
            if managed.process.stdout:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stdout, "stdout")
                    )
                )
            if managed.process.stderr:
                tasks.append(
                    asyncio.create_task(
                        self._stream_output(managed, managed.process.stderr, "stderr")
                    )
                )
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        managed.output_stream_task = asyncio.create_task(stream_both())

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _check_health(self, managed: ManagedProcess) -> bool:
        """Check health of a service via HTTP endpoint."""
        if managed.port is None:
            return False

        url = f"http://localhost:{managed.port}{managed.definition.health_endpoint}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    timeout=aiohttp.ClientTimeout(total=self.config.health_check_timeout)
                ) as response:
                    return response.status == 200
        except Exception:
            return False

    async def _health_monitor_loop(self, managed: ManagedProcess) -> None:
        """Background health monitoring for a service."""
        try:
            while not self._shutdown_event.is_set():
                await asyncio.sleep(self.config.health_check_interval)

                if not managed.is_running:
                    # Process died, trigger auto-heal if enabled
                    if self.config.auto_healing_enabled:
                        logger.warning(
                            f"🚨 Process {managed.definition.name} died (exit code: {managed.process.returncode})"
                        )
                        managed.status = ServiceStatus.FAILED
                        await self._auto_heal(managed)
                    break

                # HTTP health check
                healthy = await self._check_health(managed)
                managed.last_health_check = time.time()

                if healthy:
                    managed.consecutive_failures = 0
                    if managed.status != ServiceStatus.HEALTHY:
                        managed.status = ServiceStatus.HEALTHY
                        logger.info(f"✅ {managed.definition.name} is healthy")

                        # Update registry
                        if self.registry:
                            await self.registry.heartbeat(
                                managed.definition.name,
                                status="healthy"
                            )
                else:
                    managed.consecutive_failures += 1
                    logger.warning(
                        f"⚠️ {managed.definition.name} health check failed "
                        f"({managed.consecutive_failures} consecutive failures)"
                    )

                    if managed.consecutive_failures >= 3:
                        managed.status = ServiceStatus.DEGRADED

                        if self.config.auto_healing_enabled:
                            await self._auto_heal(managed)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Health monitor error for {managed.definition.name}: {e}")

    # =========================================================================
    # Auto-Healing
    # =========================================================================

    async def _auto_heal(self, managed: ManagedProcess) -> bool:
        """
        Attempt to restart a failed service with exponential backoff.

        Returns True if restart succeeded.
        """
        if managed.restart_count >= self.config.max_restart_attempts:
            logger.error(
                f"❌ {managed.definition.name} exceeded max restart attempts "
                f"({self.config.max_restart_attempts}). Giving up."
            )
            managed.status = ServiceStatus.FAILED
            return False

        # Calculate backoff
        backoff = managed.calculate_backoff(
            self.config.restart_backoff_base,
            self.config.restart_backoff_max
        )

        logger.info(
            f"🔄 Restarting {managed.definition.name} in {backoff:.1f}s "
            f"(attempt {managed.restart_count + 1}/{self.config.max_restart_attempts})"
        )

        managed.status = ServiceStatus.RESTARTING
        await asyncio.sleep(backoff)

        # Stop existing process if still lingering
        await self._stop_process(managed)

        # Restart
        managed.restart_count += 1
        managed.last_restart = time.time()

        success = await self._spawn_service(managed)

        if success:
            logger.info(f"✅ {managed.definition.name} restarted successfully")
            managed.consecutive_failures = 0
            return True
        else:
            logger.error(f"❌ {managed.definition.name} restart failed")
            return False

    # =========================================================================
    # Process Spawning
    # =========================================================================

    def _find_venv_python(self, repo_path: Path) -> Optional[str]:
        """
        Find the venv Python executable for a repository.

        v4.0: Auto-detects venv location and returns the Python path.
        Falls back to system Python if no venv found.
        """
        # Check common venv locations
        venv_paths = [
            repo_path / "venv" / "bin" / "python3",
            repo_path / "venv" / "bin" / "python",
            repo_path / ".venv" / "bin" / "python3",
            repo_path / ".venv" / "bin" / "python",
            repo_path / "env" / "bin" / "python3",
            repo_path / "env" / "bin" / "python",
        ]

        for venv_python in venv_paths:
            if venv_python.exists():
                logger.debug(f"Found venv Python at: {venv_python}")
                return str(venv_python)

        logger.debug(f"No venv found in {repo_path}, using system Python")
        return None

    async def _pre_spawn_validation(self, definition: ServiceDefinition) -> tuple[bool, Optional[str]]:
        """
        Validate a service before spawning.

        v4.0: Pre-launch checks:
        - Repo path exists
        - Script exists
        - Venv Python found (optional but preferred)
        - Port not already in use

        Returns:
            Tuple of (is_valid, python_executable)
        """
        # Check repo exists
        if not definition.repo_path.exists():
            logger.error(f"Repository not found: {definition.repo_path}")
            return False, None

        # Check script exists
        script_path = self._find_script(definition)
        if script_path is None:
            return False, None

        # Find Python executable (prefer venv)
        python_exec = self._find_venv_python(definition.repo_path)
        if python_exec is None:
            python_exec = sys.executable
            logger.info(f"    ℹ️ Using system Python for {definition.name}: {python_exec}")
        else:
            logger.info(f"    ✓ Using venv Python for {definition.name}: {python_exec}")

        # Check port availability
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('localhost', definition.default_port))
            sock.close()
            if result == 0:
                logger.warning(
                    f"    ⚠️ Port {definition.default_port} already in use - "
                    f"{definition.name} may already be running"
                )
                # Not a fatal error - the service might be running
        except Exception as e:
            logger.debug(f"Port check failed: {e}")

        return True, python_exec

    async def _spawn_service(self, managed: ManagedProcess) -> bool:
        """
        Spawn a service process using asyncio.create_subprocess_exec.

        v4.0: Enhanced with:
        - Pre-spawn validation (venv detection, port check)
        - Better error reporting
        - Environment isolation

        Returns True if spawn and health check succeeded.
        """
        definition = managed.definition

        # Pre-spawn validation
        is_valid, python_exec = await self._pre_spawn_validation(definition)
        if not is_valid:
            logger.error(f"Cannot spawn {definition.name}: pre-spawn validation failed")
            managed.status = ServiceStatus.FAILED
            return False

        script_path = self._find_script(definition)

        if script_path is None:
            logger.error(f"Cannot spawn {definition.name}: no script found")
            managed.status = ServiceStatus.FAILED
            return False

        managed.status = ServiceStatus.STARTING

        try:
            # Build environment
            env = os.environ.copy()
            env.update(definition.environment)

            # Add port hint for service registration
            env["SERVICE_PORT"] = str(definition.default_port)
            env["SERVICE_NAME"] = definition.name

            # v4.0: Build command using the detected Python executable
            cmd: List[str] = []

            if definition.use_uvicorn and definition.uvicorn_app:
                # Uvicorn-based FastAPI app
                cmd = [
                    python_exec, "-m", "uvicorn",
                    definition.uvicorn_app,
                    "--host", "0.0.0.0",
                    "--port", str(definition.default_port),
                ]
                logger.info(f"🚀 Spawning {definition.name} via uvicorn: {definition.uvicorn_app}")

            elif definition.module_path:
                # Module-based entry point (python -m)
                cmd = [python_exec, "-m", definition.module_path]
                # Add script_args if any
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name} via module: {definition.module_path}")

            else:
                # Traditional script-based entry point
                cmd = [python_exec, str(script_path)]
                # v4.0: Append command-line arguments (e.g., --port 8000)
                if definition.script_args:
                    cmd.extend(definition.script_args)
                logger.info(f"🚀 Spawning {definition.name}: {' '.join(cmd)}")

            # Spawn process
            managed.process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(definition.repo_path),
                stdout=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if self.config.stream_output else asyncio.subprocess.DEVNULL,
                env=env,
            )

            managed.pid = managed.process.pid
            managed.port = definition.default_port  # May be updated by registry discovery

            logger.info(f"📋 {definition.name} spawned with PID {managed.pid}")

            # Start output streaming
            await self._start_output_streaming(managed)

            # Wait for service to become healthy
            healthy = await self._wait_for_health(managed, timeout=definition.startup_timeout)

            if healthy:
                managed.status = ServiceStatus.HEALTHY

                # Register in service registry
                if self.registry:
                    await self.registry.register_service(
                        service_name=definition.name,
                        pid=managed.pid,
                        port=managed.port,
                        health_endpoint=definition.health_endpoint,
                        metadata={"repo_path": str(definition.repo_path)}
                    )

                # Start health monitor
                managed.health_monitor_task = asyncio.create_task(
                    self._health_monitor_loop(managed)
                )

                return True
            else:
                logger.warning(
                    f"⚠️ {definition.name} spawned but did not become healthy "
                    f"within {definition.startup_timeout}s"
                )
                managed.status = ServiceStatus.DEGRADED
                return False

        except Exception as e:
            logger.error(f"❌ Failed to spawn {definition.name}: {e}", exc_info=True)
            managed.status = ServiceStatus.FAILED
            return False

    async def _wait_for_health(
        self,
        managed: ManagedProcess,
        timeout: float = 60.0
    ) -> bool:
        """
        Wait for service to become healthy with progressive logging.

        v4.0: Enhanced with:
        - Progressive backoff (1s → 2s → 3s)
        - Detailed progress logging
        - Process exit code capture on failure
        """
        start_time = time.time()
        check_interval = 1.0
        check_count = 0
        max_quiet_checks = 5  # Only log periodically after first few

        logger.info(f"    ⏳ Waiting for {managed.definition.name} to become healthy (timeout: {timeout}s)...")

        while (time.time() - start_time) < timeout:
            check_count += 1

            # Check if process died
            if not managed.is_running:
                exit_code = managed.process.returncode if managed.process else "unknown"
                logger.error(
                    f"    ❌ {managed.definition.name} process died during startup "
                    f"(exit code: {exit_code})"
                )
                return False

            # Check health endpoint
            if await self._check_health(managed):
                elapsed = time.time() - start_time
                logger.info(
                    f"    ✅ {managed.definition.name} healthy after {elapsed:.1f}s "
                    f"({check_count} health checks)"
                )
                return True

            # Progressive logging: frequent at start, sparse later
            if check_count <= max_quiet_checks or check_count % 10 == 0:
                elapsed = time.time() - start_time
                logger.debug(
                    f"    ⏳ {managed.definition.name}: health check {check_count} "
                    f"(elapsed: {elapsed:.1f}s)"
                )

            # Progressive backoff: 1s, 1.5s, 2s, 2.5s, 3s (max)
            await asyncio.sleep(min(check_interval, 3.0))
            check_interval = min(check_interval + 0.5, 3.0)

        elapsed = time.time() - start_time
        logger.warning(
            f"    ⚠️ {managed.definition.name} health check timed out after {elapsed:.1f}s "
            f"({check_count} checks)"
        )
        return False

    # =========================================================================
    # Process Stopping
    # =========================================================================

    async def _stop_process(self, managed: ManagedProcess) -> None:
        """Stop a managed process gracefully."""
        # Cancel background tasks
        for task in [managed.output_stream_task, managed.health_monitor_task]:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if managed.process is None or not managed.is_running:
            return

        logger.info(f"🛑 Stopping {managed.definition.name} (PID: {managed.pid})...")

        try:
            # Try graceful shutdown first (SIGTERM)
            managed.process.terminate()

            try:
                await asyncio.wait_for(
                    managed.process.wait(),
                    timeout=self.config.shutdown_timeout
                )
                logger.info(f"✅ {managed.definition.name} stopped gracefully")

            except asyncio.TimeoutError:
                # Force kill if necessary (SIGKILL)
                logger.warning(
                    f"⚠️ {managed.definition.name} did not stop gracefully, forcing..."
                )
                managed.process.kill()
                await managed.process.wait()
                logger.info(f"✅ {managed.definition.name} force killed")

        except ProcessLookupError:
            pass  # Process already dead
        except Exception as e:
            logger.error(f"Error stopping {managed.definition.name}: {e}")

        managed.status = ServiceStatus.STOPPED

        # Deregister from service registry
        if self.registry:
            await self.registry.deregister_service(managed.definition.name)

    # =========================================================================
    # Signal Handlers
    # =========================================================================

    def _setup_signal_handlers(self) -> None:
        """Setup graceful shutdown signal handlers."""
        if self._signals_registered:
            return

        loop = asyncio.get_event_loop()

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(
                sig,
                lambda s=sig: asyncio.create_task(self._handle_shutdown(s))
            )

        self._signals_registered = True
        logger.info("🛡️ Signal handlers registered (SIGINT, SIGTERM)")

    async def _handle_shutdown(self, signum: int) -> None:
        """
        Handle shutdown signal gracefully.

        v5.2: Proper async shutdown - don't call loop.stop() or sys.exit().
        Instead, set shutdown event and let pending tasks complete naturally.
        The main loop will exit when all tasks are done.
        """
        sig_name = signal.Signals(signum).name
        logger.info(f"\n🛑 Received {sig_name}, initiating graceful shutdown...")

        # Set shutdown event FIRST (signals other tasks to stop)
        self._shutdown_event.set()
        self._running = False

        # Perform graceful service shutdown
        try:
            await self.shutdown_all_services()
            logger.info("✅ Graceful shutdown complete")
        except asyncio.CancelledError:
            # Expected during shutdown - don't log as error
            logger.info("✅ Shutdown tasks cancelled (expected)")
        except Exception as e:
            logger.error(f"⚠️ Error during shutdown: {e}")

        # v5.2: Cancel all remaining tasks gracefully instead of stopping loop
        # This allows pending futures to complete/cancel properly
        try:
            loop = asyncio.get_running_loop()
            tasks = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]

            if tasks:
                logger.debug(f"[Shutdown] Cancelling {len(tasks)} remaining tasks...")
                for task in tasks:
                    task.cancel()

                # Wait for tasks to acknowledge cancellation
                await asyncio.gather(*tasks, return_exceptions=True)

            logger.info("✅ All tasks terminated")
        except Exception as e:
            logger.debug(f"[Shutdown] Task cleanup note: {e}")

    # =========================================================================
    # Main Orchestration
    # =========================================================================

    async def start_all_services(self) -> Dict[str, bool]:
        """
        Start all configured services with coordinated orchestration.

        v5.0: Enhanced with:
        - Pre-flight cleanup of legacy ports
        - Wrong-binding detection (127.0.0.1 vs 0.0.0.0)
        - Trinity config integration for port consistency

        Returns dict mapping service names to success status.
        """
        self._running = True

        # Setup signal handlers
        try:
            self._setup_signal_handlers()
        except Exception as e:
            logger.warning(f"Could not setup signal handlers: {e}")

        # Start service registry cleanup
        if self.registry:
            await self.registry.start_cleanup_task()

        results = {"jarvis": True}  # JARVIS is already running

        logger.info("=" * 70)
        logger.info("Cross-Repo Startup Orchestrator v5.0 - Enterprise Grade")
        logger.info("=" * 70)
        logger.info(f"  Ports: jarvis-prime={self.config.jarvis_prime_default_port}, "
                    f"reactor-core={self.config.reactor_core_default_port}")

        # Phase 0: Pre-flight cleanup (v5.0)
        logger.info("\n📍 PHASE 0: Pre-flight cleanup")
        await self._cleanup_legacy_ports()

        # Phase 1: JARVIS Core (already starting)
        logger.info("\n📍 PHASE 1: JARVIS Core (starting via supervisor)")
        logger.info("✅ JARVIS Core initialization in progress...")

        # Phase 2: Probe and spawn external services
        logger.info("\n📍 PHASE 2: External services startup")

        definitions = self._get_service_definitions()

        for definition in definitions:
            logger.info(f"\n  → Processing {definition.name}...")

            # First, check if already running via registry
            existing = None
            if self.registry:
                existing = await self.registry.discover_service(definition.name)

            if existing:
                logger.info(f"    ✅ {definition.name} already running (PID: {existing.pid}, Port: {existing.port})")
                results[definition.name] = True
                continue

            # Also try HTTP probe with default port
            url = f"http://localhost:{definition.default_port}{definition.health_endpoint}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                        if resp.status == 200:
                            logger.info(f"    ✅ {definition.name} already running at port {definition.default_port}")
                            results[definition.name] = True
                            continue
            except Exception:
                pass

            # Need to spawn
            logger.info(f"    ℹ️ {definition.name} not running, spawning...")

            managed = ManagedProcess(definition=definition)
            self.processes[definition.name] = managed

            success = await self._spawn_service(managed)
            results[definition.name] = success

            if success:
                logger.info(f"    ✅ {definition.name} started successfully")
            else:
                logger.warning(f"    ⚠️ {definition.name} failed to start (degraded mode)")

        # Phase 3: Verification
        logger.info("\n📍 PHASE 3: Integration verification")

        healthy_count = sum(1 for v in results.values() if v)
        total_count = len(results)

        if healthy_count == total_count:
            logger.info(f"✅ All {total_count} services operational - FULL MODE")
        else:
            logger.warning(
                f"⚠️ Running in DEGRADED MODE: {healthy_count}/{total_count} services operational"
            )

        # Print summary
        logger.info("\n" + "=" * 70)
        logger.info("🎯 Startup Summary:")
        for name, success in results.items():
            status = "✅ Running" if success else "⚠️ Unavailable"
            logger.info(f"  {name}: {status}")
        logger.info("=" * 70)

        return results

    async def shutdown_all_services(self) -> None:
        """Gracefully shutdown all managed services."""
        logger.info("\n🛑 Shutting down all services...")

        # Stop all processes in parallel
        shutdown_tasks = [
            self._stop_process(managed)
            for managed in self.processes.values()
        ]

        if shutdown_tasks:
            await asyncio.gather(*shutdown_tasks, return_exceptions=True)

        # Stop registry cleanup
        if self.registry:
            await self.registry.stop_cleanup_task()

        logger.info("✅ All services shut down")
        self._running = False


# =============================================================================
# Convenience Functions (Backward Compatibility)
# =============================================================================

# Global orchestrator instance
_orchestrator: Optional[ProcessOrchestrator] = None


def get_orchestrator() -> ProcessOrchestrator:
    """Get global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ProcessOrchestrator()
    return _orchestrator


async def probe_jarvis_prime() -> bool:
    """Legacy: Probe J-Prime health endpoint."""
    config = OrchestratorConfig()
    url = f"http://localhost:{config.jarvis_prime_default_port}/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def probe_reactor_core() -> bool:
    """Legacy: Probe Reactor-Core health endpoint."""
    config = OrchestratorConfig()
    url = f"http://localhost:{config.reactor_core_default_port}/api/health"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5.0)) as response:
                return response.status == 200
    except Exception:
        return False


async def start_all_repos() -> Dict[str, bool]:
    """Legacy: Start all repos with orchestration."""
    orchestrator = get_orchestrator()
    return await orchestrator.start_all_services()


async def initialize_cross_repo_orchestration() -> None:
    """
    Initialize cross-repo orchestration.

    This is called by run_supervisor.py during startup.
    """
    try:
        orchestrator = get_orchestrator()
        results = await orchestrator.start_all_services()

        # Initialize advanced training coordinator if Reactor-Core available
        if results.get("reactor-core"):
            logger.info("Initializing Advanced Training Coordinator...")
            try:
                from backend.intelligence.advanced_training_coordinator import (
                    AdvancedTrainingCoordinator
                )
                coordinator = await AdvancedTrainingCoordinator.create()
                logger.info("✅ Advanced Training Coordinator initialized")
            except Exception as e:
                logger.warning(f"Advanced Training Coordinator initialization failed: {e}")

    except Exception as e:
        logger.error(f"Cross-repo orchestration error: {e}", exc_info=True)


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    "ProcessOrchestrator",
    "ManagedProcess",
    "ServiceDefinition",
    "ServiceStatus",
    "OrchestratorConfig",
    "get_orchestrator",
    "start_all_repos",
    "initialize_cross_repo_orchestration",
    "probe_jarvis_prime",
    "probe_reactor_core",
]
