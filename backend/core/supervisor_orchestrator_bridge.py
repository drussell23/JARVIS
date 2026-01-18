"""
v78.0: Supervisor-Orchestrator Bridge
=====================================

Bridges the Advanced Startup Orchestrator with run_supervisor.py.
Provides hooks for dependency graph-based startup while maintaining
backward compatibility with the existing startup flow.

This module enables:
- Advanced dependency resolution in run_supervisor.py
- Circuit breaker protection for component startups
- Connection verification loops
- Dynamic configuration discovery
- Cross-repo Trinity integration

Usage in run_supervisor.py:
    from backend.core.supervisor_orchestrator_bridge import (
        enhance_supervisor_with_orchestrator,
        OrchestratorHooks,
    )

    # During SupervisorBootstrapper initialization
    hooks = await enhance_supervisor_with_orchestrator(bootstrapper)

    # During Trinity launch
    await hooks.verify_trinity_connections()

    # During shutdown
    await hooks.shutdown()

Author: JARVIS v78.0
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from backend.core.async_safety import LazyAsyncLock

if TYPE_CHECKING:
    from .advanced_startup_orchestrator import (
        DependencyGraphOrchestrator,
        DynamicConfigDiscovery,
        ConnectionVerifier,
        DiscoveredConfig,
        StartupResult,
    )
    from .trinity_health_monitor import (
        TrinityHealthMonitor,
        TrinityHealthSnapshot,
    )

logger = logging.getLogger(__name__)

# =============================================================================
# v78.0: Trinity Health Monitor Integration
# =============================================================================
# The TrinityHealthMonitor provides unified health monitoring for all repos.
# It can be optionally enabled for continuous background monitoring.

_trinity_health_monitor: Optional["TrinityHealthMonitor"] = None


# =============================================================================
# Configuration
# =============================================================================

@dataclass
class OrchestratorBridgeConfig:
    """Configuration for the orchestrator bridge."""
    enabled: bool = True
    parallel_verification: bool = True
    max_concurrent_health_checks: int = 4
    trinity_health_timeout: float = 30.0
    backend_health_timeout: float = 60.0
    connection_retry_attempts: int = 3
    connection_retry_delay: float = 2.0
    enable_circuit_breakers: bool = True
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "OrchestratorBridgeConfig":
        """Load configuration from environment variables."""
        return cls(
            enabled=os.getenv("ORCHESTRATOR_BRIDGE_ENABLED", "true").lower() == "true",
            parallel_verification=os.getenv("ORCHESTRATOR_PARALLEL_VERIFY", "true").lower() == "true",
            max_concurrent_health_checks=int(os.getenv("ORCHESTRATOR_MAX_CONCURRENT", "4")),
            trinity_health_timeout=float(os.getenv("TRINITY_HEALTH_TIMEOUT", "30.0")),
            backend_health_timeout=float(os.getenv("BACKEND_HEALTH_TIMEOUT", "60.0")),
            connection_retry_attempts=int(os.getenv("CONNECTION_RETRY_ATTEMPTS", "3")),
            connection_retry_delay=float(os.getenv("CONNECTION_RETRY_DELAY", "2.0")),
            enable_circuit_breakers=os.getenv("CIRCUIT_BREAKERS_ENABLED", "true").lower() == "true",
            log_level=os.getenv("ORCHESTRATOR_LOG_LEVEL", "INFO"),
        )


# =============================================================================
# Orchestrator Hooks
# =============================================================================

@dataclass
class TrinityHealthStatus:
    """Health status of Trinity components."""
    jarvis_backend: bool = False
    jarvis_prime: bool = False
    reactor_core: bool = False
    trinity_sync: bool = False
    coding_council: bool = False
    all_healthy: bool = False
    check_time_ms: float = 0.0
    errors: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        """Get a summary string."""
        healthy = []
        unhealthy = []

        if self.jarvis_backend:
            healthy.append("Backend")
        else:
            unhealthy.append("Backend")

        if self.jarvis_prime:
            healthy.append("J-Prime")
        else:
            unhealthy.append("J-Prime")

        if self.reactor_core:
            healthy.append("Reactor")
        else:
            unhealthy.append("Reactor")

        if self.trinity_sync:
            healthy.append("Trinity")
        else:
            unhealthy.append("Trinity")

        if self.coding_council:
            healthy.append("CodingCouncil")
        else:
            unhealthy.append("CodingCouncil")

        return f"Healthy: {', '.join(healthy) or 'none'} | Unhealthy: {', '.join(unhealthy) or 'none'}"


class OrchestratorHooks:
    """
    Hooks for integrating the advanced orchestrator with run_supervisor.py.

    This class provides a bridge between the existing supervisor startup flow
    and the new advanced orchestrator patterns.
    """

    def __init__(
        self,
        config: OrchestratorBridgeConfig,
        logger_instance: Optional[logging.Logger] = None,
    ):
        self.config = config
        self.log = logger_instance or logger
        self._orchestrator: Optional["DependencyGraphOrchestrator"] = None
        self._config_discovery: Optional["DynamicConfigDiscovery"] = None
        self._connection_verifier: Optional["ConnectionVerifier"] = None
        self._discovered_config: Optional["DiscoveredConfig"] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> bool:
        """
        Initialize the orchestrator and discovery systems.

        Returns:
            True if initialization succeeded
        """
        if self._initialized:
            return True

        async with self._lock:
            if self._initialized:
                return True

            try:
                from .advanced_startup_orchestrator import (
                    DependencyGraphOrchestrator,
                    DynamicConfigDiscovery,
                    ConnectionVerifier,
                )

                self._orchestrator = DependencyGraphOrchestrator(self.log)
                self._config_discovery = DynamicConfigDiscovery(self.log)
                self._connection_verifier = ConnectionVerifier(self.log)

                # Discover configuration
                self._discovered_config = await self._config_discovery.discover()

                self._initialized = True
                self.log.info("[OrchestratorBridge] Initialized successfully")
                return True

            except ImportError as e:
                self.log.warning(f"[OrchestratorBridge] Import error: {e}")
                return False
            except Exception as e:
                self.log.error(f"[OrchestratorBridge] Initialization failed: {e}")
                return False

    @property
    def discovered_config(self) -> Optional["DiscoveredConfig"]:
        """Get the discovered configuration."""
        return self._discovered_config

    @property
    def orchestrator(self) -> Optional["DependencyGraphOrchestrator"]:
        """Get the orchestrator instance."""
        return self._orchestrator

    async def get_dynamic_repo_paths(self) -> Dict[str, Path]:
        """
        Get dynamically discovered repository paths.

        Returns:
            Dict mapping repo name to path
        """
        if not self._discovered_config:
            await self.initialize()

        if not self._discovered_config:
            return {}

        from .advanced_startup_orchestrator import TrinityRepo

        return {
            "jarvis": self._discovered_config.repo_paths.get(TrinityRepo.JARVIS),
            "jarvis_prime": self._discovered_config.repo_paths.get(TrinityRepo.JARVIS_PRIME),
            "reactor_core": self._discovered_config.repo_paths.get(TrinityRepo.REACTOR_CORE),
        }

    async def get_dynamic_ports(self) -> Dict[str, int]:
        """
        Get dynamically discovered/configured ports.

        Returns:
            Dict mapping service name to port
        """
        if not self._discovered_config:
            await self.initialize()

        if not self._discovered_config:
            return {
                "jarvis_backend": 8010,
                "jarvis_prime": 8000,
                "reactor_core": 8090,
            }

        return self._discovered_config.ports

    async def get_trinity_dir(self) -> Path:
        """
        Get the Trinity state directory.

        Returns:
            Path to Trinity directory
        """
        if not self._discovered_config:
            await self.initialize()

        if not self._discovered_config:
            return Path.home() / ".jarvis" / "trinity"

        return self._discovered_config.trinity_dir

    async def verify_trinity_connections(
        self,
        timeout: Optional[float] = None,
        skip_http_checks: bool = False,
    ) -> TrinityHealthStatus:
        """
        Verify all Trinity component connections.

        Args:
            timeout: Optional timeout override
            skip_http_checks: If True, skip HTTP-based health checks (Backend, CodingCouncil).
                             Use this during pre-startup phase when the backend hasn't started yet.

        Returns:
            TrinityHealthStatus with detailed status
        """
        if not self._initialized:
            await self.initialize()

        start_time = time.time()
        status = TrinityHealthStatus()

        if not self._connection_verifier or not self._discovered_config:
            status.errors.append("Orchestrator not initialized")
            return status

        ports = self._discovered_config.ports
        trinity_dir = self._discovered_config.trinity_dir
        health_timeout = timeout or self.config.trinity_health_timeout

        # v78.1: When skip_http_checks=True (pre-startup), only check heartbeat-based components
        # HTTP endpoints (Backend, CodingCouncil) won't exist until supervisor.run() completes
        if skip_http_checks:
            self.log.debug("[OrchestratorBridge] Pre-startup mode: Skipping HTTP health checks")
            # Only verify heartbeat-based components
            status.jarvis_backend = True  # Assume True - will be started soon
            status.coding_council = True  # Assume True - depends on backend

            if self.config.parallel_verification:
                tasks = [
                    self._verify_trinity_heartbeat("jarvis_prime", trinity_dir, health_timeout),
                    self._verify_trinity_heartbeat("reactor_core", trinity_dir, health_timeout),
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                status.jarvis_prime = results[0] if not isinstance(results[0], Exception) else False
                status.reactor_core = results[1] if not isinstance(results[1], Exception) else False
            else:
                status.jarvis_prime = await self._verify_trinity_heartbeat(
                    "jarvis_prime", trinity_dir, health_timeout
                )
                status.reactor_core = await self._verify_trinity_heartbeat(
                    "reactor_core", trinity_dir, health_timeout
                )
        else:
            # Verify in parallel if enabled (full health check including HTTP)
            if self.config.parallel_verification:
                tasks = [
                    self._verify_backend_health(ports.get("jarvis_backend", 8010), health_timeout),
                    self._verify_trinity_heartbeat("jarvis_prime", trinity_dir, health_timeout),
                    self._verify_trinity_heartbeat("reactor_core", trinity_dir, health_timeout),
                    self._verify_coding_council_health(ports.get("jarvis_backend", 8010), health_timeout),
                ]

                results = await asyncio.gather(*tasks, return_exceptions=True)

                status.jarvis_backend = results[0] if not isinstance(results[0], Exception) else False
                status.jarvis_prime = results[1] if not isinstance(results[1], Exception) else False
                status.reactor_core = results[2] if not isinstance(results[2], Exception) else False
                status.coding_council = results[3] if not isinstance(results[3], Exception) else False

                # Check for exceptions
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        status.errors.append(f"Task {i}: {result}")

            else:
                # Sequential verification
                status.jarvis_backend = await self._verify_backend_health(
                    ports.get("jarvis_backend", 8010), health_timeout
                )
                status.jarvis_prime = await self._verify_trinity_heartbeat(
                    "jarvis_prime", trinity_dir, health_timeout
                )
                status.reactor_core = await self._verify_trinity_heartbeat(
                    "reactor_core", trinity_dir, health_timeout
                )
                status.coding_council = await self._verify_coding_council_health(
                    ports.get("jarvis_backend", 8010), health_timeout
                )

        # Trinity sync is healthy if Backend + at least one external component is healthy
        status.trinity_sync = (
            status.jarvis_backend and
            (status.jarvis_prime or status.reactor_core)
        )

        status.all_healthy = (
            status.jarvis_backend and
            status.trinity_sync
        )

        status.check_time_ms = (time.time() - start_time) * 1000

        self.log.info(f"[OrchestratorBridge] Trinity health: {status.summary}")
        return status

    async def _verify_backend_health(self, port: int, timeout: float) -> bool:
        """Verify JARVIS backend is healthy."""
        if not self._connection_verifier:
            return False

        try:
            return await self._connection_verifier.verify_http_endpoint(
                f"http://127.0.0.1:{port}/health/ping",
                timeout=timeout,
                interval=2.0,
            )
        except Exception as e:
            self.log.debug(f"[OrchestratorBridge] Backend health check failed: {e}")
            return False

    async def _verify_trinity_heartbeat(
        self,
        component: str,
        trinity_dir: Path,
        timeout: float,
    ) -> bool:
        """Verify Trinity component via heartbeat."""
        if not self._connection_verifier:
            return False

        try:
            return await self._connection_verifier.verify_trinity_heartbeat(
                component,
                trinity_dir,
                timeout=timeout,
                interval=1.0,
            )
        except Exception as e:
            self.log.debug(f"[OrchestratorBridge] {component} heartbeat check failed: {e}")
            return False

    async def _verify_coding_council_health(self, port: int, timeout: float) -> bool:
        """Verify Coding Council is healthy."""
        if not self._connection_verifier:
            return False

        try:
            return await self._connection_verifier.verify_http_endpoint(
                f"http://127.0.0.1:{port}/coding-council/health",
                timeout=min(timeout, 10.0),  # Shorter timeout for Coding Council
                interval=2.0,
            )
        except Exception as e:
            self.log.debug(f"[OrchestratorBridge] Coding Council health check failed: {e}")
            return False

    async def wait_for_backend_ready(
        self,
        timeout: Optional[float] = None,
        port: Optional[int] = None,
    ) -> bool:
        """
        Wait for the backend to be ready.

        Args:
            timeout: Maximum time to wait
            port: Override port

        Returns:
            True if backend is ready
        """
        if not self._initialized:
            await self.initialize()

        if not self._connection_verifier:
            return False

        actual_port = port or (
            self._discovered_config.ports.get("jarvis_backend", 8010)
            if self._discovered_config else 8010
        )
        actual_timeout = timeout or self.config.backend_health_timeout

        return await self._connection_verifier.verify_http_endpoint(
            f"http://127.0.0.1:{actual_port}/health/ping",
            timeout=actual_timeout,
            interval=1.0,
        )

    async def wait_for_trinity_components(
        self,
        timeout: Optional[float] = None,
        required: Optional[List[str]] = None,
    ) -> Dict[str, bool]:
        """
        Wait for Trinity components to be ready.

        Args:
            timeout: Maximum time to wait
            required: List of required component names

        Returns:
            Dict mapping component name to ready status
        """
        if not self._initialized:
            await self.initialize()

        if required is None:
            required = ["jarvis_prime", "reactor_core"]

        actual_timeout = timeout or self.config.trinity_health_timeout
        trinity_dir = await self.get_trinity_dir()

        results = {}
        for component in required:
            results[component] = await self._verify_trinity_heartbeat(
                component, trinity_dir, actual_timeout
            )

        return results

    async def register_startup_components(
        self,
        components: Dict[str, Dict[str, Any]],
    ) -> None:
        """
        Register components with the orchestrator.

        Args:
            components: Dict mapping component name to config
                {
                    "name": {
                        "dependencies": ["dep1", "dep2"],
                        "startup_func": async_func,
                        "health_check_func": async_func,
                        "critical": True,
                        "timeout": 60.0,
                    }
                }
        """
        if not self._orchestrator:
            await self.initialize()

        if not self._orchestrator:
            self.log.warning("[OrchestratorBridge] Cannot register - orchestrator not available")
            return

        for name, config in components.items():
            self._orchestrator.register_component(
                name=name,
                dependencies=config.get("dependencies", []),
                startup_func=config.get("startup_func"),
                health_check_func=config.get("health_check_func"),
                critical=config.get("critical", True),
                timeout_seconds=config.get("timeout", 60.0),
            )

    async def start_registered_components(
        self,
        parallel: bool = True,
    ) -> Optional["StartupResult"]:
        """
        Start all registered components using the orchestrator.

        Args:
            parallel: Enable parallel startup

        Returns:
            StartupResult or None if orchestrator not available
        """
        if not self._orchestrator:
            await self.initialize()

        if not self._orchestrator:
            self.log.warning("[OrchestratorBridge] Cannot start - orchestrator not available")
            return None

        return await self._orchestrator.start_all(parallel=parallel)

    async def shutdown(self) -> None:
        """Shutdown the orchestrator and cleanup."""
        if self._orchestrator:
            await self._orchestrator.shutdown_all()

        self._initialized = False
        self.log.info("[OrchestratorBridge] Shutdown complete")

    def get_status(self) -> Dict[str, Any]:
        """Get current status."""
        status = {
            "initialized": self._initialized,
            "config_discovered": self._discovered_config is not None,
        }

        if self._discovered_config:
            from .advanced_startup_orchestrator import TrinityRepo
            status["repos_found"] = {
                repo.value: str(path)
                for repo, path in self._discovered_config.repo_paths.items()
            }
            status["trinity_dir"] = str(self._discovered_config.trinity_dir)
            status["ports"] = self._discovered_config.ports
            status["api_keys_available"] = [
                k for k, v in self._discovered_config.api_keys.items() if v
            ]

        if self._orchestrator:
            status["orchestrator"] = self._orchestrator.get_status()

        return status


# =============================================================================
# Factory Functions
# =============================================================================

_hooks_instance: Optional[OrchestratorHooks] = None
_hooks_lock = LazyAsyncLock()  # v100.1: Lazy initialization to avoid "no running event loop" error


async def get_orchestrator_hooks(
    config: Optional[OrchestratorBridgeConfig] = None,
    logger_instance: Optional[logging.Logger] = None,
) -> OrchestratorHooks:
    """
    Get or create the singleton OrchestratorHooks instance.

    Args:
        config: Optional configuration override
        logger_instance: Optional logger

    Returns:
        OrchestratorHooks instance
    """
    global _hooks_instance

    async with _hooks_lock:
        if _hooks_instance is None:
            actual_config = config or OrchestratorBridgeConfig.from_env()
            _hooks_instance = OrchestratorHooks(actual_config, logger_instance)
            await _hooks_instance.initialize()

        return _hooks_instance


async def enhance_supervisor_with_orchestrator(
    bootstrapper: Any,
    config: Optional[OrchestratorBridgeConfig] = None,
) -> OrchestratorHooks:
    """
    Enhance SupervisorBootstrapper with orchestrator capabilities.

    This function integrates the advanced orchestrator with the existing
    supervisor startup flow.

    Args:
        bootstrapper: SupervisorBootstrapper instance
        config: Optional configuration

    Returns:
        OrchestratorHooks for use during startup

    Usage:
        hooks = await enhance_supervisor_with_orchestrator(self)

        # Later in startup
        status = await hooks.verify_trinity_connections()
    """
    # Get or create hooks
    hooks = await get_orchestrator_hooks(
        config=config,
        logger_instance=getattr(bootstrapper, 'logger', None),
    )

    # Attach hooks to bootstrapper for easy access
    bootstrapper._orchestrator_hooks = hooks

    # Override dynamic path discovery if available
    if hooks.discovered_config:
        from .advanced_startup_orchestrator import TrinityRepo

        repos = hooks.discovered_config.repo_paths

        # Update bootstrapper paths if discovered
        if TrinityRepo.JARVIS_PRIME in repos:
            bootstrapper._jprime_repo_path = repos[TrinityRepo.JARVIS_PRIME]

        if TrinityRepo.REACTOR_CORE in repos:
            bootstrapper._reactor_core_repo_path = repos[TrinityRepo.REACTOR_CORE]

        # Log discovery results
        logger.info("[OrchestratorBridge] Enhanced supervisor with dynamic discovery")
        logger.info(f"  J-Prime: {bootstrapper._jprime_repo_path}")
        logger.info(f"  Reactor: {bootstrapper._reactor_core_repo_path}")

    return hooks


# =============================================================================
# Utility Functions
# =============================================================================

async def verify_trinity_health_quick() -> TrinityHealthStatus:
    """Quick Trinity health check without full initialization."""
    hooks = await get_orchestrator_hooks()
    return await hooks.verify_trinity_connections(timeout=10.0)


async def get_discovered_ports() -> Dict[str, int]:
    """Get discovered ports."""
    hooks = await get_orchestrator_hooks()
    return await hooks.get_dynamic_ports()


async def get_discovered_repo_paths() -> Dict[str, Optional[Path]]:
    """Get discovered repository paths."""
    hooks = await get_orchestrator_hooks()
    return await hooks.get_dynamic_repo_paths()


# =============================================================================
# v78.0: Trinity Health Monitor Functions
# =============================================================================

async def start_trinity_health_monitor(
    check_interval: float = 10.0,
    on_health_change: Optional[Callable[["TrinityHealthSnapshot"], None]] = None,
) -> "TrinityHealthMonitor":
    """
    Start the unified Trinity health monitor for continuous background monitoring.

    This provides real-time health status for all Trinity components:
    - JARVIS Body (HTTP endpoint)
    - J-Prime Mind (heartbeat file)
    - Reactor-Core Nerves (heartbeat file)
    - Coding Council (heartbeat file)

    Args:
        check_interval: Seconds between health checks (default: 10.0)
        on_health_change: Optional callback for health status changes

    Returns:
        TrinityHealthMonitor instance

    Example:
        monitor = await start_trinity_health_monitor(
            check_interval=5.0,
            on_health_change=lambda snap: print(f"Health: {snap.summary}")
        )
    """
    global _trinity_health_monitor

    if _trinity_health_monitor is not None:
        return _trinity_health_monitor

    try:
        from .trinity_health_monitor import (
            TrinityHealthMonitor,
            TrinityHealthConfig,
        )

        config = TrinityHealthConfig.from_env()
        config.check_interval_seconds = check_interval

        _trinity_health_monitor = TrinityHealthMonitor(config=config)

        if on_health_change:
            _trinity_health_monitor.register_health_callback(on_health_change)

        await _trinity_health_monitor.start()

        logger.info(
            f"[OrchestratorBridge] Trinity health monitor started "
            f"(interval: {check_interval}s)"
        )

        return _trinity_health_monitor

    except ImportError as e:
        logger.warning(f"[OrchestratorBridge] Trinity health monitor not available: {e}")
        raise
    except Exception as e:
        logger.error(f"[OrchestratorBridge] Failed to start Trinity health monitor: {e}")
        raise


async def stop_trinity_health_monitor() -> None:
    """Stop the Trinity health monitor."""
    global _trinity_health_monitor

    if _trinity_health_monitor:
        await _trinity_health_monitor.stop()
        _trinity_health_monitor = None
        logger.info("[OrchestratorBridge] Trinity health monitor stopped")


async def get_trinity_health_snapshot() -> Optional["TrinityHealthSnapshot"]:
    """
    Get the latest Trinity health snapshot.

    Returns:
        TrinityHealthSnapshot or None if monitor not running
    """
    if _trinity_health_monitor:
        return _trinity_health_monitor.latest_snapshot
    return None


async def check_trinity_health_now() -> Optional["TrinityHealthSnapshot"]:
    """
    Perform an immediate Trinity health check.

    This bypasses the monitoring interval and performs a check right now.

    Returns:
        TrinityHealthSnapshot with current health status
    """
    if _trinity_health_monitor:
        return await _trinity_health_monitor.check_health()

    # If monitor not running, use quick check via hooks
    try:
        from .trinity_health_monitor import TrinityHealthMonitor, TrinityHealthConfig

        monitor = TrinityHealthMonitor(config=TrinityHealthConfig.from_env())
        snapshot = await monitor.check_health()
        return snapshot
    except Exception as e:
        logger.error(f"[OrchestratorBridge] Trinity health check failed: {e}")
        return None
