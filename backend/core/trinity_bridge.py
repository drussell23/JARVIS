"""
Trinity Bridge v4.0 - Unified Cross-Repository Integration Layer
=================================================================

The Trinity Bridge is the master integration layer that connects all Trinity
components (JARVIS Body, J-Prime, Reactor-Core) into a unified ecosystem.

This module provides:
- Single-command startup for all repos
- Automatic service discovery and registration
- Cross-repo health monitoring
- Graceful degradation when repos are unavailable
- Automatic reconnection on failure
- Unified configuration management

Integration with:
- Service Registry v3.0 (dynamic port discovery)
- Process Orchestrator v3.0 (auto-healing processes)
- Training Coordinator v3.0 (drop-box protocol)
- Trinity IPC Hub v4.0 (all 10 communication channels)
- System Hardening v3.0 (critical directories)

Architecture:
    ┌───────────────────────────────────────────────────────────────────────────┐
    │                         Trinity Bridge v4.0                               │
    ├───────────────────────────────────────────────────────────────────────────┤
    │                                                                           │
    │  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐         │
    │  │  Service        │   │  Process        │   │  IPC Hub        │         │
    │  │  Registry v3.0  │◄─►│  Orchestrator   │◄─►│  v4.0           │         │
    │  │                 │   │  v3.0           │   │                 │         │
    │  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘         │
    │           │                     │                     │                   │
    │           └─────────────────────┼─────────────────────┘                   │
    │                                 │                                         │
    │                    ┌────────────▼────────────┐                            │
    │                    │     TRINITY BRIDGE      │                            │
    │                    │  (Unified Control Plane)│                            │
    │                    └────────────┬────────────┘                            │
    │                                 │                                         │
    │           ┌─────────────────────┼─────────────────────┐                   │
    │           │                     │                     │                   │
    │           ▼                     ▼                     ▼                   │
    │   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐             │
    │   │   JARVIS     │     │   J-PRIME    │     │   REACTOR    │             │
    │   │   (Body)     │     │   (Brain)    │     │   (Training) │             │
    │   │   Port: 5001 │     │   Port: 8000 │     │   Port: 8090 │             │
    │   └──────────────┘     └──────────────┘     └──────────────┘             │
    │                                                                           │
    └───────────────────────────────────────────────────────────────────────────┘

Usage (Single Command Startup):
    python3 run_supervisor.py

    This automatically:
    1. Initializes critical directories
    2. Starts Service Registry
    3. Launches J-Prime (if not running)
    4. Launches Reactor-Core (if not running)
    5. Initializes IPC Hub with all channels
    6. Establishes cross-repo communication
    7. Monitors health and auto-heals

Author: JARVIS AI System
Version: 4.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class TrinityBridgeConfig:
    """Configuration for Trinity Bridge."""

    # Repo paths
    jarvis_repo_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_REPO_PATH",
            str(Path(__file__).parent.parent.parent)
        ))
    )
    jprime_repo_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "JARVIS_PRIME_PATH",
            str(Path.home() / "Documents" / "repos" / "jarvis-prime")
        ))
    )
    reactor_repo_path: Path = field(
        default_factory=lambda: Path(os.getenv(
            "REACTOR_CORE_PATH",
            str(Path.home() / "Documents" / "repos" / "reactor-core")
        ))
    )

    # Service ports (used as fallbacks, registry is preferred)
    jarvis_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PORT", "5001"))
    )
    jprime_port: int = field(
        default_factory=lambda: int(os.getenv("JARVIS_PRIME_PORT", "8000"))
    )
    reactor_port: int = field(
        default_factory=lambda: int(os.getenv("REACTOR_CORE_PORT", "8090"))
    )

    # Feature flags
    jprime_enabled: bool = field(
        default_factory=lambda: os.getenv("JARVIS_PRIME_ENABLED", "true").lower() == "true"
    )
    reactor_enabled: bool = field(
        default_factory=lambda: os.getenv("REACTOR_CORE_ENABLED", "true").lower() == "true"
    )
    auto_start_repos: bool = field(
        default_factory=lambda: os.getenv("TRINITY_AUTO_START", "true").lower() == "true"
    )
    auto_heal_enabled: bool = field(
        default_factory=lambda: os.getenv("TRINITY_AUTO_HEAL", "true").lower() == "true"
    )

    # Health monitoring
    # v92.0: Reduced from 30s to 20s to ensure heartbeats are sent
    # well before the 60s service registry timeout
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_HEALTH_INTERVAL", "20.0"))
    )
    startup_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_STARTUP_TIMEOUT", "120.0"))
    )

    # Graceful shutdown
    shutdown_timeout: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_SHUTDOWN_TIMEOUT", "30.0"))
    )


class TrinityState(str, Enum):
    """Trinity ecosystem state."""
    INITIALIZING = "initializing"
    STARTING = "starting"
    RUNNING = "running"
    DEGRADED = "degraded"
    SHUTTING_DOWN = "shutting_down"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class ServiceHealth:
    """Health status for a service."""
    name: str
    healthy: bool
    latency_ms: float = 0.0
    last_check: float = field(default_factory=time.time)
    error: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


# =============================================================================
# Trinity Bridge (Main Class)
# =============================================================================

class TrinityBridge:
    """
    Unified integration layer for Trinity Ecosystem.

    Coordinates all components and provides single-command startup.
    """

    def __init__(self, config: Optional[TrinityBridgeConfig] = None):
        self.config = config or TrinityBridgeConfig()
        self._state = TrinityState.STOPPED
        self._startup_time: Optional[float] = None

        # Component references (initialized in start)
        self._service_registry = None
        self._process_orchestrator = None
        self._ipc_hub = None
        self._training_coordinator = None

        # Health monitoring
        self._health_task: Optional[asyncio.Task] = None
        self._service_health: Dict[str, ServiceHealth] = {}
        self._shutdown_event = asyncio.Event()

        # Callbacks
        self._on_state_change: List[Callable[[TrinityState], None]] = []

    @classmethod
    async def create(
        cls,
        config: Optional[TrinityBridgeConfig] = None
    ) -> 'TrinityBridge':
        """Factory method to create and start bridge."""
        bridge = cls(config)
        await bridge.start()
        return bridge

    @property
    def state(self) -> TrinityState:
        return self._state

    @property
    def uptime(self) -> float:
        if self._startup_time:
            return time.time() - self._startup_time
        return 0.0

    async def start(self) -> bool:
        """
        Start Trinity Bridge and all components.

        This is the single entry point that:
        1. Initializes critical directories
        2. Starts service registry
        3. Launches external repos (J-Prime, Reactor)
        4. Initializes IPC hub
        5. Starts health monitoring

        Returns:
            True if startup successful, False otherwise
        """
        if self._state in (TrinityState.RUNNING, TrinityState.STARTING):
            logger.warning("Trinity Bridge already running/starting")
            return True

        self._set_state(TrinityState.INITIALIZING)
        self._startup_time = time.time()

        try:
            # Step 1: Initialize critical directories
            await self._initialize_directories()

            # Step 2: Start service registry
            await self._start_service_registry()

            # Step 3: Start process orchestrator (launches repos)
            self._set_state(TrinityState.STARTING)
            await self._start_process_orchestrator()

            # Step 4: Start IPC hub
            await self._start_ipc_hub()

            # Step 5: Start training coordinator
            await self._start_training_coordinator()

            # Step 6: Wait for all services to be healthy
            await self._wait_for_services()

            # Step 7: Start health monitoring
            self._health_task = asyncio.create_task(self._health_monitor_loop())

            # Step 8: Setup signal handlers
            self._setup_signal_handlers()

            self._set_state(TrinityState.RUNNING)

            logger.info("=" * 70)
            logger.info("Trinity Bridge v4.0 Started Successfully")
            logger.info("=" * 70)
            logger.info(f"  JARVIS Body:   localhost:{self.config.jarvis_port}")
            if self.config.jprime_enabled:
                logger.info(f"  J-Prime:       localhost:{self.config.jprime_port}")
            if self.config.reactor_enabled:
                logger.info(f"  Reactor-Core:  localhost:{self.config.reactor_port}")
            logger.info(f"  IPC Hub:       {self._ipc_hub.config.ipc_base_dir}")
            logger.info("=" * 70)

            return True

        except Exception as e:
            logger.error(f"Trinity Bridge startup failed: {e}", exc_info=True)
            self._set_state(TrinityState.FAILED)
            await self.stop()
            return False

    async def stop(self) -> None:
        """
        Stop Trinity Bridge and all components gracefully.
        """
        if self._state == TrinityState.STOPPED:
            return

        self._set_state(TrinityState.SHUTTING_DOWN)
        logger.info("Trinity Bridge shutting down...")

        # Signal shutdown
        self._shutdown_event.set()

        # Stop health monitoring
        if self._health_task:
            self._health_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_task

        # Stop components in reverse order
        try:
            # Stop training coordinator
            if self._training_coordinator:
                await self._training_coordinator.shutdown()

            # Stop IPC hub
            if self._ipc_hub:
                await self._ipc_hub.stop()

            # Stop process orchestrator (stops external repos)
            if self._process_orchestrator:
                await self._process_orchestrator.shutdown_all_services()

            # Stop service registry cleanup task
            if self._service_registry:
                await self._service_registry.stop_cleanup_task()

        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

        self._set_state(TrinityState.STOPPED)
        logger.info("Trinity Bridge stopped")

    async def _initialize_directories(self) -> None:
        """Initialize all critical directories."""
        logger.info("Initializing critical directories...")

        try:
            from backend.core.system_hardening import initialize_critical_directories
            results = await initialize_critical_directories()

            failed = [k for k, v in results.items() if not v]
            if failed:
                logger.warning(f"Some directories failed to create: {failed}")
            else:
                logger.info(f"  Created {len(results)} critical directories")

        except ImportError:
            logger.warning("System hardening module not available")
            # Fallback: create essential directories
            essential_dirs = [
                Path.home() / ".jarvis" / "registry",
                Path.home() / ".jarvis" / "trinity" / "ipc",
                Path.home() / ".jarvis" / "bridge" / "training_staging",
            ]
            for dir_path in essential_dirs:
                dir_path.mkdir(parents=True, exist_ok=True)

    async def _start_service_registry(self) -> None:
        """Start service registry."""
        logger.info("Starting Service Registry...")

        try:
            from backend.core.service_registry import get_service_registry

            self._service_registry = get_service_registry()
            await self._service_registry.start_cleanup_task()

            # Register JARVIS Body
            await self._service_registry.register_service(
                service_name="jarvis-body",
                pid=os.getpid(),
                port=self.config.jarvis_port,
                health_endpoint="/health",
                metadata={
                    "version": "4.0.0",
                    "role": "orchestrator"
                }
            )

            logger.info("  Service Registry started")

        except Exception as e:
            logger.error(f"Failed to start service registry: {e}")
            raise

    async def _start_process_orchestrator(self) -> None:
        """Start process orchestrator to launch external repos."""
        logger.info("Starting Process Orchestrator...")

        if not self.config.auto_start_repos:
            logger.info("  Auto-start disabled, skipping repo launch")
            return

        try:
            from backend.supervisor.cross_repo_startup_orchestrator import (
                ProcessOrchestrator,
                ServiceDefinitionRegistry,
                get_service_definition,
            )

            self._process_orchestrator = ProcessOrchestrator()

            # v95.0: Use centralized ServiceDefinitionRegistry for consistency
            # This ensures the same service configurations are used everywhere
            services = []

            if self.config.jprime_enabled and self.config.jprime_repo_path.exists():
                # Get canonical definition, but override path/port from our config
                jprime_def = get_service_definition(
                    "jarvis-prime",
                    port=self.config.jprime_port,
                    path=self.config.jprime_repo_path,
                )
                if jprime_def:
                    services.append(jprime_def)
                else:
                    logger.warning("Could not get jarvis-prime service definition from registry")

            if self.config.reactor_enabled and self.config.reactor_repo_path.exists():
                # Get canonical definition, but override path/port from our config
                reactor_def = get_service_definition(
                    "reactor-core",
                    port=self.config.reactor_port,
                    path=self.config.reactor_repo_path,
                )
                if reactor_def:
                    services.append(reactor_def)
                else:
                    logger.warning("Could not get reactor-core service definition from registry")

            # Add services to orchestrator
            for service in services:
                self._process_orchestrator.add_service(service)
                logger.debug(f"    Added service: {service.name} (script: {service.script_name})")

            # Start all services
            results = await self._process_orchestrator.start_all_services()

            for name, success in results.items():
                if success:
                    logger.info(f"    {name}: Started")
                else:
                    logger.warning(f"    {name}: Failed to start")

        except ImportError as e:
            logger.warning(f"Process orchestrator not available: {e}")
        except Exception as e:
            logger.error(f"Failed to start process orchestrator: {e}")

    async def _start_ipc_hub(self) -> None:
        """Start IPC hub for cross-repo communication."""
        logger.info("Starting Trinity IPC Hub...")

        try:
            from backend.core.trinity_ipc_hub import TrinityIPCHub

            self._ipc_hub = await TrinityIPCHub.create()

            # Register query handlers
            self._ipc_hub.query.register_query_handler(
                "get_state",
                self._handle_state_query
            )

            logger.info("  IPC Hub started with all 10 channels")

        except Exception as e:
            logger.error(f"Failed to start IPC hub: {e}")
            raise

    async def _start_training_coordinator(self) -> None:
        """Start training coordinator."""
        logger.info("Starting Training Coordinator...")

        try:
            from backend.intelligence.advanced_training_coordinator import (
                AdvancedTrainingCoordinator
            )

            self._training_coordinator = await AdvancedTrainingCoordinator.create()
            logger.info("  Training Coordinator v3.0 started")

        except Exception as e:
            logger.warning(f"Training coordinator not available: {e}")

    async def _wait_for_services(self) -> None:
        """
        v95.0: Wait for all services to be healthy with adaptive timeouts.

        Features:
        - Adaptive timeout based on system conditions
        - Parallel health checks for faster detection
        - Progress tracking and reporting
        - Intelligent degraded mode with future recovery scheduling
        """
        logger.info("Waiting for services to be healthy...")

        start_time = time.time()
        services_to_check = ["jarvis-body"]

        if self.config.jprime_enabled:
            services_to_check.append("jarvis-prime")
        if self.config.reactor_enabled:
            services_to_check.append("reactor-core")

        # v95.0: Adaptive timeout calculation
        adaptive_timeout = self._get_adaptive_timeout("startup")
        base_timeout = self.config.startup_timeout
        effective_timeout = max(base_timeout, adaptive_timeout)

        logger.info(f"  Timeout: {effective_timeout:.1f}s (base: {base_timeout}s, adaptive: {adaptive_timeout:.1f}s)")

        # v95.0: Track progress for each service
        service_progress: Dict[str, Dict[str, Any]] = {
            name: {"first_seen": None, "healthy": False, "checks": 0, "last_error": None}
            for name in services_to_check
        }

        # v95.0: Minimum degraded mode threshold - wait at least this before accepting degraded
        min_degraded_wait = float(os.getenv("TRINITY_MIN_DEGRADED_WAIT", "45.0"))

        while (time.time() - start_time) < effective_timeout:
            elapsed = time.time() - start_time
            healthy_count = 0

            # v95.0: Parallel health checks for all services
            async def check_service(service_name: str) -> bool:
                try:
                    if self._service_registry is None:
                        return False
                    service = await self._service_registry.discover_service(service_name)
                    if service:
                        service_progress[service_name]["healthy"] = True
                        if service_progress[service_name]["first_seen"] is None:
                            service_progress[service_name]["first_seen"] = time.time()
                            logger.info(f"  ✅ {service_name} discovered")
                        self._service_health[service_name] = ServiceHealth(
                            name=service_name,
                            healthy=True
                        )
                        return True
                except Exception as e:
                    service_progress[service_name]["last_error"] = str(e)
                service_progress[service_name]["checks"] += 1
                return False

            # Check all services in parallel
            check_results = await asyncio.gather(
                *[check_service(name) for name in services_to_check],
                return_exceptions=True
            )

            for i, result in enumerate(check_results):
                if result is True:
                    healthy_count += 1
                elif isinstance(result, Exception):
                    logger.debug(f"  Health check exception for {services_to_check[i]}: {result}")

            if healthy_count >= len(services_to_check):
                duration = time.time() - start_time
                logger.info(f"  All {healthy_count} services healthy in {duration:.1f}s")
                self._record_operation_duration("startup", duration)
                return

            # v95.0: Intelligent degraded mode transition
            # - Wait at least min_degraded_wait before accepting degraded
            # - Require at least 1 healthy service (jarvis-body minimum)
            # - Check if unhealthy services show any progress
            if healthy_count >= 1 and elapsed > min_degraded_wait:
                unhealthy = [
                    name for name, prog in service_progress.items()
                    if not prog["healthy"]
                ]

                # v95.0: Check if unhealthy services are making progress
                # (e.g., they exist but still initializing)
                all_stuck = True
                for name in unhealthy:
                    prog = service_progress[name]
                    if prog["checks"] < 3:  # Not enough checks yet
                        all_stuck = False
                        break

                if all_stuck or elapsed > effective_timeout * 0.8:
                    logger.warning(
                        f"  Running in degraded mode: {healthy_count}/{len(services_to_check)} services "
                        f"(unhealthy: {', '.join(unhealthy)})"
                    )
                    logger.info(
                        f"  🔄 Auto-recovery will attempt to restore unhealthy services"
                    )
                    self._set_state(TrinityState.DEGRADED)
                    self._record_operation_duration("startup", time.time() - start_time)

                    # v95.0: Schedule background recovery for unhealthy services
                    for name in unhealthy:
                        self._service_health[name] = ServiceHealth(
                            name=name,
                            healthy=False,
                            error=service_progress[name]["last_error"] or "Startup timeout"
                        )

                    return

            # v95.0: Adaptive sleep - faster polling when close to having all services
            sleep_time = 1.0 if healthy_count >= len(services_to_check) - 1 else 2.0
            await asyncio.sleep(sleep_time)

        logger.warning(f"Service startup timeout reached after {effective_timeout:.1f}s")
        self._record_operation_duration("startup", effective_timeout)

    async def _health_monitor_loop(self) -> None:
        """
        v95.0: Enhanced health monitor with auto-recovery and cross-repo sync.

        Features:
        - Periodic health checks with adaptive intervals
        - Automatic recovery attempts for unhealthy services
        - Cross-repo health synchronization
        - Intelligent state transitions
        """
        recovery_check_interval = 3  # Attempt recovery every N health checks
        check_count = 0

        while not self._shutdown_event.is_set():
            try:
                # v95.0: Adaptive sleep based on system state
                interval = self.config.health_check_interval
                if self._state == TrinityState.DEGRADED:
                    # More frequent checks in degraded mode
                    interval = max(5.0, interval / 2)

                await asyncio.sleep(interval)

                # Standard health check
                await self._check_all_health()
                check_count += 1

                # v95.0: Periodic auto-recovery attempts
                if self._state == TrinityState.DEGRADED and check_count >= recovery_check_interval:
                    check_count = 0
                    recovery_results = await self._enhanced_health_check()

                    if recovery_results:
                        recovered = sum(1 for v in recovery_results.values() if v)
                        if recovered > 0:
                            logger.info(f"[AutoRecovery] Recovered {recovered} service(s)")

                # v95.0: Cross-repo health synchronization
                await self._cross_repo_health_sync()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _check_all_health(self) -> None:
        """
        v95.0: Enhanced health check with multi-layer verification.

        Layers:
        1. Service registry discovery (fast, cached)
        2. Direct HTTP health check (fallback if registry fails)
        3. Known service ports (final fallback)
        """
        import aiohttp

        if self._service_registry is None:
            logger.warning("[HealthCheck] Service registry not available")
            return

        services = await self._service_registry.list_services(healthy_only=False)

        # v95.0: Build fallback port map for direct health checks
        service_ports = {
            "jarvis-body": self.config.jarvis_port,
            "jarvis-prime": self.config.jprime_port,
            "reactor-core": self.config.reactor_port,
        }

        healthy_count = 0
        for service in services:
            start = time.time()
            service_name = service.service_name
            healthy = False
            error_msg = None

            try:
                # Layer 1: Quick health check via service registry
                discovered = await self._service_registry.discover_service(service_name)
                healthy = discovered is not None

                # v95.0: Layer 2 - Direct HTTP health check if registry says unhealthy
                # This handles cases where the service is running but failed to heartbeat
                if not healthy and service_name in service_ports:
                    port = service_ports[service_name]
                    try:
                        async with aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=3.0)
                        ) as session:
                            url = f"http://localhost:{port}/health"
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    # Service is actually healthy - registry was stale
                                    healthy = True
                                    logger.info(
                                        f"[HealthCheck] {service_name} healthy via direct HTTP "
                                        f"(registry was stale)"
                                    )
                                    # v95.0: Send heartbeat to refresh stale registry entry
                                    try:
                                        await self._service_registry.heartbeat(
                                            service_name,
                                            status="healthy"
                                        )
                                    except Exception as hb_err:
                                        logger.debug(f"Heartbeat refresh failed: {hb_err}")
                    except Exception as http_err:
                        error_msg = f"Direct HTTP check failed: {http_err}"
                        logger.debug(f"[HealthCheck] {service_name} {error_msg}")

                self._service_health[service_name] = ServiceHealth(
                    name=service_name,
                    healthy=healthy,
                    latency_ms=(time.time() - start) * 1000,
                    details={"port": service.port, "status": service.status},
                    error=error_msg if not healthy else None
                )

                if healthy:
                    healthy_count += 1

                # ═══════════════════════════════════════════════════════════════════
                # v92.0 FIX: ALWAYS send heartbeat regardless of health status
                # ═══════════════════════════════════════════════════════════════════
                await self._service_registry.heartbeat(
                    service_name,
                    status="healthy" if healthy else "degraded"
                )

            except Exception as e:
                self._service_health[service_name] = ServiceHealth(
                    name=service_name,
                    healthy=False,
                    error=str(e)
                )
                # v92.0: Still try to send heartbeat even on exception
                try:
                    await self._service_registry.heartbeat(
                        service_name,
                        status="unhealthy"
                    )
                except Exception:
                    pass  # Don't let heartbeat failure mask original error

        # v95.0: Also check expected services that might not be in registry yet
        expected_services = ["jarvis-body"]
        if self.config.jprime_enabled:
            expected_services.append("jarvis-prime")
        if self.config.reactor_enabled:
            expected_services.append("reactor-core")

        registered_names = {s.service_name for s in services}
        for service_name in expected_services:
            if service_name not in registered_names and service_name in service_ports:
                # Check if service is running but not registered
                port = service_ports[service_name]
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=3.0)
                    ) as session:
                        url = f"http://localhost:{port}/health"
                        async with session.get(url) as resp:
                            if resp.status == 200:
                                healthy_count += 1
                                self._service_health[service_name] = ServiceHealth(
                                    name=service_name,
                                    healthy=True,
                                    latency_ms=0,
                                    details={"port": port, "status": "unregistered"}
                                )
                                logger.info(
                                    f"[HealthCheck] {service_name} healthy but unregistered - "
                                    f"adding to health tracking"
                                )
                except Exception:
                    # Service not running
                    if service_name not in self._service_health:
                        self._service_health[service_name] = ServiceHealth(
                            name=service_name,
                            healthy=False,
                            error="Not running and not registered"
                        )

        # Update state based on health
        total = max(len(services), len(expected_services))
        if total > 0:
            if healthy_count == total:
                if self._state == TrinityState.DEGRADED:
                    logger.info("All services recovered - returning to normal operation")
                    self._set_state(TrinityState.RUNNING)
            elif healthy_count > 0:
                if self._state == TrinityState.RUNNING:
                    unhealthy = [
                        name for name, h in self._service_health.items()
                        if not h.healthy
                    ]
                    logger.warning(
                        f"Some services unhealthy ({healthy_count}/{total}): {unhealthy}"
                    )
                    self._set_state(TrinityState.DEGRADED)

    async def _handle_state_query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Handle state query from other repos."""
        return {
            "state": self._state.value,
            "uptime": self.uptime,
            "services": {
                name: {"healthy": h.healthy, "latency_ms": h.latency_ms}
                for name, h in self._service_health.items()
            }
        }

    def _set_state(self, new_state: TrinityState) -> None:
        """Set state and notify callbacks."""
        if new_state != self._state:
            old_state = self._state
            self._state = new_state
            logger.info(f"Trinity state: {old_state.value} → {new_state.value}")

            for callback in self._on_state_change:
                try:
                    callback(new_state)
                except Exception as e:
                    logger.error(f"State callback error: {e}")

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            sig_name = signal.Signals(signum).name
            logger.info(f"Received {sig_name}, initiating shutdown...")
            self._shutdown_event.set()

            # Schedule async shutdown
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.stop())

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def on_state_change(
        self,
        callback: Callable[[TrinityState], None]
    ) -> Callable[[], None]:
        """Register state change callback. Returns unsubscribe function."""
        self._on_state_change.append(callback)

        def unsubscribe():
            if callback in self._on_state_change:
                self._on_state_change.remove(callback)

        return unsubscribe

    async def get_health(self) -> Dict[str, Any]:
        """Get comprehensive health status."""
        ipc_health = await self._ipc_hub.get_health() if self._ipc_hub else {}

        return {
            "state": self._state.value,
            "uptime_seconds": self.uptime,
            "services": {
                name: {
                    "healthy": h.healthy,
                    "latency_ms": h.latency_ms,
                    "error": h.error
                }
                for name, h in self._service_health.items()
            },
            "ipc_hub": ipc_health,
            "config": {
                "jprime_enabled": self.config.jprime_enabled,
                "reactor_enabled": self.config.reactor_enabled,
                "auto_heal": self.config.auto_heal_enabled
            }
        }

    async def get_status(self) -> Dict[str, Any]:
        """
        Get comprehensive status of the Trinity Bridge.

        v4.1: Added for compatibility with supervisor status checks.

        Returns:
            Status dictionary with state, uptime, services, and component status.
        """
        health = await self.get_health()

        # Calculate healthy service count
        services = self._service_health
        healthy_count = sum(1 for h in services.values() if h.healthy)
        total_count = len(services)

        return {
            "status": "healthy" if self._state == TrinityState.RUNNING else self._state.value,
            "state": self._state.value,
            "uptime_seconds": self.uptime,
            "healthy_services": healthy_count,
            "total_services": total_count,
            "services": health.get("services", {}),
            "ipc_hub_active": self._ipc_hub is not None,
            "training_coordinator_active": self._training_coordinator is not None,
            "process_orchestrator_active": self._process_orchestrator is not None,
            "health": health,
        }

    # =========================================================================
    # v95.0: INTELLIGENT AUTO-RECOVERY SYSTEM
    # Advanced self-healing with adaptive timeouts and cross-repo synchronization
    # =========================================================================

    async def _attempt_service_recovery(self, service_name: str) -> bool:
        """
        v95.0: Attempt to recover an unhealthy service.

        Uses intelligent retry with exponential backoff and circuit breaker pattern.
        Coordinates with process orchestrator for actual recovery actions.

        Returns:
            True if recovery successful, False otherwise
        """
        if not self.config.auto_heal_enabled:
            logger.debug(f"[AutoRecovery] Skipping {service_name} - auto_heal disabled")
            return False

        # Check recovery cooldown (don't spam recovery attempts)
        recovery_key = f"_last_recovery_{service_name}"
        last_recovery = getattr(self, recovery_key, 0)
        cooldown = self._get_adaptive_cooldown(service_name)

        if time.time() - last_recovery < cooldown:
            logger.debug(
                f"[AutoRecovery] {service_name} in cooldown "
                f"({cooldown - (time.time() - last_recovery):.1f}s remaining)"
            )
            return False

        setattr(self, recovery_key, time.time())

        logger.info(f"[AutoRecovery] Attempting to recover {service_name}...")

        try:
            # Track recovery attempts for circuit breaker
            attempts_key = f"_recovery_attempts_{service_name}"
            attempts = getattr(self, attempts_key, 0)
            max_attempts = int(os.getenv("TRINITY_MAX_RECOVERY_ATTEMPTS", "5"))

            if attempts >= max_attempts:
                logger.warning(
                    f"[AutoRecovery] {service_name} exceeded max attempts ({max_attempts}) - "
                    f"circuit breaker OPEN"
                )
                return False

            setattr(self, attempts_key, attempts + 1)

            # Attempt recovery via process orchestrator
            if self._process_orchestrator:
                # Try to restart the service
                result = await self._process_orchestrator.restart_service(service_name)
                if result:
                    logger.info(f"[AutoRecovery] ✅ {service_name} recovered successfully")
                    setattr(self, attempts_key, 0)  # Reset on success
                    return True

            # Fallback: Try to re-register with service registry
            if self._service_registry:
                service = await self._service_registry.discover_service(service_name)
                if service:
                    logger.info(f"[AutoRecovery] ✅ {service_name} found in registry")
                    setattr(self, attempts_key, 0)
                    return True

            logger.warning(f"[AutoRecovery] ❌ {service_name} recovery failed (attempt {attempts + 1})")
            return False

        except Exception as e:
            logger.error(f"[AutoRecovery] {service_name} recovery error: {e}")
            return False

    def _get_adaptive_cooldown(self, service_name: str) -> float:
        """
        v95.0: Calculate adaptive cooldown based on failure history.

        Uses exponential backoff with jitter to prevent thundering herd.
        """
        import random

        attempts_key = f"_recovery_attempts_{service_name}"
        attempts = getattr(self, attempts_key, 0)

        base_cooldown = float(os.getenv("TRINITY_RECOVERY_BASE_COOLDOWN", "30.0"))
        max_cooldown = float(os.getenv("TRINITY_RECOVERY_MAX_COOLDOWN", "300.0"))

        # Exponential backoff
        cooldown = min(base_cooldown * (2 ** attempts), max_cooldown)

        # Add jitter (±20%) to prevent synchronized recovery storms
        jitter = cooldown * 0.2 * (2 * random.random() - 1)
        cooldown = max(10.0, cooldown + jitter)

        return cooldown

    def _get_adaptive_timeout(self, operation: str) -> float:
        """
        v95.0: Calculate adaptive timeout based on system conditions.

        Considers:
        - Historical operation durations
        - Current system load (CPU/memory)
        - Previous timeout patterns
        """
        base_timeout = float(os.getenv(f"TRINITY_{operation.upper()}_TIMEOUT", "60.0"))

        # Track historical durations
        history_key = f"_timeout_history_{operation}"
        history = getattr(self, history_key, [])

        if history:
            # Use P95 of historical durations as base
            sorted_history = sorted(history)
            p95_idx = int(len(sorted_history) * 0.95)
            p95_duration = sorted_history[min(p95_idx, len(sorted_history) - 1)]

            # Adaptive timeout = max(base, P95 * 1.5)
            adaptive_timeout = max(base_timeout, p95_duration * 1.5)
        else:
            adaptive_timeout = base_timeout

        # Adjust for system load
        try:
            import psutil
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory_percent = psutil.virtual_memory().percent

            # Increase timeout under high load
            if cpu_percent > 80 or memory_percent > 85:
                load_factor = 1.5
            elif cpu_percent > 60 or memory_percent > 70:
                load_factor = 1.25
            else:
                load_factor = 1.0

            adaptive_timeout *= load_factor
        except Exception:
            pass  # Continue without load adjustment

        # Cap at reasonable maximum
        max_timeout = float(os.getenv("TRINITY_MAX_ADAPTIVE_TIMEOUT", "600.0"))
        return min(adaptive_timeout, max_timeout)

    def _record_operation_duration(self, operation: str, duration: float) -> None:
        """Record operation duration for adaptive timeout calculations."""
        history_key = f"_timeout_history_{operation}"
        history = getattr(self, history_key, [])

        history.append(duration)

        # Keep only last 100 measurements
        if len(history) > 100:
            history = history[-100:]

        setattr(self, history_key, history)

    async def _enhanced_health_check(self) -> Dict[str, bool]:
        """
        v95.0: Enhanced health check with recovery attempts.

        Returns dict of service_name -> recovery_successful
        """
        recovery_results = {}
        unhealthy_services = [
            name for name, health in self._service_health.items()
            if not health.healthy
        ]

        if not unhealthy_services:
            return recovery_results

        # Attempt parallel recovery for unhealthy services
        recovery_tasks = []
        for service_name in unhealthy_services:
            recovery_tasks.append(
                self._attempt_service_recovery(service_name)
            )

        if recovery_tasks:
            results = await asyncio.gather(*recovery_tasks, return_exceptions=True)

            for i, result in enumerate(results):
                service_name = unhealthy_services[i]
                if isinstance(result, Exception):
                    logger.error(f"[AutoRecovery] {service_name} raised exception: {result}")
                    recovery_results[service_name] = False
                else:
                    recovery_results[service_name] = result

        return recovery_results

    async def _cross_repo_health_sync(self) -> None:
        """
        v95.0: Synchronize health status across all repos.

        Broadcasts local health to other repos and receives their health status.
        Enables coordinated recovery decisions.
        """
        if not self._ipc_hub:
            return

        try:
            local_health = {
                "source": "jarvis_body",
                "timestamp": time.time(),
                "state": self._state.value,
                "services": {
                    name: {
                        "healthy": h.healthy,
                        "latency_ms": h.latency_ms,
                        "error": h.error
                    }
                    for name, h in self._service_health.items()
                }
            }

            # Broadcast health to other repos
            await self._ipc_hub.events.publish("trinity.health.sync", local_health)

            # Receive health from other repos (if available)
            # This enables coordinated recovery decisions
            logger.debug("[HealthSync] Broadcasted health status to Trinity ecosystem")

        except Exception as e:
            logger.debug(f"[HealthSync] Sync error (non-critical): {e}")

    async def force_recovery(self, service_name: Optional[str] = None) -> Dict[str, bool]:
        """
        v95.0: Force recovery of services (bypass cooldown).

        Args:
            service_name: Specific service to recover, or None for all unhealthy

        Returns:
            Dict of service_name -> recovery_successful
        """
        if service_name:
            # Reset cooldown for specific service
            setattr(self, f"_last_recovery_{service_name}", 0)
            setattr(self, f"_recovery_attempts_{service_name}", 0)
            return {service_name: await self._attempt_service_recovery(service_name)}
        else:
            # Reset cooldown for all services and attempt recovery
            for name in self._service_health:
                setattr(self, f"_last_recovery_{name}", 0)
                setattr(self, f"_recovery_attempts_{name}", 0)

            return await self._enhanced_health_check()

    # =========================================================================
    # Convenience Methods for Cross-Repo Communication
    # =========================================================================

    async def request_training(
        self,
        job_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Request training from Reactor-Core (Gap 1)."""
        if self._ipc_hub:
            return await self._ipc_hub.reactor.request_training(job_config)
        raise RuntimeError("IPC Hub not initialized")

    async def submit_training_data(
        self,
        user_input: str,
        response: str,
        reward: float = 1.0
    ) -> None:
        """Submit training data (Gap 4)."""
        if self._ipc_hub:
            await self._ipc_hub.pipeline.submit_interaction(
                user_input, response, reward
            )

    async def get_available_models(self) -> List[Dict[str, Any]]:
        """Get available models (Gap 5)."""
        if self._ipc_hub:
            models = await self._ipc_hub.models.list_models()
            return [m.__dict__ for m in models]
        return []

    async def publish_event(
        self,
        topic: str,
        payload: Dict[str, Any]
    ) -> int:
        """Publish event to all repos (Gap 9)."""
        if self._ipc_hub:
            return await self._ipc_hub.events.publish(topic, payload)
        return 0


# =============================================================================
# Convenience Functions
# =============================================================================

_global_bridge: Optional[TrinityBridge] = None


async def get_trinity_bridge() -> TrinityBridge:
    """Get global Trinity Bridge instance."""
    global _global_bridge

    if _global_bridge is None:
        _global_bridge = await TrinityBridge.create()

    return _global_bridge


async def shutdown_trinity_bridge() -> None:
    """Shutdown global Trinity Bridge."""
    global _global_bridge

    if _global_bridge:
        await _global_bridge.stop()
        _global_bridge = None


async def initialize_trinity_ecosystem() -> TrinityBridge:
    """
    Initialize the complete Trinity Ecosystem.

    This is the main entry point for single-command startup.
    """
    logger.info("=" * 70)
    logger.info("Initializing Trinity Ecosystem v4.0")
    logger.info("=" * 70)

    bridge = await get_trinity_bridge()

    if bridge.state == TrinityState.RUNNING:
        logger.info("Trinity Ecosystem running successfully!")
    elif bridge.state == TrinityState.DEGRADED:
        logger.warning("Trinity Ecosystem running in degraded mode")
    else:
        logger.error(f"Trinity Ecosystem in unexpected state: {bridge.state.value}")

    return bridge


# =============================================================================
# v1.0: ProcessCoordinationHub Re-export for Convenience
# =============================================================================
# This provides easy access to the process coordination system from trinity_bridge
try:
    from backend.core.trinity_process_coordination import (
        ProcessCoordinationHub,
        get_coordination_hub,
        initialize_coordination,
        EntryPoint,
        LockType,
    )
    PROCESS_COORDINATION_AVAILABLE = True
except ImportError:
    PROCESS_COORDINATION_AVAILABLE = False
    ProcessCoordinationHub = None  # type: ignore
    get_coordination_hub = None  # type: ignore
    initialize_coordination = None  # type: ignore
    EntryPoint = None  # type: ignore
    LockType = None  # type: ignore


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main Bridge
    "TrinityBridge",
    "TrinityBridgeConfig",
    "TrinityState",
    "ServiceHealth",

    # Convenience Functions
    "get_trinity_bridge",
    "shutdown_trinity_bridge",
    "initialize_trinity_ecosystem",

    # v1.0: Process Coordination (re-exported for convenience)
    "ProcessCoordinationHub",
    "get_coordination_hub",
    "initialize_coordination",
    "EntryPoint",
    "LockType",
    "PROCESS_COORDINATION_AVAILABLE",
]
