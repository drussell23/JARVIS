"""
Intelligence Component Manager - Orchestrates all intelligence providers for voice authentication.

This manager coordinates initialization, health monitoring, and graceful shutdown of:
1. Network Context Provider
2. Unlock Pattern Tracker
3. Device State Monitor
4. Multi-Factor Auth Fusion Engine
5. Intelligence Learning Coordinator (RAG + RLHF)

Architecture:
- Async/parallel initialization with dependency resolution
- Health monitoring during runtime
- Graceful shutdown coordination
- Zero hardcoding - all configuration via environment variables
- Integration with supervisor startup progress reporting

Author: Claude Sonnet 4.5
Version: 5.0.0
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ComponentStatus(Enum):
    """Status of an intelligence component."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"
    SHUTDOWN = "shutdown"


@dataclass
class ComponentHealth:
    """Health information for an intelligence component."""
    name: str
    status: ComponentStatus
    initialized_at: Optional[datetime] = None
    last_check: Optional[datetime] = None
    error_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            'name': self.name,
            'status': self.status.value,
            'initialized_at': self.initialized_at.isoformat() if self.initialized_at else None,
            'last_check': self.last_check.isoformat() if self.last_check else None,
            'error_message': self.error_message,
            'metadata': self.metadata
        }


@dataclass
class IntelligenceConfig:
    """Configuration for intelligence components - all from environment variables."""

    # Global intelligence settings
    enabled: bool = field(default_factory=lambda: os.getenv('INTELLIGENCE_ENABLED', 'true').lower() == 'true')
    parallel_init: bool = field(default_factory=lambda: os.getenv('INTELLIGENCE_PARALLEL_INIT', 'true').lower() == 'true')
    init_timeout_seconds: int = field(default_factory=lambda: int(os.getenv('INTELLIGENCE_INIT_TIMEOUT', '30')))
    health_check_interval: int = field(default_factory=lambda: int(os.getenv('INTELLIGENCE_HEALTH_INTERVAL', '300')))  # 5 minutes

    # Component-specific enable flags
    network_context_enabled: bool = field(default_factory=lambda: os.getenv('NETWORK_CONTEXT_ENABLED', 'true').lower() == 'true')
    pattern_tracker_enabled: bool = field(default_factory=lambda: os.getenv('PATTERN_TRACKER_ENABLED', 'true').lower() == 'true')
    device_monitor_enabled: bool = field(default_factory=lambda: os.getenv('DEVICE_MONITOR_ENABLED', 'true').lower() == 'true')
    fusion_engine_enabled: bool = field(default_factory=lambda: os.getenv('FUSION_ENGINE_ENABLED', 'true').lower() == 'true')
    learning_coordinator_enabled: bool = field(default_factory=lambda: os.getenv('LEARNING_COORDINATOR_ENABLED', 'true').lower() == 'true')

    # Data directory
    data_dir: Path = field(default_factory=lambda: Path(os.getenv('JARVIS_DATA_DIR', str(Path.home() / '.jarvis'))))

    # Graceful degradation
    fail_fast: bool = field(default_factory=lambda: os.getenv('INTELLIGENCE_FAIL_FAST', 'false').lower() == 'true')
    required_components: List[str] = field(default_factory=lambda: os.getenv('INTELLIGENCE_REQUIRED_COMPONENTS', 'fusion_engine').split(','))

    def __post_init__(self):
        """Ensure data directory exists."""
        self.data_dir.mkdir(parents=True, exist_ok=True)


class IntelligenceComponentManager:
    """
    Manages all intelligence components for voice authentication.

    Responsibilities:
    - Parallel async initialization with dependency resolution
    - Health monitoring and component status tracking
    - Graceful shutdown coordination
    - Startup progress reporting
    - Component availability checks

    Usage:
        manager = IntelligenceComponentManager(config)
        await manager.initialize()

        # Check component availability
        if manager.is_component_ready('fusion_engine'):
            fusion = manager.get_component('fusion_engine')
            result = await fusion.fuse(...)

        # Shutdown
        await manager.shutdown()
    """

    def __init__(
        self,
        config: Optional[IntelligenceConfig] = None,
        progress_callback: Optional[Callable[[str, float], None]] = None
    ):
        """
        Initialize Intelligence Component Manager.

        Args:
            config: Configuration (uses defaults from environment if None)
            progress_callback: Optional callback for startup progress reporting
                              Signature: callback(component_name: str, progress: float)
        """
        self.config = config or IntelligenceConfig()
        self.progress_callback = progress_callback

        # Component storage
        self._components: Dict[str, Any] = {}
        self._health: Dict[str, ComponentHealth] = {}

        # Initialization tracking
        self._initialized = False
        self._init_tasks: Dict[str, asyncio.Task] = {}

        # Health monitoring
        self._health_check_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

        logger.info("🧠 Intelligence Component Manager created")

    async def initialize(self) -> Dict[str, ComponentHealth]:
        """
        Initialize all enabled intelligence components.

        Returns:
            Dictionary of component health status

        Raises:
            RuntimeError: If fail_fast=true and required component fails
        """
        if self._initialized:
            logger.warning("⚠️ Intelligence components already initialized")
            return self._health

        if not self.config.enabled:
            logger.info("ℹ️ Intelligence system disabled via INTELLIGENCE_ENABLED=false")
            return {}

        logger.info("🚀 Initializing intelligence components...")
        start_time = datetime.now()

        try:
            if self.config.parallel_init:
                await self._parallel_initialize()
            else:
                await self._sequential_initialize()

            # Start health monitoring
            if self.config.health_check_interval > 0:
                self._health_check_task = asyncio.create_task(self._health_monitor_loop())

            self._initialized = True
            duration = (datetime.now() - start_time).total_seconds()

            ready_count = sum(1 for h in self._health.values() if h.status == ComponentStatus.READY)
            total_count = len(self._health)

            logger.info(
                f"✅ Intelligence initialization complete: {ready_count}/{total_count} "
                f"components ready in {duration:.2f}s"
            )

            return self._health

        except Exception as e:
            logger.error(f"❌ Intelligence initialization failed: {e}")
            if self.config.fail_fast:
                raise RuntimeError(f"Intelligence initialization failed: {e}") from e
            return self._health

    async def _parallel_initialize(self) -> None:
        """Initialize all components in parallel (faster startup)."""
        tasks = {}

        # Create initialization tasks for enabled components
        if self.config.network_context_enabled:
            tasks['network_context'] = asyncio.create_task(
                self._init_network_context()
            )

        if self.config.pattern_tracker_enabled:
            tasks['pattern_tracker'] = asyncio.create_task(
                self._init_pattern_tracker()
            )

        if self.config.device_monitor_enabled:
            tasks['device_monitor'] = asyncio.create_task(
                self._init_device_monitor()
            )

        # Fusion engine depends on the above, but can still init in parallel
        if self.config.fusion_engine_enabled:
            tasks['fusion_engine'] = asyncio.create_task(
                self._init_fusion_engine()
            )

        # Learning coordinator can init independently
        if self.config.learning_coordinator_enabled:
            tasks['learning_coordinator'] = asyncio.create_task(
                self._init_learning_coordinator()
            )

        # Wait for all with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks.values(), return_exceptions=True),
                timeout=self.config.init_timeout_seconds
            )
        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Component initialization timeout after {self.config.init_timeout_seconds}s")

            # Mark incomplete tasks as failed
            for name, task in tasks.items():
                if not task.done():
                    task.cancel()
                    self._set_component_health(
                        name,
                        ComponentStatus.FAILED,
                        error="Initialization timeout"
                    )

    async def _sequential_initialize(self) -> None:
        """Initialize components sequentially (more reliable, slower)."""
        if self.config.network_context_enabled:
            await self._init_network_context()

        if self.config.pattern_tracker_enabled:
            await self._init_pattern_tracker()

        if self.config.device_monitor_enabled:
            await self._init_device_monitor()

        if self.config.fusion_engine_enabled:
            await self._init_fusion_engine()

        if self.config.learning_coordinator_enabled:
            await self._init_learning_coordinator()

    async def _init_network_context(self) -> None:
        """Initialize Network Context Provider."""
        component_name = 'network_context'
        self._set_component_health(component_name, ComponentStatus.INITIALIZING)

        try:
            self._report_progress(component_name, 0.0)

            from intelligence.network_context_provider import get_network_context_provider
            provider = await get_network_context_provider()

            self._components[component_name] = provider
            self._set_component_health(
                component_name,
                ComponentStatus.READY,
                metadata={'type': 'NetworkContextProvider'}
            )

            self._report_progress(component_name, 1.0)
            logger.info("✅ Network Context Provider ready")

        except Exception as e:
            logger.error(f"❌ Network Context Provider failed: {e}")
            self._set_component_health(component_name, ComponentStatus.FAILED, error=str(e))
            self._check_required_component_failure(component_name)

    async def _init_pattern_tracker(self) -> None:
        """Initialize Unlock Pattern Tracker."""
        component_name = 'pattern_tracker'
        self._set_component_health(component_name, ComponentStatus.INITIALIZING)

        try:
            self._report_progress(component_name, 0.0)

            from intelligence.unlock_pattern_tracker import get_pattern_tracker
            tracker = await get_pattern_tracker()

            self._components[component_name] = tracker
            self._set_component_health(
                component_name,
                ComponentStatus.READY,
                metadata={'type': 'UnlockPatternTracker'}
            )

            self._report_progress(component_name, 1.0)
            logger.info("✅ Unlock Pattern Tracker ready")

        except Exception as e:
            logger.error(f"❌ Unlock Pattern Tracker failed: {e}")
            self._set_component_health(component_name, ComponentStatus.FAILED, error=str(e))
            self._check_required_component_failure(component_name)

    async def _init_device_monitor(self) -> None:
        """Initialize Device State Monitor."""
        component_name = 'device_monitor'
        self._set_component_health(component_name, ComponentStatus.INITIALIZING)

        try:
            self._report_progress(component_name, 0.0)

            from intelligence.device_state_monitor import get_device_monitor
            monitor = await get_device_monitor()

            self._components[component_name] = monitor
            self._set_component_health(
                component_name,
                ComponentStatus.READY,
                metadata={'type': 'DeviceStateMonitor'}
            )

            self._report_progress(component_name, 1.0)
            logger.info("✅ Device State Monitor ready")

        except Exception as e:
            logger.error(f"❌ Device State Monitor failed: {e}")
            self._set_component_health(component_name, ComponentStatus.FAILED, error=str(e))
            self._check_required_component_failure(component_name)

    async def _init_fusion_engine(self) -> None:
        """Initialize Multi-Factor Auth Fusion Engine."""
        component_name = 'fusion_engine'
        self._set_component_health(component_name, ComponentStatus.INITIALIZING)

        try:
            self._report_progress(component_name, 0.0)

            from intelligence.multi_factor_auth_fusion import get_fusion_engine
            engine = await get_fusion_engine()

            self._components[component_name] = engine
            self._set_component_health(
                component_name,
                ComponentStatus.READY,
                metadata={'type': 'MultiFactorAuthFusion'}
            )

            self._report_progress(component_name, 1.0)
            logger.info("✅ Multi-Factor Auth Fusion Engine ready")

        except Exception as e:
            logger.error(f"❌ Multi-Factor Auth Fusion Engine failed: {e}")
            self._set_component_health(component_name, ComponentStatus.FAILED, error=str(e))
            self._check_required_component_failure(component_name)

    async def _init_learning_coordinator(self) -> None:
        """Initialize Intelligence Learning Coordinator (RAG + RLHF)."""
        component_name = 'learning_coordinator'
        self._set_component_health(component_name, ComponentStatus.INITIALIZING)

        try:
            self._report_progress(component_name, 0.0)

            from intelligence.intelligence_learning_coordinator import get_learning_coordinator
            coordinator = await get_learning_coordinator()

            self._components[component_name] = coordinator
            self._set_component_health(
                component_name,
                ComponentStatus.READY,
                metadata={'type': 'IntelligenceLearningCoordinator', 'rag_enabled': True, 'rlhf_enabled': True}
            )

            self._report_progress(component_name, 1.0)
            logger.info("✅ Intelligence Learning Coordinator ready (RAG + RLHF)")

        except Exception as e:
            logger.error(f"❌ Intelligence Learning Coordinator failed: {e}")
            self._set_component_health(component_name, ComponentStatus.FAILED, error=str(e))
            self._check_required_component_failure(component_name)

    def _set_component_health(
        self,
        name: str,
        status: ComponentStatus,
        error: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Update component health status."""
        if name not in self._health:
            self._health[name] = ComponentHealth(name=name, status=status)

        health = self._health[name]
        health.status = status
        health.last_check = datetime.now()

        if status == ComponentStatus.READY and health.initialized_at is None:
            health.initialized_at = datetime.now()

        if error:
            health.error_message = error

        if metadata:
            health.metadata.update(metadata)

    def _check_required_component_failure(self, component_name: str) -> None:
        """Check if a required component failed and handle accordingly."""
        if self.config.fail_fast and component_name in self.config.required_components:
            raise RuntimeError(f"Required component '{component_name}' failed to initialize")

    def _report_progress(self, component_name: str, progress: float) -> None:
        """Report initialization progress via callback."""
        if self.progress_callback:
            try:
                self.progress_callback(component_name, progress)
            except Exception as e:
                logger.debug(f"Progress callback error: {e}")

    async def _health_monitor_loop(self) -> None:
        """Background health monitoring loop."""
        logger.info(f"🩺 Starting health monitor (interval: {self.config.health_check_interval}s)")

        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(self.config.health_check_interval)
                await self._check_all_health()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health monitor error: {e}")

    async def _check_all_health(self) -> None:
        """Check health of all components."""
        for name, component in self._components.items():
            try:
                # Check if component has health check method
                if hasattr(component, 'health_check'):
                    is_healthy = await component.health_check()
                    if not is_healthy:
                        self._set_component_health(name, ComponentStatus.DEGRADED)
                    elif self._health[name].status == ComponentStatus.DEGRADED:
                        # Recovered
                        self._set_component_health(name, ComponentStatus.READY)
                        logger.info(f"✅ Component '{name}' recovered")
                else:
                    # No health check method - assume healthy if initialized
                    self._health[name].last_check = datetime.now()

            except Exception as e:
                logger.warning(f"Health check failed for '{name}': {e}")
                self._set_component_health(name, ComponentStatus.DEGRADED, error=str(e))

    def is_component_ready(self, component_name: str) -> bool:
        """Check if a component is ready for use."""
        return (
            component_name in self._health and
            self._health[component_name].status == ComponentStatus.READY
        )

    def get_component(self, component_name: str) -> Optional[Any]:
        """Get a component instance if ready."""
        if self.is_component_ready(component_name):
            return self._components.get(component_name)
        return None

    def get_all_components(self) -> Dict[str, Any]:
        """Get all component instances."""
        return self._components.copy()

    def get_health_status(self) -> Dict[str, Dict[str, Any]]:
        """Get health status of all components."""
        return {name: health.to_dict() for name, health in self._health.items()}

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of intelligence system status."""
        total = len(self._health)
        ready = sum(1 for h in self._health.values() if h.status == ComponentStatus.READY)
        degraded = sum(1 for h in self._health.values() if h.status == ComponentStatus.DEGRADED)
        failed = sum(1 for h in self._health.values() if h.status == ComponentStatus.FAILED)

        return {
            'initialized': self._initialized,
            'enabled': self.config.enabled,
            'total_components': total,
            'ready': ready,
            'degraded': degraded,
            'failed': failed,
            'health_monitoring': self._health_check_task is not None and not self._health_check_task.done(),
            'components': self.get_health_status()
        }

    async def shutdown(self) -> None:
        """Gracefully shutdown all intelligence components."""
        if not self._initialized:
            logger.debug("Intelligence components not initialized, nothing to shutdown")
            return

        logger.info("🛑 Shutting down intelligence components...")
        self._shutdown_event.set()

        # Stop health monitoring
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

        # Shutdown components in reverse order
        shutdown_order = [
            'learning_coordinator',
            'fusion_engine',
            'device_monitor',
            'pattern_tracker',
            'network_context'
        ]

        for name in shutdown_order:
            if name in self._components:
                try:
                    component = self._components[name]
                    if hasattr(component, 'shutdown'):
                        await component.shutdown()
                    elif hasattr(component, 'close'):
                        await component.close()

                    self._set_component_health(name, ComponentStatus.SHUTDOWN)
                    logger.info(f"✅ Component '{name}' shutdown complete")

                except Exception as e:
                    logger.error(f"Error shutting down '{name}': {e}")

        self._components.clear()
        self._initialized = False

        logger.info("✅ Intelligence shutdown complete")


# Singleton instance management
_intelligence_manager: Optional[IntelligenceComponentManager] = None
_manager_lock = asyncio.Lock()


async def get_intelligence_manager(
    config: Optional[IntelligenceConfig] = None,
    progress_callback: Optional[Callable[[str, float], None]] = None,
    force_new: bool = False
) -> IntelligenceComponentManager:
    """
    Get or create the global Intelligence Component Manager.

    Args:
        config: Optional configuration (uses environment defaults if None)
        progress_callback: Optional progress reporting callback
        force_new: If True, create new instance even if one exists

    Returns:
        IntelligenceComponentManager instance
    """
    global _intelligence_manager

    async with _manager_lock:
        if _intelligence_manager is None or force_new:
            _intelligence_manager = IntelligenceComponentManager(
                config=config,
                progress_callback=progress_callback
            )

        return _intelligence_manager


async def shutdown_intelligence_manager() -> None:
    """Shutdown the global intelligence manager."""
    global _intelligence_manager

    if _intelligence_manager:
        await _intelligence_manager.shutdown()
        _intelligence_manager = None
