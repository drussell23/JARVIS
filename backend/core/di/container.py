"""
JARVIS Enterprise-Grade Service Container v1.0
===============================================

Production-ready dependency injection container with advanced features:
- Double-check locking for singleton resolution
- Transactional initialization with rollback on failure
- Lazy lock initialization for event loop safety
- Scoped containers for request-level isolation
- Health monitoring with background tasks
- Comprehensive event emission
- Full metrics and observability

Architecture:
    +------------------------------------------------------------------+
    |                    ServiceContainer                               |
    +------------------------------------------------------------------+
    |  Registration    |  Resolution     |  Lifecycle     | Health     |
    +------------------------------------------------------------------+
    |  register()      |  resolve()      |  initialize()  | health()   |
    |  register_all()  |  resolve_all()  |  start()       | monitor()  |
    |  unregister()    |  try_resolve()  |  stop()        | metrics()  |
    +------------------------------------------------------------------+
    |                   ScopedContainer (child)                         |
    +------------------------------------------------------------------+

Thread Safety:
    - All operations use proper locking (lazy async locks, threading RLocks)
    - Double-check locking pattern for singleton resolution
    - Atomic state transitions with validation
    - Task tracking for cleanup

CRITICAL: This module uses lazy lock initialization.
    Never create asyncio.Lock() or asyncio.Event() at module/class init.
    Always create them in async context on first access.

Author: JARVIS AI System
Version: 1.0.0 (January 2026)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
import traceback
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    Generic,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

# Python 3.11+ has Self in typing, earlier versions need typing_extensions
try:
    from typing import Self
except ImportError:
    from typing_extensions import Self

from backend.core.di.protocols import (
    AsyncDisposable,
    AsyncService,
    BaseAsyncService,
    ContainerShutdownError,
    CircularDependencyError,
    DEFAULT_HEALTH_CHECK_TIMEOUT,
    DEFAULT_INITIALIZATION_TIMEOUT,
    DEFAULT_SHUTDOWN_TIMEOUT,
    DependencyResolutionError,
    DependencySpec,
    DependencyType,
    DIError,
    Disposable,
    HealthReport,
    HealthStatus,
    Scope,
    ServiceCriticality,
    ServiceDefinition,
    ServiceDefinitionSpec,
    ServiceInitializationError,
    ServiceMetrics,
    ServiceNotFoundError,
    ServiceState,
    ServiceTimeoutError,
    is_async_disposable,
    is_async_service,
    is_disposable,
)
from backend.core.di.resolution import DependencyResolver
from backend.core.di.events import (
    EventData,
    EventDataBuilder,
    EventEmitter,
    ServiceEvent,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
ServiceT = TypeVar("ServiceT")


# =============================================================================
# Container State Enum
# =============================================================================


class ContainerState:
    """
    Container lifecycle states.

    States flow: CREATED -> INITIALIZING -> READY -> SHUTTING_DOWN -> SHUTDOWN
    """
    CREATED = "created"
    INITIALIZING = "initializing"
    READY = "ready"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"


# =============================================================================
# Service Registration Entry
# =============================================================================


@dataclass
class ServiceRegistration(Generic[T]):
    """
    Internal registration entry for a service.

    Tracks all metadata and state for a registered service type.
    """
    service_type: Type[T]
    scope: Scope
    criticality: ServiceCriticality
    dependencies: Tuple[DependencySpec, ...]
    factory: Optional[Callable[..., Union[T, Awaitable[T]]]]
    enabled_when: Optional[Callable[[], bool]]
    tags: FrozenSet[str]

    # Runtime state
    state: ServiceState = ServiceState.REGISTERED
    instance: Optional[T] = None
    error: Optional[Exception] = None

    # Metrics
    init_start_time: Optional[float] = None
    init_duration_ms: float = 0.0
    start_duration_ms: float = 0.0
    started_at: Optional[float] = None
    health_check_count: int = 0
    error_count: int = 0
    recovery_count: int = 0
    last_health_check: Optional[datetime] = None
    last_error: Optional[str] = None

    # Configuration
    initialization_timeout: float = DEFAULT_INITIALIZATION_TIMEOUT
    health_check_timeout: float = DEFAULT_HEALTH_CHECK_TIMEOUT

    @property
    def service_name(self) -> str:
        """Get the service name."""
        return self.service_type.__name__

    @property
    def is_enabled(self) -> bool:
        """Check if service is enabled."""
        if self.enabled_when is None:
            return True
        try:
            return self.enabled_when()
        except Exception:
            return False

    @property
    def is_singleton(self) -> bool:
        """Check if service is singleton scoped."""
        return self.scope == Scope.SINGLETON

    @property
    def has_instance(self) -> bool:
        """Check if service has been instantiated."""
        return self.instance is not None

    @property
    def uptime_seconds(self) -> float:
        """Get uptime in seconds since start."""
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    def get_metrics(self) -> ServiceMetrics:
        """Get current metrics snapshot."""
        return ServiceMetrics(
            service_name=self.service_name,
            state=self.state,
            init_duration_ms=self.init_duration_ms,
            start_duration_ms=self.start_duration_ms,
            uptime_seconds=self.uptime_seconds,
            health_check_count=self.health_check_count,
            error_count=self.error_count,
            recovery_count=self.recovery_count,
            last_health_check=self.last_health_check,
            last_error=self.last_error,
        )


# =============================================================================
# Scoped Container
# =============================================================================


class ScopedContainer:
    """
    Child container for scoped service resolution.

    Creates isolated service instances for a specific scope (e.g., request).
    Automatically cleans up instances when the scope ends.

    Example:
        async with container.create_scope("request-123") as scope:
            service = await scope.resolve(RequestScopedService)
            # Service is unique to this scope
    """

    __slots__ = (
        "_parent",
        "_scope_id",
        "_instances",
        "_lock",
        "_created_at",
        "_is_disposed",
    )

    def __init__(
        self,
        parent: "ServiceContainer",
        scope_id: str,
    ) -> None:
        """
        Initialize scoped container.

        Args:
            parent: Parent container for non-scoped resolution
            scope_id: Unique identifier for this scope
        """
        self._parent = parent
        self._scope_id = scope_id
        self._instances: Dict[Type, Any] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._created_at = time.time()
        self._is_disposed = False

    @property
    def scope_id(self) -> str:
        """Get the scope identifier."""
        return self._scope_id

    @property
    def parent(self) -> "ServiceContainer":
        """Get the parent container."""
        return self._parent

    async def _get_lock(self) -> asyncio.Lock:
        """Lazily create and return the async lock."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def resolve(self, service_type: Type[T]) -> T:
        """
        Resolve a service within this scope.

        For SCOPED/REQUEST scope, returns scope-local instance.
        For other scopes, delegates to parent container.

        Args:
            service_type: The type to resolve

        Returns:
            Service instance
        """
        if self._is_disposed:
            raise ContainerShutdownError(
                f"Cannot resolve {service_type.__name__}: scope is disposed"
            )

        registration = self._parent._get_registration(service_type)
        if registration is None:
            raise ServiceNotFoundError(service_type)

        # Non-scoped services resolve from parent
        if not registration.scope.requires_scope:
            return await self._parent.resolve(service_type)

        # Scoped services use scope-local instance
        lock = await self._get_lock()
        async with lock:
            if service_type in self._instances:
                return cast(T, self._instances[service_type])

            # Create new instance for this scope
            instance = await self._parent._create_instance(service_type)
            self._instances[service_type] = instance

            # Initialize if AsyncService
            if is_async_service(instance):
                await instance.initialize()

            return instance

    async def dispose(self) -> None:
        """
        Dispose of all scoped instances.

        Calls stop() and dispose() on all instances that support it.
        """
        if self._is_disposed:
            return

        self._is_disposed = True
        errors: List[Exception] = []

        # Dispose in reverse creation order
        for service_type in reversed(list(self._instances.keys())):
            instance = self._instances[service_type]

            try:
                if is_async_service(instance):
                    await instance.stop()

                if is_async_disposable(instance):
                    await instance.dispose()
                elif is_disposable(instance):
                    instance.dispose()

            except Exception as e:
                errors.append(e)
                logger.error(
                    f"Error disposing scoped {service_type.__name__}: {e}"
                )

        self._instances.clear()

        if errors:
            logger.warning(
                f"Scope {self._scope_id} disposed with {len(errors)} errors"
            )

    async def __aenter__(self) -> "ScopedContainer":
        """Enter the scope context."""
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the scope context and cleanup."""
        await self.dispose()


# =============================================================================
# Service Container
# =============================================================================


class ServiceContainer:
    """
    Enterprise-grade async dependency injection container.

    Features:
    - Double-check locking for thread-safe singleton resolution
    - Transactional initialization with rollback on failure
    - Lazy lock initialization for event loop safety
    - Scoped containers for request-level isolation
    - Health monitoring with background tasks
    - Comprehensive event emission
    - Full metrics and observability

    Example:
        container = ServiceContainer()

        # Registration
        container.register(
            DatabaseService,
            scope=Scope.SINGLETON,
            criticality=ServiceCriticality.CRITICAL,
        ).register(
            CacheService,
            scope=Scope.SINGLETON,
            dependencies=[DependencySpec(DatabaseService)],
        )

        # Lifecycle
        await container.initialize_all()
        await container.start_all()

        # Resolution
        db = await container.resolve(DatabaseService)
        cache = await container.resolve(CacheService)

        # Scoped resolution
        async with container.create_scope("request-123") as scope:
            request_service = await scope.resolve(RequestService)

        # Shutdown
        await container.shutdown_all()
    """

    __slots__ = (
        # Registrations
        "_registrations",
        "_instances",
        "_singleton_instances",
        "_instance_locks",

        # Locks (lazy initialization)
        "_resolution_lock",
        "_lifecycle_lock",
        "_registration_lock",
        "_init_lock",

        # Thread-safe locks
        "_sync_lock",

        # State
        "_state",
        "_initialization_order",

        # Dependencies
        "_resolver",
        "_event_emitter",

        # Tasks
        "_background_tasks",
        "_health_monitor_task",
        "_executor",

        # Configuration
        "_default_timeout",
        "_health_check_interval",
        "_enable_health_monitoring",

        # Metrics
        "_container_created_at",
        "_container_started_at",
        "_resolution_count",
        "_error_count",

        # Scopes
        "_active_scopes",
        "_scope_counter",
    )

    def __init__(
        self,
        event_emitter: Optional[EventEmitter] = None,
        default_timeout: float = DEFAULT_INITIALIZATION_TIMEOUT,
        health_check_interval: float = 30.0,
        enable_health_monitoring: bool = True,
        max_workers: int = 4,
    ) -> None:
        """
        Initialize the service container.

        IMPORTANT: No asyncio primitives are created here.
        They are lazily initialized on first async access.

        Args:
            event_emitter: Optional event emitter for lifecycle events
            default_timeout: Default timeout for service operations
            health_check_interval: Interval between health checks (seconds)
            enable_health_monitoring: Whether to enable background health monitoring
            max_workers: Maximum worker threads for blocking operations
        """
        # Registrations
        self._registrations: Dict[Type, ServiceRegistration] = {}
        self._instances: Dict[Type, Any] = {}
        self._singleton_instances: Dict[Type, Any] = {}
        self._instance_locks: Dict[Type, asyncio.Lock] = {}

        # LAZY LOCK INITIALIZATION - None until first async access
        self._resolution_lock: Optional[asyncio.Lock] = None
        self._lifecycle_lock: Optional[asyncio.Lock] = None
        self._registration_lock: Optional[asyncio.Lock] = None
        self._init_lock: Optional[asyncio.Lock] = None

        # Thread-safe lock for sync operations
        self._sync_lock = threading.RLock()

        # State
        self._state = ContainerState.CREATED
        self._initialization_order: List[Type] = []

        # Dependencies
        self._resolver: Optional[DependencyResolver] = None
        self._event_emitter = event_emitter or EventEmitter()

        # Tasks
        self._background_tasks: Set[asyncio.Task] = set()
        self._health_monitor_task: Optional[asyncio.Task] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

        # Configuration
        self._default_timeout = default_timeout
        self._health_check_interval = health_check_interval
        self._enable_health_monitoring = enable_health_monitoring

        # Metrics
        self._container_created_at = time.time()
        self._container_started_at: Optional[float] = None
        self._resolution_count = 0
        self._error_count = 0

        # Scopes
        self._active_scopes: Dict[str, ScopedContainer] = {}
        self._scope_counter = 0

        logger.debug("ServiceContainer created")

    # =========================================================================
    # Lazy Lock Initialization
    # =========================================================================

    async def _get_resolution_lock(self) -> asyncio.Lock:
        """Lazily create and return the resolution lock."""
        if self._resolution_lock is None:
            self._resolution_lock = asyncio.Lock()
        return self._resolution_lock

    async def _get_lifecycle_lock(self) -> asyncio.Lock:
        """Lazily create and return the lifecycle lock."""
        if self._lifecycle_lock is None:
            self._lifecycle_lock = asyncio.Lock()
        return self._lifecycle_lock

    async def _get_registration_lock(self) -> asyncio.Lock:
        """Lazily create and return the registration lock."""
        if self._registration_lock is None:
            self._registration_lock = asyncio.Lock()
        return self._registration_lock

    async def _get_init_lock(self) -> asyncio.Lock:
        """Lazily create and return the initialization lock."""
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()
        return self._init_lock

    async def _get_instance_lock(self, service_type: Type) -> asyncio.Lock:
        """Get or create a lock for a specific service type."""
        if service_type not in self._instance_locks:
            self._instance_locks[service_type] = asyncio.Lock()
        return self._instance_locks[service_type]

    # =========================================================================
    # Registration Methods
    # =========================================================================

    def register(
        self,
        service_type: Type[T],
        scope: Scope = Scope.SINGLETON,
        criticality: ServiceCriticality = ServiceCriticality.REQUIRED,
        dependencies: Optional[Sequence[DependencySpec]] = None,
        factory: Optional[Callable[..., Union[T, Awaitable[T]]]] = None,
        enabled_when: Optional[Callable[[], bool]] = None,
        tags: Optional[Set[str]] = None,
        initialization_timeout: Optional[float] = None,
        health_check_timeout: Optional[float] = None,
    ) -> Self:
        """
        Register a service type with the container.

        Args:
            service_type: The type to register
            scope: Service lifetime scope
            criticality: Criticality level for startup/failure handling
            dependencies: Optional sequence of dependency specifications
            factory: Optional factory function for creating instances
            enabled_when: Optional function to check if service is enabled
            tags: Optional set of string tags for categorization
            initialization_timeout: Optional initialization timeout override
            health_check_timeout: Optional health check timeout override

        Returns:
            Self for method chaining
        """
        with self._sync_lock:
            registration = ServiceRegistration(
                service_type=service_type,
                scope=scope,
                criticality=criticality,
                dependencies=tuple(dependencies or ()),
                factory=factory,
                enabled_when=enabled_when,
                tags=frozenset(tags or set()),
                initialization_timeout=initialization_timeout or self._default_timeout,
                health_check_timeout=health_check_timeout or DEFAULT_HEALTH_CHECK_TIMEOUT,
            )

            self._registrations[service_type] = registration

            # Invalidate resolver cache
            self._resolver = None

            logger.debug(f"Registered {service_type.__name__} with scope {scope.name}")

        return self

    def register_instance(
        self,
        service_type: Type[T],
        instance: T,
        tags: Optional[Set[str]] = None,
    ) -> Self:
        """
        Register an existing instance as a singleton.

        Args:
            service_type: The type to register
            instance: The pre-created instance
            tags: Optional set of string tags

        Returns:
            Self for method chaining
        """
        with self._sync_lock:
            registration = ServiceRegistration(
                service_type=service_type,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.REQUIRED,
                dependencies=(),
                factory=None,
                enabled_when=None,
                tags=frozenset(tags or set()),
                state=ServiceState.INITIALIZED,
                instance=instance,
            )

            self._registrations[service_type] = registration
            self._singleton_instances[service_type] = instance

            logger.debug(f"Registered instance of {service_type.__name__}")

        return self

    def register_all(
        self,
        definitions: List[ServiceDefinition],
    ) -> Self:
        """
        Register multiple services from definitions.

        Args:
            definitions: List of ServiceDefinition objects

        Returns:
            Self for method chaining
        """
        with self._sync_lock:
            for defn in definitions:
                registration = ServiceRegistration(
                    service_type=defn.interface,
                    scope=Scope[defn.scope.name.upper()] if hasattr(defn.scope, 'name') else Scope.SINGLETON,
                    criticality=ServiceCriticality.REQUIRED,
                    dependencies=tuple(defn.dependencies),
                    factory=defn.factory,
                    enabled_when=None,
                    tags=frozenset(defn.tags),
                )

                self._registrations[defn.interface] = registration

            # Invalidate resolver cache
            self._resolver = None

            logger.debug(f"Registered {len(definitions)} services")

        return self

    def unregister(self, service_type: Type) -> bool:
        """
        Remove a service registration.

        Args:
            service_type: The type to unregister

        Returns:
            True if service was found and removed
        """
        with self._sync_lock:
            if service_type in self._registrations:
                del self._registrations[service_type]
                self._singleton_instances.pop(service_type, None)
                self._resolver = None

                logger.debug(f"Unregistered {service_type.__name__}")
                return True
            return False

    def is_registered(self, service_type: Type) -> bool:
        """
        Check if a service type is registered.

        Args:
            service_type: The type to check

        Returns:
            True if registered
        """
        with self._sync_lock:
            return service_type in self._registrations

    def _get_registration(
        self,
        service_type: Type[T],
    ) -> Optional[ServiceRegistration[T]]:
        """Get registration for a service type."""
        with self._sync_lock:
            return self._registrations.get(service_type)

    # =========================================================================
    # Resolution Methods (Double-Check Locking)
    # =========================================================================

    async def resolve(self, service_type: Type[T]) -> T:
        """
        Resolve a service instance.

        Uses double-check locking for thread-safe singleton resolution.

        Args:
            service_type: The type to resolve

        Returns:
            Service instance

        Raises:
            ServiceNotFoundError: If service is not registered
            ContainerShutdownError: If container is shutting down
            DependencyResolutionError: If resolution fails
        """
        # Check container state
        if self._state == ContainerState.SHUTDOWN:
            raise ContainerShutdownError(f"resolve {service_type.__name__}")

        # First check - unsynchronized
        if service_type in self._singleton_instances:
            self._resolution_count += 1
            return cast(T, self._singleton_instances[service_type])

        # Get registration
        registration = self._get_registration(service_type)
        if registration is None:
            raise ServiceNotFoundError(service_type)

        if not registration.is_enabled:
            raise DependencyResolutionError(
                service_type,
                reason="Service is disabled",
            )

        # Handle non-singleton scopes
        if not registration.is_singleton:
            return await self._create_instance(service_type)

        # Double-check locking for singletons
        lock = await self._get_instance_lock(service_type)
        async with lock:
            # Second check - synchronized
            if service_type in self._singleton_instances:
                self._resolution_count += 1
                return cast(T, self._singleton_instances[service_type])

            # Create and cache instance
            instance = await self._create_instance(service_type)
            self._singleton_instances[service_type] = instance
            registration.instance = instance

            self._resolution_count += 1
            return instance

    async def resolve_optional(
        self,
        service_type: Type[T],
    ) -> Optional[T]:
        """
        Resolve a service if registered, else return None.

        Args:
            service_type: The type to resolve

        Returns:
            Service instance or None
        """
        try:
            return await self.resolve(service_type)
        except (ServiceNotFoundError, DependencyResolutionError):
            return None

    async def resolve_all(
        self,
        service_types: List[Type],
    ) -> Dict[Type, Any]:
        """
        Resolve multiple services concurrently.

        Args:
            service_types: List of types to resolve

        Returns:
            Dict mapping types to instances
        """
        results: Dict[Type, Any] = {}

        # Create resolution tasks
        async def resolve_one(svc_type: Type) -> Tuple[Type, Any]:
            instance = await self.resolve(svc_type)
            return (svc_type, instance)

        tasks = [resolve_one(t) for t in service_types]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for result in completed:
            if isinstance(result, Exception):
                raise result
            svc_type, instance = result
            results[svc_type] = instance

        return results

    async def try_resolve(
        self,
        service_type: Type[T],
    ) -> Tuple[bool, Optional[T]]:
        """
        Try to resolve a service, returning success status.

        Args:
            service_type: The type to resolve

        Returns:
            Tuple of (success, instance or None)
        """
        try:
            instance = await self.resolve(service_type)
            return (True, instance)
        except Exception:
            return (False, None)

    async def _create_instance(self, service_type: Type[T]) -> T:
        """
        Create a new instance of a service.

        Resolves dependencies and calls factory or constructor.
        """
        registration = self._get_registration(service_type)
        if registration is None:
            raise ServiceNotFoundError(service_type)

        start_time = time.time()

        try:
            # Resolve dependencies
            resolved_deps: Dict[str, Any] = {}
            for dep_spec in registration.dependencies:
                dep_instance = await self._resolve_dependency(dep_spec)
                # Use explicit param_name from spec, or derive from type name
                param_name = (
                    dep_spec.param_name
                    if dep_spec.param_name is not None
                    else self._get_param_name(dep_spec.service_type)
                )
                resolved_deps[param_name] = dep_instance

            # Create instance
            if registration.factory is not None:
                # Use factory
                instance = registration.factory(**resolved_deps)
                if asyncio.iscoroutine(instance):
                    instance = await instance
            else:
                # Use constructor
                instance = service_type(**resolved_deps)

            # Emit event
            duration_ms = (time.time() - start_time) * 1000
            await self._emit_event(
                ServiceEvent.INITIALIZED,
                service_type,
                duration_ms=duration_ms,
            )

            return cast(T, instance)

        except Exception as e:
            self._error_count += 1
            registration.error_count += 1
            registration.last_error = str(e)
            registration.error = e

            await self._emit_event(
                ServiceEvent.FAILED,
                service_type,
                error=e,
            )

            raise DependencyResolutionError(
                service_type,
                reason=str(e),
                cause=e,
            ) from e

    async def _resolve_dependency(
        self,
        dep_spec: DependencySpec,
    ) -> Any:
        """Resolve a single dependency specification."""
        try:
            return await self.resolve(dep_spec.service_type)
        except ServiceNotFoundError:
            if dep_spec.is_optional:
                return dep_spec.default
            raise

    def _get_param_name(self, service_type: Type) -> str:
        """Convert type to snake_case parameter name."""
        name = service_type.__name__
        # Convert CamelCase to snake_case
        result = []
        for i, char in enumerate(name):
            if char.isupper() and i > 0:
                result.append('_')
            result.append(char.lower())
        return ''.join(result)

    # =========================================================================
    # Lifecycle Methods (Transactional Initialization)
    # =========================================================================

    async def initialize_all(self) -> None:
        """
        Initialize all registered services.

        Uses transactional pattern - if any initialization fails,
        all previously initialized services are cleaned up (rollback).

        Raises:
            ServiceInitializationError: If critical service fails
        """
        lock = await self._get_lifecycle_lock()
        async with lock:
            if self._state != ContainerState.CREATED:
                logger.warning(f"Cannot initialize from state {self._state}")
                return

            self._state = ContainerState.INITIALIZING

            # Emit container event
            await self._emit_event(
                ServiceEvent.CONTAINER_INITIALIZED,
                type(self),
            )

            # Build dependency resolver
            self._build_resolver()

            # Get initialization order
            try:
                self._initialization_order = self._resolver.get_initialization_order()
            except CircularDependencyError as e:
                self._state = ContainerState.CREATED
                raise

            # Transactional initialization
            initialized: List[Type] = []

            try:
                for service_type in self._initialization_order:
                    registration = self._get_registration(service_type)

                    if registration is None:
                        continue

                    if not registration.is_enabled:
                        logger.debug(f"Skipping disabled service {service_type.__name__}")
                        continue

                    await self._initialize_service(service_type, registration)
                    initialized.append(service_type)

                self._state = ContainerState.READY
                logger.info(f"Container initialized: {len(initialized)} services")

            except Exception as e:
                # Rollback - cleanup in reverse order
                logger.error(f"Initialization failed: {e}, rolling back...")

                for service_type in reversed(initialized):
                    await self._cleanup_service(service_type)

                self._state = ContainerState.CREATED
                raise

    async def _initialize_service(
        self,
        service_type: Type,
        registration: ServiceRegistration,
    ) -> None:
        """Initialize a single service."""
        registration.init_start_time = time.time()
        registration.state = ServiceState.INITIALIZING

        await self._emit_event(
            ServiceEvent.INITIALIZING,
            service_type,
        )

        try:
            # Resolve (creates instance)
            instance = await self.resolve(service_type)

            # Call initialize if AsyncService
            if is_async_service(instance):
                try:
                    await asyncio.wait_for(
                        instance.initialize(),
                        timeout=registration.initialization_timeout,
                    )
                except asyncio.TimeoutError:
                    raise ServiceTimeoutError(
                        registration.service_name,
                        "initialize",
                        registration.initialization_timeout,
                    )

            registration.init_duration_ms = (
                time.time() - registration.init_start_time
            ) * 1000
            registration.state = ServiceState.INITIALIZED

            await self._emit_event(
                ServiceEvent.INITIALIZED,
                service_type,
                duration_ms=registration.init_duration_ms,
            )

            logger.debug(
                f"Initialized {registration.service_name} "
                f"in {registration.init_duration_ms:.1f}ms"
            )

        except Exception as e:
            registration.state = ServiceState.FAILED
            registration.error = e
            registration.error_count += 1
            registration.last_error = str(e)

            await self._emit_event(
                ServiceEvent.FAILED,
                service_type,
                error=e,
            )

            # Critical services abort startup
            if registration.criticality == ServiceCriticality.CRITICAL:
                raise ServiceInitializationError(
                    f"Critical service {registration.service_name} failed",
                    service_name=registration.service_name,
                    cause=e,
                ) from e

            # Non-critical services log and continue
            logger.error(
                f"Non-critical service {registration.service_name} failed: {e}"
            )

    async def _cleanup_service(self, service_type: Type) -> None:
        """Cleanup a single service during rollback."""
        registration = self._get_registration(service_type)
        if registration is None:
            return

        instance = registration.instance
        if instance is None:
            instance = self._singleton_instances.get(service_type)

        if instance is None:
            return

        try:
            if is_async_service(instance):
                await instance.stop()

            if is_async_disposable(instance):
                await instance.dispose()
            elif is_disposable(instance):
                instance.dispose()

        except Exception as e:
            logger.error(
                f"Error cleaning up {registration.service_name}: {e}"
            )
        finally:
            registration.instance = None
            registration.state = ServiceState.STOPPED
            self._singleton_instances.pop(service_type, None)

    async def start_all(self) -> None:
        """
        Start all initialized services.

        Calls start() on all AsyncService implementations.
        """
        lock = await self._get_lifecycle_lock()
        async with lock:
            if self._state != ContainerState.READY:
                logger.warning(f"Cannot start from state {self._state}")
                return

            self._container_started_at = time.time()

            for service_type in self._initialization_order:
                registration = self._get_registration(service_type)

                if registration is None or not registration.is_enabled:
                    continue

                if registration.state != ServiceState.INITIALIZED:
                    continue

                await self._start_service(service_type, registration)

            # Start health monitoring
            if self._enable_health_monitoring:
                await self._start_health_monitoring()

            logger.info("Container started all services")

    async def _start_service(
        self,
        service_type: Type,
        registration: ServiceRegistration,
    ) -> None:
        """Start a single service."""
        instance = registration.instance or self._singleton_instances.get(service_type)

        if instance is None:
            return

        if not is_async_service(instance):
            registration.state = ServiceState.RUNNING
            registration.started_at = time.time()
            return

        registration.state = ServiceState.STARTING
        start_time = time.time()

        await self._emit_event(
            ServiceEvent.STARTING,
            service_type,
        )

        try:
            await asyncio.wait_for(
                instance.start(),
                timeout=registration.initialization_timeout,
            )

            registration.start_duration_ms = (time.time() - start_time) * 1000
            registration.state = ServiceState.RUNNING
            registration.started_at = time.time()

            await self._emit_event(
                ServiceEvent.STARTED,
                service_type,
                duration_ms=registration.start_duration_ms,
            )

            logger.debug(
                f"Started {registration.service_name} "
                f"in {registration.start_duration_ms:.1f}ms"
            )

        except asyncio.TimeoutError:
            registration.state = ServiceState.FAILED
            registration.error_count += 1
            raise ServiceTimeoutError(
                registration.service_name,
                "start",
                registration.initialization_timeout,
            )

        except Exception as e:
            registration.state = ServiceState.FAILED
            registration.error = e
            registration.error_count += 1
            registration.last_error = str(e)

            await self._emit_event(
                ServiceEvent.FAILED,
                service_type,
                error=e,
            )
            raise

    async def shutdown_all(self) -> None:
        """
        Shutdown all services in reverse order.

        Gracefully stops and disposes all services.
        """
        lock = await self._get_lifecycle_lock()
        async with lock:
            if self._state == ContainerState.SHUTDOWN:
                return

            self._state = ContainerState.SHUTTING_DOWN

            await self._emit_event(
                ServiceEvent.CONTAINER_SHUTDOWN,
                type(self),
            )

            # Stop health monitoring
            await self._stop_health_monitoring()

            # Cancel background tasks
            for task in self._background_tasks:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            self._background_tasks.clear()

            # Dispose active scopes
            for scope_id in list(self._active_scopes.keys()):
                scope = self._active_scopes.pop(scope_id)
                await scope.dispose()

            # Shutdown services in reverse order
            for service_type in reversed(self._initialization_order):
                await self._shutdown_service(service_type)

            # Cleanup executor
            self._executor.shutdown(wait=False)

            self._state = ContainerState.SHUTDOWN
            logger.info("Container shutdown complete")

    async def _shutdown_service(self, service_type: Type) -> None:
        """Shutdown a single service."""
        registration = self._get_registration(service_type)
        if registration is None:
            return

        instance = registration.instance or self._singleton_instances.get(service_type)
        if instance is None:
            return

        registration.state = ServiceState.STOPPING

        await self._emit_event(
            ServiceEvent.STOPPING,
            service_type,
        )

        try:
            if is_async_service(instance):
                try:
                    await asyncio.wait_for(
                        instance.stop(),
                        timeout=DEFAULT_SHUTDOWN_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        f"Timeout stopping {registration.service_name}"
                    )

            if is_async_disposable(instance):
                await instance.dispose()
            elif is_disposable(instance):
                instance.dispose()

            registration.state = ServiceState.STOPPED
            registration.instance = None

            await self._emit_event(
                ServiceEvent.STOPPED,
                service_type,
            )

            await self._emit_event(
                ServiceEvent.DISPOSED,
                service_type,
            )

        except Exception as e:
            logger.error(
                f"Error shutting down {registration.service_name}: {e}"
            )
            registration.state = ServiceState.FAILED
            registration.error = e

        finally:
            self._singleton_instances.pop(service_type, None)

    async def restart_service(self, service_type: Type) -> None:
        """
        Restart a single service.

        Stops, reinitializes, and starts the service.

        Args:
            service_type: The type to restart
        """
        registration = self._get_registration(service_type)
        if registration is None:
            raise ServiceNotFoundError(service_type)

        # Stop the service
        await self._shutdown_service(service_type)

        # Reinitialize
        await self._initialize_service(service_type, registration)

        # Start
        await self._start_service(service_type, registration)

        registration.recovery_count += 1

        await self._emit_event(
            ServiceEvent.RECOVERED,
            service_type,
        )

        logger.info(f"Restarted service {registration.service_name}")

    # =========================================================================
    # Health Monitoring
    # =========================================================================

    async def health_check_all(self) -> Dict[Type, HealthReport]:
        """
        Perform health check on all running services.

        Returns:
            Dict mapping service types to their health reports
        """
        results: Dict[Type, HealthReport] = {}

        for service_type, registration in self._registrations.items():
            if registration.state != ServiceState.RUNNING:
                results[service_type] = HealthReport.unknown(
                    registration.service_name,
                    reason=f"Service in state {registration.state.name}",
                )
                continue

            instance = registration.instance or self._singleton_instances.get(service_type)

            if instance is None:
                results[service_type] = HealthReport.unknown(
                    registration.service_name,
                    reason="No instance available",
                )
                continue

            results[service_type] = await self._check_service_health(
                service_type, registration, instance
            )

        return results

    async def _check_service_health(
        self,
        service_type: Type,
        registration: ServiceRegistration,
        instance: Any,
    ) -> HealthReport:
        """Check health of a single service."""
        registration.health_check_count += 1
        registration.last_health_check = datetime.utcnow()

        if not is_async_service(instance):
            return HealthReport.healthy(
                registration.service_name,
                metadata={"state": registration.state.name},
            )

        start_time = time.time()

        try:
            report = await asyncio.wait_for(
                instance.health_check(),
                timeout=registration.health_check_timeout,
            )

            await self._emit_event(
                ServiceEvent.HEALTH_CHANGED,
                service_type,
                metadata={"status": report.status.name},
            )

            return report

        except asyncio.TimeoutError:
            registration.error_count += 1
            return HealthReport.unhealthy(
                registration.service_name,
                error="Health check timed out",
                latency_ms=(time.time() - start_time) * 1000,
            )

        except Exception as e:
            registration.error_count += 1
            registration.last_error = str(e)
            return HealthReport.unhealthy(
                registration.service_name,
                error=str(e),
                latency_ms=(time.time() - start_time) * 1000,
            )

    async def get_system_health(self) -> HealthReport:
        """
        Get aggregated system health.

        Returns:
            Aggregated HealthReport for the entire container
        """
        service_reports = await self.health_check_all()

        if not service_reports:
            return HealthReport.unknown(
                "ServiceContainer",
                reason="No services registered",
            )

        # Aggregate status
        statuses = [r.status for r in service_reports.values()]

        if all(s == HealthStatus.HEALTHY for s in statuses):
            status = HealthStatus.HEALTHY
        elif any(s == HealthStatus.UNHEALTHY for s in statuses):
            status = HealthStatus.UNHEALTHY
        elif any(s == HealthStatus.DEGRADED for s in statuses):
            status = HealthStatus.DEGRADED
        else:
            status = HealthStatus.UNKNOWN

        # Aggregate checks
        checks = {
            r.service_name: r.is_healthy
            for r in service_reports.values()
        }

        # Aggregate latency
        latencies = [r.latency_ms for r in service_reports.values()]
        total_latency = sum(latencies)

        return HealthReport(
            service_name="ServiceContainer",
            status=status,
            latency_ms=total_latency,
            timestamp=datetime.utcnow(),
            checks=checks,
            metadata={
                "service_count": len(service_reports),
                "healthy_count": sum(1 for r in service_reports.values() if r.is_healthy),
                "container_state": self._state,
                "uptime_seconds": self._get_uptime(),
            },
        )

    async def _start_health_monitoring(self) -> None:
        """Start background health monitoring task."""
        if self._health_monitor_task is not None:
            return

        async def monitor_loop() -> None:
            while self._state == ContainerState.READY:
                await asyncio.sleep(self._health_check_interval)

                if self._state != ContainerState.READY:
                    break

                try:
                    await self.health_check_all()
                except Exception as e:
                    logger.error(f"Health monitoring error: {e}")

        self._health_monitor_task = asyncio.create_task(monitor_loop())
        self._background_tasks.add(self._health_monitor_task)
        logger.debug("Started health monitoring")

    async def _stop_health_monitoring(self) -> None:
        """Stop background health monitoring."""
        if self._health_monitor_task is not None:
            self._health_monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._health_monitor_task
            self._health_monitor_task = None
            logger.debug("Stopped health monitoring")

    # =========================================================================
    # Scoped Containers
    # =========================================================================

    def create_scope(self, scope_id: Optional[str] = None) -> ScopedContainer:
        """
        Create a child container for scoped services.

        Args:
            scope_id: Optional scope identifier (auto-generated if None)

        Returns:
            ScopedContainer for the new scope
        """
        with self._sync_lock:
            self._scope_counter += 1
            scope_id = scope_id or f"scope-{self._scope_counter}"

            scope = ScopedContainer(self, scope_id)
            self._active_scopes[scope_id] = scope

            return scope

    @asynccontextmanager
    async def scope(
        self,
        scope_id: Optional[str] = None,
    ):
        """
        Context manager for scoped resolution.

        Example:
            async with container.scope() as scoped:
                service = await scoped.resolve(RequestService)
        """
        scoped = self.create_scope(scope_id)
        try:
            yield scoped
        finally:
            await scoped.dispose()
            with self._sync_lock:
                self._active_scopes.pop(scoped.scope_id, None)

    # =========================================================================
    # Events Integration
    # =========================================================================

    async def _emit_event(
        self,
        event: ServiceEvent,
        service_type: Type,
        duration_ms: float = 0.0,
        error: Optional[Exception] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Emit a lifecycle event."""
        event_data = (
            EventDataBuilder()
            .with_event(event)
            .with_service(service_type.__name__, service_type)
            .with_duration(duration_ms)
        )

        if error:
            event_data = event_data.with_error(error)

        if metadata:
            event_data = event_data.with_metadata(**metadata)

        try:
            await self._event_emitter.emit_async(event_data.build())
        except Exception as e:
            logger.warning(f"Failed to emit event {event.value}: {e}")

    def on_event(
        self,
        event: ServiceEvent,
        handler: Callable[[EventData], Any],
        service_type: Optional[Type] = None,
    ) -> str:
        """
        Subscribe to lifecycle events.

        Args:
            event: Event type to subscribe to
            handler: Event handler callback
            service_type: Optional filter for specific service type

        Returns:
            Subscription ID for unsubscription
        """
        return self._event_emitter.subscribe(
            event=event,
            handler=handler,
            service_type=service_type,
        )

    def off_event(self, subscription_id: str) -> bool:
        """
        Unsubscribe from lifecycle events.

        Args:
            subscription_id: ID returned from on_event()

        Returns:
            True if subscription was found and removed
        """
        return self._event_emitter.unsubscribe(subscription_id)

    # =========================================================================
    # Metrics & Observability
    # =========================================================================

    def get_metrics(self) -> Dict[Type, ServiceMetrics]:
        """
        Get metrics for all registered services.

        Returns:
            Dict mapping service types to their metrics
        """
        with self._sync_lock:
            return {
                service_type: registration.get_metrics()
                for service_type, registration in self._registrations.items()
            }

    def get_container_metrics(self) -> Dict[str, Any]:
        """
        Get container-level metrics.

        Returns:
            Dict of container metrics
        """
        with self._sync_lock:
            states = {}
            for registration in self._registrations.values():
                state_name = registration.state.name
                states[state_name] = states.get(state_name, 0) + 1

            return {
                "state": self._state,
                "registered_services": len(self._registrations),
                "singleton_instances": len(self._singleton_instances),
                "active_scopes": len(self._active_scopes),
                "resolution_count": self._resolution_count,
                "error_count": self._error_count,
                "uptime_seconds": self._get_uptime(),
                "services_by_state": states,
                "background_tasks": len(self._background_tasks),
            }

    def _get_uptime(self) -> float:
        """Get container uptime in seconds."""
        if self._container_started_at is None:
            return 0.0
        return time.time() - self._container_started_at

    # =========================================================================
    # Dependency Resolution
    # =========================================================================

    def _build_resolver(self) -> None:
        """Build or rebuild the dependency resolver."""
        with self._sync_lock:
            # Convert registrations to ServiceDefinitions
            definitions: List[ServiceDefinition] = []

            for service_type, reg in self._registrations.items():
                defn = ServiceDefinition(
                    interface=service_type,
                    implementation=service_type,
                    dependencies=list(reg.dependencies),
                    factory=reg.factory,
                    tags=set(reg.tags),
                    priority=(
                        reg.criticality.startup_priority
                        if hasattr(reg.criticality, 'startup_priority')
                        else 100
                    ),
                )
                definitions.append(defn)

            self._resolver = DependencyResolver(definitions)

    def get_dependency_graph(self) -> Dict[str, Any]:
        """
        Get the dependency graph as JSON.

        Returns:
            JSON-serializable dependency graph
        """
        if self._resolver is None:
            self._build_resolver()

        return self._resolver.to_json()

    def visualize_dependencies(self, format: str = "mermaid") -> str:
        """
        Get a visualization of the dependency graph.

        Args:
            format: Output format ("mermaid" or "dot")

        Returns:
            Visualization string
        """
        if self._resolver is None:
            self._build_resolver()

        if format == "dot":
            return self._resolver.to_dot()
        return self._resolver.to_mermaid()

    # =========================================================================
    # Advanced Features
    # =========================================================================

    async def run_in_executor(
        self,
        func: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> T:
        """
        Run a blocking function in the thread pool.

        Args:
            func: Blocking function to run
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            lambda: func(*args, **kwargs),
        )

    def get_services_by_tag(self, tag: str) -> List[Type]:
        """
        Get all services with a specific tag.

        Args:
            tag: Tag to filter by

        Returns:
            List of service types with the tag
        """
        with self._sync_lock:
            return [
                service_type
                for service_type, reg in self._registrations.items()
                if tag in reg.tags
            ]

    def get_services_by_criticality(
        self,
        criticality: ServiceCriticality,
    ) -> List[Type]:
        """
        Get all services with a specific criticality.

        Args:
            criticality: Criticality level to filter by

        Returns:
            List of service types with the criticality
        """
        with self._sync_lock:
            return [
                service_type
                for service_type, reg in self._registrations.items()
                if reg.criticality == criticality
            ]

    def get_registered_services(self) -> List[Type]:
        """
        Get all registered service types.

        Returns:
            List of all registered service types
        """
        with self._sync_lock:
            return list(self._registrations.keys())

    # =========================================================================
    # Context Manager Support
    # =========================================================================

    async def __aenter__(self) -> Self:
        """Enter the container context (initializes and starts)."""
        await self.initialize_all()
        await self.start_all()
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit the container context (shuts down)."""
        await self.shutdown_all()

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def __len__(self) -> int:
        """Get number of registered services."""
        return len(self._registrations)

    def __contains__(self, service_type: Type) -> bool:
        """Check if a service type is registered."""
        return self.is_registered(service_type)

    def __repr__(self) -> str:
        return (
            f"ServiceContainer("
            f"state={self._state}, "
            f"services={len(self._registrations)}, "
            f"singletons={len(self._singleton_instances)})"
        )


# =============================================================================
# Factory Functions
# =============================================================================


def create_container(
    event_emitter: Optional[EventEmitter] = None,
    default_timeout: float = DEFAULT_INITIALIZATION_TIMEOUT,
    enable_health_monitoring: bool = True,
) -> ServiceContainer:
    """
    Create a new service container with default configuration.

    Args:
        event_emitter: Optional event emitter
        default_timeout: Default operation timeout
        enable_health_monitoring: Enable background health checks

    Returns:
        Configured ServiceContainer
    """
    return ServiceContainer(
        event_emitter=event_emitter,
        default_timeout=default_timeout,
        enable_health_monitoring=enable_health_monitoring,
    )


async def bootstrap_container(
    registrations: List[Tuple[Type, Dict[str, Any]]],
    auto_start: bool = True,
) -> ServiceContainer:
    """
    Create and bootstrap a container with registrations.

    Args:
        registrations: List of (service_type, kwargs) tuples
        auto_start: Whether to auto-start services

    Returns:
        Initialized ServiceContainer

    Example:
        container = await bootstrap_container([
            (DatabaseService, {"scope": Scope.SINGLETON}),
            (CacheService, {"dependencies": [DependencySpec(DatabaseService)]}),
        ])
    """
    container = create_container()

    for service_type, kwargs in registrations:
        container.register(service_type, **kwargs)

    await container.initialize_all()

    if auto_start:
        await container.start_all()

    return container


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main class
    "ServiceContainer",

    # Supporting classes
    "ServiceRegistration",
    "ScopedContainer",
    "ContainerState",

    # Factory functions
    "create_container",
    "bootstrap_container",
]
