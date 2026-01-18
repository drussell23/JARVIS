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
    health_check_interval: float = field(
        default_factory=lambda: float(os.getenv("TRINITY_HEALTH_INTERVAL", "30.0"))
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
                ServiceDefinition,
                ServiceStatus
            )

            self._process_orchestrator = ProcessOrchestrator()

            # Define services to launch
            services = []

            if self.config.jprime_enabled and self.config.jprime_repo_path.exists():
                services.append(ServiceDefinition(
                    name="jarvis-prime",
                    repo_path=self.config.jprime_repo_path,
                    script_name="main.py",
                    default_port=self.config.jprime_port,
                    health_endpoint="/health"
                ))

            if self.config.reactor_enabled and self.config.reactor_repo_path.exists():
                services.append(ServiceDefinition(
                    name="reactor-core",
                    repo_path=self.config.reactor_repo_path,
                    script_name="main.py",
                    default_port=self.config.reactor_port,
                    health_endpoint="/api/health"
                ))

            # Add services to orchestrator
            for service in services:
                self._process_orchestrator.add_service(service)

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
        """Wait for all services to be healthy."""
        logger.info("Waiting for services to be healthy...")

        start_time = time.time()
        services_to_check = ["jarvis-body"]

        if self.config.jprime_enabled:
            services_to_check.append("jarvis-prime")
        if self.config.reactor_enabled:
            services_to_check.append("reactor-core")

        while (time.time() - start_time) < self.config.startup_timeout:
            healthy_count = 0

            for service_name in services_to_check:
                try:
                    service = await self._service_registry.discover_service(service_name)
                    if service:
                        healthy_count += 1
                        self._service_health[service_name] = ServiceHealth(
                            name=service_name,
                            healthy=True
                        )
                except Exception:
                    pass

            if healthy_count >= len(services_to_check):
                logger.info(f"  All {healthy_count} services healthy")
                return

            # Some services not ready, check if degraded mode is acceptable
            if healthy_count >= 1 and (time.time() - start_time) > 30:
                logger.warning(
                    f"  Running in degraded mode: {healthy_count}/{len(services_to_check)} services"
                )
                self._set_state(TrinityState.DEGRADED)
                return

            await asyncio.sleep(2.0)

        logger.warning("Service startup timeout reached")

    async def _health_monitor_loop(self) -> None:
        """Monitor health of all services."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._check_all_health()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _check_all_health(self) -> None:
        """Check health of all registered services."""
        services = await self._service_registry.list_services(healthy_only=False)

        healthy_count = 0
        for service in services:
            start = time.time()

            try:
                # Quick health check via service registry
                discovered = await self._service_registry.discover_service(
                    service.service_name
                )
                healthy = discovered is not None

                self._service_health[service.service_name] = ServiceHealth(
                    name=service.service_name,
                    healthy=healthy,
                    latency_ms=(time.time() - start) * 1000,
                    details={"port": service.port, "status": service.status}
                )

                if healthy:
                    healthy_count += 1
                    # ═══════════════════════════════════════════════════════════════════
                    # CRITICAL FIX: Send heartbeat on EVERY successful health check
                    # ═══════════════════════════════════════════════════════════════════
                    # Without this, services become "stale" after 60 seconds because
                    # their heartbeat timestamps never get updated after registration.
                    # ═══════════════════════════════════════════════════════════════════
                    await self._service_registry.heartbeat(
                        service.service_name,
                        status="healthy"
                    )

            except Exception as e:
                self._service_health[service.service_name] = ServiceHealth(
                    name=service.service_name,
                    healthy=False,
                    error=str(e)
                )

        # Update state based on health
        total = len(services)
        if total > 0:
            if healthy_count == total:
                if self._state == TrinityState.DEGRADED:
                    logger.info("All services recovered - returning to normal operation")
                    self._set_state(TrinityState.RUNNING)
            elif healthy_count > 0:
                if self._state == TrinityState.RUNNING:
                    logger.warning(f"Some services unhealthy ({healthy_count}/{total})")
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
]
