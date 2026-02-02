"""
Enterprise Hooks - Integration layer for enterprise modules with existing supervisor.

This module provides hooks that wire the enterprise modules (RecoveryEngine,
CapabilityRouter, HealthContracts, SubprocessManager) into the existing
run_supervisor.py and cross_repo_startup_orchestrator.py without requiring
massive refactoring of those large files.

Features:
- Intelligent failure classification and recovery
- Capability-based routing with circuit breakers
- Health aggregation across all components
- Subprocess lifecycle management for cross-repo processes

Usage:
    from backend.core.enterprise_hooks import (
        enterprise_init,
        handle_gcp_failure,
        get_routing_decision,
        aggregate_health,
    )

    # Initialize at startup
    await enterprise_init()

    # Handle failures intelligently
    strategy = await handle_gcp_failure(error, context)

    # Route with fallback
    decision = await get_routing_decision("llm_inference")
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("jarvis.enterprise_hooks")

# =============================================================================
# Module Availability Detection
# =============================================================================

_ENTERPRISE_AVAILABLE = False
_RECOVERY_ENGINE = None
_CAPABILITY_ROUTER = None
_HEALTH_AGGREGATOR = None
_SUBPROCESS_MANAGER = None
_COMPONENT_REGISTRY = None

# =============================================================================
# Fallback Enums (used when enterprise modules aren't available)
# =============================================================================

class _FallbackRecoveryStrategy(Enum):
    """Fallback RecoveryStrategy enum when enterprise modules unavailable."""
    RETRY = "retry"
    FULL_RESTART = "full_restart"
    FALLBACK_MODE = "fallback_mode"
    DISABLE_AND_CONTINUE = "disable_and_continue"
    ESCALATE_TO_USER = "escalate_to_user"


class _FallbackErrorClass(Enum):
    """Fallback ErrorClass enum when enterprise modules unavailable."""
    TRANSIENT = "transient"
    TIMEOUT = "timeout"
    NETWORK = "network"
    RESOURCE_EXHAUSTED = "resource_exhausted"
    CONFIGURATION = "configuration"
    FATAL = "fatal"
    UNKNOWN = "unknown"


class _FallbackRecoveryPhase(Enum):
    """Fallback RecoveryPhase enum when enterprise modules unavailable."""
    STARTUP = "startup"
    RUNTIME = "runtime"
    SHUTDOWN = "shutdown"


# Default to fallback enums
RecoveryStrategy = _FallbackRecoveryStrategy
ErrorClass = _FallbackErrorClass
RecoveryPhase = _FallbackRecoveryPhase

# Placeholder types for when modules aren't available
RecoveryEngine = None
CapabilityRouter = None
CircuitBreaker = None
CircuitState = None
RoutingDecision = None
HealthStatus = None
HealthReport = None
CapabilityHealth = None
SystemHealth = None
SystemHealthAggregator = None
ComponentRegistry = None
ComponentDefinition = None
ComponentStatus = None
SubprocessManager = None
ProcessHandle = None
ProcessState = None

# Factory functions
get_recovery_engine = None
get_capability_router = None
get_registry = None
get_subprocess_manager = None

try:
    from backend.core.recovery_engine import (
        RecoveryEngine,
        RecoveryStrategy,
        RecoveryPhase,
        ErrorClass,
        ErrorClassifier,
        get_recovery_engine,
    )
    from backend.core.capability_router import (
        CapabilityRouter,
        CircuitBreaker,
        CircuitState,
        RoutingDecision,
        get_capability_router,
    )
    from backend.core.health_contracts import (
        HealthStatus,
        HealthReport,
        CapabilityHealth,
        SystemHealth,
        SystemHealthAggregator,
    )
    from backend.core.component_registry import (
        ComponentRegistry,
        ComponentDefinition,
        ComponentStatus,
        get_registry,
    )
    from backend.core.subprocess_manager import (
        SubprocessManager,
        ProcessHandle,
        ProcessState,
        get_subprocess_manager,
    )
    _ENTERPRISE_AVAILABLE = True
    logger.info("[Enterprise] All enterprise modules available")
except ImportError as e:
    logger.warning(f"[Enterprise] Enterprise modules not fully available: {e}")
    # Keep using fallback enums - they're already set above


# =============================================================================
# GCP-Specific Error Patterns
# =============================================================================

GCP_ERROR_PATTERNS = {
    # Timeout patterns
    "gcp_startup_timeout": [
        "GCP VM not ready after",
        "GCP ready wait timed out",
        "startup script may have failed",
        "GCP pre-warm failed",
    ],
    # Network patterns
    "gcp_network_error": [
        "Connection refused",
        "Connection timed out",
        "Network is unreachable",
        "No route to host",
    ],
    # VM state patterns
    "gcp_vm_state_error": [
        "VM is in TERMINATED state",
        "VM is in STAGING state",
        "VM failed to reach RUNNING",
        "Instance not found",
    ],
    # Resource patterns
    "gcp_resource_error": [
        "Quota exceeded",
        "Resource exhausted",
        "Zone does not have enough resources",
        "RESOURCE_ALREADY_EXISTS",
    ],
    # Auth patterns
    "gcp_auth_error": [
        "Permission denied",
        "Invalid credentials",
        "Token expired",
        "Authentication failed",
    ],
}

# Trinity fallback capabilities
TRINITY_FALLBACK_CHAIN = {
    "llm_inference": ["gcp_vm", "claude_api", "local_llama"],
    "voice_synthesis": ["gcp_tts", "local_tts", "espeak"],
    "voice_recognition": ["gcp_stt", "local_whisper"],
    "memory_retrieval": ["gcp_memory", "local_chromadb"],
}


# =============================================================================
# Error Classification for GCP/Trinity
# =============================================================================

@dataclass
class GCPErrorContext:
    """Context for GCP-related errors."""
    error: Exception
    error_message: str
    vm_ip: Optional[str] = None
    timeout_seconds: Optional[float] = None
    gcp_attempts: int = 0
    component: str = "gcp_vm"
    timestamp: datetime = field(default_factory=datetime.now)
    additional_context: Dict[str, Any] = field(default_factory=dict)


def classify_gcp_error(error: Exception, context: Optional[Dict] = None) -> Tuple[str, ErrorClass]:
    """
    Classify a GCP-related error into a category and error class.

    Args:
        error: The exception that occurred
        context: Optional additional context

    Returns:
        Tuple of (error_category, ErrorClass)
    """
    error_str = str(error).lower()
    context = context or {}

    # Check each pattern category
    for category, patterns in GCP_ERROR_PATTERNS.items():
        for pattern in patterns:
            if pattern.lower() in error_str:
                # Map category to error class
                if "timeout" in category:
                    return category, ErrorClass.TIMEOUT
                elif "network" in category:
                    return category, ErrorClass.NETWORK
                elif "resource" in category:
                    return category, ErrorClass.RESOURCE_EXHAUSTED
                elif "auth" in category:
                    return category, ErrorClass.CONFIGURATION
                else:
                    return category, ErrorClass.TRANSIENT

    # Default classification
    return "gcp_unknown", ErrorClass.TRANSIENT


# =============================================================================
# Enterprise Initialization
# =============================================================================

async def enterprise_init(
    enable_recovery: bool = True,
    enable_routing: bool = True,
    enable_health: bool = True,
    enable_subprocess: bool = True,
) -> bool:
    """
    Initialize enterprise modules.

    This should be called early in supervisor startup to enable
    enterprise-grade error handling, routing, and health monitoring.

    Args:
        enable_recovery: Enable RecoveryEngine
        enable_routing: Enable CapabilityRouter
        enable_health: Enable HealthAggregator
        enable_subprocess: Enable SubprocessManager

    Returns:
        True if initialization succeeded
    """
    global _RECOVERY_ENGINE, _CAPABILITY_ROUTER, _HEALTH_AGGREGATOR
    global _SUBPROCESS_MANAGER, _COMPONENT_REGISTRY

    if not _ENTERPRISE_AVAILABLE:
        logger.warning("[Enterprise] Modules not available, skipping init")
        return False

    try:
        logger.info("[Enterprise] Initializing enterprise modules...")

        # Initialize component registry
        _COMPONENT_REGISTRY = get_registry()

        # Initialize recovery engine
        if enable_recovery:
            _RECOVERY_ENGINE = get_recovery_engine(_COMPONENT_REGISTRY)
            # Register Trinity-specific recovery handlers
            _register_trinity_recovery_handlers(_RECOVERY_ENGINE)
            logger.info("[Enterprise] RecoveryEngine initialized")

        # Initialize capability router
        if enable_routing:
            _CAPABILITY_ROUTER = get_capability_router(_COMPONENT_REGISTRY)
            # Register Trinity fallback chains
            _register_trinity_fallback_chains(_CAPABILITY_ROUTER)
            logger.info("[Enterprise] CapabilityRouter initialized")

        # Initialize health aggregator
        if enable_health:
            _HEALTH_AGGREGATOR = SystemHealthAggregator(_COMPONENT_REGISTRY)
            logger.info("[Enterprise] HealthAggregator initialized")

        # Initialize subprocess manager
        if enable_subprocess:
            _SUBPROCESS_MANAGER = get_subprocess_manager(
                _COMPONENT_REGISTRY,
                _RECOVERY_ENGINE,
            )
            logger.info("[Enterprise] SubprocessManager initialized")

        logger.info("[Enterprise] All enterprise modules initialized successfully")
        return True

    except Exception as e:
        logger.error(f"[Enterprise] Initialization failed: {e}", exc_info=True)
        return False


def _register_trinity_recovery_handlers(engine: RecoveryEngine) -> None:
    """Register Trinity-specific recovery handlers."""
    # GCP timeout recovery
    async def gcp_timeout_recovery(component: str, error: Exception, context: Dict) -> bool:
        """Recover from GCP timeout by signaling fallback."""
        logger.info(f"[Enterprise] GCP timeout recovery for {component}")

        # Write fallback signal file
        fallback_file = Path.home() / ".jarvis" / "trinity" / "claude_api_fallback.json"
        fallback_file.parent.mkdir(parents=True, exist_ok=True)

        import json
        fallback_data = {
            "triggered_at": datetime.now().isoformat(),
            "reason": "gcp_timeout_recovery",
            "original_error": str(error),
            "component": component,
        }
        fallback_file.write_text(json.dumps(fallback_data, indent=2))

        logger.info(f"[Enterprise] Claude API fallback signal written: {fallback_file}")
        return True

    engine.register_custom_recovery("gcp_timeout", gcp_timeout_recovery)

    # Memory pressure recovery
    async def memory_pressure_recovery(component: str, error: Exception, context: Dict) -> bool:
        """Recover from memory pressure by triggering GCP offload."""
        logger.info(f"[Enterprise] Memory pressure recovery for {component}")

        # This is a hook point - the actual GCP provisioning is handled
        # by the existing cross_repo_startup_orchestrator
        return True

    engine.register_custom_recovery("memory_pressure", memory_pressure_recovery)


def _register_trinity_fallback_chains(router: CapabilityRouter) -> None:
    """Register Trinity fallback chains in the router."""
    for capability, providers in TRINITY_FALLBACK_CHAIN.items():
        for i, provider in enumerate(providers):
            if i == 0:
                # Primary provider
                router.register_provider(capability, provider)
            else:
                # Fallback provider
                router.register_fallback(capability, provider)


# =============================================================================
# Failure Handling Hooks
# =============================================================================

async def handle_gcp_failure(
    error: Exception,
    context: Optional[GCPErrorContext] = None,
) -> RecoveryStrategy:
    """
    Handle a GCP-related failure using the enterprise recovery engine.

    This is the main entry point for handling GCP failures. It classifies
    the error, determines the appropriate recovery strategy, and executes
    recovery actions.

    Args:
        error: The exception that occurred
        context: Optional GCP error context

    Returns:
        The recovery strategy that was applied
    """
    if not _RECOVERY_ENGINE:
        logger.warning("[Enterprise] RecoveryEngine not available, returning default strategy")
        return RecoveryStrategy.RETRY

    # Build context
    ctx = context or GCPErrorContext(error=error, error_message=str(error))

    # Classify the error
    category, error_class = classify_gcp_error(error)
    logger.info(f"[Enterprise] GCP error classified: {category} ({error_class.value})")

    # Determine recovery phase
    phase = RecoveryPhase.RUNTIME
    if ctx.gcp_attempts == 0:
        phase = RecoveryPhase.STARTUP

    # Handle failure through recovery engine
    result = await _RECOVERY_ENGINE.handle_failure(
        component=ctx.component,
        error=error,
        phase=phase,
        context={
            "category": category,
            "vm_ip": ctx.vm_ip,
            "timeout_seconds": ctx.timeout_seconds,
            "gcp_attempts": ctx.gcp_attempts,
            **ctx.additional_context,
        },
    )

    logger.info(f"[Enterprise] Recovery result: {result.strategy.value}, success={result.success}")
    return result.strategy


async def handle_memory_pressure(
    memory_percent: float,
    trend: str = "stable",
    slope: float = 0.0,
) -> RecoveryStrategy:
    """
    Handle memory pressure using the enterprise recovery engine.

    Args:
        memory_percent: Current memory usage percentage
        trend: Memory trend ("increasing", "stable", "decreasing")
        slope: Rate of change in percent per second

    Returns:
        The recovery strategy that was applied
    """
    if not _RECOVERY_ENGINE:
        return RecoveryStrategy.RETRY

    # Determine severity
    if memory_percent >= 95:
        severity = "critical"
    elif memory_percent >= 85:
        severity = "warning"
    else:
        severity = "normal"

    if severity == "normal":
        return RecoveryStrategy.RETRY  # No action needed

    # Create error for recovery engine
    error = RuntimeError(
        f"Memory pressure: {memory_percent:.1f}% used "
        f"(trend: {trend}, slope: {slope:.2f}%/s)"
    )

    result = await _RECOVERY_ENGINE.handle_failure(
        component="memory_monitor",
        error=error,
        phase=RecoveryPhase.RUNTIME,
        context={
            "memory_percent": memory_percent,
            "trend": trend,
            "slope": slope,
            "severity": severity,
        },
    )

    return result.strategy


# =============================================================================
# Routing Hooks
# =============================================================================

async def get_routing_decision(
    capability: str,
    preferred_provider: Optional[str] = None,
) -> Optional[RoutingDecision]:
    """
    Get a routing decision for a capability.

    Uses the CapabilityRouter to determine the best provider for a
    capability, considering health status and circuit breaker state.

    Args:
        capability: The capability to route (e.g., "llm_inference")
        preferred_provider: Optional preferred provider

    Returns:
        RoutingDecision with provider and fallback chain, or None
    """
    if not _CAPABILITY_ROUTER:
        logger.warning("[Enterprise] CapabilityRouter not available")
        return None

    return _CAPABILITY_ROUTER.get_routing_decision(capability, preferred_provider)


async def route_with_fallback(
    capability: str,
    operation: Callable[..., Awaitable[Any]],
    *args,
    **kwargs,
) -> Any:
    """
    Execute an operation with automatic fallback routing.

    If the primary provider fails, automatically tries fallback providers
    in order until one succeeds or all fail.

    Args:
        capability: The capability being used
        operation: The async operation to execute
        *args: Arguments to pass to operation
        **kwargs: Keyword arguments to pass to operation

    Returns:
        Result of the operation

    Raises:
        Exception: If all providers fail
    """
    if not _CAPABILITY_ROUTER:
        # No router, just execute directly
        return await operation(*args, **kwargs)

    return await _CAPABILITY_ROUTER.call_with_fallback(
        capability,
        operation,
        *args,
        **kwargs,
    )


def record_provider_success(capability: str, provider: str) -> None:
    """Record a successful provider call (updates circuit breaker)."""
    if _CAPABILITY_ROUTER:
        _CAPABILITY_ROUTER.record_success(capability)


def record_provider_failure(capability: str, provider: str, error: Exception) -> None:
    """Record a failed provider call (updates circuit breaker)."""
    if _CAPABILITY_ROUTER:
        _CAPABILITY_ROUTER.record_failure(capability, error)


def get_circuit_breaker_status(capability: str) -> Optional[Dict[str, Any]]:
    """Get circuit breaker status for a capability."""
    if _CAPABILITY_ROUTER:
        return _CAPABILITY_ROUTER.get_circuit_breaker_status(capability)
    return None


# =============================================================================
# Health Aggregation Hooks
# =============================================================================

async def aggregate_health() -> Optional[SystemHealth]:
    """
    Aggregate health from all components.

    Returns:
        SystemHealth with aggregated status, or None if not available
    """
    if not _HEALTH_AGGREGATOR:
        return None

    return await _HEALTH_AGGREGATOR.collect_all()


async def get_component_health(component: str) -> Optional[HealthReport]:
    """
    Get health for a specific component.

    Args:
        component: Component name

    Returns:
        HealthReport for the component, or None
    """
    if not _COMPONENT_REGISTRY:
        return None

    defn = _COMPONENT_REGISTRY.get(component)
    if not defn:
        return None

    return HealthReport(
        name=component,
        status=HealthStatus.HEALTHY if defn.status == ComponentStatus.RUNNING else HealthStatus.FAILED,
        timestamp=datetime.now(),
    )


def update_component_health(
    component: str,
    status: HealthStatus,
    message: Optional[str] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Update health status for a component.

    Args:
        component: Component name
        status: New health status
        message: Optional status message
        metrics: Optional health metrics
    """
    if not _COMPONENT_REGISTRY:
        return

    # Map HealthStatus to ComponentStatus
    status_map = {
        HealthStatus.HEALTHY: ComponentStatus.RUNNING,
        HealthStatus.DEGRADED: ComponentStatus.DEGRADED,
        HealthStatus.UNHEALTHY: ComponentStatus.FAILED,
        HealthStatus.FAILED: ComponentStatus.FAILED,
        HealthStatus.UNKNOWN: ComponentStatus.UNKNOWN,
    }

    component_status = status_map.get(status, ComponentStatus.UNKNOWN)
    _COMPONENT_REGISTRY.update_status(component, component_status)


# =============================================================================
# Subprocess Management Hooks
# =============================================================================

async def start_cross_repo_process(
    name: str,
    repo_path: str,
    entry_point: str = "main.py",
    env: Optional[Dict[str, str]] = None,
) -> Optional[ProcessHandle]:
    """
    Start a cross-repo subprocess using the enterprise SubprocessManager.

    Args:
        name: Component name
        repo_path: Path to the repository
        entry_point: Entry point script
        env: Additional environment variables

    Returns:
        ProcessHandle for the started process, or None
    """
    if not _SUBPROCESS_MANAGER or not _COMPONENT_REGISTRY:
        logger.warning("[Enterprise] SubprocessManager not available")
        return None

    # Create component definition if it doesn't exist
    if not _COMPONENT_REGISTRY.get(name):
        _COMPONENT_REGISTRY.register(ComponentDefinition(
            name=name,
            capabilities=set(),
            repo_path=repo_path,
        ))

    component = _COMPONENT_REGISTRY.get(name)
    if not component:
        return None

    return await _SUBPROCESS_MANAGER.start(component)


async def stop_cross_repo_process(
    name: str,
    graceful_timeout: float = 10.0,
) -> bool:
    """
    Stop a cross-repo subprocess gracefully.

    Args:
        name: Component name
        graceful_timeout: Seconds to wait before SIGKILL

    Returns:
        True if stopped successfully
    """
    if not _SUBPROCESS_MANAGER:
        return True

    return await _SUBPROCESS_MANAGER.stop(name, graceful_timeout)


async def restart_cross_repo_process(name: str) -> Optional[ProcessHandle]:
    """
    Restart a cross-repo subprocess with exponential backoff.

    Args:
        name: Component name

    Returns:
        ProcessHandle for the restarted process, or None
    """
    if not _SUBPROCESS_MANAGER:
        return None

    return await _SUBPROCESS_MANAGER.restart(name)


async def shutdown_all_processes(reverse_order: bool = True) -> None:
    """
    Shutdown all managed subprocesses.

    Args:
        reverse_order: If True, stop in reverse start order
    """
    if _SUBPROCESS_MANAGER:
        await _SUBPROCESS_MANAGER.shutdown_all(reverse_order)


def is_process_running(name: str) -> bool:
    """Check if a subprocess is running."""
    if not _SUBPROCESS_MANAGER:
        return False
    return _SUBPROCESS_MANAGER.is_running(name)


def get_process_handle(name: str) -> Optional[ProcessHandle]:
    """Get handle for a subprocess."""
    if not _SUBPROCESS_MANAGER:
        return None
    return _SUBPROCESS_MANAGER.get_handle(name)


# =============================================================================
# Convenience Functions
# =============================================================================

def is_enterprise_available() -> bool:
    """Check if enterprise modules are available."""
    return _ENTERPRISE_AVAILABLE


def get_enterprise_status() -> Dict[str, bool]:
    """Get status of all enterprise components."""
    return {
        "available": _ENTERPRISE_AVAILABLE,
        "recovery_engine": _RECOVERY_ENGINE is not None,
        "capability_router": _CAPABILITY_ROUTER is not None,
        "health_aggregator": _HEALTH_AGGREGATOR is not None,
        "subprocess_manager": _SUBPROCESS_MANAGER is not None,
        "component_registry": _COMPONENT_REGISTRY is not None,
    }


async def enterprise_shutdown() -> None:
    """Shutdown all enterprise components."""
    global _RECOVERY_ENGINE, _CAPABILITY_ROUTER, _HEALTH_AGGREGATOR
    global _SUBPROCESS_MANAGER, _COMPONENT_REGISTRY

    logger.info("[Enterprise] Shutting down enterprise components...")

    # Shutdown subprocess manager first
    if _SUBPROCESS_MANAGER:
        await shutdown_all_processes()
        _SUBPROCESS_MANAGER = None

    # Clear other references
    _RECOVERY_ENGINE = None
    _CAPABILITY_ROUTER = None
    _HEALTH_AGGREGATOR = None
    _COMPONENT_REGISTRY = None

    logger.info("[Enterprise] Enterprise shutdown complete")


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Initialization
    "enterprise_init",
    "enterprise_shutdown",
    "is_enterprise_available",
    "get_enterprise_status",
    # Failure handling
    "handle_gcp_failure",
    "handle_memory_pressure",
    "GCPErrorContext",
    "classify_gcp_error",
    # Routing
    "get_routing_decision",
    "route_with_fallback",
    "record_provider_success",
    "record_provider_failure",
    "get_circuit_breaker_status",
    # Health
    "aggregate_health",
    "get_component_health",
    "update_component_health",
    # Subprocess management
    "start_cross_repo_process",
    "stop_cross_repo_process",
    "restart_cross_repo_process",
    "shutdown_all_processes",
    "is_process_running",
    "get_process_handle",
    # Constants
    "GCP_ERROR_PATTERNS",
    "TRINITY_FALLBACK_CHAIN",
]
