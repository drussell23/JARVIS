"""
JARVIS Orchestrator Package v1.0.0
===================================

Enterprise-grade cross-repository orchestration for the Trinity ecosystem.

This package provides modular components for managing the lifecycle of:
- JARVIS Body (main system - port 8010)
- JARVIS Prime (ML inference - port 8001)
- Reactor Core (training/learning - port 8090)

Package Structure:
    orchestrator/
    ├── __init__.py           - Package exports
    ├── service_registry.py   - Service discovery and registration
    ├── process_spawner.py    - Process lifecycle management
    ├── health_coordinator.py - Cross-service health monitoring
    ├── crash_recovery.py     - Intelligent crash recovery
    └── cloud_integration.py  - GCP VM management

Key Features:
1. SINGLE COMMAND STARTUP: `python unified_supervisor.py` starts all services
2. DYNAMIC SERVICE DISCOVERY: Auto-detect running services on ports
3. INTELLIGENT CRASH RECOVERY: OOM detection, GCP fallback, circuit breakers
4. HEALTH COORDINATION: Cross-service health aggregation and dependencies
5. GRACEFUL SHUTDOWN: Coordinated shutdown with proper ordering

Trinity Architecture:
    ┌─────────────────────────────────────────────────────────────┐
    │              JARVIS Orchestrator (Control Plane)             │
    ├─────────────────────────────────────────────────────────────┤
    │                                                             │
    │   Service Registry          Health Coordinator              │
    │   ┌─────────────────┐      ┌─────────────────┐             │
    │   │ jarvis-body     │      │ Aggregate Health │             │
    │   │ jarvis-prime    │ ───▶ │ Dependency Chain │             │
    │   │ reactor-core    │      │ Readiness Gates  │             │
    │   └─────────────────┘      └─────────────────┘             │
    │                                                             │
    │   Process Spawner           Crash Recovery                  │
    │   ┌─────────────────┐      ┌─────────────────┐             │
    │   │ Spawn Services  │      │ OOM Detection   │             │
    │   │ Monitor PIDs    │ ───▶ │ GCP Failover    │             │
    │   │ Graceful Stop   │      │ Auto-Restart    │             │
    │   └─────────────────┘      └─────────────────┘             │
    │                                                             │
    └─────────────────────────────────────────────────────────────┘
              │               │               │
    ┌─────────▼──────┐ ┌──────▼──────┐ ┌──────▼──────┐
    │  JARVIS Body   │ │JARVIS Prime │ │Reactor Core │
    │  (port 8010)   │ │ (port 8001) │ │ (port 8090) │
    │  main.py       │ │run_server.py│ │run_reactor  │
    └────────────────┘ └─────────────┘ └─────────────┘

Migration Strategy:
    This package extracts functionality from the monolithic
    cross_repo_startup_orchestrator.py (22K+ lines) into clean modules.
    
    1. New code should import from backend.orchestrator
    2. Legacy code can continue to import from cross_repo_startup_orchestrator
    3. Gradually move functionality to orchestrator modules

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

logger = logging.getLogger(__name__)

# Version
__version__ = "1.0.0"

# =============================================================================
# SERVICE PORTS (from trinity_config)
# =============================================================================

DEFAULT_PORTS = {
    "jarvis_body": 8010,
    "jarvis_prime": 8001,
    "reactor_core": 8090,
    "loading_server": 3001,
    "frontend": 3000,
    "websocket": 8765,
}


# =============================================================================
# LAZY IMPORTS
# =============================================================================

_service_registry: Optional[Any] = None
_process_spawner: Optional[Any] = None
_health_coordinator: Optional[Any] = None


def get_service_registry():
    """
    Get the service registry for managing Trinity services.
    
    Returns:
        ServiceRegistry instance
    """
    global _service_registry
    
    if _service_registry is not None:
        return _service_registry
    
    # Try to import from modular module
    try:
        from backend.orchestrator.service_registry import ServiceRegistry
        _service_registry = ServiceRegistry()
        return _service_registry
    except ImportError:
        pass
    
    # Fallback to cross_repo_startup_orchestrator
    try:
        from backend.supervisor.cross_repo_startup_orchestrator import (
            get_service_registry as _get_registry
        )
        return _get_registry()
    except ImportError:
        pass
    
    return None


def get_process_spawner():
    """
    Get the process spawner for managing service lifecycles.
    
    Returns:
        ProcessSpawner instance
    """
    global _process_spawner
    
    if _process_spawner is not None:
        return _process_spawner
    
    # Try to import from modular module
    try:
        from backend.orchestrator.process_spawner import ProcessSpawner
        _process_spawner = ProcessSpawner()
        return _process_spawner
    except ImportError:
        pass
    
    return None


def get_health_coordinator():
    """
    Get the health coordinator for cross-service health monitoring.
    
    Returns:
        HealthCoordinator instance
    """
    global _health_coordinator
    
    if _health_coordinator is not None:
        return _health_coordinator
    
    # Try to import from modular module
    try:
        from backend.orchestrator.health_coordinator import HealthCoordinator
        _health_coordinator = HealthCoordinator()
        return _health_coordinator
    except ImportError:
        pass
    
    return None


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

async def start_all_services(
    skip_prime: bool = False,
    skip_reactor: bool = False,
) -> Dict[str, bool]:
    """
    Start all Trinity services.
    
    Args:
        skip_prime: Skip JARVIS Prime startup
        skip_reactor: Skip Reactor Core startup
    
    Returns:
        Dictionary mapping service names to startup success status
    """
    # Try modular spawner first
    spawner = get_process_spawner()
    if spawner:
        return await spawner.start_all(
            skip_prime=skip_prime,
            skip_reactor=skip_reactor,
        )
    
    # Fallback to cross_repo_startup_orchestrator
    try:
        from backend.supervisor.cross_repo_startup_orchestrator import (
            CrossRepoStartupOrchestrator
        )
        orchestrator = CrossRepoStartupOrchestrator()
        await orchestrator.start_all_services()
        return {"jarvis_body": True, "jarvis_prime": not skip_prime, "reactor_core": not skip_reactor}
    except ImportError as e:
        logger.error(f"[Orchestrator] Failed to start services: {e}")
        return {"jarvis_body": False, "jarvis_prime": False, "reactor_core": False}


async def stop_all_services(timeout: float = 30.0) -> None:
    """
    Stop all Trinity services gracefully.
    
    Args:
        timeout: Maximum time to wait for services to stop
    """
    spawner = get_process_spawner()
    if spawner:
        await spawner.stop_all(timeout=timeout)
        return
    
    # Fallback
    try:
        from backend.supervisor.cross_repo_startup_orchestrator import (
            CrossRepoStartupOrchestrator
        )
        orchestrator = CrossRepoStartupOrchestrator()
        await orchestrator.shutdown_all_services()
    except ImportError:
        pass


async def get_aggregate_health() -> Dict[str, Any]:
    """
    Get aggregated health status of all Trinity services.
    
    Returns:
        Dictionary with health status for each service and overall status
    """
    coordinator = get_health_coordinator()
    if coordinator:
        return await coordinator.get_aggregate_health()
    
    # Fallback: basic health check
    from backend.core.trinity_config import get_config
    import aiohttp
    
    config = get_config()
    health = {
        "overall": "unknown",
        "services": {},
    }
    
    services = [
        ("jarvis_body", f"http://localhost:{config.jarvis_port}/health"),
        ("jarvis_prime", f"http://localhost:{config.jarvis_prime_port}/health"),
        ("reactor_core", f"http://localhost:{config.reactor_core_port}/health"),
    ]
    
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=5.0)
        ) as session:
            for name, url in services:
                try:
                    async with session.get(url) as resp:
                        health["services"][name] = {
                            "status": "healthy" if resp.status == 200 else "unhealthy",
                            "http_code": resp.status,
                        }
                except Exception as e:
                    health["services"][name] = {
                        "status": "offline",
                        "error": str(e),
                    }
    except Exception:
        pass
    
    # Determine overall health
    statuses = [s.get("status") for s in health["services"].values()]
    if all(s == "healthy" for s in statuses):
        health["overall"] = "healthy"
    elif any(s == "healthy" for s in statuses):
        health["overall"] = "degraded"
    else:
        health["overall"] = "unhealthy"
    
    return health


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Version
    "__version__",
    
    # Constants
    "DEFAULT_PORTS",
    
    # Instance accessors
    "get_service_registry",
    "get_process_spawner",
    "get_health_coordinator",
    
    # Convenience functions
    "start_all_services",
    "stop_all_services",
    "get_aggregate_health",
]

logger.debug(f"[Orchestrator] Package initialized (v{__version__})")
