# backend/core/log_severity_bridge.py
"""
Log Severity Bridge - Temporary module for criticality-based logging.

This module provides immediate log noise relief by deriving log severity
from component criticality. It will be replaced by ComponentLogger once
the full ComponentRegistry is implemented.

Usage:
    from backend.core.log_severity_bridge import log_component_failure

    try:
        await connect_redis()
    except Exception as e:
        log_component_failure("redis", "Connection failed", error=e)
"""
import os
import logging
from typing import Optional

logger = logging.getLogger("jarvis.component_bridge")

# Component criticality map
# REQUIRED: System cannot start without this - logs ERROR
# DEGRADED_OK: Can run degraded if unavailable - logs WARNING
# OPTIONAL: Nice to have - logs INFO
COMPONENT_CRITICALITY = {
    # Required - system cannot function without these
    "jarvis-core": "required",
    "backend": "required",

    # Degraded OK - preferred but can fallback
    "jarvis-prime": "degraded_ok",
    "cloud-sql": "degraded_ok",
    "gcp-vm": "degraded_ok",
    "gcp-prewarm": "degraded_ok",
    "voice-unlock": "degraded_ok",

    # Optional - nice to have
    "redis": "optional",
    "reactor-core": "optional",
    "frontend": "optional",
    "trinity": "optional",
    "trinity-indexer": "optional",
    "trinity-bridge": "optional",
    "ouroboros": "optional",
    "uae": "optional",
    "sai": "optional",
    "neural-mesh": "optional",
    "mas": "optional",
    "cai": "optional",
    "docker-manager": "optional",
    "infrastructure-orchestrator": "optional",
    "ipc-hub": "optional",
    "state-manager": "optional",
    "observability": "optional",
    "di-container": "optional",
    "cost-sync": "optional",
    "cost-tracker": "optional",
    "hybrid-router": "optional",
    "heartbeat-system": "optional",
    "knowledge-indexer": "optional",
    "voice-coordinator": "optional",
    "coding-council": "optional",
    "slim": "optional",
    "hollow-client": "optional",
}


def _normalize_component_name(name: str) -> str:
    """Normalize component name to canonical kebab-case.

    Examples:
        "jarvis_prime" -> "jarvis-prime"
        "JARVIS_PRIME" -> "jarvis-prime"
        "Jarvis Prime" -> "jarvis-prime"
    """
    return name.lower().replace("_", "-").replace(" ", "-")


def _get_criticality(canonical: str) -> str:
    """Get criticality for component, checking env override first.

    Environment variable format: {COMPONENT_NAME}_CRITICALITY
    Example: REDIS_CRITICALITY=required
    """
    env_key = f"{canonical.upper().replace('-', '_')}_CRITICALITY"
    override = os.environ.get(env_key)
    if override and override.lower() in ("required", "degraded_ok", "optional"):
        return override.lower()
    return COMPONENT_CRITICALITY.get(canonical, "optional")


def log_component_failure(
    component: str,
    message: str,
    error: Optional[Exception] = None,
    **context
) -> None:
    """Log a component failure at appropriate severity based on criticality.

    This is a temporary bridge function that will be replaced by ComponentLogger
    once the full ComponentRegistry is implemented.

    Args:
        component: Component name (will be normalized)
        message: Failure message
        error: Optional exception to include traceback
        **context: Additional context to include in log
    """
    canonical = _normalize_component_name(component)
    criticality = _get_criticality(canonical)

    extra = context if context else None
    exc_info = (type(error), error, error.__traceback__) if error else None

    if criticality == "required":
        logger.error(f"{canonical}: {message}", exc_info=exc_info, extra=extra)
    elif criticality == "degraded_ok":
        logger.warning(f"{canonical}: {message}", exc_info=exc_info, extra=extra)
    else:  # optional
        logger.info(f"{canonical} (optional): {message}", exc_info=exc_info, extra=extra)


def is_component_required(component: str) -> bool:
    """Check if a component is required for system operation."""
    canonical = _normalize_component_name(component)
    return _get_criticality(canonical) == "required"


def is_component_optional(component: str) -> bool:
    """Check if a component is fully optional."""
    canonical = _normalize_component_name(component)
    return _get_criticality(canonical) == "optional"
