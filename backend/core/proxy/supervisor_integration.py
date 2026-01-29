"""
Supervisor Integration - Bridge between run_supervisor.py and Distributed Proxy System

Provides drop-in replacement functions for existing CloudSQL initialization,
allowing gradual migration to the new distributed proxy system.

Usage in run_supervisor.py:
    from backend.core.proxy.supervisor_integration import (
        initialize_distributed_proxy,
        get_proxy_orchestrator,
    )

    # Replace old _initialize_cloudsql_proxy call with:
    result = await initialize_distributed_proxy(
        repo_name="jarvis",
        components=components,
    )

Author: JARVIS System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, FrozenSet, List, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# Result Types
# =============================================================================

@dataclass
class ProxyInitResult:
    """Result from distributed proxy initialization."""
    ready: bool
    message: str
    latency: Optional[float] = None
    failure_reason: Optional[str] = None
    is_leader: bool = False
    startup_duration: Optional[float] = None
    phases_completed: List[str] = None

    def __post_init__(self):
        if self.phases_completed is None:
            self.phases_completed = []


# =============================================================================
# Global Orchestrator Instance
# =============================================================================

_orchestrator_instance: Optional[Any] = None
_orchestrator_lock = asyncio.Lock()


async def get_proxy_orchestrator() -> Optional[Any]:
    """Get the singleton orchestrator instance."""
    return _orchestrator_instance


async def set_proxy_orchestrator(orchestrator: Any) -> None:
    """Set the singleton orchestrator instance."""
    global _orchestrator_instance
    _orchestrator_instance = orchestrator


# =============================================================================
# Feature Flag
# =============================================================================

def is_distributed_proxy_enabled() -> bool:
    """Check if the new distributed proxy system is enabled."""
    # Can be disabled via environment variable during rollout
    return os.getenv("DISTRIBUTED_PROXY_ENABLED", "true").lower() in ("1", "true", "yes")


def is_legacy_fallback_enabled() -> bool:
    """Check if fallback to legacy system is enabled."""
    return os.getenv("DISTRIBUTED_PROXY_LEGACY_FALLBACK", "true").lower() in ("1", "true", "yes")


# =============================================================================
# Main Integration Function
# =============================================================================

async def initialize_distributed_proxy(
    repo_name: str = "jarvis",
    components: Optional[List[Dict[str, Any]]] = None,
    on_ready_callback: Optional[Callable[[], Awaitable[None]]] = None,
    print_func: Optional[Callable[[str], None]] = None,
) -> ProxyInitResult:
    """
    Initialize the distributed proxy system.

    This is the main integration point for run_supervisor.py. It:
    1. Runs leader election
    2. Starts proxy (if leader)
    3. Verifies connectivity (all repos)
    4. Initializes registered components in dependency order

    Args:
        repo_name: Name of this repository (jarvis, prime, reactor)
        components: Optional list of component definitions to initialize
        on_ready_callback: Called when CloudSQL is verified ready
        print_func: Function to print status messages (for supervisor UI)

    Returns:
        ProxyInitResult with success/failure status
    """
    global _orchestrator_instance

    # Helper for printing
    def _print(msg: str) -> None:
        if print_func:
            print_func(msg)
        else:
            print(msg)

    # Check feature flag
    if not is_distributed_proxy_enabled():
        logger.info("[DistributedProxy] Disabled via DISTRIBUTED_PROXY_ENABLED=false")
        _print("  ○ Distributed Proxy: Disabled (using legacy system)")
        return ProxyInitResult(
            ready=False,
            message="Distributed proxy disabled",
            failure_reason="feature_disabled",
        )

    start_time = time.time()

    try:
        # Import the orchestrator
        from .orchestrator import (
            UnifiedProxyOrchestrator,
            setup_signal_handlers,
        )
        from .startup_barrier import ComponentManifest, DependencyType

        async with _orchestrator_lock:
            # Create orchestrator
            _print("  🚀 Initializing Distributed Proxy System...")

            # Convert component definitions to manifests
            component_manifests = []
            if components:
                for comp in components:
                    # Support both dict and ComponentManifest
                    if isinstance(comp, ComponentManifest):
                        component_manifests.append(comp)
                    else:
                        # Convert dict to ComponentManifest
                        deps = comp.get("dependencies", [])
                        if isinstance(deps, (list, set)):
                            deps = frozenset(
                                DependencyType[d] if isinstance(d, str) else d
                                for d in deps
                            )

                        manifest = ComponentManifest(
                            name=comp.get("name", "unknown"),
                            dependencies=deps,
                            init_func=comp.get("init_func", lambda: asyncio.sleep(0)),
                            priority=comp.get("priority", 50),
                            timeout=comp.get("timeout", 30.0),
                            required=comp.get("required", True),
                            description=comp.get("description", ""),
                        )
                        component_manifests.append(manifest)

            orchestrator = UnifiedProxyOrchestrator(
                repo_name=repo_name,
                components=component_manifests,
            )

            # Start the orchestrator
            _print("  🔐 Running leader election...")
            success = await orchestrator.start()

            if success:
                # Store for later access
                _orchestrator_instance = orchestrator

                # Get status
                status = orchestrator.get_status()
                is_leader = status.get("is_leader", False)
                startup_info = status.get("startup", {})
                phases = [p.get("name", "") for p in startup_info.get("phases", [])]

                duration = time.time() - start_time

                # Report success
                role = "LEADER" if is_leader else "FOLLOWER"
                _print(f"  ✅ Distributed Proxy Ready ({role}, {duration:.1f}s)")

                # Call ready callback
                if on_ready_callback:
                    try:
                        await on_ready_callback()
                    except Exception as e:
                        logger.warning(f"[DistributedProxy] Ready callback failed: {e}")

                return ProxyInitResult(
                    ready=True,
                    message=f"Proxy ready as {role}",
                    is_leader=is_leader,
                    startup_duration=duration,
                    phases_completed=phases,
                )

            else:
                # Startup failed
                duration = time.time() - start_time
                status = orchestrator.get_status()
                state = status.get("state", "UNKNOWN")

                _print(f"  ⚠️ Distributed Proxy Failed (state: {state})")

                # Try legacy fallback if enabled
                if is_legacy_fallback_enabled():
                    _print("  🔄 Attempting legacy fallback...")
                    return await _legacy_fallback(print_func=print_func)

                return ProxyInitResult(
                    ready=False,
                    message=f"Startup failed in state {state}",
                    failure_reason=state,
                    startup_duration=duration,
                )

    except ImportError as e:
        logger.warning(f"[DistributedProxy] Import error: {e}")
        _print(f"  ⚠️ Distributed Proxy: Module import failed")

        # Fall back to legacy system
        if is_legacy_fallback_enabled():
            _print("  🔄 Falling back to legacy system...")
            return await _legacy_fallback(print_func=print_func)

        return ProxyInitResult(
            ready=False,
            message=f"Import error: {e}",
            failure_reason="import_error",
        )

    except Exception as e:
        logger.exception(f"[DistributedProxy] Initialization failed: {e}")
        _print(f"  ❌ Distributed Proxy: Error - {e}")

        # Fall back to legacy system
        if is_legacy_fallback_enabled():
            _print("  🔄 Falling back to legacy system...")
            return await _legacy_fallback(print_func=print_func)

        return ProxyInitResult(
            ready=False,
            message=str(e),
            failure_reason="exception",
        )


async def _legacy_fallback(print_func: Optional[Callable[[str], None]] = None) -> ProxyInitResult:
    """Fall back to legacy ProxyReadinessGate system."""
    def _print(msg: str) -> None:
        if print_func:
            print_func(msg)
        else:
            print(msg)

    try:
        # Import legacy system
        try:
            from backend.intelligence.cloud_sql_connection_manager import (
                ProxyReadinessGate,
            )
        except ImportError:
            from intelligence.cloud_sql_connection_manager import (
                ProxyReadinessGate,
            )

        gate = ProxyReadinessGate.get_instance()

        timeout = float(os.getenv("CLOUDSQL_ENSURE_READY_TIMEOUT", "60.0"))
        max_attempts = int(os.getenv("CLOUDSQL_PROXY_START_ATTEMPTS", "3"))

        result = await gate.ensure_proxy_ready(
            timeout=timeout,
            auto_start=True,
            max_start_attempts=max_attempts,
            notify_cross_repo=True,
        )

        if result.ready:
            latency_ms = (result.latency or 0) * 1000
            _print(f"  ✅ Legacy Proxy Ready ({latency_ms:.1f}ms)")
            return ProxyInitResult(
                ready=True,
                message="Legacy proxy ready",
                latency=result.latency,
            )
        else:
            _print(f"  ⚠️ Legacy Proxy Failed: {result.failure_reason}")
            return ProxyInitResult(
                ready=False,
                message=result.message,
                failure_reason=result.failure_reason,
            )

    except Exception as e:
        _print(f"  ❌ Legacy Fallback Failed: {e}")
        return ProxyInitResult(
            ready=False,
            message=str(e),
            failure_reason="legacy_fallback_failed",
        )


# =============================================================================
# Shutdown Integration
# =============================================================================

async def shutdown_distributed_proxy(graceful: bool = True) -> bool:
    """
    Shutdown the distributed proxy system.

    Call this during supervisor shutdown.
    """
    global _orchestrator_instance

    if _orchestrator_instance is None:
        return True

    try:
        await _orchestrator_instance.stop(graceful=graceful)
        _orchestrator_instance = None
        return True
    except Exception as e:
        logger.error(f"[DistributedProxy] Shutdown error: {e}")
        return False


# =============================================================================
# Status and Diagnostics
# =============================================================================

async def get_proxy_status() -> Dict[str, Any]:
    """Get current proxy system status."""
    if _orchestrator_instance is None:
        return {
            "initialized": False,
            "distributed_enabled": is_distributed_proxy_enabled(),
        }

    try:
        status = _orchestrator_instance.get_status()
        return {
            "initialized": True,
            "distributed_enabled": True,
            **status,
        }
    except Exception as e:
        return {
            "initialized": True,
            "error": str(e),
        }


async def diagnose_proxy_failure(failure_time: Optional[float] = None) -> Dict[str, Any]:
    """Run diagnosis on a proxy failure."""
    if _orchestrator_instance is None:
        return {"error": "Orchestrator not initialized"}

    try:
        return await _orchestrator_instance.diagnose(failure_time)
    except Exception as e:
        return {"error": str(e)}


# =============================================================================
# Signal CloudSQL Ready to Registry (Compatibility)
# =============================================================================

async def signal_cloudsql_ready() -> bool:
    """
    Signal to AgentRegistry that CloudSQL dependency is ready.

    This maintains compatibility with the existing v112.0 dependency system.
    """
    try:
        # Import AgentRegistry
        try:
            from backend.neural_mesh.registry.agent_registry import AgentRegistry
        except ImportError:
            try:
                from neural_mesh.registry.agent_registry import AgentRegistry
            except ImportError:
                logger.debug("[DistributedProxy] AgentRegistry not available")
                return False

        registry = AgentRegistry()
        if hasattr(registry, 'set_dependency_ready'):
            set_dep_method = registry.set_dependency_ready
            metadata = {
                "source": "distributed_proxy_orchestrator",
                "version": "1.0.0",
                "ready_at": time.time(),
            }

            if asyncio.iscoroutinefunction(set_dep_method):
                await set_dep_method("cloudsql", True, metadata)
            else:
                set_dep_method("cloudsql", True, metadata)

            return True

        return False

    except Exception as e:
        logger.warning(f"[DistributedProxy] Failed to signal registry: {e}")
        return False


# =============================================================================
# Component Registration Helpers
# =============================================================================

def create_component_manifest(
    name: str,
    init_func: Callable[[], Awaitable[bool]],
    dependencies: Optional[List[str]] = None,
    priority: int = 50,
    timeout: float = 30.0,
    required: bool = True,
    description: str = "",
) -> Dict[str, Any]:
    """
    Helper to create a component manifest dictionary.

    This is a convenience function for registering components
    without directly importing ComponentManifest.

    Args:
        name: Component name
        init_func: Async function that returns True on success
        dependencies: List of dependency names (e.g., ["CLOUDSQL", "FILESYSTEM"])
        priority: Lower = earlier initialization (0-100)
        timeout: Maximum time for initialization
        required: If True, failure blocks startup
        description: Human-readable description

    Returns:
        Dictionary that can be passed to initialize_distributed_proxy
    """
    return {
        "name": name,
        "init_func": init_func,
        "dependencies": dependencies or [],
        "priority": priority,
        "timeout": timeout,
        "required": required,
        "description": description,
    }
