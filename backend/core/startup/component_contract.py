"""
JARVIS Component Contract System v149.2
=======================================

Enterprise-grade component classification and logging severity policy.

This module defines the contract between the startup system and individual
components, enabling:
- Explicit required vs optional classification
- Appropriate logging severity based on component type
- Environment-based overrides for flexibility
- Consistent failure handling across the system

Usage:
    from backend.core.startup.component_contract import (
        ComponentType, 
        ComponentState,
        get_failure_level,
        get_component_type,
    )
    
    # Get the type of a component
    component_type = get_component_type("cloudsql")  # OPTIONAL in dev
    
    # Get the appropriate log level for a failure
    level = get_failure_level(component_type)  # logging.INFO for optional
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, Set


# =============================================================================
# COMPONENT CLASSIFICATION
# =============================================================================

class ComponentType(Enum):
    """
    Classification of component importance for startup behavior.
    
    REQUIRED: Component must initialize successfully. Failure is logged as
              ERROR and may block startup.
              
    OPTIONAL: Component is nice-to-have. Failure is logged as INFO and
              startup continues with reduced functionality.
              
    DEGRADABLE: Component is important but can operate in degraded mode.
                Failure is logged as WARNING, component may retry or
                fall back to reduced functionality.
    """
    REQUIRED = "required"
    OPTIONAL = "optional"
    DEGRADABLE = "degradable"


class ComponentState(Enum):
    """
    Lifecycle state of a component during initialization.
    
    State transitions:
        PENDING → INITIALIZING → READY (success)
                               → FAILED (unrecoverable)
                               → DEGRADED (partial success)
                               → SKIPPED (intentionally not started)
    """
    PENDING = "pending"          # Not yet started
    INITIALIZING = "initializing"  # Currently starting
    READY = "ready"              # Fully operational
    FAILED = "failed"            # Failed to initialize
    DEGRADED = "degraded"        # Operating with reduced capability
    SKIPPED = "skipped"          # Intentionally not initialized


# =============================================================================
# DEFAULT COMPONENT CLASSIFICATIONS
# =============================================================================

# Components and their default types
# These can be overridden via environment variables
DEFAULT_COMPONENT_TYPES: Dict[str, ComponentType] = {
    # === REQUIRED (Core infrastructure) ===
    "database": ComponentType.REQUIRED,
    "backend": ComponentType.REQUIRED,
    "supervisor": ComponentType.REQUIRED,
    "ipc_server": ComponentType.REQUIRED,
    
    # === DEGRADABLE (Important but can fall back) ===
    "jarvis_prime": ComponentType.DEGRADABLE,
    "reactor_core": ComponentType.DEGRADABLE,
    "gcp_vm": ComponentType.DEGRADABLE,
    "voice_system": ComponentType.DEGRADABLE,
    "websocket": ComponentType.DEGRADABLE,
    
    # === OPTIONAL (Nice-to-have features) ===
    "cloudsql": ComponentType.OPTIONAL,
    "redis": ComponentType.OPTIONAL,
    "cost_tracker": ComponentType.OPTIONAL,
    "hardware_optimizer": ComponentType.OPTIONAL,
    "coreml": ComponentType.OPTIONAL,
    "mps": ComponentType.OPTIONAL,
    "neural_engine": ComponentType.OPTIONAL,
    "biometric_auth": ComponentType.OPTIONAL,
    "encryption": ComponentType.OPTIONAL,
    "secret_manager": ComponentType.OPTIONAL,
}

# Components that become REQUIRED in production
PRODUCTION_REQUIRED: Set[str] = {
    "cloudsql",
    "jarvis_prime",
    "reactor_core",
}


# =============================================================================
# LOGGING SEVERITY POLICY
# =============================================================================

# Log levels for each component type
FAILURE_LOG_LEVELS: Dict[ComponentType, int] = {
    ComponentType.REQUIRED: logging.ERROR,
    ComponentType.OPTIONAL: logging.INFO,
    ComponentType.DEGRADABLE: logging.WARNING,
}

# Log levels for successful initialization
SUCCESS_LOG_LEVELS: Dict[ComponentType, int] = {
    ComponentType.REQUIRED: logging.INFO,
    ComponentType.OPTIONAL: logging.DEBUG,
    ComponentType.DEGRADABLE: logging.INFO,
}


def get_failure_level(component_type: ComponentType) -> int:
    """
    Get the appropriate log level for a component failure.
    
    Args:
        component_type: The classification of the component
        
    Returns:
        The logging level (e.g., logging.INFO, logging.ERROR)
        
    Example:
        level = get_failure_level(ComponentType.OPTIONAL)
        logger.log(level, "CloudSQL not available, using SQLite")
    """
    return FAILURE_LOG_LEVELS.get(component_type, logging.ERROR)


def get_success_level(component_type: ComponentType) -> int:
    """
    Get the appropriate log level for successful initialization.
    
    Args:
        component_type: The classification of the component
        
    Returns:
        The logging level
    """
    return SUCCESS_LOG_LEVELS.get(component_type, logging.INFO)


def get_component_type(component_name: str) -> ComponentType:
    """
    Get the type classification for a component.
    
    Checks (in order):
    1. Environment override: JARVIS_COMPONENT_{NAME}_TYPE
    2. Production environment escalation
    3. Default classification
    4. Falls back to OPTIONAL if unknown
    
    Args:
        component_name: The name of the component (e.g., "cloudsql", "redis")
        
    Returns:
        The ComponentType for this component
        
    Example:
        >>> get_component_type("redis")
        ComponentType.OPTIONAL
        
        >>> os.environ["JARVIS_COMPONENT_REDIS_TYPE"] = "required"
        >>> get_component_type("redis")
        ComponentType.REQUIRED
    """
    name_lower = component_name.lower().replace("-", "_")
    
    # 1. Check for environment override
    env_key = f"JARVIS_COMPONENT_{name_lower.upper()}_TYPE"
    env_value = os.getenv(env_key, "").lower()
    
    if env_value in ("required", "req"):
        return ComponentType.REQUIRED
    elif env_value in ("optional", "opt"):
        return ComponentType.OPTIONAL
    elif env_value in ("degradable", "deg"):
        return ComponentType.DEGRADABLE
    
    # 2. Check if production escalation applies
    is_production = os.getenv("JARVIS_ENV", "").lower() == "production"
    if is_production and name_lower in PRODUCTION_REQUIRED:
        return ComponentType.REQUIRED
    
    # 3. Use default classification
    return DEFAULT_COMPONENT_TYPES.get(name_lower, ComponentType.OPTIONAL)


def is_failure_acceptable(component_name: str) -> bool:
    """
    Check if a component failure is acceptable (non-fatal).
    
    Returns True for OPTIONAL and DEGRADABLE components.
    Returns False for REQUIRED components.
    
    Args:
        component_name: The name of the component
        
    Returns:
        True if failure should not block startup
    """
    component_type = get_component_type(component_name)
    return component_type in (ComponentType.OPTIONAL, ComponentType.DEGRADABLE)


def should_retry_on_failure(component_name: str) -> bool:
    """
    Check if a component should be retried on failure.
    
    Returns True for REQUIRED and DEGRADABLE components.
    Returns False for OPTIONAL components (fail fast).
    
    Args:
        component_name: The name of the component
        
    Returns:
        True if component should be retried
    """
    component_type = get_component_type(component_name)
    return component_type in (ComponentType.REQUIRED, ComponentType.DEGRADABLE)


# =============================================================================
# COMPONENT STATUS
# =============================================================================

@dataclass
class ComponentStatus:
    """
    Status of a component in the startup process.
    
    This is used by the ComponentRegistry to track component lifecycle.
    """
    name: str
    component_type: ComponentType
    state: ComponentState = ComponentState.PENDING
    error: Optional[str] = None
    error_type: Optional[str] = None
    initialized_at: Optional[float] = None
    duration_seconds: Optional[float] = None
    degradation_reason: Optional[str] = None
    
    @property
    def is_available(self) -> bool:
        """Check if component is available for use."""
        return self.state in (ComponentState.READY, ComponentState.DEGRADED)
    
    @property
    def is_healthy(self) -> bool:
        """Check if component is fully healthy."""
        return self.state == ComponentState.READY
    
    @property
    def is_failed(self) -> bool:
        """Check if component failed to initialize."""
        return self.state in (ComponentState.FAILED, ComponentState.SKIPPED)
    
    def to_dict(self) -> dict:
        """Serialize to dictionary for logging/API."""
        return {
            "name": self.name,
            "type": self.component_type.value,
            "state": self.state.value,
            "available": self.is_available,
            "healthy": self.is_healthy,
            "error": self.error,
            "duration_seconds": self.duration_seconds,
        }


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Enums
    "ComponentType",
    "ComponentState",
    # Functions
    "get_failure_level",
    "get_success_level",
    "get_component_type",
    "is_failure_acceptable",
    "should_retry_on_failure",
    # Data classes
    "ComponentStatus",
    # Constants
    "DEFAULT_COMPONENT_TYPES",
    "PRODUCTION_REQUIRED",
]
