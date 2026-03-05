"""
ComponentRegistry - Single source of truth for component lifecycle.

This module provides:
- ComponentDefinition: Declares a component's criticality, dependencies, capabilities
- ComponentRegistry: Manages component registration, status tracking, capability queries
- Automatic log severity derivation based on criticality
- Startup DAG construction from dependencies
"""
from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Callable, Optional, Union, Dict, List
from datetime import datetime, timezone

logger = logging.getLogger("jarvis.component_registry")


class Criticality(Enum):
    """Component criticality levels determining log severity and startup behavior."""
    REQUIRED = "required"       # System cannot start without this -> ERROR
    DEGRADED_OK = "degraded_ok" # Can run degraded if unavailable -> WARNING
    OPTIONAL = "optional"       # Nice to have -> INFO


class ProcessType(Enum):
    """How the component runs."""
    IN_PROCESS = "in_process"           # Python module, same process
    SUBPROCESS = "subprocess"           # Managed child process
    EXTERNAL_SERVICE = "external"       # External dependency (Redis, CloudSQL)


class HealthCheckType(Enum):
    """Type of health check to perform."""
    HTTP = "http"       # HTTP endpoint check
    TCP = "tcp"         # TCP port check
    CUSTOM = "custom"   # Callback function
    NONE = "none"       # No health check


class FallbackStrategy(Enum):
    """Strategy when component fails to start."""
    BLOCK = "block"                     # Block startup on failure
    CONTINUE = "continue"               # Continue without component
    RETRY_THEN_CONTINUE = "retry"       # Retry N times, then continue


class ComponentStatus(Enum):
    """Runtime status of a component."""
    PENDING = "pending"       # Not yet started
    STARTING = "starting"     # In progress
    HEALTHY = "healthy"       # Running and healthy
    DEGRADED = "degraded"     # Running with reduced capability
    FAILED = "failed"         # Startup failed
    DISABLED = "disabled"     # Explicitly disabled


# --- Governance Enums (Wave 0) ---

class PromotionLevel(Enum):
    """Whether a component has been promoted to governance-aware status."""
    LEGACY = "legacy"
    PROMOTED = "promoted"


class ActivationMode(Enum):
    """How a component is activated at runtime."""
    ALWAYS_ON = "always_on"
    WARM_STANDBY = "warm_standby"
    EVENT_DRIVEN = "event_driven"
    BATCH_WINDOW = "batch_window"


class ReadinessClass(Enum):
    """How a component participates in system readiness."""
    BLOCK_READY = "block_ready"
    NON_BLOCKING = "non_blocking"
    DEFERRED_AFTER_READY = "deferred_after_ready"


class ActivationTier(IntEnum):
    """Biological-metaphor tiers controlling activation order."""
    FOUNDATION = 0
    IMMUNE = 1
    NERVOUS = 2
    METABOLIC = 3
    HIGHER = 4


class RetryStrategy(Enum):
    """Retry strategies for component failure recovery."""
    NONE = "none"
    FIXED_DELAY = "fixed_delay"
    EXP_BACKOFF = "exp_backoff"
    EXP_BACKOFF_JITTER = "exp_backoff_jitter"


class OwnershipMode(Enum):
    """Ownership semantics for a state domain."""
    EXCLUSIVE_WRITE = "exclusive_write"
    SHARED_READ_ONLY = "shared_read_only"


# --- Governance Dataclasses (Wave 0) ---

@dataclass(frozen=True)
class ResourceBudget:
    """Hard resource limits for a governed component."""
    max_memory_mb: int
    max_cpu_percent: float
    max_concurrency: int
    max_startup_time_s: float


@dataclass(frozen=True)
class FailurePolicy:
    """Retry/circuit-breaker policy for a governed component."""
    retry_strategy: RetryStrategy
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    circuit_breaker: bool
    breaker_threshold: int
    breaker_recovery_s: float
    quarantine_on_repeated: bool


@dataclass(frozen=True)
class StateDomain:
    """Declares a named state domain and its ownership semantics."""
    domain: str
    ownership_mode: OwnershipMode


@dataclass(frozen=True)
class ObservabilityContract:
    """Observability requirements for a governed component."""
    schema_version: str = "1.0"
    emit_trace_id: bool = True
    emit_reason_codes: bool = True
    required_log_fields: tuple = (
        "trace_id", "reason_code", "service_name",
        "activation_mode", "readiness_class",
    )
    health_check_interval_s: float = 30.0


@dataclass(frozen=True)
class HealthPolicy:
    """Health-check policy for a governed component."""
    supports_liveness: bool = True
    supports_readiness: bool = True
    supports_drain: bool = False
    hysteresis_window: int = 3
    health_check_timeout_s: float = 5.0


@dataclass
class Dependency:
    """A dependency on another component."""
    component: str
    soft: bool = False  # If True, failure doesn't block dependent


@dataclass
class ComponentDefinition:
    """Complete definition of a component."""
    name: str
    criticality: Criticality
    process_type: ProcessType

    # Dependencies & capabilities
    dependencies: List[Union[str, Dependency]] = field(default_factory=list)
    provides_capabilities: List[str] = field(default_factory=list)

    # Health checking
    health_check_type: HealthCheckType = HealthCheckType.NONE
    health_endpoint: Optional[str] = None
    health_check_callback: Optional[Callable] = None

    # Subprocess/external config
    repo_path: Optional[str] = None

    # Retry & timeout
    startup_timeout: float = 60.0
    retry_max_attempts: int = 3
    retry_delay_seconds: float = 5.0
    fallback_strategy: FallbackStrategy = FallbackStrategy.CONTINUE

    # Fallback configuration
    fallback_for_capabilities: Dict[str, str] = field(default_factory=dict)
    conservative_skip_priority: int = 50  # Lower = skipped first

    # Environment integration
    disable_env_var: Optional[str] = None
    criticality_override_env: Optional[str] = None

    @property
    def effective_criticality(self) -> Criticality:
        """Get criticality, checking env override first."""
        if self.criticality_override_env:
            override = os.environ.get(self.criticality_override_env, "").lower()
            if override == "true":
                return Criticality.REQUIRED
        return self.criticality

    def is_disabled_by_env(self) -> bool:
        """Check if component is disabled via environment variable.

        The disable_env_var field specifies an ENABLE variable (e.g., "JARVIS_PRIME_ENABLED").
        If the variable is set to "false", "0", "no", or "disabled", the component is disabled.
        If the variable is not set or set to any other value, the component is enabled.

        Returns:
            True if the component should be disabled, False otherwise.
        """
        if self.disable_env_var:
            value = os.environ.get(self.disable_env_var, "true").lower()
            return value in ("false", "0", "no", "disabled")
        return False


@dataclass
class ComponentState:
    """Runtime state of a registered component."""
    definition: ComponentDefinition
    status: ComponentStatus = ComponentStatus.PENDING
    started_at: Optional[datetime] = None
    healthy_at: Optional[datetime] = None
    failed_at: Optional[datetime] = None
    failure_reason: Optional[str] = None
    attempt_count: int = 0

    def mark_starting(self):
        self.status = ComponentStatus.STARTING
        self.started_at = datetime.now(timezone.utc)
        self.attempt_count += 1

    def mark_healthy(self):
        self.status = ComponentStatus.HEALTHY
        self.healthy_at = datetime.now(timezone.utc)
        self.failure_reason = None

    def mark_degraded(self, reason: str):
        self.status = ComponentStatus.DEGRADED
        self.failure_reason = reason

    def mark_failed(self, reason: str):
        self.status = ComponentStatus.FAILED
        self.failed_at = datetime.now(timezone.utc)
        self.failure_reason = reason

    def mark_disabled(self, reason: str):
        self.status = ComponentStatus.DISABLED
        self.failure_reason = reason


class ComponentRegistry:
    """
    Central registry for all JARVIS components.

    Provides:
    - Component registration and lookup
    - Capability-based routing
    - Status tracking
    - Singleton pattern for global access
    """

    _instance: Optional['ComponentRegistry'] = None

    def __init__(self):
        self._components: Dict[str, ComponentState] = {}
        self._capabilities: Dict[str, str] = {}  # capability -> component name
        self._initialized = False

    @classmethod
    def get_instance(cls) -> 'ComponentRegistry':
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _reset_for_testing(self):
        """Reset registry state for testing. NOT for production use."""
        self._components.clear()
        self._capabilities.clear()
        self._initialized = False

    def register(self, definition: ComponentDefinition) -> ComponentState:
        """Register a component definition."""
        if definition.name in self._components:
            logger.warning(f"Component {definition.name} already registered, updating")

        state = ComponentState(definition=definition)
        self._components[definition.name] = state

        # Index capabilities
        for cap in definition.provides_capabilities:
            if cap in self._capabilities:
                logger.debug(
                    f"Capability {cap} already provided by {self._capabilities[cap]}, "
                    f"now also by {definition.name}"
                )
            self._capabilities[cap] = definition.name

        logger.debug(f"Registered component: {definition.name}")
        return state

    def has(self, name: str) -> bool:
        """Check if a component is registered."""
        return name in self._components

    def get(self, name: str) -> ComponentDefinition:
        """Get component definition by name."""
        if name not in self._components:
            raise KeyError(f"Component not registered: {name}")
        return self._components[name].definition

    def get_state(self, name: str) -> ComponentState:
        """Get component state by name."""
        if name not in self._components:
            raise KeyError(f"Component not registered: {name}")
        return self._components[name]

    def has_capability(self, capability: str) -> bool:
        """Check if a capability is available (component is healthy or degraded)."""
        if capability not in self._capabilities:
            return False
        provider = self._capabilities[capability]
        state = self._components.get(provider)
        if not state:
            return False
        return state.status in (ComponentStatus.HEALTHY, ComponentStatus.DEGRADED)

    def get_provider(self, capability: str) -> Optional[str]:
        """Get the component name that provides a capability."""
        return self._capabilities.get(capability)

    def all_definitions(self) -> List[ComponentDefinition]:
        """Get all registered component definitions."""
        return [state.definition for state in self._components.values()]

    def all_states(self) -> List[ComponentState]:
        """Get all component states."""
        return list(self._components.values())

    def mark_status(self, name: str, status: ComponentStatus, reason: Optional[str] = None):
        """Update component status."""
        state = self.get_state(name)
        if status == ComponentStatus.STARTING:
            state.mark_starting()
        elif status == ComponentStatus.HEALTHY:
            state.mark_healthy()
        elif status == ComponentStatus.DEGRADED:
            state.mark_degraded(reason or "Unknown")
        elif status == ComponentStatus.FAILED:
            state.mark_failed(reason or "Unknown")
        elif status == ComponentStatus.DISABLED:
            state.mark_disabled(reason or "Disabled")
        else:
            state.status = status


def get_component_registry() -> ComponentRegistry:
    """Get the global ComponentRegistry instance."""
    return ComponentRegistry.get_instance()


# Alias for enterprise_hooks compatibility
def get_registry() -> ComponentRegistry:
    """
    Alias for get_component_registry.

    Provides consistent naming with other enterprise factory functions
    (get_recovery_engine, get_capability_router, etc.)

    Returns:
        Global ComponentRegistry instance
    """
    return ComponentRegistry.get_instance()
