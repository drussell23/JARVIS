"""
JARVIS Health Coordinator v1.0.0
=================================

Cross-service health monitoring and coordination for the Trinity ecosystem.

Provides:
1. Aggregated health status across all services
2. Dependency-aware health evaluation
3. Readiness gates for startup coordination
4. Health degradation detection and alerting
5. Circuit breaker integration

Health Model:
    The coordinator uses a hierarchical health model:
    
    System Health (aggregate)
    ├── JARVIS Body Health
    │   ├── Backend API (required)
    │   ├── Vision System (optional)
    │   └── Voice Pipeline (optional)
    ├── JARVIS Prime Health
    │   ├── Inference API (required if Prime enabled)
    │   └── Model Status (degraded OK)
    └── Reactor Core Health
        ├── Training API (optional)
        └── Data Pipeline (degraded OK)

Health States:
    HEALTHY     - All required components operational
    DEGRADED    - Required components OK, optional components impaired
    UNHEALTHY   - Required components failing
    UNKNOWN     - Cannot determine health state

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)


# =============================================================================
# ENUMS
# =============================================================================

class HealthLevel(Enum):
    """System health levels."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ComponentCriticality(Enum):
    """Criticality levels for components."""
    REQUIRED = "required"       # System cannot function without this
    DEGRADED_OK = "degraded_ok" # System degrades but continues without this
    OPTIONAL = "optional"       # System fully functional without this


# =============================================================================
# HEALTH STATUS
# =============================================================================

@dataclass
class ComponentHealth:
    """Health status of a single component."""
    name: str
    level: HealthLevel
    criticality: ComponentCriticality
    message: str = ""
    latency_ms: float = 0.0
    last_check: datetime = field(default_factory=datetime.now)
    consecutive_failures: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    @property
    def is_healthy(self) -> bool:
        return self.level == HealthLevel.HEALTHY
    
    @property
    def is_critical_failure(self) -> bool:
        return (
            self.criticality == ComponentCriticality.REQUIRED and
            self.level in (HealthLevel.UNHEALTHY, HealthLevel.UNKNOWN)
        )
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level.value,
            "criticality": self.criticality.value,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "last_check": self.last_check.isoformat(),
            "consecutive_failures": self.consecutive_failures,
        }


@dataclass
class AggregateHealth:
    """Aggregated health status for the system."""
    level: HealthLevel
    components: Dict[str, ComponentHealth]
    message: str = ""
    checked_at: datetime = field(default_factory=datetime.now)
    
    @property
    def healthy_count(self) -> int:
        return len([c for c in self.components.values() if c.is_healthy])
    
    @property
    def unhealthy_count(self) -> int:
        return len([c for c in self.components.values() if not c.is_healthy])
    
    @property
    def critical_failures(self) -> List[str]:
        return [c.name for c in self.components.values() if c.is_critical_failure]
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "message": self.message,
            "checked_at": self.checked_at.isoformat(),
            "healthy_count": self.healthy_count,
            "unhealthy_count": self.unhealthy_count,
            "critical_failures": self.critical_failures,
            "components": {
                name: comp.to_dict()
                for name, comp in self.components.items()
            },
        }


# =============================================================================
# COMPONENT CONFIGURATION
# =============================================================================

DEFAULT_COMPONENTS: Dict[str, Dict[str, Any]] = {
    "jarvis-body": {
        "criticality": ComponentCriticality.REQUIRED,
        "url": "http://localhost:8010/health",
        "timeout": 5.0,
    },
    "jarvis-prime": {
        "criticality": ComponentCriticality.DEGRADED_OK,
        "url": "http://localhost:8001/health",
        "timeout": 10.0,  # Prime may be slow during model loading
    },
    "reactor-core": {
        "criticality": ComponentCriticality.OPTIONAL,
        "url": "http://localhost:8090/health",
        "timeout": 5.0,
    },
    "loading-server": {
        "criticality": ComponentCriticality.OPTIONAL,
        "url": "http://localhost:3001/health",
        "timeout": 3.0,
    },
}


# =============================================================================
# HEALTH COORDINATOR
# =============================================================================

class HealthCoordinator:
    """
    Coordinates health monitoring across all Trinity services.
    
    Features:
    - Parallel health checks for efficiency
    - Circuit breaker integration for failing services
    - Configurable criticality levels
    - Aggregate health calculation
    - Health change callbacks
    
    Usage:
        coordinator = HealthCoordinator()
        
        # Check all services
        health = await coordinator.check_all()
        print(f"System health: {health.level.value}")
        
        # Check specific service
        component = await coordinator.check_component("jarvis-prime")
        
        # Add health change callback
        coordinator.add_callback(on_health_change)
    """
    
    _instance: Optional["HealthCoordinator"] = None
    
    def __new__(cls) -> "HealthCoordinator":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._components: Dict[str, Dict[str, Any]] = {}
        self._last_health: Dict[str, ComponentHealth] = {}
        self._callbacks: List[Callable[[str, HealthLevel], None]] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._check_lock = asyncio.Lock()
        self._initialized = True
        
        # Register default components
        for name, config in DEFAULT_COMPONENTS.items():
            self._components[name] = config
        
        logger.debug("[HealthCoordinator] Initialized")
    
    def register_component(
        self,
        name: str,
        url: str,
        criticality: ComponentCriticality = ComponentCriticality.OPTIONAL,
        timeout: float = 5.0,
    ) -> None:
        """
        Register a component for health monitoring.
        
        Args:
            name: Component name
            url: Health check URL
            criticality: How critical this component is
            timeout: Health check timeout
        """
        self._components[name] = {
            "criticality": criticality,
            "url": url,
            "timeout": timeout,
        }
        logger.debug(f"[HealthCoordinator] Registered component: {name}")
    
    def add_callback(
        self,
        callback: Callable[[str, HealthLevel], None],
    ) -> None:
        """Add a callback for health level changes."""
        self._callbacks.append(callback)
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30.0)
            )
        return self._session
    
    async def check_component(self, name: str) -> ComponentHealth:
        """
        Check health of a single component.
        
        Args:
            name: Component name
        
        Returns:
            ComponentHealth with current status
        """
        config = self._components.get(name)
        if not config:
            return ComponentHealth(
                name=name,
                level=HealthLevel.UNKNOWN,
                criticality=ComponentCriticality.OPTIONAL,
                message=f"Unknown component: {name}",
            )
        
        url = config["url"]
        timeout = config.get("timeout", 5.0)
        criticality = config["criticality"]
        
        start_time = time.time()
        
        try:
            session = await self._get_session()
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                latency_ms = (time.time() - start_time) * 1000
                
                if resp.status in (200, 204):
                    # Try to parse response for detailed health
                    try:
                        data = await resp.json()
                        level = HealthLevel.HEALTHY
                        message = data.get("message", "Healthy")
                        
                        # Check for degraded status in response
                        status = data.get("status", "").lower()
                        if status == "degraded":
                            level = HealthLevel.DEGRADED
                    except Exception:
                        level = HealthLevel.HEALTHY
                        message = "Healthy (no JSON response)"
                    
                    health = ComponentHealth(
                        name=name,
                        level=level,
                        criticality=criticality,
                        message=message,
                        latency_ms=latency_ms,
                        consecutive_failures=0,
                    )
                else:
                    health = ComponentHealth(
                        name=name,
                        level=HealthLevel.UNHEALTHY,
                        criticality=criticality,
                        message=f"HTTP {resp.status}",
                        latency_ms=latency_ms,
                        consecutive_failures=(
                            self._last_health.get(name, ComponentHealth(
                                name=name, level=HealthLevel.UNKNOWN,
                                criticality=criticality
                            )).consecutive_failures + 1
                        ),
                    )
        
        except asyncio.TimeoutError:
            health = ComponentHealth(
                name=name,
                level=HealthLevel.UNHEALTHY,
                criticality=criticality,
                message=f"Timeout after {timeout}s",
                latency_ms=timeout * 1000,
                consecutive_failures=(
                    self._last_health.get(name, ComponentHealth(
                        name=name, level=HealthLevel.UNKNOWN,
                        criticality=criticality
                    )).consecutive_failures + 1
                ),
            )
        
        except aiohttp.ClientConnectorError:
            health = ComponentHealth(
                name=name,
                level=HealthLevel.UNHEALTHY,
                criticality=criticality,
                message="Connection refused",
                latency_ms=0,
                consecutive_failures=(
                    self._last_health.get(name, ComponentHealth(
                        name=name, level=HealthLevel.UNKNOWN,
                        criticality=criticality
                    )).consecutive_failures + 1
                ),
            )
        
        except Exception as e:
            health = ComponentHealth(
                name=name,
                level=HealthLevel.UNKNOWN,
                criticality=criticality,
                message=str(e)[:100],
                latency_ms=0,
                consecutive_failures=(
                    self._last_health.get(name, ComponentHealth(
                        name=name, level=HealthLevel.UNKNOWN,
                        criticality=criticality
                    )).consecutive_failures + 1
                ),
            )
        
        # Check for health level change
        old_health = self._last_health.get(name)
        if old_health and old_health.level != health.level:
            logger.info(
                f"[HealthCoordinator] {name}: {old_health.level.value} -> {health.level.value}"
            )
            for callback in self._callbacks:
                try:
                    callback(name, health.level)
                except Exception as e:
                    logger.warning(f"[HealthCoordinator] Callback failed: {e}")
        
        self._last_health[name] = health
        return health
    
    async def check_all(self) -> AggregateHealth:
        """
        Check health of all registered components in parallel.
        
        Returns:
            AggregateHealth with status of all components
        """
        async with self._check_lock:
            # Check all components in parallel
            tasks = [
                self.check_component(name)
                for name in self._components.keys()
            ]
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Build component health dict
            components: Dict[str, ComponentHealth] = {}
            for name, result in zip(self._components.keys(), results):
                if isinstance(result, Exception):
                    components[name] = ComponentHealth(
                        name=name,
                        level=HealthLevel.UNKNOWN,
                        criticality=self._components[name]["criticality"],
                        message=str(result)[:100],
                    )
                else:
                    components[name] = result
            
            # Calculate aggregate health
            level = self._calculate_aggregate_level(components)
            message = self._generate_health_message(components, level)
            
            return AggregateHealth(
                level=level,
                components=components,
                message=message,
            )
    
    def _calculate_aggregate_level(
        self,
        components: Dict[str, ComponentHealth],
    ) -> HealthLevel:
        """
        Calculate aggregate health level based on component health.
        
        Rules:
        - If any REQUIRED component is unhealthy -> UNHEALTHY
        - If any REQUIRED component is unknown -> UNKNOWN
        - If any DEGRADED_OK component is unhealthy -> DEGRADED
        - Otherwise -> HEALTHY
        """
        required_unhealthy = False
        required_unknown = False
        degraded_ok_unhealthy = False
        
        for comp in components.values():
            if comp.criticality == ComponentCriticality.REQUIRED:
                if comp.level == HealthLevel.UNHEALTHY:
                    required_unhealthy = True
                elif comp.level == HealthLevel.UNKNOWN:
                    required_unknown = True
            elif comp.criticality == ComponentCriticality.DEGRADED_OK:
                if comp.level in (HealthLevel.UNHEALTHY, HealthLevel.UNKNOWN):
                    degraded_ok_unhealthy = True
        
        if required_unhealthy:
            return HealthLevel.UNHEALTHY
        if required_unknown:
            return HealthLevel.UNKNOWN
        if degraded_ok_unhealthy:
            return HealthLevel.DEGRADED
        
        return HealthLevel.HEALTHY
    
    def _generate_health_message(
        self,
        components: Dict[str, ComponentHealth],
        level: HealthLevel,
    ) -> str:
        """Generate a human-readable health message."""
        healthy = [n for n, c in components.items() if c.is_healthy]
        unhealthy = [n for n, c in components.items() if not c.is_healthy]
        
        if level == HealthLevel.HEALTHY:
            return f"All {len(healthy)} components healthy"
        elif level == HealthLevel.DEGRADED:
            return f"Degraded: {', '.join(unhealthy)} unhealthy"
        elif level == HealthLevel.UNHEALTHY:
            critical = [n for n, c in components.items() if c.is_critical_failure]
            return f"Critical failure: {', '.join(critical)}"
        else:
            return "Unable to determine health status"
    
    async def get_aggregate_health(self) -> Dict[str, Any]:
        """
        Get aggregate health as a dictionary (convenience method).
        
        Returns:
            Dictionary representation of aggregate health
        """
        health = await self.check_all()
        return health.to_dict()
    
    async def wait_for_ready(
        self,
        components: Optional[List[str]] = None,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
    ) -> bool:
        """
        Wait for specified components to become healthy.
        
        Args:
            components: List of component names (None = all required)
            timeout: Maximum time to wait
            poll_interval: Time between health checks
        
        Returns:
            True if all components became healthy, False if timeout
        """
        if components is None:
            components = [
                name for name, config in self._components.items()
                if config["criticality"] == ComponentCriticality.REQUIRED
            ]
        
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            # Check specified components
            all_healthy = True
            for name in components:
                health = await self.check_component(name)
                if not health.is_healthy:
                    all_healthy = False
                    logger.debug(
                        f"[HealthCoordinator] Waiting for {name}: {health.level.value}"
                    )
            
            if all_healthy:
                logger.info(
                    f"[HealthCoordinator] All {len(components)} components ready "
                    f"after {time.time() - start_time:.1f}s"
                )
                return True
            
            await asyncio.sleep(poll_interval)
        
        logger.warning(
            f"[HealthCoordinator] Timeout waiting for components: {components}"
        )
        return False
    
    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()


# =============================================================================
# SINGLETON ACCESS
# =============================================================================

_coordinator_instance: Optional[HealthCoordinator] = None


def get_health_coordinator() -> HealthCoordinator:
    """Get the singleton HealthCoordinator instance."""
    global _coordinator_instance
    if _coordinator_instance is None:
        _coordinator_instance = HealthCoordinator()
    return _coordinator_instance


# =============================================================================
# MODULE INITIALIZATION
# =============================================================================

logger.debug("[HealthCoordinator] Module loaded")
