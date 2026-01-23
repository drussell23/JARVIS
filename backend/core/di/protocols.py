"""
JARVIS Dependency Injection Protocols Module v2.0
==================================================

Enterprise-grade protocols, enums, data classes, and base classes for the
JARVIS dependency injection container. This module provides the foundational
abstractions and contracts that enable type-safe, lifecycle-managed service
composition.

Key Design Principles:
1. LAZY LOCK INITIALIZATION - Never create asyncio primitives at module/class init
2. IMMUTABLE VALUE OBJECTS - Frozen dataclasses with slots for efficiency
3. RUNTIME TYPE CHECKING - Protocol checking with @runtime_checkable
4. MEMORY EFFICIENCY - __slots__ throughout for reduced memory footprint
5. GENERIC TYPE SUPPORT - Full TypeVar/Generic support for typed resolution

Architecture:
    +------------------------------------------------------------------+
    |                     DI Protocol Layer                            |
    +------------------------------------------------------------------+
    |  ServiceDefinition  |  DependencySpec  |  Lifecycle Protocols    |
    +------------------------------------------------------------------+
    |  DependencyType     |  ServiceScope    |  Resolution Context     |
    +------------------------------------------------------------------+
    |  ServiceState       |  HealthStatus    |  ServiceCriticality     |
    +------------------------------------------------------------------+

Thread Safety:
    All protocol definitions are immutable or use frozen dataclasses
    for thread-safe access across concurrent resolution operations.

CRITICAL: This module uses lazy lock initialization patterns.
    Never create asyncio.Lock() or asyncio.Event() in __init__ or at module level.
    Always use the lazy initialization pattern demonstrated in BaseAsyncService.

Author: JARVIS AI System
Version: 2.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from abc import ABC, abstractmethod
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Dict,
    Final,
    FrozenSet,
    Generic,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    runtime_checkable,
)

if TYPE_CHECKING:
    from types import TracebackType

logger = logging.getLogger(__name__)


# =============================================================================
# TYPE VARIABLES
# =============================================================================

T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
T_contra = TypeVar("T_contra", contravariant=True)
ServiceT = TypeVar("ServiceT", bound="AsyncService")
FactoryT = TypeVar("FactoryT")


# =============================================================================
# ENUMS - Service Lifecycle and Configuration
# =============================================================================


class ServiceState(Enum):
    """
    Comprehensive service lifecycle states with auto values for safety.

    State transitions follow this flow:

    UNREGISTERED -> REGISTERED -> INITIALIZING -> INITIALIZED
                                                       |
                                                       v
                                    STOPPING <- RUNNING <- STARTING
                                       |
                                       v
                                    STOPPED
                                       |
                                       v
                                    DISPOSED

    FAILED can occur from any active state.
    """
    UNREGISTERED = auto()   # Service type known but not registered
    REGISTERED = auto()     # Registered but not yet initialized
    INITIALIZING = auto()   # Currently in initialize() call
    INITIALIZED = auto()    # initialize() completed successfully
    STARTING = auto()       # Currently in start() call
    RUNNING = auto()        # Service is active and healthy
    STOPPING = auto()       # Currently in stop() call
    STOPPED = auto()        # Gracefully stopped
    FAILED = auto()         # Entered error state
    DISPOSED = auto()       # Fully cleaned up, cannot be reused

    @property
    def is_active(self) -> bool:
        """Check if service is in an active operational state."""
        return self in (
            ServiceState.INITIALIZED,
            ServiceState.STARTING,
            ServiceState.RUNNING,
            ServiceState.STOPPING,
        )

    @property
    def is_terminal(self) -> bool:
        """Check if service is in a terminal state."""
        return self in (ServiceState.STOPPED, ServiceState.FAILED, ServiceState.DISPOSED)

    @property
    def can_start(self) -> bool:
        """Check if service can transition to STARTING."""
        return self == ServiceState.INITIALIZED

    @property
    def can_stop(self) -> bool:
        """Check if service can transition to STOPPING."""
        return self in (ServiceState.RUNNING, ServiceState.STARTING, ServiceState.INITIALIZED)

    @property
    def can_initialize(self) -> bool:
        """Check if service can transition to INITIALIZING."""
        return self in (ServiceState.REGISTERED, ServiceState.STOPPED)

    def __str__(self) -> str:
        return self.name


class HealthStatus(Enum):
    """
    Service health status indicators with auto values for safety.

    Used by health checks to report the current operational status.
    """
    HEALTHY = auto()    # Service is fully operational
    DEGRADED = auto()   # Service is operational but with reduced capacity
    UNHEALTHY = auto()  # Service is not operational
    UNKNOWN = auto()    # Health status cannot be determined

    @property
    def is_operational(self) -> bool:
        """Check if service is at least partially operational."""
        return self in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)

    @classmethod
    def from_score(cls, score: float) -> HealthStatus:
        """
        Convert a numeric health score (0.0 to 1.0) to a HealthStatus.

        Args:
            score: Health score where 1.0 is fully healthy, 0.0 is unhealthy

        Returns:
            Corresponding HealthStatus enum value
        """
        if score >= 0.9:
            return cls.HEALTHY
        elif score >= 0.5:
            return cls.DEGRADED
        elif score >= 0.0:
            return cls.UNHEALTHY
        else:
            return cls.UNKNOWN

    def __str__(self) -> str:
        return self.name


class ServiceCriticality(Enum):
    """
    Service criticality levels for startup and failure handling.

    Criticality determines:
    - Startup order (CRITICAL first, OPTIONAL last)
    - Failure handling (CRITICAL failures abort startup, OPTIONAL failures are logged)
    - Recovery priority (CRITICAL services are recovered first)
    """
    CRITICAL = auto()   # System cannot function without this service
    REQUIRED = auto()   # Degraded operation possible without this service
    OPTIONAL = auto()   # Nice-to-have, system fully functional without it

    @property
    def startup_priority(self) -> int:
        """Get numeric priority for startup ordering (lower = first)."""
        return {
            ServiceCriticality.CRITICAL: 0,
            ServiceCriticality.REQUIRED: 50,
            ServiceCriticality.OPTIONAL: 100,
        }[self]

    @property
    def abort_on_failure(self) -> bool:
        """Check if failure should abort system startup."""
        return self == ServiceCriticality.CRITICAL

    def __str__(self) -> str:
        return self.name


class Scope(Enum):
    """
    Service instance scope (lifetime management) with auto values.

    Determines how and when service instances are created:

    - SINGLETON: One instance for the entire application lifetime
    - TRANSIENT: New instance on every resolution
    - SCOPED: One instance per scope (e.g., per request, per session)
    - REQUEST: Special scoped for HTTP request lifetime
    """
    SINGLETON = auto()  # Single shared instance
    TRANSIENT = auto()  # New instance per resolution
    SCOPED = auto()     # One instance per scope
    REQUEST = auto()    # One instance per HTTP request

    @property
    def is_singleton(self) -> bool:
        """Check if scope creates at most one instance."""
        return self == Scope.SINGLETON

    @property
    def requires_scope(self) -> bool:
        """Check if scope requires an active scope context."""
        return self in (Scope.SCOPED, Scope.REQUEST)

    def __str__(self) -> str:
        return self.name


class DependencyType(Enum):
    """
    Dependency injection types with auto values.

    Determines how dependencies are resolved and when they are instantiated:

    - REQUIRED: Must be resolved, failure throws exception
    - OPTIONAL: Resolved if available, None if not
    - LAZY: Resolved on first access via proxy
    - FACTORY: Returns a factory function instead of instance
    """
    REQUIRED = auto()   # Must be present
    OPTIONAL = auto()   # Can be None
    LAZY = auto()       # Resolved on first access
    FACTORY = auto()    # Provides a factory function

    @property
    def allows_none(self) -> bool:
        """Check if None is an acceptable resolution result."""
        return self == DependencyType.OPTIONAL

    @property
    def is_deferred(self) -> bool:
        """Check if resolution is deferred."""
        return self in (DependencyType.LAZY, DependencyType.FACTORY)

    def __str__(self) -> str:
        return self.name


# Legacy enums maintained for backward compatibility
class ServiceScope(str, Enum):
    """
    Lifecycle scope for service instances (legacy compatibility).

    Consider using Scope enum for new code.
    """
    SINGLETON = "singleton"     # One instance for entire application lifetime
    SCOPED = "scoped"           # One instance per defined scope
    TRANSIENT = "transient"     # New instance on each request
    REQUEST = "request"         # One instance per HTTP request
    THREAD = "thread"           # One instance per thread
    TASK = "task"               # One instance per async task


class LifecyclePhase(str, Enum):
    """
    Current phase in service lifecycle (legacy compatibility).

    Consider using ServiceState enum for new code.
    """
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    DISPOSING = "disposing"
    DISPOSED = "disposed"
    FAILED = "failed"


class ResolutionStrategy(str, Enum):
    """
    Strategy for resolving dependencies when multiple implementations exist.
    """
    FIRST = "first"             # Use first registered
    LAST = "last"               # Use most recently registered
    ALL = "all"                 # Return all as list
    PRIORITY = "priority"       # Use highest priority
    QUALIFIED = "qualified"     # Use qualifier to select


# =============================================================================
# FROZEN DATA CLASSES - Immutable Value Objects with Slots
# =============================================================================


@dataclass(frozen=True)
class HealthReport:
    """
    Immutable health check report for a service.

    Contains all information about a service's current health status,
    including latency metrics, individual check results, and error details.
    """
    service_name: str
    status: HealthStatus
    latency_ms: float
    timestamp: datetime
    checks: Mapping[str, bool] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate report data."""
        if self.latency_ms < 0:
            object.__setattr__(self, 'latency_ms', 0.0)

    @property
    def is_healthy(self) -> bool:
        """Check if service is healthy."""
        return self.status == HealthStatus.HEALTHY

    @property
    def is_operational(self) -> bool:
        """Check if service is at least degraded (operational)."""
        return self.status.is_operational

    @property
    def all_checks_passed(self) -> bool:
        """Check if all individual checks passed."""
        return all(self.checks.values()) if self.checks else True

    @property
    def failed_checks(self) -> FrozenSet[str]:
        """Get names of failed checks."""
        return frozenset(name for name, passed in self.checks.items() if not passed)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "service_name": self.service_name,
            "status": self.status.name,
            "latency_ms": self.latency_ms,
            "timestamp": self.timestamp.isoformat(),
            "checks": dict(self.checks),
            "metadata": dict(self.metadata),
            "error": self.error,
            "is_healthy": self.is_healthy,
            "failed_checks": list(self.failed_checks),
        }

    @classmethod
    def healthy(
        cls,
        service_name: str,
        latency_ms: float = 0.0,
        checks: Optional[Mapping[str, bool]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> HealthReport:
        """Factory for creating a healthy report."""
        return cls(
            service_name=service_name,
            status=HealthStatus.HEALTHY,
            latency_ms=latency_ms,
            timestamp=datetime.utcnow(),
            checks=checks or {},
            metadata=metadata or {},
        )

    @classmethod
    def unhealthy(
        cls,
        service_name: str,
        error: str,
        latency_ms: float = 0.0,
        checks: Optional[Mapping[str, bool]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> HealthReport:
        """Factory for creating an unhealthy report."""
        return cls(
            service_name=service_name,
            status=HealthStatus.UNHEALTHY,
            latency_ms=latency_ms,
            timestamp=datetime.utcnow(),
            checks=checks or {},
            metadata=metadata or {},
            error=error,
        )

    @classmethod
    def degraded(
        cls,
        service_name: str,
        reason: str,
        latency_ms: float = 0.0,
        checks: Optional[Mapping[str, bool]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> HealthReport:
        """Factory for creating a degraded report."""
        return cls(
            service_name=service_name,
            status=HealthStatus.DEGRADED,
            latency_ms=latency_ms,
            timestamp=datetime.utcnow(),
            checks=checks or {},
            metadata={"degraded_reason": reason, **(metadata or {})},
        )

    @classmethod
    def unknown(
        cls,
        service_name: str,
        reason: str = "Health check not performed",
    ) -> HealthReport:
        """Factory for creating an unknown status report."""
        return cls(
            service_name=service_name,
            status=HealthStatus.UNKNOWN,
            latency_ms=0.0,
            timestamp=datetime.utcnow(),
            metadata={"reason": reason},
        )


@dataclass(frozen=True)
class DependencySpec:
    """
    Immutable specification for a service dependency.

    Describes how a dependency should be resolved, including type information,
    resolution strategy, and default value handling.
    """
    service_type: Type[Any]
    dependency_type: DependencyType = DependencyType.REQUIRED
    default: Any = None
    qualifier: Optional[str] = None
    description: Optional[str] = None
    param_name: Optional[str] = None  # Override for factory parameter name

    @property
    def is_required(self) -> bool:
        """Check if dependency is required (non-optional)."""
        return self.dependency_type == DependencyType.REQUIRED

    @property
    def is_optional(self) -> bool:
        """Check if dependency is optional."""
        return self.dependency_type == DependencyType.OPTIONAL

    @property
    def is_lazy(self) -> bool:
        """Check if dependency should be lazily resolved."""
        return self.dependency_type == DependencyType.LAZY

    @property
    def is_factory(self) -> bool:
        """Check if dependency is a factory."""
        return self.dependency_type == DependencyType.FACTORY

    def resolve_key(self) -> Tuple[Type[Any], Optional[str]]:
        """Get the resolution key for this dependency."""
        return (self.service_type, self.qualifier)

    def with_qualifier(self, qualifier: str) -> DependencySpec:
        """Create a new spec with a different qualifier."""
        return DependencySpec(
            service_type=self.service_type,
            dependency_type=self.dependency_type,
            default=self.default,
            qualifier=qualifier,
            description=self.description,
        )

    def with_default(self, default: Any) -> DependencySpec:
        """Create a new spec with a different default value."""
        return DependencySpec(
            service_type=self.service_type,
            dependency_type=self.dependency_type,
            default=default,
            qualifier=self.qualifier,
            description=self.description,
        )

    def __repr__(self) -> str:
        return (
            f"DependencySpec({self.service_type.__name__}, "
            f"{self.dependency_type.name}, qualifier={self.qualifier!r})"
        )


@dataclass(frozen=True)
class ServiceDefinitionSpec(Generic[T]):
    """
    Immutable definition of a service registration.

    Contains all configuration for how a service should be instantiated,
    initialized, and managed throughout its lifecycle.

    Type Parameters:
        T: The service type this definition describes
    """
    service_type: Type[T]
    scope: Scope = Scope.SINGLETON
    criticality: ServiceCriticality = ServiceCriticality.REQUIRED
    dependencies: Tuple[DependencySpec, ...] = field(default_factory=tuple)
    factory: Optional[Callable[..., Union[T, Awaitable[T]]]] = None
    enabled_when: Optional[Callable[[], bool]] = None
    tags: FrozenSet[str] = field(default_factory=frozenset)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    initialization_timeout: float = 30.0
    health_check_timeout: float = 5.0
    max_retry_count: int = 3
    retry_backoff_factor: float = 2.0

    def __post_init__(self) -> None:
        """Validate definition parameters."""
        if self.initialization_timeout <= 0:
            object.__setattr__(self, 'initialization_timeout', 30.0)
        if self.health_check_timeout <= 0:
            object.__setattr__(self, 'health_check_timeout', 5.0)
        if self.max_retry_count < 0:
            object.__setattr__(self, 'max_retry_count', 0)
        if self.retry_backoff_factor < 1.0:
            object.__setattr__(self, 'retry_backoff_factor', 1.0)

    @property
    def is_enabled(self) -> bool:
        """Check if service is currently enabled."""
        if self.enabled_when is None:
            return True
        try:
            return self.enabled_when()
        except Exception:
            logger.exception(f"Error checking if {self.service_type.__name__} is enabled")
            return False

    @property
    def is_singleton(self) -> bool:
        """Check if service is singleton scoped."""
        return self.scope.is_singleton

    @property
    def has_factory(self) -> bool:
        """Check if service has a custom factory."""
        return self.factory is not None

    @property
    def required_dependencies(self) -> Tuple[DependencySpec, ...]:
        """Get all required dependencies."""
        return tuple(d for d in self.dependencies if d.is_required)

    @property
    def optional_dependencies(self) -> Tuple[DependencySpec, ...]:
        """Get all optional dependencies."""
        return tuple(d for d in self.dependencies if d.is_optional)

    def has_tag(self, tag: str) -> bool:
        """Check if service has a specific tag."""
        return tag in self.tags

    def get_metadata(self, key: str, default: Any = None) -> Any:
        """Get metadata value by key."""
        return self.metadata.get(key, default)

    def with_dependencies(self, *deps: DependencySpec) -> ServiceDefinitionSpec[T]:
        """Create a new definition with additional dependencies."""
        return ServiceDefinitionSpec(
            service_type=self.service_type,
            scope=self.scope,
            criticality=self.criticality,
            dependencies=self.dependencies + tuple(deps),
            factory=self.factory,
            enabled_when=self.enabled_when,
            tags=self.tags,
            metadata=self.metadata,
            initialization_timeout=self.initialization_timeout,
            health_check_timeout=self.health_check_timeout,
            max_retry_count=self.max_retry_count,
            retry_backoff_factor=self.retry_backoff_factor,
        )

    def __repr__(self) -> str:
        return (
            f"ServiceDefinitionSpec({self.service_type.__name__}, "
            f"scope={self.scope.name}, criticality={self.criticality.name})"
        )


@dataclass(frozen=True)
class ServiceMetrics:
    """
    Immutable metrics snapshot for a service.

    Contains operational metrics including timing, health, and error statistics.
    """
    service_name: str
    state: ServiceState
    init_duration_ms: float = 0.0
    start_duration_ms: float = 0.0
    uptime_seconds: float = 0.0
    health_check_count: int = 0
    error_count: int = 0
    recovery_count: int = 0
    last_health_check: Optional[datetime] = None
    last_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        """Check if service is currently running."""
        return self.state == ServiceState.RUNNING

    @property
    def total_startup_ms(self) -> float:
        """Get total startup time (init + start)."""
        return self.init_duration_ms + self.start_duration_ms

    @property
    def error_rate(self) -> float:
        """Calculate error rate based on health check count."""
        if self.health_check_count == 0:
            return 0.0
        return self.error_count / self.health_check_count

    @property
    def recovery_rate(self) -> float:
        """Calculate recovery rate based on error count."""
        if self.error_count == 0:
            return 1.0
        return self.recovery_count / self.error_count

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary representation."""
        return {
            "service_name": self.service_name,
            "state": self.state.name,
            "init_duration_ms": self.init_duration_ms,
            "start_duration_ms": self.start_duration_ms,
            "uptime_seconds": self.uptime_seconds,
            "health_check_count": self.health_check_count,
            "error_count": self.error_count,
            "recovery_count": self.recovery_count,
            "last_health_check": self.last_health_check.isoformat() if self.last_health_check else None,
            "last_error": self.last_error,
            "is_running": self.is_running,
            "error_rate": self.error_rate,
        }


# =============================================================================
# LEGACY MUTABLE DATA CLASSES (for backward compatibility)
# =============================================================================


@dataclass
class ServiceDefinition:
    """
    Complete definition of a service for DI registration.

    NOTE: This is the legacy mutable version. For new code, prefer
    ServiceDefinitionSpec which is frozen and uses slots.

    Captures all metadata needed for:
    - Dependency resolution and injection
    - Lifecycle management
    - Scope handling
    - Priority ordering
    """
    interface: Type
    implementation: Type
    scope: ServiceScope = ServiceScope.SINGLETON
    dependencies: List[DependencySpec] = field(default_factory=list)
    qualifier: Optional[str] = None
    priority: int = 100
    factory: Optional[Callable[..., Any]] = None
    lazy: bool = False
    async_init: bool = False
    async_dispose: bool = False
    tags: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Lifecycle hooks
    on_create: Optional[Callable[[Any], None]] = None
    on_start: Optional[Callable[[Any], Awaitable[None]]] = None
    on_stop: Optional[Callable[[Any], Awaitable[None]]] = None
    on_dispose: Optional[Callable[[Any], None]] = None

    # Internal state (mutable, not part of identity)
    _phase: LifecyclePhase = field(
        default=LifecyclePhase.UNINITIALIZED,
        repr=False,
        compare=False,
    )
    _instance: Optional[Any] = field(
        default=None,
        repr=False,
        compare=False,
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock,
        repr=False,
        compare=False,
    )

    def __post_init__(self):
        """Validate and finalize the service definition."""
        # Ensure interface is actually a type
        if not isinstance(self.interface, type):
            if hasattr(self.interface, "__origin__"):
                # Generic type - extract origin
                pass
            else:
                raise ValueError(f"interface must be a type, got {type(self.interface)}")

        # Ensure implementation is a type or callable
        if not (isinstance(self.implementation, type) or callable(self.implementation)):
            raise ValueError(
                f"implementation must be a type or callable, "
                f"got {type(self.implementation)}"
            )

    @property
    def key(self) -> Tuple[Type, Optional[str]]:
        """Unique key for this service (interface + qualifier)."""
        return (self.interface, self.qualifier)

    @property
    def phase(self) -> LifecyclePhase:
        """Current lifecycle phase."""
        return self._phase

    @phase.setter
    def phase(self, value: LifecyclePhase) -> None:
        """Set lifecycle phase (thread-safe)."""
        with self._lock:
            self._phase = value

    @property
    def instance(self) -> Optional[Any]:
        """Get the singleton instance if available."""
        return self._instance

    @instance.setter
    def instance(self, value: Any) -> None:
        """Set the singleton instance (thread-safe)."""
        with self._lock:
            self._instance = value

    def get_dependency_types(self) -> Set[Type]:
        """Get all types this service depends on."""
        return {dep.service_type for dep in self.dependencies}

    def depends_on(self, service_type: Type) -> bool:
        """Check if this service depends on the given type."""
        return any(dep.service_type == service_type for dep in self.dependencies)

    def has_lazy_dependencies(self) -> bool:
        """Check if any dependencies are lazy."""
        return any(dep.is_lazy for dep in self.dependencies)

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, ServiceDefinition):
            return False
        return self.key == other.key


@dataclass
class ResolutionContext:
    """
    Context for dependency resolution operations.

    Tracks resolution state to detect cycles and provide debugging info.
    """
    resolution_stack: List[Type] = field(default_factory=list)
    scope_id: Optional[str] = None
    parent: Optional[ResolutionContext] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    _start_time: datetime = field(default_factory=datetime.utcnow)

    def push(self, service_type: Type) -> None:
        """Push a type onto the resolution stack."""
        if service_type in self.resolution_stack:
            # Cycle detected!
            cycle_start = self.resolution_stack.index(service_type)
            cycle_path = self.resolution_stack[cycle_start:] + [service_type]
            raise CircularDependencyError(cycle_path)
        self.resolution_stack.append(service_type)

    def pop(self) -> Optional[Type]:
        """Pop a type from the resolution stack."""
        if self.resolution_stack:
            return self.resolution_stack.pop()
        return None

    @property
    def depth(self) -> int:
        """Get the current resolution depth."""
        return len(self.resolution_stack)

    @property
    def elapsed_ms(self) -> float:
        """Get elapsed time in milliseconds."""
        return (datetime.utcnow() - self._start_time).total_seconds() * 1000

    def child(self, scope_id: Optional[str] = None) -> ResolutionContext:
        """Create a child context for nested resolution."""
        return ResolutionContext(
            resolution_stack=list(self.resolution_stack),
            scope_id=scope_id or self.scope_id,
            parent=self,
            metadata=dict(self.metadata),
        )


# =============================================================================
# PROTOCOLS - Runtime Checkable Interface Contracts
# =============================================================================


@runtime_checkable
class AsyncService(Protocol):
    """
    Protocol for async services managed by the DI container.

    Services implementing this protocol can be fully managed through
    their lifecycle - initialization, startup, health monitoring, and shutdown.

    IMPORTANT: Implementations MUST use lazy lock initialization.
    Never create asyncio.Lock() or asyncio.Event() in __init__!

    Example Implementation:
        class MyService:
            def __init__(self):
                self._lock: Optional[asyncio.Lock] = None  # LAZY!
                self._state = ServiceState.REGISTERED

            async def _ensure_lock(self) -> asyncio.Lock:
                if self._lock is None:
                    self._lock = asyncio.Lock()  # Created in async context
                return self._lock

            async def initialize(self) -> None:
                lock = await self._ensure_lock()
                async with lock:
                    # ... initialization
                    pass

            async def start(self) -> None: ...
            async def stop(self) -> None: ...
            async def health_check(self) -> HealthReport: ...
            def get_dependencies(self) -> Sequence[DependencySpec]: ...
    """

    async def initialize(self) -> None:
        """
        Initialize the service.

        Called once after construction but before start().
        Should perform any async setup operations like connecting
        to databases, loading configurations, etc.

        Must be idempotent - calling multiple times should be safe.
        """
        ...

    async def start(self) -> None:
        """
        Start the service.

        Called after initialize() to begin active operation.
        Should start any background tasks, begin listening, etc.

        Must be idempotent - calling multiple times should be safe.
        """
        ...

    async def stop(self) -> None:
        """
        Stop the service.

        Called during shutdown to gracefully stop operation.
        Should stop background tasks, close connections, etc.

        Must be idempotent - calling multiple times should be safe.
        """
        ...

    async def health_check(self) -> HealthReport:
        """
        Perform a health check.

        Returns a HealthReport indicating the current status.
        Should complete within the configured health_check_timeout.

        Returns:
            HealthReport with current status, latency, and check details
        """
        ...

    def get_dependencies(self) -> Sequence[DependencySpec]:
        """
        Get the service's dependencies.

        Returns a sequence of DependencySpec describing what this
        service requires from other services.

        Returns:
            Sequence of DependencySpec objects
        """
        ...


@runtime_checkable
class ServiceFactory(Protocol[T_co]):
    """
    Protocol for service factory functions.

    Factories are responsible for creating service instances,
    potentially based on runtime configuration or context.

    Type Parameters:
        T_co: The covariant type of service this factory produces
    """

    def __call__(self, **kwargs: Any) -> Union[T_co, Awaitable[T_co]]:
        """
        Create a service instance.

        Args:
            **kwargs: Dependency injection and configuration arguments

        Returns:
            A service instance or awaitable producing one
        """
        ...


@runtime_checkable
class Disposable(Protocol):
    """Protocol for services that require synchronous cleanup."""

    def dispose(self) -> None:
        """Release resources synchronously."""
        ...


@runtime_checkable
class AsyncDisposable(Protocol):
    """
    Protocol for resources that need async cleanup.

    Services implementing AsyncDisposable will have dispose() called
    during container shutdown, after stop().
    """

    async def dispose(self) -> None:
        """
        Dispose of the resource asynchronously.

        Called after stop() during shutdown. Should release any
        resources that were not released during stop().

        Must be idempotent - calling multiple times should be safe.
        """
        ...


@runtime_checkable
class Resettable(Protocol):
    """
    Protocol for services that can be reset to initial state.

    Useful for testing or when a service needs to be recycled
    without full stop/start cycle.
    """

    async def reset(self) -> None:
        """
        Reset the service to initial state.

        Should clear caches, reset counters, etc. while keeping
        the service running.

        Must be idempotent.
        """
        ...


@runtime_checkable
class Configurable(Protocol):
    """
    Protocol for services that accept runtime configuration.
    """

    async def configure(self, config: Mapping[str, Any]) -> None:
        """
        Apply configuration to the service.

        Args:
            config: Configuration key-value pairs
        """
        ...


@runtime_checkable
class Initializable(Protocol):
    """Protocol for services that require synchronous initialization."""

    def initialize(self) -> None:
        """Initialize the service synchronously."""
        ...


@runtime_checkable
class AsyncInitializable(Protocol):
    """Protocol for services that require async initialization."""

    async def initialize_async(self) -> None:
        """Initialize the service asynchronously."""
        ...


@runtime_checkable
class Startable(Protocol):
    """Protocol for services that can be started/stopped."""

    async def start(self) -> None:
        """Start the service."""
        ...

    async def stop(self) -> None:
        """Stop the service."""
        ...


@runtime_checkable
class HealthCheckable(Protocol):
    """Protocol for services that support health checks."""

    async def health_check(self) -> bool:
        """Check if the service is healthy."""
        ...


class ServiceProvider(Protocol[T_co]):
    """Protocol for lazy service resolution."""

    def get(self) -> T_co:
        """Get the service instance."""
        ...


class AsyncServiceProvider(Protocol[T_co]):
    """Protocol for async lazy service resolution."""

    async def get_async(self) -> T_co:
        """Get the service instance asynchronously."""
        ...


class ScopeProvider(Protocol):
    """Protocol for scope management."""

    def begin_scope(self, scope_id: str) -> ScopeContext:
        """Begin a new scope."""
        ...

    def end_scope(self, scope_id: str) -> None:
        """End an existing scope."""
        ...


class ScopeContext(Protocol):
    """Protocol for scope context."""

    @property
    def scope_id(self) -> str:
        """Get the scope identifier."""
        ...

    def __enter__(self) -> ScopeContext:
        ...

    def __exit__(self, *args: Any) -> None:
        ...


# =============================================================================
# BASE CLASSES - Abstract implementations with lazy initialization
# =============================================================================


class BaseAsyncService(ABC):
    """
    Abstract base class for async services with lazy lock initialization.

    Provides default implementations of the AsyncService protocol methods
    with proper lazy initialization of asyncio primitives to avoid
    event loop issues.

    CRITICAL: This class uses lazy lock initialization pattern.
    All asyncio primitives (Lock, Event, etc.) are created on first
    async access, NOT in __init__. This prevents "attached to a different
    event loop" errors.

    Example Usage:
        class MyService(BaseAsyncService):
            def __init__(self):
                super().__init__()
                self._data = {}  # Regular sync initialization

            async def _do_initialize(self) -> None:
                # Called by initialize() under lock
                await load_data()

            async def _do_start(self) -> None:
                # Called by start() under lock
                await begin_processing()

            async def _do_stop(self) -> None:
                # Called by stop() under lock
                await stop_processing()

            async def _do_health_check(self) -> HealthReport:
                # Called by health_check()
                return HealthReport.healthy(self.service_name)
    """

    # Class-level slots for memory efficiency
    __slots__ = (
        '_service_name',
        '_state',
        '_dependencies',
        '_lock',
        '_initialized_event',
        '_started_at',
        '_init_duration_ms',
        '_start_duration_ms',
        '_error_count',
        '_health_check_count',
        '_recovery_count',
        '_last_error',
        '_last_health_check',
    )

    def __init__(
        self,
        service_name: Optional[str] = None,
        dependencies: Optional[Sequence[DependencySpec]] = None,
    ) -> None:
        """
        Initialize the base service.

        IMPORTANT: No asyncio primitives are created here!
        They are lazily initialized on first async access.

        Args:
            service_name: Optional name for the service (defaults to class name)
            dependencies: Optional sequence of service dependencies
        """
        self._service_name = service_name or self.__class__.__name__
        self._state = ServiceState.REGISTERED
        self._dependencies = tuple(dependencies) if dependencies else ()

        # LAZY INITIALIZATION - these are None until first async access
        self._lock: Optional[asyncio.Lock] = None
        self._initialized_event: Optional[asyncio.Event] = None

        # Tracking state
        self._started_at: Optional[float] = None
        self._init_duration_ms: float = 0.0
        self._start_duration_ms: float = 0.0
        self._error_count: int = 0
        self._health_check_count: int = 0
        self._recovery_count: int = 0
        self._last_error: Optional[str] = None
        self._last_health_check: Optional[datetime] = None

    @property
    def service_name(self) -> str:
        """Get the service name."""
        return self._service_name

    @property
    def state(self) -> ServiceState:
        """Get the current service state."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Check if service is currently running."""
        return self._state == ServiceState.RUNNING

    @property
    def is_initialized(self) -> bool:
        """Check if service is initialized."""
        return self._state in (
            ServiceState.INITIALIZED,
            ServiceState.STARTING,
            ServiceState.RUNNING,
            ServiceState.STOPPING,
        )

    @property
    def uptime_seconds(self) -> float:
        """Get uptime in seconds since start."""
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    async def _ensure_lock(self) -> asyncio.Lock:
        """
        Ensure the main lock exists, creating it lazily.

        This pattern is CRITICAL for avoiding event loop issues.
        The lock is created on first access in an async context,
        guaranteeing it's attached to the correct event loop.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def _ensure_initialized_event(self) -> asyncio.Event:
        """Ensure the initialized event exists, creating it lazily."""
        if self._initialized_event is None:
            self._initialized_event = asyncio.Event()
        return self._initialized_event

    async def initialize(self) -> None:
        """
        Initialize the service (template method pattern).

        Handles state transitions and calls _do_initialize() for
        subclass-specific initialization.
        """
        lock = await self._ensure_lock()
        async with lock:
            # Check if already initialized
            if self._state in (
                ServiceState.INITIALIZED,
                ServiceState.STARTING,
                ServiceState.RUNNING,
            ):
                logger.debug(f"[{self._service_name}] Already initialized, skipping")
                return

            # Check if in valid state for initialization
            if not self._state.can_initialize:
                raise ServiceInitializationError(
                    f"Cannot initialize from state {self._state}",
                    service_name=self._service_name,
                )

            self._state = ServiceState.INITIALIZING
            logger.debug(f"[{self._service_name}] Initializing...")

            start_time = time.time()
            try:
                await self._do_initialize()
                self._init_duration_ms = (time.time() - start_time) * 1000
                self._state = ServiceState.INITIALIZED

                # Signal initialization complete
                event = await self._ensure_initialized_event()
                event.set()

                logger.info(
                    f"[{self._service_name}] Initialized successfully "
                    f"in {self._init_duration_ms:.1f}ms"
                )

            except Exception as e:
                self._state = ServiceState.FAILED
                self._last_error = str(e)
                self._error_count += 1
                logger.error(f"[{self._service_name}] Initialization failed: {e}")
                raise ServiceInitializationError(
                    f"Failed to initialize {self._service_name}",
                    service_name=self._service_name,
                    cause=e,
                ) from e

    async def start(self) -> None:
        """
        Start the service (template method pattern).

        Handles state transitions and calls _do_start() for
        subclass-specific startup logic.
        """
        lock = await self._ensure_lock()
        async with lock:
            # Check if already running
            if self._state == ServiceState.RUNNING:
                logger.debug(f"[{self._service_name}] Already running, skipping")
                return

            # Must be initialized to start
            if self._state != ServiceState.INITIALIZED:
                raise ServiceInitializationError(
                    f"Cannot start from state {self._state}, must be INITIALIZED",
                    service_name=self._service_name,
                )

            self._state = ServiceState.STARTING
            logger.debug(f"[{self._service_name}] Starting...")

            start_time = time.time()
            try:
                await self._do_start()
                self._start_duration_ms = (time.time() - start_time) * 1000
                self._state = ServiceState.RUNNING
                self._started_at = time.time()
                logger.info(
                    f"[{self._service_name}] Started successfully "
                    f"in {self._start_duration_ms:.1f}ms"
                )

            except Exception as e:
                self._state = ServiceState.FAILED
                self._last_error = str(e)
                self._error_count += 1
                logger.error(f"[{self._service_name}] Start failed: {e}")
                raise ServiceInitializationError(
                    f"Failed to start {self._service_name}",
                    service_name=self._service_name,
                    cause=e,
                ) from e

    async def stop(self) -> None:
        """
        Stop the service (template method pattern).

        Handles state transitions and calls _do_stop() for
        subclass-specific shutdown logic.
        """
        lock = await self._ensure_lock()
        async with lock:
            # Check if already stopped
            if self._state in (ServiceState.STOPPED, ServiceState.DISPOSED):
                logger.debug(f"[{self._service_name}] Already stopped, skipping")
                return

            # Can stop from running or initialized states
            if not self._state.can_stop:
                logger.warning(
                    f"[{self._service_name}] Cannot stop from state {self._state}"
                )
                return

            self._state = ServiceState.STOPPING
            logger.debug(f"[{self._service_name}] Stopping...")

            try:
                await self._do_stop()
                self._state = ServiceState.STOPPED
                self._started_at = None
                logger.info(f"[{self._service_name}] Stopped successfully")

            except Exception as e:
                # Still mark as stopped, but log the error
                self._state = ServiceState.STOPPED
                self._last_error = str(e)
                self._error_count += 1
                logger.error(f"[{self._service_name}] Stop encountered error: {e}")
                # Don't re-raise - stopping should be best-effort

    async def health_check(self) -> HealthReport:
        """
        Perform a health check (template method pattern).

        Tracks health check metrics and delegates to _do_health_check().
        """
        start_time = time.time()
        self._health_check_count += 1
        self._last_health_check = datetime.utcnow()

        try:
            report = await self._do_health_check()
            return report

        except Exception as e:
            self._error_count += 1
            self._last_error = str(e)
            latency_ms = (time.time() - start_time) * 1000
            return HealthReport.unhealthy(
                service_name=self._service_name,
                error=str(e),
                latency_ms=latency_ms,
            )

    def get_dependencies(self) -> Sequence[DependencySpec]:
        """Get the service's dependencies."""
        return self._dependencies

    def get_metrics(self) -> ServiceMetrics:
        """Get current service metrics snapshot."""
        return ServiceMetrics(
            service_name=self._service_name,
            state=self._state,
            init_duration_ms=self._init_duration_ms,
            start_duration_ms=self._start_duration_ms,
            uptime_seconds=self.uptime_seconds,
            health_check_count=self._health_check_count,
            error_count=self._error_count,
            recovery_count=self._recovery_count,
            last_health_check=self._last_health_check,
            last_error=self._last_error,
        )

    async def wait_initialized(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for the service to be initialized.

        Args:
            timeout: Maximum time to wait in seconds, or None for no timeout

        Returns:
            True if initialized, False if timeout occurred
        """
        event = await self._ensure_initialized_event()
        if event.is_set():
            return True

        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # Abstract methods for subclasses to implement

    @abstractmethod
    async def _do_initialize(self) -> None:
        """
        Subclass-specific initialization logic.

        Called by initialize() under lock.
        Override this method instead of initialize().
        """
        ...

    @abstractmethod
    async def _do_start(self) -> None:
        """
        Subclass-specific startup logic.

        Called by start() under lock.
        Override this method instead of start().
        """
        ...

    @abstractmethod
    async def _do_stop(self) -> None:
        """
        Subclass-specific shutdown logic.

        Called by stop() under lock.
        Override this method instead of stop().
        """
        ...

    @abstractmethod
    async def _do_health_check(self) -> HealthReport:
        """
        Subclass-specific health check logic.

        Called by health_check().
        Override this method instead of health_check().

        Returns:
            HealthReport with current status
        """
        ...

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._service_name!r}, state={self._state.name})"


# =============================================================================
# EXCEPTION CLASSES - DI-specific errors
# =============================================================================


class DIError(Exception):
    """
    Base exception for all dependency injection errors.

    All DI-related exceptions inherit from this class,
    allowing for broad exception handling when needed.
    """

    __slots__ = ('message', 'details', 'timestamp')

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        self.timestamp = datetime.utcnow()

    def __str__(self) -> str:
        if self.details:
            detail_str = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.message!r}, {self.details!r})"


# Legacy alias
DIException = DIError


class CircularDependencyError(DIError):
    """
    Exception raised when a circular dependency is detected.

    Contains the full cycle path for debugging.
    """

    __slots__ = ('cycle',)

    def __init__(self, cycle: Sequence[Type]) -> None:
        self.cycle = tuple(cycle)
        # Support both Type objects and strings
        cycle_names = [
            t.__name__ if hasattr(t, '__name__') else str(t)
            for t in cycle
        ]
        cycle_str = " -> ".join(cycle_names)
        super().__init__(
            f"Circular dependency detected: {cycle_str}",
            cycle=self.cycle,
        )

    @property
    def cycle_path(self) -> str:
        """Get the cycle as a formatted path string."""
        cycle_names = [
            t.__name__ if hasattr(t, '__name__') else str(t)
            for t in self.cycle
        ]
        return " -> ".join(cycle_names)


class ServiceNotFoundError(DIError):
    """
    Exception raised when a requested service is not registered.
    """

    __slots__ = ('service_type', 'qualifier')

    def __init__(
        self,
        service_type: Type[Any],
        qualifier: Optional[str] = None,
    ) -> None:
        self.service_type = service_type
        self.qualifier = qualifier

        if qualifier:
            msg = f"Service {service_type.__name__} with qualifier '{qualifier}' not found"
        else:
            msg = f"Service {service_type.__name__} not found"

        super().__init__(
            msg,
            service_type=service_type.__name__,
            qualifier=qualifier,
        )


class ServiceInitializationError(DIError):
    """
    Exception raised when a service fails to initialize or start.

    Contains the original cause for proper exception chaining.
    """

    __slots__ = ('service_name', 'cause', 'phase')

    def __init__(
        self,
        message: str,
        service_name: Optional[str] = None,
        service_type: Optional[Type] = None,
        phase: Optional[LifecyclePhase] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        self.service_name = service_name or (
            service_type.__name__ if service_type else "Unknown"
        )
        self.cause = cause
        self.phase = phase
        super().__init__(
            message,
            service_name=self.service_name,
            phase=phase.value if phase else None,
            cause_type=type(cause).__name__ if cause else None,
            cause_message=str(cause) if cause else None,
        )

    def __str__(self) -> str:
        base = f"{self.message}"
        if self.cause:
            base = f"{base}: {self.cause}"
        return base


class DependencyResolutionError(DIError):
    """
    Exception raised when a dependency cannot be resolved.

    May be caused by missing registrations, failed factories, or
    other resolution issues.
    """

    __slots__ = ('dependency_type', 'service_type', 'reason', 'resolver_chain', 'cause')

    def __init__(
        self,
        dependency_type: Type[Any],
        reason: str,
        service_type: Optional[Type[Any]] = None,
        resolver_chain: Optional[Sequence[str]] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        self.dependency_type = dependency_type
        self.service_type = service_type
        self.reason = reason
        self.resolver_chain = tuple(resolver_chain) if resolver_chain else ()
        self.cause = cause

        if service_type:
            msg = (
                f"Cannot resolve {dependency_type.__name__} "
                f"for {service_type.__name__}: {reason}"
            )
        elif self.resolver_chain:
            chain_str = " <- ".join(self.resolver_chain)
            msg = f"Cannot resolve {dependency_type.__name__}: {reason} (resolution chain: {chain_str})"
        else:
            msg = f"Cannot resolve {dependency_type.__name__}: {reason}"

        super().__init__(
            msg,
            dependency_type=dependency_type.__name__,
            service_type=service_type.__name__ if service_type else None,
            reason=reason,
            resolver_chain=self.resolver_chain,
        )


class ServiceTimeoutError(DIError):
    """
    Exception raised when a service operation times out.
    """

    __slots__ = ('service_name', 'operation', 'timeout_seconds')

    def __init__(
        self,
        service_name: str,
        operation: str,
        timeout_seconds: float,
    ) -> None:
        self.service_name = service_name
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Service {service_name} timed out during {operation} "
            f"(timeout: {timeout_seconds}s)",
            service_name=service_name,
            operation=operation,
            timeout_seconds=timeout_seconds,
        )


class ContainerShutdownError(DIError):
    """
    Exception raised when operations are attempted on a shutdown container.
    """

    __slots__ = ('operation',)

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(
            f"Cannot perform {operation}: container is shutting down",
            operation=operation,
        )


class ScopeError(DIError):
    """
    Exception raised for scope-related errors.
    """

    __slots__ = ('scope', 'service_type', 'scope_name')

    def __init__(
        self,
        message: str,
        scope: Optional[Scope] = None,
        scope_name: Optional[str] = None,
        service_type: Optional[Type[Any]] = None,
    ) -> None:
        self.scope = scope
        self.scope_name = scope_name
        self.service_type = service_type
        super().__init__(
            message,
            scope=scope.name if scope else None,
            scope_name=scope_name,
            service_type=service_type.__name__ if service_type else None,
        )


# Legacy alias
ScopeNotFoundError = ScopeError


class DuplicateRegistrationError(DIError):
    """Raised when attempting to register a duplicate service."""

    __slots__ = ('service_type', 'existing_impl', 'new_impl')

    def __init__(
        self,
        service_type: Type,
        existing_impl: Type,
        new_impl: Type,
    ):
        self.service_type = service_type
        self.existing_impl = existing_impl
        self.new_impl = new_impl
        super().__init__(
            f"Duplicate registration for {service_type.__name__}: "
            f"existing={existing_impl.__name__}, new={new_impl.__name__}",
            service_type=service_type.__name__,
            existing_impl=existing_impl.__name__,
            new_impl=new_impl.__name__,
        )


class InvalidGraphStateError(DIError):
    """Raised when the dependency graph is in an invalid state."""

    __slots__ = ('reason', 'graph_details')

    def __init__(self, reason: str, details: Optional[Dict[str, Any]] = None):
        self.reason = reason
        self.graph_details = details or {}
        super().__init__(
            f"Invalid dependency graph state: {reason}",
            reason=reason,
            details=self.graph_details,
        )


# =============================================================================
# EVENT DEFINITIONS - Service Lifecycle Events
# =============================================================================


@dataclass(frozen=True)
class ServiceEvent:
    """Base class for service lifecycle events."""
    service_type: Type
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass(frozen=True)
class ServiceRegisteredEvent(ServiceEvent):
    """Emitted when a service is registered."""
    qualifier: Optional[str] = None
    scope: Scope = Scope.SINGLETON


@dataclass(frozen=True)
class ServiceResolvedEvent(ServiceEvent):
    """Emitted when a service is resolved."""
    resolution_time_ms: float = 0.0
    from_cache: bool = False


@dataclass(frozen=True)
class ServiceCreatedEvent(ServiceEvent):
    """Emitted when a service instance is created."""
    factory_used: bool = False


@dataclass(frozen=True)
class ServiceDisposedEvent(ServiceEvent):
    """Emitted when a service is disposed."""
    pass


@dataclass(frozen=True)
class CycleDetectedEvent(ServiceEvent):
    """Emitted when a cycle is detected."""
    cycle_path: Tuple[Type, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ServiceStateChangedEvent(ServiceEvent):
    """Emitted when a service state changes."""
    old_state: ServiceState = ServiceState.UNREGISTERED
    new_state: ServiceState = ServiceState.REGISTERED


@dataclass(frozen=True)
class HealthCheckCompletedEvent(ServiceEvent):
    """Emitted when a health check completes."""
    report: Optional[HealthReport] = None


# =============================================================================
# RUNTIME TYPE CHECKING HELPERS
# =============================================================================


def is_async_service(obj: Any) -> bool:
    """
    Check if an object implements the AsyncService protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements AsyncService
    """
    return isinstance(obj, AsyncService)


def is_disposable(obj: Any) -> bool:
    """
    Check if an object implements the Disposable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements Disposable
    """
    return isinstance(obj, Disposable)


def is_async_disposable(obj: Any) -> bool:
    """
    Check if an object implements the AsyncDisposable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements AsyncDisposable
    """
    return isinstance(obj, AsyncDisposable)


def is_resettable(obj: Any) -> bool:
    """
    Check if an object implements the Resettable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements Resettable
    """
    return isinstance(obj, Resettable)


def is_configurable(obj: Any) -> bool:
    """
    Check if an object implements the Configurable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements Configurable
    """
    return isinstance(obj, Configurable)


def is_startable(obj: Any) -> bool:
    """
    Check if an object implements the Startable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements Startable
    """
    return isinstance(obj, Startable)


def is_health_checkable(obj: Any) -> bool:
    """
    Check if an object implements the HealthCheckable protocol.

    Args:
        obj: Object to check

    Returns:
        True if object implements HealthCheckable
    """
    return isinstance(obj, HealthCheckable)


def validate_service_factory(factory: Any) -> bool:
    """
    Validate that an object is a valid service factory.

    Args:
        factory: Object to validate

    Returns:
        True if object is a valid factory
    """
    return callable(factory)


def get_type_name(t: Type[Any]) -> str:
    """
    Get the fully qualified name of a type.

    Args:
        t: Type to get name of

    Returns:
        Fully qualified type name
    """
    module = getattr(t, '__module__', None)
    qualname = getattr(t, '__qualname__', getattr(t, '__name__', str(t)))

    if module and module != 'builtins':
        return f"{module}.{qualname}"
    return qualname


# =============================================================================
# CONSTANTS
# =============================================================================

# Default timeouts in seconds
DEFAULT_INITIALIZATION_TIMEOUT: Final[float] = 30.0
DEFAULT_HEALTH_CHECK_TIMEOUT: Final[float] = 5.0
DEFAULT_SHUTDOWN_TIMEOUT: Final[float] = 30.0

# Default retry configuration
DEFAULT_MAX_RETRY_COUNT: Final[int] = 3
DEFAULT_RETRY_BACKOFF_FACTOR: Final[float] = 2.0
DEFAULT_INITIAL_RETRY_DELAY: Final[float] = 0.1

# Service tags
TAG_CORE: Final[str] = "core"
TAG_INFRASTRUCTURE: Final[str] = "infrastructure"
TAG_BUSINESS: Final[str] = "business"
TAG_PRESENTATION: Final[str] = "presentation"
TAG_TEST: Final[str] = "test"


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    # Type Variables
    "T",
    "T_co",
    "T_contra",
    "ServiceT",
    "FactoryT",

    # Enums (new)
    "ServiceState",
    "HealthStatus",
    "ServiceCriticality",
    "Scope",
    "DependencyType",

    # Enums (legacy)
    "ServiceScope",
    "LifecyclePhase",
    "ResolutionStrategy",

    # Frozen Data Classes (new)
    "HealthReport",
    "DependencySpec",
    "ServiceDefinitionSpec",
    "ServiceMetrics",

    # Mutable Data Classes (legacy)
    "ServiceDefinition",
    "ResolutionContext",

    # Protocols
    "AsyncService",
    "ServiceFactory",
    "Disposable",
    "AsyncDisposable",
    "Resettable",
    "Configurable",
    "Initializable",
    "AsyncInitializable",
    "Startable",
    "HealthCheckable",
    "ServiceProvider",
    "AsyncServiceProvider",
    "ScopeProvider",
    "ScopeContext",

    # Base Classes
    "BaseAsyncService",

    # Exceptions
    "DIError",
    "DIException",  # Legacy alias
    "CircularDependencyError",
    "ServiceNotFoundError",
    "ServiceInitializationError",
    "DependencyResolutionError",
    "ServiceTimeoutError",
    "ContainerShutdownError",
    "ScopeError",
    "ScopeNotFoundError",  # Legacy alias
    "DuplicateRegistrationError",
    "InvalidGraphStateError",

    # Events
    "ServiceEvent",
    "ServiceRegisteredEvent",
    "ServiceResolvedEvent",
    "ServiceCreatedEvent",
    "ServiceDisposedEvent",
    "CycleDetectedEvent",
    "ServiceStateChangedEvent",
    "HealthCheckCompletedEvent",

    # Type Helpers
    "is_async_service",
    "is_disposable",
    "is_async_disposable",
    "is_resettable",
    "is_configurable",
    "is_startable",
    "is_health_checkable",
    "validate_service_factory",
    "get_type_name",

    # Constants
    "DEFAULT_INITIALIZATION_TIMEOUT",
    "DEFAULT_HEALTH_CHECK_TIMEOUT",
    "DEFAULT_SHUTDOWN_TIMEOUT",
    "DEFAULT_MAX_RETRY_COUNT",
    "DEFAULT_RETRY_BACKOFF_FACTOR",
    "DEFAULT_INITIAL_RETRY_DELAY",
    "TAG_CORE",
    "TAG_INFRASTRUCTURE",
    "TAG_BUSINESS",
    "TAG_PRESENTATION",
    "TAG_TEST",
]
