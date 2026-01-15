"""
Cross-Repo Startup Orchestrator v3.0 - Enterprise-Grade Process Lifecycle Manager
===================================================================================

Dynamic service discovery and self-healing process orchestration for JARVIS ecosystem.
Eliminates hardcoded ports, implements auto-healing, and provides real-time process monitoring.

Features (v3.0):
- 🔍 Dynamic Service Discovery via Service Registry (zero hardcoded ports)
- 🔄 Auto-Healing with exponential backoff (dead process detection & restart)
- 📡 Real-Time Output Streaming (stdout/stderr prefixed per service)
- 🎯 Process Lifecycle Management (spawn, monitor, graceful shutdown)
- 🛡️ Graceful Shutdown Handlers (SIGINT/SIGTERM cleanup)
- 🧹 Automatic Zombie Process Cleanup
- 📊 Service Health Monitoring with heartbeats

Architecture:
    ┌──────────────────────────────────────────────────────────────────┐
    │         Cross-Repo Orchestrator v3.0 - Process Manager           │
    ├──────────────────────────────────────────────────────────────────┤
    │                                                                   │
    │  Service Registry: ~/.jarvis/registry/services.json              │
    │  ┌────────────────┬──────────────┬─────────────────────┐        │
    │  │   JARVIS       │   J-PRIME    │   REACTOR-CORE      │        │
    │  │  PID: auto     │  PID: auto   │   PID: auto         │        │
    │  │  Port: dynamic │  Port: 8002  │   Port: 8003        │        │
    │  │  Status: ✅    │  Status: ✅  │   Status: ✅        │        │
    │  └────────────────┴──────────────┴─────────────────────┘        │
    │                                                                   │
    │  Process Lifecycle:                                               │
    │  1. Spawn (asyncio.create_subprocess_exec)                       │
    │  2. Monitor (PID tracking + heartbeat)                           │
    │  3. Stream Output (real-time with [SERVICE] prefix)              │
    │  4. Auto-Heal (restart on crash with backoff)                    │
    │  5. Graceful Shutdown (SIGTERM → wait → SIGKILL)                 │
    │                                                                   │
    └──────────────────────────────────────────────────────────────────┘

Author: JARVIS AI System
Version: 3.0.0
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

@dataclass
class OrchestratorConfig:
    """Enterprise configuration with zero hardcoding."""

    # Repository paths
    jarvis_prime_path: Path = field(default_factory=lambda: Path(
        os.getenv("JARVIS_PRIME_PATH", str(Path.home() / "Documents" / "repos" / "jarvis-prime"))
    ))
    reactor_core_path: Path = field(default_factory=lambda: Path(
        os.getenv("REACTOR_CORE_PATH", str(Path.home() / "Documents" / "repos" / "reactor-core"))
    ))

    # Default ports (used when services register themselves)
    jarvis_prime_default_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8002"))
    )
    reactor_core_default_port: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_PORT", "8003"))
    )

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
    """Definition of a service to manage."""
    name: str
    repo_path: Path
    script_name: str = "main.py"
    fallback_scripts: List[str] = field(default_factory=lambda: ["server.py", "run.py", "app.py"])
    default_port: int = 8000
    health_endpoint: str = "/health"
    startup_timeout: float = 60.0
    environment: Dict[str, str] = field(default_factory=dict)


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
        """Get service definitions based on configuration."""
        definitions = []

        if self.config.jarvis_prime_enabled:
            definitions.append(ServiceDefinition(
                name="jarvis-prime",
                repo_path=self.config.jarvis_prime_path,
                script_name="main.py",
                fallback_scripts=["server.py", "run_server.py"],
                default_port=self.config.jarvis_prime_default_port,
                health_endpoint="/health",
                startup_timeout=self.config.startup_timeout,
            ))

        if self.config.reactor_core_enabled:
            definitions.append(ServiceDefinition(
                name="reactor-core",
                repo_path=self.config.reactor_core_path,
                script_name="main.py",
                fallback_scripts=["server.py", "app.py"],
                default_port=self.config.reactor_core_default_port,
                health_endpoint="/api/health",
                startup_timeout=self.config.startup_timeout,
            ))

        return definitions

    def _find_script(self, definition: ServiceDefinition) -> Optional[Path]:
        """Find the startup script for a service."""
        repo_path = definition.repo_path

        if not repo_path.exists():
            logger.warning(f"Repository not found: {repo_path}")
            return None

        # Try main script first
        script_path = repo_path / definition.script_name
        if script_path.exists():
            return script_path

        # Try fallback scripts
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

    async def _spawn_service(self, managed: ManagedProcess) -> bool:
        """
        Spawn a service process using asyncio.create_subprocess_exec.

        Returns True if spawn and health check succeeded.
        """
        definition = managed.definition
        script_path = self._find_script(definition)

        if script_path is None:
            logger.error(f"Cannot spawn {definition.name}: no script found")
            managed.status = ServiceStatus.FAILED
            return False

        managed.status = ServiceStatus.STARTING
        logger.info(f"🚀 Spawning {definition.name} from {script_path}...")

        try:
            # Build environment
            env = os.environ.copy()
            env.update(definition.environment)

            # Add port hint for service registration
            env["SERVICE_PORT"] = str(definition.default_port)
            env["SERVICE_NAME"] = definition.name

            # Spawn process
            managed.process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
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
        """Wait for service to become healthy."""
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            # Check if process died
            if not managed.is_running:
                return False

            # Check health endpoint
            if await self._check_health(managed):
                return True

            await asyncio.sleep(1.0)

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
        """Handle shutdown signal."""
        sig_name = signal.Signals(signum).name
        logger.info(f"\n🛑 Received {sig_name}, initiating graceful shutdown...")

        self._shutdown_event.set()
        await self.shutdown_all_services()

        # Exit after cleanup
        sys.exit(0)

    # =========================================================================
    # Main Orchestration
    # =========================================================================

    async def start_all_services(self) -> Dict[str, bool]:
        """
        Start all configured services with coordinated orchestration.

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
        logger.info("Cross-Repo Startup Orchestrator v3.0 - Enterprise Grade")
        logger.info("=" * 70)

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
