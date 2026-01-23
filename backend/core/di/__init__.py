"""
JARVIS Dependency Injection Framework v2.0
==========================================

Enterprise-grade dependency injection for the JARVIS AI system.

This package provides:
- Type-safe service registration and resolution
- Automatic dependency graph analysis
- Cycle detection via Tarjan's algorithm
- Topological initialization ordering via Kahn's algorithm
- Parallel group computation for concurrent startup
- Rich visualization exports (DOT, Mermaid, JSON)
- Lifecycle event system with distributed tracing
- Lazy lock initialization for async safety
- Cross-repo coordination for Trinity (JARVIS, Prime, Reactor)
- Circuit breaker pattern for cascade failure prevention
- Retry policies with exponential backoff and jitter

Modules:
    protocols: Core protocol definitions, exceptions, and data classes
    resolution: Dependency resolver with graph algorithms
    events: Lifecycle event system with event emission and subscription
    container: Enterprise DI container with lifecycle management
    remote: Cross-repo coordination with circuit breakers and service discovery

Usage:
    from backend.core.di import (
        ServiceDefinition,
        DependencySpec,
        DependencyType,
        DependencyResolver,
    )

    # Define services
    definitions = [
        ServiceDefinition(
            interface=IDatabase,
            implementation=PostgresDatabase,
            dependencies=[
                DependencySpec(IConfig, DependencyType.REQUIRED),
            ],
        ),
        ServiceDefinition(
            interface=IConfig,
            implementation=EnvConfig,
        ),
    ]

    # Create resolver and validate
    resolver = DependencyResolver(definitions)
    resolver.validate_all()  # Raises if cycles exist

    # Get initialization order
    init_order = resolver.get_initialization_order()
    parallel_groups = resolver.get_parallel_groups()

    # Visualize
    print(resolver.to_mermaid())

Author: JARVIS AI System
Version: 2.0.0
"""

# Import from protocols module - comprehensive type system
from .protocols import (
    # New Enums (use these for new code)
    ServiceState,
    HealthStatus,
    ServiceCriticality,
    Scope,
    DependencyType,

    # Legacy Enums (for backward compatibility)
    ServiceScope,
    LifecyclePhase,
    ResolutionStrategy,

    # Frozen Data Classes (new - immutable, slots)
    HealthReport,
    ServiceDefinitionSpec,
    ServiceMetrics,

    # Data Classes (includes both frozen and mutable)
    DependencySpec,
    ServiceDefinition,
    ResolutionContext,

    # Protocols
    AsyncService,
    ServiceFactory,
    Disposable,
    AsyncDisposable,
    Resettable,
    Configurable,
    Initializable,
    AsyncInitializable,
    Startable,
    HealthCheckable,
    ServiceProvider,
    AsyncServiceProvider,
    ScopeProvider,
    ScopeContext,

    # Base Classes
    BaseAsyncService,

    # Exceptions
    DIError,
    DIException,  # Legacy alias
    CircularDependencyError,
    ServiceNotFoundError,
    ServiceInitializationError,
    DependencyResolutionError,
    ServiceTimeoutError,
    ContainerShutdownError,
    ScopeError,
    ScopeNotFoundError,  # Legacy alias
    DuplicateRegistrationError,
    InvalidGraphStateError,

    # Events (from protocols - frozen dataclasses)
    ServiceEvent as ProtocolServiceEvent,
    ServiceRegisteredEvent,
    ServiceResolvedEvent,
    ServiceCreatedEvent,
    ServiceDisposedEvent,
    CycleDetectedEvent,
    ServiceStateChangedEvent,
    HealthCheckCompletedEvent,

    # Type Helpers
    is_async_service,
    is_disposable,
    is_async_disposable,
    is_resettable,
    is_configurable,
    is_startable,
    is_health_checkable,
    validate_service_factory,
    get_type_name,

    # Constants
    DEFAULT_INITIALIZATION_TIMEOUT,
    DEFAULT_HEALTH_CHECK_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    DEFAULT_MAX_RETRY_COUNT,
    DEFAULT_RETRY_BACKOFF_FACTOR,
    DEFAULT_INITIAL_RETRY_DELAY,
    TAG_CORE,
    TAG_INFRASTRUCTURE,
    TAG_BUSINESS,
    TAG_PRESENTATION,
    TAG_TEST,
)

# Import from resolution module
from .resolution import (
    # Main class
    DependencyResolver,

    # Supporting types
    EdgeMetadata,
    StronglyConnectedComponent,
    ParallelGroup,
    GraphStatistics,
    CacheEntry,

    # Convenience functions
    create_resolver,
    detect_cycles_in_definitions,
    get_init_order,
)

# Import from events module (lifecycle event system)
from .events import (
    # Enums
    ServiceEvent,  # This is the comprehensive lifecycle enum

    # Data classes
    EventData,
    EventDataBuilder,
    Subscription,
    EventHistoryEntry,

    # Core classes
    EventEmitter,
    EventHistory,
    WeakMethodRef,

    # Protocols
    MetricsCollector,
    EventExporter,

    # Built-in implementations
    LoggingExporter,
    NoOpMetricsCollector,

    # Decorators and context managers
    on_event,
    on_all_events,
    event_scope,
    event_scope_sync,
    trace_context,

    # Trace context
    set_trace_context,
    get_trace_context,
    clear_trace_context,

    # Global instance
    get_emitter,
    init_emitter,
    shutdown_emitter,

    # Testing
    EventCapture,
)

# Import from container module (enterprise DI container)
from .container import (
    # Main class
    ServiceContainer,

    # Supporting classes
    ServiceRegistration,
    ScopedContainer,
    ContainerState,

    # Factory functions
    create_container,
    bootstrap_container,
)

# Import from remote module (cross-repo coordination)
from .remote import (
    # Enums
    CircuitBreakerState,
    RemoteCallState,

    # Exceptions
    RemoteServiceError,
    CircuitOpenError,
    RemoteCallTimeoutError,
    ServiceUnavailableError,
    RegistryError,

    # Data classes
    RetryPolicy,
    CircuitBreakerMetrics,
    ServiceRegistryEntry,
    RemoteCallResult,

    # Core classes
    CircuitBreaker,
    ServiceRegistry,
    RemoteServiceProxy,
    CrossRepoCoordinator,

    # Factory functions
    create_circuit_breaker,
    create_retry_policy,
    create_coordinator,

    # Global instance
    get_coordinator,
    shutdown_coordinator,

    # Constants
    TRINITY_SERVICES,
)


__version__ = "2.0.0"

__all__ = [
    # Version
    "__version__",

    # === PROTOCOLS MODULE ===

    # New Enums
    "ServiceState",
    "HealthStatus",
    "ServiceCriticality",
    "Scope",
    "DependencyType",

    # Legacy Enums
    "ServiceScope",
    "LifecyclePhase",
    "ResolutionStrategy",

    # Frozen Data Classes
    "HealthReport",
    "ServiceDefinitionSpec",
    "ServiceMetrics",

    # Data Classes
    "DependencySpec",
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
    "DIException",
    "CircularDependencyError",
    "ServiceNotFoundError",
    "ServiceInitializationError",
    "DependencyResolutionError",
    "ServiceTimeoutError",
    "ContainerShutdownError",
    "ScopeError",
    "ScopeNotFoundError",
    "DuplicateRegistrationError",
    "InvalidGraphStateError",

    # Protocol Events (frozen dataclasses)
    "ProtocolServiceEvent",
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

    # === RESOLUTION MODULE ===

    # Resolver
    "DependencyResolver",
    "EdgeMetadata",
    "StronglyConnectedComponent",
    "ParallelGroup",
    "GraphStatistics",
    "CacheEntry",

    # Convenience functions
    "create_resolver",
    "detect_cycles_in_definitions",
    "get_init_order",

    # === EVENTS MODULE ===

    # Lifecycle events enum
    "ServiceEvent",

    # Data classes
    "EventData",
    "EventDataBuilder",
    "Subscription",
    "EventHistoryEntry",

    # Core classes
    "EventEmitter",
    "EventHistory",
    "WeakMethodRef",

    # Protocols
    "MetricsCollector",
    "EventExporter",

    # Built-in implementations
    "LoggingExporter",
    "NoOpMetricsCollector",

    # Decorators and context managers
    "on_event",
    "on_all_events",
    "event_scope",
    "event_scope_sync",
    "trace_context",

    # Trace context
    "set_trace_context",
    "get_trace_context",
    "clear_trace_context",

    # Global instance
    "get_emitter",
    "init_emitter",
    "shutdown_emitter",

    # Testing
    "EventCapture",

    # === CONTAINER MODULE ===

    # Main class
    "ServiceContainer",

    # Supporting classes
    "ServiceRegistration",
    "ScopedContainer",
    "ContainerState",

    # Factory functions
    "create_container",
    "bootstrap_container",

    # === REMOTE MODULE ===

    # Enums
    "CircuitBreakerState",
    "RemoteCallState",

    # Exceptions
    "RemoteServiceError",
    "CircuitOpenError",
    "RemoteCallTimeoutError",
    "ServiceUnavailableError",
    "RegistryError",

    # Data classes
    "RetryPolicy",
    "CircuitBreakerMetrics",
    "ServiceRegistryEntry",
    "RemoteCallResult",

    # Core classes
    "CircuitBreaker",
    "ServiceRegistry",
    "RemoteServiceProxy",
    "CrossRepoCoordinator",

    # Factory functions
    "create_circuit_breaker",
    "create_retry_policy",
    "create_coordinator",

    # Global instance
    "get_coordinator",
    "shutdown_coordinator",

    # Constants
    "TRINITY_SERVICES",
]
