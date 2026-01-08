"""
Trinity Integrator - Unified Cross-Repo Integration Module.
=============================================================

The central nervous system that connects JARVIS Body, Prime, and Reactor-Core
into a cohesive Trinity architecture.

This module provides:
1. Single-command initialization of all Trinity components
2. Cross-repo communication via resilient IPC
3. Heartbeat-gated startup coordination
4. Port allocation with conflict resolution
5. Graceful degradation when components fail
6. Coordinated shutdown with orphan cleanup
7. Unified metrics and health monitoring

Usage:
    from backend.core.trinity_integrator import TrinityIntegrator

    async def main():
        integrator = TrinityIntegrator()

        # Start everything with one command
        success = await integrator.start()

        if success:
            # Monitor health
            health = await integrator.get_health()
            print(f"Trinity Status: {health}")

        # Graceful shutdown
        await integrator.stop()

Author: JARVIS Trinity v81.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Environment Helpers
# =============================================================================

def _env_str(key: str, default: str) -> str:
    return os.getenv(key, default)

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default

def _env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).lower() in ("true", "1", "yes", "on")


# =============================================================================
# Types and Enums
# =============================================================================

class TrinityState(str, Enum):
    """Overall Trinity system state."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    STARTING = "starting"
    READY = "ready"
    DEGRADED = "degraded"
    STOPPING = "stopping"
    STOPPED = "stopped"
    ERROR = "error"


class ComponentHealth(str, Enum):
    """Health status of a component."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ComponentStatus:
    """Status of a Trinity component."""
    name: str
    health: ComponentHealth
    online: bool
    last_heartbeat: Optional[float] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class TrinityHealth:
    """Overall Trinity system health."""
    state: TrinityState
    components: Dict[str, ComponentStatus]
    uptime_seconds: float
    last_check: float
    degraded_components: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# =============================================================================
# Trinity Integrator
# =============================================================================

class TrinityIntegrator:
    """
    Unified Trinity cross-repo integration manager.

    Coordinates startup, health monitoring, and shutdown across:
    - JARVIS Body (this repo)
    - JARVIS Prime (cognitive mind)
    - Reactor-Core (training nerves)

    Features:
    - Single-command startup with dependency ordering
    - Heartbeat-gated readiness verification
    - Circuit breaker protected IPC
    - Graceful degradation on component failure
    - Orphan process cleanup on startup
    - Coordinated phased shutdown
    """

    def __init__(
        self,
        enable_jprime: bool = True,
        enable_reactor: bool = True,
        startup_timeout: float = 120.0,
        health_check_interval: float = 30.0,
    ):
        """
        Initialize the Trinity integrator.

        Args:
            enable_jprime: Enable JARVIS Prime integration
            enable_reactor: Enable Reactor-Core integration
            startup_timeout: Max time to wait for components
            health_check_interval: Interval between health checks
        """
        self.enable_jprime = _env_bool("JARVIS_PRIME_ENABLED", enable_jprime)
        self.enable_reactor = _env_bool("REACTOR_CORE_ENABLED", enable_reactor)
        self.startup_timeout = _env_float("TRINITY_STARTUP_TIMEOUT", startup_timeout)
        self.health_check_interval = _env_float(
            "TRINITY_HEALTH_INTERVAL", health_check_interval
        )

        # State
        self._state = TrinityState.UNINITIALIZED
        self._start_time: Optional[float] = None
        self._lock = asyncio.Lock()

        # Components (lazy initialized)
        self._ipc_bus = None
        self._shutdown_manager = None
        self._port_manager = None
        self._startup_coordinator = None

        # Clients
        self._jprime_client = None
        self._reactor_client = None

        # Background tasks
        self._health_task: Optional[asyncio.Task] = None
        self._running = False

        # Callbacks
        self._on_state_change: List[Callable[[TrinityState, TrinityState], None]] = []
        self._on_component_change: List[Callable[[str, ComponentHealth], None]] = []

        logger.info(
            f"[TrinityIntegrator] Initialized "
            f"(jprime={self.enable_jprime}, reactor={self.enable_reactor})"
        )

    @property
    def state(self) -> TrinityState:
        return self._state

    @property
    def is_ready(self) -> bool:
        return self._state in (TrinityState.READY, TrinityState.DEGRADED)

    @property
    def uptime(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.time() - self._start_time

    # =========================================================================
    # Startup
    # =========================================================================

    async def start(self) -> bool:
        """
        Start the Trinity system.

        This is the single command that initializes everything:
        1. Cleanup orphan processes from previous runs
        2. Initialize resilient IPC bus
        3. Allocate ports for all components
        4. Start JARVIS Body heartbeat
        5. Wait for JARVIS Prime (if enabled)
        6. Wait for Reactor-Core (if enabled)
        7. Verify all heartbeats
        8. Start health monitoring

        Returns:
            True if startup successful (or degraded), False on failure
        """
        async with self._lock:
            if self._state != TrinityState.UNINITIALIZED:
                logger.warning(
                    f"[TrinityIntegrator] Cannot start in state {self._state.value}"
                )
                return False

            self._set_state(TrinityState.INITIALIZING)
            self._start_time = time.time()

            try:
                # Step 1: Orphan cleanup
                await self._cleanup_orphans()

                # Step 2: Initialize IPC
                await self._init_ipc()

                # Step 3: Port allocation
                await self._allocate_ports()

                # Step 4: Initialize shutdown manager
                await self._init_shutdown_manager()

                self._set_state(TrinityState.STARTING)

                # Step 5: Start JARVIS Body heartbeat
                await self._start_body_heartbeat()

                # Step 6: Wait for external components
                jprime_ok = True
                reactor_ok = True

                if self.enable_jprime:
                    jprime_ok = await self._wait_for_jprime()

                if self.enable_reactor:
                    reactor_ok = await self._wait_for_reactor()

                # Step 7: Determine final state
                if jprime_ok and reactor_ok:
                    self._set_state(TrinityState.READY)
                else:
                    self._set_state(TrinityState.DEGRADED)
                    logger.warning(
                        "[TrinityIntegrator] Starting in degraded mode "
                        f"(jprime={jprime_ok}, reactor={reactor_ok})"
                    )

                # Step 8: Start health monitoring
                self._running = True
                self._health_task = asyncio.create_task(self._health_loop())

                elapsed = time.time() - self._start_time
                logger.info(
                    f"[TrinityIntegrator] Started in {elapsed:.2f}s "
                    f"(state={self._state.value})"
                )

                return True

            except Exception as e:
                logger.error(f"[TrinityIntegrator] Startup failed: {e}")
                self._set_state(TrinityState.ERROR)
                return False

    async def _cleanup_orphans(self) -> None:
        """Clean up orphan processes from previous runs."""
        try:
            from backend.core.coordinated_shutdown import cleanup_orphan_processes

            terminated, failed = await cleanup_orphan_processes()

            if terminated > 0:
                logger.info(
                    f"[TrinityIntegrator] Cleaned up {terminated} orphan processes"
                )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Orphan cleanup failed: {e}")

    async def _init_ipc(self) -> None:
        """Initialize the resilient IPC bus."""
        from backend.core.trinity_ipc import get_resilient_trinity_ipc_bus

        self._ipc_bus = await get_resilient_trinity_ipc_bus()
        logger.debug("[TrinityIntegrator] IPC bus initialized")

    async def _allocate_ports(self) -> None:
        """Allocate ports for all components."""
        try:
            from backend.core.trinity_port_manager import get_trinity_port_manager

            self._port_manager = await get_trinity_port_manager()
            allocations = await self._port_manager.allocate_all_ports()

            for component, result in allocations.items():
                if result.success:
                    logger.info(
                        f"[TrinityIntegrator] Port allocated: "
                        f"{component.value}={result.port}"
                    )
                else:
                    logger.warning(
                        f"[TrinityIntegrator] Port allocation failed: "
                        f"{component.value}: {result.error}"
                    )

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Port allocation failed: {e}")

    async def _init_shutdown_manager(self) -> None:
        """Initialize the shutdown manager."""
        from backend.core.coordinated_shutdown import (
            EnhancedShutdownManager,
            setup_signal_handlers,
        )

        self._shutdown_manager = EnhancedShutdownManager(
            ipc_bus=self._ipc_bus,
            detect_orphans_on_start=False,  # Already done
        )

        # Register signal handlers
        try:
            loop = asyncio.get_running_loop()
            setup_signal_handlers(self._shutdown_manager, loop)
        except Exception as e:
            logger.debug(f"[TrinityIntegrator] Signal handler setup failed: {e}")

        logger.debug("[TrinityIntegrator] Shutdown manager initialized")

    async def _start_body_heartbeat(self) -> None:
        """Start JARVIS Body heartbeat publishing."""
        try:
            from backend.core.trinity_ipc import ComponentType

            await self._ipc_bus.publish_heartbeat(
                component=ComponentType.JARVIS_BODY,
                status="starting",
                pid=os.getpid(),
                metrics={"startup_time": self._start_time},
            )

            logger.debug("[TrinityIntegrator] Body heartbeat started")

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Body heartbeat failed: {e}")

    async def _wait_for_jprime(self) -> bool:
        """Wait for JARVIS Prime to be ready."""
        try:
            from backend.clients.jarvis_prime_client import get_jarvis_prime_client

            self._jprime_client = await get_jarvis_prime_client()

            # Wait for connection with timeout
            start = time.time()
            while time.time() - start < self.startup_timeout:
                if self._jprime_client.is_online:
                    logger.info("[TrinityIntegrator] JARVIS Prime is ready")
                    return True

                await asyncio.sleep(2.0)

            logger.warning("[TrinityIntegrator] JARVIS Prime timeout")
            return False

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] JARVIS Prime init failed: {e}")
            return False

    async def _wait_for_reactor(self) -> bool:
        """Wait for Reactor-Core to be ready."""
        try:
            from backend.clients.reactor_core_client import (
                initialize_reactor_client,
                get_reactor_client,
            )

            await initialize_reactor_client()
            self._reactor_client = get_reactor_client()

            if self._reactor_client and self._reactor_client.is_online:
                logger.info("[TrinityIntegrator] Reactor-Core is ready")
                return True

            logger.warning("[TrinityIntegrator] Reactor-Core not available")
            return False

        except Exception as e:
            logger.warning(f"[TrinityIntegrator] Reactor-Core init failed: {e}")
            return False

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def _health_loop(self) -> None:
        """Background health monitoring loop."""
        while self._running:
            try:
                await asyncio.sleep(self.health_check_interval)

                health = await self.get_health()

                # Update state based on health
                if health.degraded_components:
                    if self._state == TrinityState.READY:
                        self._set_state(TrinityState.DEGRADED)
                elif self._state == TrinityState.DEGRADED:
                    if not health.degraded_components:
                        self._set_state(TrinityState.READY)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[TrinityIntegrator] Health check error: {e}")

    async def get_health(self) -> TrinityHealth:
        """Get current Trinity system health."""
        components: Dict[str, ComponentStatus] = {}
        degraded: List[str] = []
        errors: List[str] = []

        # Check JARVIS Body (self)
        body_status = ComponentStatus(
            name="jarvis_body",
            health=ComponentHealth.HEALTHY,
            online=True,
            last_heartbeat=time.time(),
            metrics={"uptime": self.uptime},
        )
        components["jarvis_body"] = body_status

        # Check JARVIS Prime
        if self.enable_jprime:
            jprime_status = await self._check_jprime_health()
            components["jarvis_prime"] = jprime_status
            if jprime_status.health != ComponentHealth.HEALTHY:
                degraded.append("jarvis_prime")
            if jprime_status.error:
                errors.append(jprime_status.error)

        # Check Reactor-Core
        if self.enable_reactor:
            reactor_status = await self._check_reactor_health()
            components["reactor_core"] = reactor_status
            if reactor_status.health != ComponentHealth.HEALTHY:
                degraded.append("reactor_core")
            if reactor_status.error:
                errors.append(reactor_status.error)

        return TrinityHealth(
            state=self._state,
            components=components,
            uptime_seconds=self.uptime,
            last_check=time.time(),
            degraded_components=degraded,
            errors=errors,
        )

    async def _check_jprime_health(self) -> ComponentStatus:
        """Check JARVIS Prime health."""
        if not self._jprime_client:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._jprime_client.is_online
            metrics = self._jprime_client.get_metrics()

            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=metrics.get("last_health_check"),
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="jarvis_prime",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    async def _check_reactor_health(self) -> ComponentStatus:
        """Check Reactor-Core health."""
        if not self._reactor_client:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNKNOWN,
                online=False,
                error="Client not initialized",
            )

        try:
            is_online = self._reactor_client.is_online
            metrics = self._reactor_client.get_metrics()

            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.HEALTHY if is_online else ComponentHealth.UNHEALTHY,
                online=is_online,
                last_heartbeat=time.time() if is_online else None,
                metrics=metrics,
            )

        except Exception as e:
            return ComponentStatus(
                name="reactor_core",
                health=ComponentHealth.UNHEALTHY,
                online=False,
                error=str(e),
            )

    # =========================================================================
    # Shutdown
    # =========================================================================

    async def stop(
        self,
        timeout: float = 30.0,
        force: bool = False,
    ) -> bool:
        """
        Stop the Trinity system.

        Args:
            timeout: Max time to wait for graceful shutdown
            force: Skip drain phase for immediate shutdown

        Returns:
            True if shutdown successful
        """
        async with self._lock:
            if self._state in (TrinityState.STOPPED, TrinityState.STOPPING):
                return True

            self._set_state(TrinityState.STOPPING)
            self._running = False

            try:
                # Stop health monitoring
                if self._health_task:
                    self._health_task.cancel()
                    try:
                        await self._health_task
                    except asyncio.CancelledError:
                        pass

                # Close clients
                if self._jprime_client:
                    await self._jprime_client.disconnect()

                if self._reactor_client:
                    from backend.clients.reactor_core_client import shutdown_reactor_client
                    await shutdown_reactor_client()

                # Coordinated shutdown
                if self._shutdown_manager:
                    from backend.core.coordinated_shutdown import ShutdownReason

                    result = await self._shutdown_manager.initiate_shutdown(
                        reason=ShutdownReason.USER_REQUEST,
                        timeout=timeout,
                        force=force,
                    )

                    if not result.success:
                        logger.warning(
                            f"[TrinityIntegrator] Shutdown incomplete: {result.errors}"
                        )

                # Close IPC
                if self._ipc_bus:
                    from backend.core.trinity_ipc import close_resilient_trinity_ipc_bus
                    await close_resilient_trinity_ipc_bus()

                self._set_state(TrinityState.STOPPED)

                elapsed = time.time() - (self._start_time or time.time())
                logger.info(
                    f"[TrinityIntegrator] Stopped after {elapsed:.2f}s uptime"
                )

                return True

            except Exception as e:
                logger.error(f"[TrinityIntegrator] Shutdown error: {e}")
                self._set_state(TrinityState.ERROR)
                return False

    # =========================================================================
    # State Management
    # =========================================================================

    def _set_state(self, new_state: TrinityState) -> None:
        """Set new state and notify callbacks."""
        old_state = self._state
        self._state = new_state

        if old_state != new_state:
            logger.info(
                f"[TrinityIntegrator] State: {old_state.value} -> {new_state.value}"
            )

            for callback in self._on_state_change:
                try:
                    callback(old_state, new_state)
                except Exception as e:
                    logger.warning(f"[TrinityIntegrator] Callback error: {e}")

    def on_state_change(
        self,
        callback: Callable[[TrinityState, TrinityState], None],
    ) -> None:
        """Register callback for state changes."""
        self._on_state_change.append(callback)

    def on_component_change(
        self,
        callback: Callable[[str, ComponentHealth], None],
    ) -> None:
        """Register callback for component health changes."""
        self._on_component_change.append(callback)

    # =========================================================================
    # API Access
    # =========================================================================

    @property
    def ipc_bus(self):
        """Get the IPC bus."""
        return self._ipc_bus

    @property
    def jprime_client(self):
        """Get the JARVIS Prime client."""
        return self._jprime_client

    @property
    def reactor_client(self):
        """Get the Reactor-Core client."""
        return self._reactor_client

    def get_metrics(self) -> Dict[str, Any]:
        """Get integrator metrics."""
        return {
            "state": self._state.value,
            "uptime": self.uptime,
            "jprime_enabled": self.enable_jprime,
            "reactor_enabled": self.enable_reactor,
            "jprime_online": self._jprime_client.is_online if self._jprime_client else False,
            "reactor_online": self._reactor_client.is_online if self._reactor_client else False,
        }


# =============================================================================
# Singleton Access
# =============================================================================

_integrator: Optional[TrinityIntegrator] = None
_integrator_lock = asyncio.Lock()


async def get_trinity_integrator(
    **kwargs,
) -> TrinityIntegrator:
    """Get or create the singleton Trinity integrator."""
    global _integrator

    async with _integrator_lock:
        if _integrator is None:
            _integrator = TrinityIntegrator(**kwargs)
        return _integrator


async def start_trinity() -> bool:
    """Start the Trinity system."""
    integrator = await get_trinity_integrator()
    return await integrator.start()


async def stop_trinity(force: bool = False) -> bool:
    """Stop the Trinity system."""
    global _integrator

    if _integrator:
        result = await _integrator.stop(force=force)
        _integrator = None
        return result

    return True


async def get_trinity_health() -> Optional[TrinityHealth]:
    """Get Trinity system health."""
    if _integrator:
        return await _integrator.get_health()
    return None


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Types
    "TrinityState",
    "ComponentHealth",
    "ComponentStatus",
    "TrinityHealth",
    # Main Class
    "TrinityIntegrator",
    # Convenience
    "get_trinity_integrator",
    "start_trinity",
    "stop_trinity",
    "get_trinity_health",
]
