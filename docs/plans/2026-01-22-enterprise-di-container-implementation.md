# Enterprise DI Container Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace manual service instantiation with an enterprise-grade async dependency injection container that fixes the 4 initialization bugs and provides automatic dependency resolution, lifecycle management, and cross-repo coordination.

**Architecture:** Protocol-based service contracts with topological dependency sorting, parallel initialization by dependency level, LIFO shutdown ordering, health monitoring with graceful degradation, and backward-compatible factory function wrappers.

**Tech Stack:** Python 3.9+, asyncio, typing (Protocol, Generic), dataclasses, graphlib (topological sort), weakref (cleanup), structlog (logging)

---

## Task 1: Create DI Module Structure and Protocols

**Files:**
- Create: `backend/core/di/__init__.py`
- Create: `backend/core/di/protocols.py`
- Test: `tests/core/di/test_protocols.py`

**Step 1: Create the di directory**

```bash
mkdir -p backend/core/di
mkdir -p tests/core/di
touch backend/core/di/__init__.py
touch tests/core/di/__init__.py
```

**Step 2: Write the failing test for protocols**

```python
# tests/core/di/test_protocols.py
"""Tests for DI protocol definitions."""

import pytest
from typing import List, Type


class TestAsyncServiceProtocol:
    """Test that AsyncService protocol enforces correct interface."""

    def test_service_state_enum_has_required_states(self):
        """ServiceState must have all lifecycle states."""
        from backend.core.di.protocols import ServiceState

        required_states = [
            "UNREGISTERED", "REGISTERED", "INITIALIZING", "INITIALIZED",
            "STARTING", "RUNNING", "STOPPING", "STOPPED", "FAILED", "DISPOSED"
        ]
        for state in required_states:
            assert hasattr(ServiceState, state), f"Missing state: {state}"

    def test_health_status_enum_has_required_values(self):
        """HealthStatus must have healthy, degraded, unhealthy, unknown."""
        from backend.core.di.protocols import HealthStatus

        assert hasattr(HealthStatus, "HEALTHY")
        assert hasattr(HealthStatus, "DEGRADED")
        assert hasattr(HealthStatus, "UNHEALTHY")
        assert hasattr(HealthStatus, "UNKNOWN")

    def test_service_criticality_enum_exists(self):
        """ServiceCriticality must define CRITICAL, REQUIRED, OPTIONAL."""
        from backend.core.di.protocols import ServiceCriticality

        assert hasattr(ServiceCriticality, "CRITICAL")
        assert hasattr(ServiceCriticality, "REQUIRED")
        assert hasattr(ServiceCriticality, "OPTIONAL")

    def test_scope_enum_has_singleton_transient_scoped(self):
        """Scope must define SINGLETON, TRANSIENT, SCOPED."""
        from backend.core.di.protocols import Scope

        assert hasattr(Scope, "SINGLETON")
        assert hasattr(Scope, "TRANSIENT")
        assert hasattr(Scope, "SCOPED")

    def test_health_report_dataclass_fields(self):
        """HealthReport must have required fields."""
        from backend.core.di.protocols import HealthReport, HealthStatus

        report = HealthReport(
            service_name="TestService",
            status=HealthStatus.HEALTHY,
            latency_ms=10.5,
        )
        assert report.service_name == "TestService"
        assert report.status == HealthStatus.HEALTHY
        assert report.latency_ms == 10.5

    def test_service_definition_dataclass_fields(self):
        """ServiceDefinition must have all required fields."""
        from backend.core.di.protocols import ServiceDefinition, Scope, ServiceCriticality

        class DummyService:
            pass

        definition = ServiceDefinition(
            service_type=DummyService,
            scope=Scope.SINGLETON,
            criticality=ServiceCriticality.OPTIONAL,
        )
        assert definition.service_type == DummyService
        assert definition.scope == Scope.SINGLETON
        assert definition.criticality == ServiceCriticality.OPTIONAL
```

**Step 3: Run test to verify it fails**

Run: `pytest tests/core/di/test_protocols.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'backend.core.di.protocols'"

**Step 4: Write minimal implementation**

```python
# backend/core/di/protocols.py
"""
Dependency Injection Protocols and Types
========================================

Defines the core protocols, enums, and dataclasses for the DI container.
All managed services must implement the AsyncService protocol.

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    List,
    Optional,
    Protocol,
    Set,
    Type,
    TypeVar,
    Union,
    runtime_checkable,
)


# =============================================================================
# ENUMS
# =============================================================================


class ServiceState(Enum):
    """Lifecycle state of a managed service."""

    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    DISPOSED = "disposed"


class HealthStatus(Enum):
    """Health status of a service."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ServiceCriticality(Enum):
    """How critical a service is to system operation."""

    CRITICAL = "critical"      # Fail-fast if unavailable
    REQUIRED = "required"      # Retry with backoff, then degrade
    OPTIONAL = "optional"      # Skip and log if unavailable


class Scope(Enum):
    """Lifecycle scope of a service instance."""

    SINGLETON = "singleton"    # One instance, shared
    TRANSIENT = "transient"    # New instance per resolution
    SCOPED = "scoped"          # One instance per scope (e.g., request)


class DependencyType(Enum):
    """Type of dependency relationship."""

    REQUIRED = "required"      # Must resolve before init
    OPTIONAL = "optional"      # None if unavailable
    LAZY = "lazy"              # Resolved on first access
    FACTORY = "factory"        # Returns factory function


# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class HealthReport:
    """Health check result for a service."""

    service_name: str
    status: HealthStatus
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    checks: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def is_healthy(self) -> bool:
        """Return True if status is HEALTHY."""
        return self.status == HealthStatus.HEALTHY

    def is_operational(self) -> bool:
        """Return True if status is HEALTHY or DEGRADED."""
        return self.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)


@dataclass
class DependencySpec:
    """Specification for a service dependency."""

    service_type: Type
    dependency_type: DependencyType = DependencyType.REQUIRED
    default: Any = None


@dataclass
class ServiceDefinition:
    """Definition for registering a service with the container."""

    service_type: Type
    scope: Scope = Scope.SINGLETON
    criticality: ServiceCriticality = ServiceCriticality.OPTIONAL
    dependencies: List[DependencySpec] = field(default_factory=list)
    factory: Optional[Callable[..., Any]] = None
    enabled_when: Optional[Union[str, Callable[[], bool]]] = None
    tags: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """Convert simple type dependencies to DependencySpec."""
        converted = []
        for dep in self.dependencies:
            if isinstance(dep, type):
                converted.append(DependencySpec(service_type=dep))
            elif isinstance(dep, DependencySpec):
                converted.append(dep)
            else:
                raise TypeError(f"Invalid dependency type: {type(dep)}")
        self.dependencies = converted


@dataclass
class ServiceMetrics:
    """Runtime metrics for a service."""

    service_name: str
    state: ServiceState
    init_duration_ms: float = 0.0
    start_duration_ms: float = 0.0
    uptime_seconds: float = 0.0
    health_check_count: int = 0
    last_health_check: Optional[float] = None
    error_count: int = 0
    recovery_count: int = 0


# =============================================================================
# PROTOCOLS
# =============================================================================


T = TypeVar("T")


@runtime_checkable
class AsyncService(Protocol):
    """
    Protocol for async services managed by the DI container.

    All managed services should implement these lifecycle methods.
    The container calls them in order: initialize -> start -> (running) -> stop
    """

    async def initialize(self) -> None:
        """
        Initialize the service.

        Called once after dependencies are resolved.
        Use for: setting up resources, connections, caches.
        """
        ...

    async def start(self) -> None:
        """
        Start the service.

        Called after initialize succeeds.
        Use for: starting background tasks, listeners, workers.
        """
        ...

    async def stop(self) -> None:
        """
        Stop the service gracefully.

        Called during shutdown.
        Use for: stopping workers, closing connections, flushing buffers.
        """
        ...

    async def health_check(self) -> HealthReport:
        """
        Check service health.

        Called periodically by the container.
        Return a HealthReport with current status.
        """
        ...

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        """
        Return list of dependency types.

        The container uses this for topological sorting.
        Override to declare dependencies.
        """
        ...


class ServiceFactory(Protocol[T]):
    """Protocol for service factory functions."""

    def __call__(self, **dependencies: Any) -> T:
        """Create a service instance with resolved dependencies."""
        ...


# =============================================================================
# BASE CLASSES
# =============================================================================


class BaseAsyncService(ABC):
    """
    Abstract base class providing default AsyncService implementation.

    Extend this class to get sensible defaults for lifecycle methods.
    Override only what you need.
    """

    _state: ServiceState = ServiceState.UNREGISTERED
    _started_at: Optional[float] = None
    _error_count: int = 0

    @property
    def state(self) -> ServiceState:
        """Current service state."""
        return self._state

    @property
    def is_running(self) -> bool:
        """True if service is in RUNNING state."""
        return self._state == ServiceState.RUNNING

    async def initialize(self) -> None:
        """Default initialize (no-op). Override for setup logic."""
        pass

    async def start(self) -> None:
        """Default start (no-op). Override for startup logic."""
        self._started_at = time.time()

    async def stop(self) -> None:
        """Default stop (no-op). Override for shutdown logic."""
        pass

    async def health_check(self) -> HealthReport:
        """Default health check returns HEALTHY if running."""
        status = HealthStatus.HEALTHY if self.is_running else HealthStatus.UNKNOWN
        uptime = time.time() - self._started_at if self._started_at else 0.0

        return HealthReport(
            service_name=self.__class__.__name__,
            status=status,
            metadata={"uptime_seconds": uptime},
        )

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        """Default: no dependencies. Override to declare deps."""
        return []


# =============================================================================
# EXCEPTIONS
# =============================================================================


class DIError(Exception):
    """Base exception for DI container errors."""
    pass


class CircularDependencyError(DIError):
    """Raised when circular dependencies are detected."""

    def __init__(self, cycle: List[Type]):
        self.cycle = cycle
        cycle_str = " -> ".join(t.__name__ for t in cycle)
        super().__init__(
            f"Circular dependency detected: {cycle_str}\n"
            f"Suggestion: Use lazy injection or event-based decoupling"
        )


class ServiceNotFoundError(DIError):
    """Raised when a service is not registered."""

    def __init__(self, service_type: Type):
        self.service_type = service_type
        super().__init__(f"Service not registered: {service_type.__name__}")


class ServiceInitializationError(DIError):
    """Raised when a service fails to initialize."""

    def __init__(self, service_type: Type, cause: Exception):
        self.service_type = service_type
        self.cause = cause
        super().__init__(
            f"Failed to initialize {service_type.__name__}: {cause}"
        )


class DependencyResolutionError(DIError):
    """Raised when a dependency cannot be resolved."""

    def __init__(self, service_type: Type, dependency_type: Type, reason: str):
        self.service_type = service_type
        self.dependency_type = dependency_type
        super().__init__(
            f"Cannot resolve {dependency_type.__name__} for {service_type.__name__}: {reason}"
        )
```

**Step 5: Update __init__.py exports**

```python
# backend/core/di/__init__.py
"""
JARVIS Enterprise Dependency Injection Container
================================================

Provides async-native dependency injection with:
- Automatic dependency resolution
- Topological initialization ordering
- Health monitoring and graceful degradation
- Cross-repo service coordination

Usage:
    from backend.core.di import ServiceContainer, AsyncService, Scope

    container = ServiceContainer()
    container.register(MyService, scope=Scope.SINGLETON)
    await container.initialize_all()

    service = container.resolve(MyService)

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from backend.core.di.protocols import (
    # Enums
    DependencyType,
    HealthStatus,
    Scope,
    ServiceCriticality,
    ServiceState,
    # Data classes
    DependencySpec,
    HealthReport,
    ServiceDefinition,
    ServiceMetrics,
    # Protocols
    AsyncService,
    BaseAsyncService,
    ServiceFactory,
    # Exceptions
    CircularDependencyError,
    DependencyResolutionError,
    DIError,
    ServiceInitializationError,
    ServiceNotFoundError,
)

__all__ = [
    # Enums
    "DependencyType",
    "HealthStatus",
    "Scope",
    "ServiceCriticality",
    "ServiceState",
    # Data classes
    "DependencySpec",
    "HealthReport",
    "ServiceDefinition",
    "ServiceMetrics",
    # Protocols
    "AsyncService",
    "BaseAsyncService",
    "ServiceFactory",
    # Exceptions
    "CircularDependencyError",
    "DependencyResolutionError",
    "DIError",
    "ServiceInitializationError",
    "ServiceNotFoundError",
]
```

**Step 6: Run test to verify it passes**

Run: `pytest tests/core/di/test_protocols.py -v`
Expected: PASS (6 tests)

**Step 7: Commit**

```bash
git add backend/core/di/ tests/core/di/
git commit -m "feat(di): add core protocols, enums, and exceptions for DI container"
```

---

## Task 2: Implement Dependency Resolution with Cycle Detection

**Files:**
- Create: `backend/core/di/resolution.py`
- Test: `tests/core/di/test_resolution.py`

**Step 1: Write the failing test**

```python
# tests/core/di/test_resolution.py
"""Tests for dependency resolution and cycle detection."""

import pytest
from typing import List, Type

from backend.core.di.protocols import (
    BaseAsyncService,
    CircularDependencyError,
    DependencySpec,
    ServiceDefinition,
    Scope,
)


class ConfigA:
    """Simple config with no dependencies."""
    pass


class ServiceA(BaseAsyncService):
    """Service that depends on ConfigA."""

    def __init__(self, config: ConfigA):
        self.config = config

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [ConfigA]


class ServiceB(BaseAsyncService):
    """Service that depends on ServiceA."""

    def __init__(self, service_a: ServiceA):
        self.service_a = service_a

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [ServiceA]


class ServiceC(BaseAsyncService):
    """Service that depends on ServiceB."""

    def __init__(self, service_b: ServiceB):
        self.service_b = service_b

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [ServiceB]


# Circular dependency classes
class CyclicA(BaseAsyncService):
    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [CyclicB]


class CyclicB(BaseAsyncService):
    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [CyclicA]


class CyclicSelf(BaseAsyncService):
    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [CyclicSelf]


class TestDependencyResolver:
    """Test dependency resolution logic."""

    def test_build_dependency_graph(self):
        """Should build correct adjacency list from definitions."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=ConfigA),
            ServiceDefinition(service_type=ServiceA, dependencies=[ConfigA]),
            ServiceDefinition(service_type=ServiceB, dependencies=[ServiceA]),
        ]

        resolver = DependencyResolver(definitions)
        graph = resolver.build_graph()

        # ServiceA depends on ConfigA
        assert ConfigA in graph[ServiceA]
        # ServiceB depends on ServiceA
        assert ServiceA in graph[ServiceB]
        # ConfigA has no dependencies
        assert len(graph[ConfigA]) == 0

    def test_topological_sort_simple(self):
        """Should return correct initialization order."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=ConfigA),
            ServiceDefinition(service_type=ServiceA, dependencies=[ConfigA]),
            ServiceDefinition(service_type=ServiceB, dependencies=[ServiceA]),
        ]

        resolver = DependencyResolver(definitions)
        order = resolver.get_initialization_order()

        # ConfigA must come before ServiceA
        assert order.index(ConfigA) < order.index(ServiceA)
        # ServiceA must come before ServiceB
        assert order.index(ServiceA) < order.index(ServiceB)

    def test_topological_sort_complex(self):
        """Should handle diamond dependencies correctly."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=ConfigA),
            ServiceDefinition(service_type=ServiceA, dependencies=[ConfigA]),
            ServiceDefinition(service_type=ServiceB, dependencies=[ServiceA]),
            ServiceDefinition(service_type=ServiceC, dependencies=[ServiceB]),
        ]

        resolver = DependencyResolver(definitions)
        order = resolver.get_initialization_order()

        # Verify full ordering
        assert order.index(ConfigA) < order.index(ServiceA)
        assert order.index(ServiceA) < order.index(ServiceB)
        assert order.index(ServiceB) < order.index(ServiceC)

    def test_detect_simple_cycle(self):
        """Should detect A -> B -> A cycle."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=CyclicA, dependencies=[CyclicB]),
            ServiceDefinition(service_type=CyclicB, dependencies=[CyclicA]),
        ]

        resolver = DependencyResolver(definitions)

        with pytest.raises(CircularDependencyError) as exc_info:
            resolver.get_initialization_order()

        # Should report the cycle
        assert CyclicA in exc_info.value.cycle or CyclicB in exc_info.value.cycle

    def test_detect_self_cycle(self):
        """Should detect A -> A self-reference."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=CyclicSelf, dependencies=[CyclicSelf]),
        ]

        resolver = DependencyResolver(definitions)

        with pytest.raises(CircularDependencyError) as exc_info:
            resolver.get_initialization_order()

        assert CyclicSelf in exc_info.value.cycle

    def test_get_parallel_groups(self):
        """Should identify services that can initialize in parallel."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=ConfigA),
            ServiceDefinition(service_type=ServiceA, dependencies=[ConfigA]),
            ServiceDefinition(service_type=ServiceB, dependencies=[ServiceA]),
        ]

        resolver = DependencyResolver(definitions)
        groups = resolver.get_parallel_groups()

        # Level 0: ConfigA (no deps)
        assert ConfigA in groups[0]
        # Level 1: ServiceA (depends on ConfigA)
        assert ServiceA in groups[1]
        # Level 2: ServiceB (depends on ServiceA)
        assert ServiceB in groups[2]

    def test_get_shutdown_order(self):
        """Shutdown order should be reverse of init order."""
        from backend.core.di.resolution import DependencyResolver

        definitions = [
            ServiceDefinition(service_type=ConfigA),
            ServiceDefinition(service_type=ServiceA, dependencies=[ConfigA]),
            ServiceDefinition(service_type=ServiceB, dependencies=[ServiceA]),
        ]

        resolver = DependencyResolver(definitions)
        init_order = resolver.get_initialization_order()
        shutdown_order = resolver.get_shutdown_order()

        assert shutdown_order == list(reversed(init_order))
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/di/test_resolution.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'backend.core.di.resolution'"

**Step 3: Write minimal implementation**

```python
# backend/core/di/resolution.py
"""
Dependency Resolution Engine
============================

Provides topological sorting of service dependencies with:
- Cycle detection using Tarjan's algorithm
- Parallel group identification for concurrent init
- Shutdown order computation (reverse of init)

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Set, Type

from backend.core.di.protocols import (
    CircularDependencyError,
    DependencySpec,
    ServiceDefinition,
)


class DependencyResolver:
    """
    Resolves service dependencies and computes initialization order.

    Uses topological sorting to ensure dependencies initialize before
    dependents. Detects circular dependencies and reports them clearly.
    """

    def __init__(self, definitions: List[ServiceDefinition]):
        """
        Initialize resolver with service definitions.

        Args:
            definitions: List of ServiceDefinition objects
        """
        self._definitions = {d.service_type: d for d in definitions}
        self._graph: Dict[Type, Set[Type]] = {}
        self._reverse_graph: Dict[Type, Set[Type]] = {}
        self._built = False

    def build_graph(self) -> Dict[Type, Set[Type]]:
        """
        Build dependency adjacency list.

        Returns:
            Dict mapping service type to set of dependency types
        """
        if self._built:
            return self._graph

        self._graph = defaultdict(set)
        self._reverse_graph = defaultdict(set)

        for service_type, definition in self._definitions.items():
            # Ensure service is in graph even if no dependencies
            if service_type not in self._graph:
                self._graph[service_type] = set()

            for dep_spec in definition.dependencies:
                dep_type = (
                    dep_spec.service_type
                    if isinstance(dep_spec, DependencySpec)
                    else dep_spec
                )
                self._graph[service_type].add(dep_type)
                self._reverse_graph[dep_type].add(service_type)

                # Ensure dependency is in graph
                if dep_type not in self._graph:
                    self._graph[dep_type] = set()

        self._built = True
        return dict(self._graph)

    def detect_cycles(self) -> List[List[Type]]:
        """
        Detect all cycles in the dependency graph.

        Uses Tarjan's algorithm to find strongly connected components.

        Returns:
            List of cycles (each cycle is a list of types)
        """
        self.build_graph()

        index_counter = [0]
        stack: List[Type] = []
        lowlinks: Dict[Type, int] = {}
        index: Dict[Type, int] = {}
        on_stack: Set[Type] = set()
        cycles: List[List[Type]] = []

        def strongconnect(v: Type) -> None:
            index[v] = index_counter[0]
            lowlinks[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for w in self._graph.get(v, set()):
                if w not in index:
                    strongconnect(w)
                    lowlinks[v] = min(lowlinks[v], lowlinks[w])
                elif w in on_stack:
                    lowlinks[v] = min(lowlinks[v], index[w])

            # If v is a root node, pop the SCC
            if lowlinks[v] == index[v]:
                scc: List[Type] = []
                while True:
                    w = stack.pop()
                    on_stack.remove(w)
                    scc.append(w)
                    if w == v:
                        break
                # Only report SCCs with size > 1 or self-loops
                if len(scc) > 1 or (len(scc) == 1 and scc[0] in self._graph.get(scc[0], set())):
                    cycles.append(scc)

        for v in self._graph:
            if v not in index:
                strongconnect(v)

        return cycles

    def get_initialization_order(self) -> List[Type]:
        """
        Get topologically sorted initialization order.

        Returns:
            List of service types in initialization order

        Raises:
            CircularDependencyError: If cycles are detected
        """
        # First check for cycles
        cycles = self.detect_cycles()
        if cycles:
            # Report the first cycle found
            raise CircularDependencyError(cycles[0])

        self.build_graph()

        # Kahn's algorithm for stable topological sort
        in_degree: Dict[Type, int] = {t: 0 for t in self._graph}
        for service_type in self._graph:
            for dep in self._graph[service_type]:
                if dep in in_degree:
                    in_degree[dep] = in_degree.get(dep, 0)

        # Count incoming edges
        for service_type, deps in self._graph.items():
            for dep in deps:
                if service_type in in_degree:
                    pass  # dep -> service_type edge

        # Recompute using reverse graph
        in_degree = {t: 0 for t in self._graph}
        for service_type, dependents in self._reverse_graph.items():
            for dependent in dependents:
                if dependent in in_degree:
                    in_degree[dependent] += 1

        # Start with nodes that have no dependencies
        queue = [t for t, degree in in_degree.items() if degree == 0]
        queue.sort(key=lambda t: t.__name__)  # Stable ordering

        result: List[Type] = []
        while queue:
            current = queue.pop(0)
            result.append(current)

            # Reduce in-degree of dependents
            for dependent in self._reverse_graph.get(current, set()):
                if dependent in in_degree:
                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        queue.append(dependent)
                        queue.sort(key=lambda t: t.__name__)

        return result

    def get_shutdown_order(self) -> List[Type]:
        """
        Get shutdown order (reverse of initialization order).

        Returns:
            List of service types in shutdown order
        """
        return list(reversed(self.get_initialization_order()))

    def get_parallel_groups(self) -> List[Set[Type]]:
        """
        Get groups of services that can initialize in parallel.

        Services in the same group have no dependencies on each other.

        Returns:
            List of sets, where each set is a parallel group
        """
        cycles = self.detect_cycles()
        if cycles:
            raise CircularDependencyError(cycles[0])

        self.build_graph()

        # Calculate level for each service (longest path from root)
        levels: Dict[Type, int] = {}

        def get_level(service_type: Type) -> int:
            if service_type in levels:
                return levels[service_type]

            deps = self._graph.get(service_type, set())
            if not deps:
                levels[service_type] = 0
            else:
                max_dep_level = max(
                    get_level(dep) for dep in deps if dep in self._graph
                )
                levels[service_type] = max_dep_level + 1

            return levels[service_type]

        for service_type in self._graph:
            get_level(service_type)

        # Group by level
        groups: Dict[int, Set[Type]] = defaultdict(set)
        for service_type, level in levels.items():
            groups[level].add(service_type)

        # Return ordered list of groups
        max_level = max(levels.values()) if levels else -1
        return [groups[i] for i in range(max_level + 1)]

    def get_dependents(self, service_type: Type) -> Set[Type]:
        """
        Get all services that depend on the given service.

        Args:
            service_type: The service to find dependents of

        Returns:
            Set of service types that depend on this service
        """
        self.build_graph()
        return self._reverse_graph.get(service_type, set()).copy()

    def get_dependencies(self, service_type: Type) -> Set[Type]:
        """
        Get all services that the given service depends on.

        Args:
            service_type: The service to find dependencies of

        Returns:
            Set of service types this service depends on
        """
        self.build_graph()
        return self._graph.get(service_type, set()).copy()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/di/test_resolution.py -v`
Expected: PASS (7 tests)

**Step 5: Update __init__.py**

```python
# Add to backend/core/di/__init__.py
from backend.core.di.resolution import DependencyResolver

# Add to __all__
__all__ = [
    # ... existing exports ...
    "DependencyResolver",
]
```

**Step 6: Commit**

```bash
git add backend/core/di/resolution.py tests/core/di/test_resolution.py backend/core/di/__init__.py
git commit -m "feat(di): add dependency resolver with cycle detection and parallel groups"
```

---

## Task 3: Implement Service Container Core

**Files:**
- Create: `backend/core/di/container.py`
- Test: `tests/core/di/test_container.py`

**Step 1: Write the failing test**

```python
# tests/core/di/test_container.py
"""Tests for the main ServiceContainer."""

import pytest
from typing import List, Type

from backend.core.di.protocols import (
    BaseAsyncService,
    HealthReport,
    HealthStatus,
    Scope,
    ServiceCriticality,
    ServiceDefinition,
    ServiceNotFoundError,
)


class SimpleConfig:
    """Simple config class."""

    def __init__(self):
        self.value = "test"


class SimpleService(BaseAsyncService):
    """Service with a config dependency."""

    def __init__(self, config: SimpleConfig):
        super().__init__()
        self.config = config
        self.initialized = False
        self.started = False

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [SimpleConfig]

    async def initialize(self) -> None:
        self.initialized = True

    async def start(self) -> None:
        await super().start()
        self.started = True


class DependentService(BaseAsyncService):
    """Service that depends on SimpleService."""

    def __init__(self, simple: SimpleService):
        super().__init__()
        self.simple = simple

    @classmethod
    def get_dependencies(cls) -> List[Type]:
        return [SimpleService]


class TestServiceContainer:
    """Test ServiceContainer functionality."""

    @pytest.mark.asyncio
    async def test_register_and_resolve_singleton(self):
        """Should register and resolve a singleton service."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig, scope=Scope.SINGLETON)

        instance1 = container.resolve(SimpleConfig)
        instance2 = container.resolve(SimpleConfig)

        assert instance1 is instance2  # Same instance

    @pytest.mark.asyncio
    async def test_register_and_resolve_transient(self):
        """Should create new instance for transient scope."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig, scope=Scope.TRANSIENT)

        instance1 = container.resolve(SimpleConfig)
        instance2 = container.resolve(SimpleConfig)

        assert instance1 is not instance2  # Different instances

    @pytest.mark.asyncio
    async def test_resolve_with_dependencies(self):
        """Should resolve dependencies automatically."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig, scope=Scope.SINGLETON)
        container.register(
            SimpleService,
            scope=Scope.SINGLETON,
            dependencies=[SimpleConfig],
        )

        service = container.resolve(SimpleService)

        assert service.config is not None
        assert isinstance(service.config, SimpleConfig)

    @pytest.mark.asyncio
    async def test_resolve_not_registered_raises(self):
        """Should raise ServiceNotFoundError for unregistered service."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()

        with pytest.raises(ServiceNotFoundError):
            container.resolve(SimpleConfig)

    @pytest.mark.asyncio
    async def test_initialize_all(self):
        """Should initialize all services in correct order."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig, scope=Scope.SINGLETON)
        container.register(
            SimpleService,
            scope=Scope.SINGLETON,
            dependencies=[SimpleConfig],
        )

        await container.initialize_all()

        service = container.resolve(SimpleService)
        assert service.initialized is True

    @pytest.mark.asyncio
    async def test_start_all(self):
        """Should start all services after initialization."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig, scope=Scope.SINGLETON)
        container.register(
            SimpleService,
            scope=Scope.SINGLETON,
            dependencies=[SimpleConfig],
        )

        await container.initialize_all()
        await container.start_all()

        service = container.resolve(SimpleService)
        assert service.started is True

    @pytest.mark.asyncio
    async def test_shutdown_reverse_order(self):
        """Should shutdown services in reverse initialization order."""
        from backend.core.di.container import ServiceContainer

        shutdown_order = []

        class TrackingService(BaseAsyncService):
            def __init__(self, name: str):
                super().__init__()
                self.name = name

            async def stop(self) -> None:
                shutdown_order.append(self.name)

        container = ServiceContainer()

        # Register with factory to track shutdown
        container.register(
            SimpleConfig,
            scope=Scope.SINGLETON,
            factory=lambda: TrackingService("config"),
        )
        container.register(
            SimpleService,
            scope=Scope.SINGLETON,
            dependencies=[SimpleConfig],
            factory=lambda config: TrackingService("service"),
        )

        await container.initialize_all()
        await container.shutdown_all()

        # Service should shutdown before config (reverse order)
        assert shutdown_order == ["service", "config"]

    @pytest.mark.asyncio
    async def test_is_registered(self):
        """Should report whether a service is registered."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()

        assert container.is_registered(SimpleConfig) is False

        container.register(SimpleConfig)

        assert container.is_registered(SimpleConfig) is True

    @pytest.mark.asyncio
    async def test_get_all_services(self):
        """Should return all registered service types."""
        from backend.core.di.container import ServiceContainer

        container = ServiceContainer()
        container.register(SimpleConfig)
        container.register(SimpleService, dependencies=[SimpleConfig])

        services = container.get_all_services()

        assert SimpleConfig in services
        assert SimpleService in services
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/di/test_container.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'backend.core.di.container'"

**Step 3: Write minimal implementation**

```python
# backend/core/di/container.py
"""
Service Container
=================

The main DI container that manages service registration, resolution,
and lifecycle. Provides async-native dependency injection with:

- Automatic dependency resolution
- Singleton, transient, and scoped lifetimes
- Parallel initialization by dependency level
- Graceful shutdown in reverse order
- Health monitoring integration

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
    TypeVar,
    Union,
)

from backend.core.di.protocols import (
    AsyncService,
    BaseAsyncService,
    DependencySpec,
    DependencyType,
    HealthReport,
    HealthStatus,
    Scope,
    ServiceCriticality,
    ServiceDefinition,
    ServiceMetrics,
    ServiceNotFoundError,
    ServiceState,
    ServiceInitializationError,
)
from backend.core.di.resolution import DependencyResolver

logger = logging.getLogger("jarvis.di.container")

T = TypeVar("T")


class ServiceInstance:
    """Wrapper for a service instance with metadata."""

    def __init__(
        self,
        service_type: Type,
        instance: Any,
        definition: ServiceDefinition,
    ):
        self.service_type = service_type
        self.instance = instance
        self.definition = definition
        self.state = ServiceState.REGISTERED
        self.created_at = time.time()
        self.initialized_at: Optional[float] = None
        self.started_at: Optional[float] = None
        self.error: Optional[Exception] = None


class ServiceContainer:
    """
    Async-native dependency injection container.

    Manages service registration, dependency resolution, and lifecycle.

    Usage:
        container = ServiceContainer()
        container.register(ConfigService, scope=Scope.SINGLETON)
        container.register(MyService, dependencies=[ConfigService])

        await container.initialize_all()
        await container.start_all()

        service = container.resolve(MyService)

        await container.shutdown_all()
    """

    def __init__(self, name: str = "default"):
        """
        Initialize the container.

        Args:
            name: Container name for logging
        """
        self._name = name
        self._definitions: Dict[Type, ServiceDefinition] = {}
        self._instances: Dict[Type, ServiceInstance] = {}
        self._singletons: Dict[Type, Any] = {}
        self._initialization_order: List[Type] = []
        self._is_initialized = False
        self._is_started = False
        self._is_shutdown = False
        self._lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        """True if container has been initialized."""
        return self._is_initialized

    @property
    def is_started(self) -> bool:
        """True if container has been started."""
        return self._is_started

    def register(
        self,
        service_type: Type[T],
        *,
        scope: Scope = Scope.SINGLETON,
        criticality: ServiceCriticality = ServiceCriticality.OPTIONAL,
        dependencies: Optional[List[Union[Type, DependencySpec]]] = None,
        factory: Optional[Callable[..., T]] = None,
        enabled_when: Optional[Union[str, Callable[[], bool]]] = None,
        tags: Optional[Set[str]] = None,
    ) -> "ServiceContainer":
        """
        Register a service with the container.

        Args:
            service_type: The service class to register
            scope: Lifetime scope (SINGLETON, TRANSIENT, SCOPED)
            criticality: How critical this service is
            dependencies: List of dependency types
            factory: Optional factory function
            enabled_when: Condition for enabling (env var or callable)
            tags: Optional tags for filtering

        Returns:
            Self for method chaining
        """
        # Check enabled_when condition
        if enabled_when is not None:
            if isinstance(enabled_when, str):
                import os
                if os.getenv(enabled_when, "true").lower() != "true":
                    logger.debug(f"Skipping {service_type.__name__}: {enabled_when} is not true")
                    return self
            elif callable(enabled_when):
                if not enabled_when():
                    logger.debug(f"Skipping {service_type.__name__}: enabled_when returned False")
                    return self

        definition = ServiceDefinition(
            service_type=service_type,
            scope=scope,
            criticality=criticality,
            dependencies=dependencies or [],
            factory=factory,
            enabled_when=enabled_when,
            tags=tags or set(),
        )

        self._definitions[service_type] = definition
        logger.debug(f"Registered {service_type.__name__} with scope={scope.value}")

        return self

    def register_instance(
        self,
        service_type: Type[T],
        instance: T,
    ) -> "ServiceContainer":
        """
        Register an existing instance as a singleton.

        Args:
            service_type: The service type
            instance: The instance to register

        Returns:
            Self for method chaining
        """
        self._singletons[service_type] = instance
        self._definitions[service_type] = ServiceDefinition(
            service_type=service_type,
            scope=Scope.SINGLETON,
        )
        return self

    def is_registered(self, service_type: Type) -> bool:
        """Check if a service type is registered."""
        return service_type in self._definitions

    def get_all_services(self) -> List[Type]:
        """Get all registered service types."""
        return list(self._definitions.keys())

    def resolve(self, service_type: Type[T]) -> T:
        """
        Resolve a service instance.

        Args:
            service_type: The service type to resolve

        Returns:
            The service instance

        Raises:
            ServiceNotFoundError: If service is not registered
        """
        if service_type not in self._definitions:
            raise ServiceNotFoundError(service_type)

        definition = self._definitions[service_type]

        # Check for existing singleton
        if definition.scope == Scope.SINGLETON:
            if service_type in self._singletons:
                return self._singletons[service_type]

        # Create new instance
        instance = self._create_instance(service_type, definition)

        # Store singleton
        if definition.scope == Scope.SINGLETON:
            self._singletons[service_type] = instance

        return instance

    def _create_instance(
        self,
        service_type: Type[T],
        definition: ServiceDefinition,
    ) -> T:
        """Create a new service instance with resolved dependencies."""
        # Resolve dependencies
        resolved_deps: Dict[str, Any] = {}
        dep_list: List[Any] = []

        for dep_spec in definition.dependencies:
            if isinstance(dep_spec, DependencySpec):
                dep_type = dep_spec.service_type
                dep_kind = dep_spec.dependency_type
            else:
                dep_type = dep_spec
                dep_kind = DependencyType.REQUIRED

            if dep_kind == DependencyType.OPTIONAL:
                if self.is_registered(dep_type):
                    dep_list.append(self.resolve(dep_type))
                else:
                    dep_list.append(None)
            else:
                dep_list.append(self.resolve(dep_type))

        # Use factory if provided
        if definition.factory is not None:
            return definition.factory(*dep_list)

        # Otherwise, instantiate directly
        return service_type(*dep_list)

    async def initialize_all(self) -> None:
        """
        Initialize all registered services in dependency order.

        Services are initialized in topological order to ensure
        dependencies are ready before dependents.
        """
        async with self._lock:
            if self._is_initialized:
                logger.warning("Container already initialized")
                return

            # Build dependency resolver
            resolver = DependencyResolver(list(self._definitions.values()))
            self._initialization_order = resolver.get_initialization_order()
            parallel_groups = resolver.get_parallel_groups()

            logger.info(f"Initializing {len(self._initialization_order)} services")

            # Initialize by parallel groups
            for level, group in enumerate(parallel_groups):
                logger.debug(f"Initializing level {level}: {[t.__name__ for t in group]}")

                # Resolve all services in this group
                for service_type in group:
                    if service_type in self._definitions:
                        try:
                            instance = self.resolve(service_type)

                            # Call initialize if it's an AsyncService
                            if hasattr(instance, "initialize"):
                                await instance.initialize()
                                logger.debug(f"Initialized {service_type.__name__}")
                        except Exception as e:
                            definition = self._definitions[service_type]
                            if definition.criticality == ServiceCriticality.CRITICAL:
                                raise ServiceInitializationError(service_type, e)
                            elif definition.criticality == ServiceCriticality.REQUIRED:
                                logger.warning(f"Failed to initialize {service_type.__name__}: {e}")
                            else:
                                logger.info(f"Optional service {service_type.__name__} unavailable: {e}")

            self._is_initialized = True
            logger.info("Container initialization complete")

    async def start_all(self) -> None:
        """Start all initialized services."""
        async with self._lock:
            if not self._is_initialized:
                raise RuntimeError("Container must be initialized before starting")

            if self._is_started:
                logger.warning("Container already started")
                return

            logger.info("Starting all services")

            for service_type in self._initialization_order:
                if service_type in self._singletons:
                    instance = self._singletons[service_type]
                    if hasattr(instance, "start"):
                        try:
                            await instance.start()
                            logger.debug(f"Started {service_type.__name__}")
                        except Exception as e:
                            logger.warning(f"Failed to start {service_type.__name__}: {e}")

            self._is_started = True
            logger.info("All services started")

    async def shutdown_all(self) -> None:
        """Shutdown all services in reverse initialization order."""
        async with self._lock:
            if self._is_shutdown:
                logger.warning("Container already shutdown")
                return

            logger.info("Shutting down all services")

            # Reverse order
            shutdown_order = list(reversed(self._initialization_order))

            for service_type in shutdown_order:
                if service_type in self._singletons:
                    instance = self._singletons[service_type]
                    if hasattr(instance, "stop"):
                        try:
                            await instance.stop()
                            logger.debug(f"Stopped {service_type.__name__}")
                        except Exception as e:
                            logger.warning(f"Error stopping {service_type.__name__}: {e}")

            self._singletons.clear()
            self._is_shutdown = True
            self._is_started = False
            self._is_initialized = False
            logger.info("Container shutdown complete")

    async def health_check_all(self) -> Dict[Type, HealthReport]:
        """
        Run health checks on all services.

        Returns:
            Dict mapping service type to health report
        """
        reports: Dict[Type, HealthReport] = {}

        for service_type, instance in self._singletons.items():
            if hasattr(instance, "health_check"):
                try:
                    report = await instance.health_check()
                    reports[service_type] = report
                except Exception as e:
                    reports[service_type] = HealthReport(
                        service_name=service_type.__name__,
                        status=HealthStatus.UNHEALTHY,
                        error=str(e),
                    )
            else:
                reports[service_type] = HealthReport(
                    service_name=service_type.__name__,
                    status=HealthStatus.UNKNOWN,
                )

        return reports

    @asynccontextmanager
    async def scope(self, name: str = "request"):
        """
        Create a scoped container for request-scoped services.

        Usage:
            async with container.scope("request") as scoped:
                service = scoped.resolve(RequestService)
        """
        scoped = ScopedContainer(self, name)
        try:
            yield scoped
        finally:
            await scoped.cleanup()


class ScopedContainer:
    """Container for scoped service instances."""

    def __init__(self, parent: ServiceContainer, scope_name: str):
        self._parent = parent
        self._scope_name = scope_name
        self._instances: Dict[Type, Any] = {}

    def resolve(self, service_type: Type[T]) -> T:
        """Resolve a service, using scoped instance if available."""
        if service_type in self._instances:
            return self._instances[service_type]

        definition = self._parent._definitions.get(service_type)
        if definition and definition.scope == Scope.SCOPED:
            instance = self._parent._create_instance(service_type, definition)
            self._instances[service_type] = instance
            return instance

        return self._parent.resolve(service_type)

    async def cleanup(self) -> None:
        """Cleanup scoped instances."""
        for instance in self._instances.values():
            if hasattr(instance, "stop"):
                try:
                    await instance.stop()
                except Exception as e:
                    logger.warning(f"Error cleaning up scoped instance: {e}")
        self._instances.clear()


# Global container instance
_global_container: Optional[ServiceContainer] = None


def get_container() -> ServiceContainer:
    """Get the global service container."""
    global _global_container
    if _global_container is None:
        _global_container = ServiceContainer("global")
    return _global_container


def set_container(container: ServiceContainer) -> None:
    """Set the global service container."""
    global _global_container
    _global_container = container
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/di/test_container.py -v`
Expected: PASS (9 tests)

**Step 5: Update __init__.py**

```python
# Add to backend/core/di/__init__.py
from backend.core.di.container import (
    ServiceContainer,
    ScopedContainer,
    get_container,
    set_container,
)

# Add to __all__
__all__ = [
    # ... existing exports ...
    "ServiceContainer",
    "ScopedContainer",
    "get_container",
    "set_container",
]
```

**Step 6: Commit**

```bash
git add backend/core/di/container.py tests/core/di/test_container.py backend/core/di/__init__.py
git commit -m "feat(di): add ServiceContainer with registration, resolution, and lifecycle management"
```

---

## Task 4: Implement Lifecycle Events

**Files:**
- Create: `backend/core/di/events.py`
- Test: `tests/core/di/test_events.py`

**Step 1: Write the failing test**

```python
# tests/core/di/test_events.py
"""Tests for lifecycle event system."""

import pytest
from typing import List

from backend.core.di.protocols import ServiceState


class TestEventEmitter:
    """Test EventEmitter functionality."""

    def test_subscribe_and_emit(self):
        """Should notify subscribers when event is emitted."""
        from backend.core.di.events import EventEmitter, ServiceEvent

        emitter = EventEmitter()
        received = []

        def handler(event):
            received.append(event)

        emitter.subscribe(ServiceEvent.INITIALIZED, handler)
        emitter.emit(ServiceEvent.INITIALIZED, {"service": "TestService"})

        assert len(received) == 1
        assert received[0]["service"] == "TestService"

    def test_subscribe_to_specific_service(self):
        """Should filter events by service type."""
        from backend.core.di.events import EventEmitter, ServiceEvent

        emitter = EventEmitter()
        received = []

        class ServiceA:
            pass

        class ServiceB:
            pass

        def handler(event):
            received.append(event)

        emitter.subscribe(ServiceEvent.INITIALIZED, handler, service_type=ServiceA)

        emitter.emit(ServiceEvent.INITIALIZED, {"service_type": ServiceB})
        assert len(received) == 0

        emitter.emit(ServiceEvent.INITIALIZED, {"service_type": ServiceA})
        assert len(received) == 1

    def test_unsubscribe(self):
        """Should stop receiving events after unsubscribe."""
        from backend.core.di.events import EventEmitter, ServiceEvent

        emitter = EventEmitter()
        received = []

        def handler(event):
            received.append(event)

        sub_id = emitter.subscribe(ServiceEvent.INITIALIZED, handler)
        emitter.emit(ServiceEvent.INITIALIZED, {})
        assert len(received) == 1

        emitter.unsubscribe(sub_id)
        emitter.emit(ServiceEvent.INITIALIZED, {})
        assert len(received) == 1  # No new events

    def test_subscribe_to_all_events(self):
        """Should receive all event types with global subscription."""
        from backend.core.di.events import EventEmitter, ServiceEvent

        emitter = EventEmitter()
        received = []

        def handler(event):
            received.append(event)

        emitter.subscribe_all(handler)

        emitter.emit(ServiceEvent.INITIALIZED, {"event": 1})
        emitter.emit(ServiceEvent.STARTED, {"event": 2})
        emitter.emit(ServiceEvent.FAILED, {"event": 3})

        assert len(received) == 3

    @pytest.mark.asyncio
    async def test_async_handler(self):
        """Should support async event handlers."""
        from backend.core.di.events import EventEmitter, ServiceEvent

        emitter = EventEmitter()
        received = []

        async def async_handler(event):
            received.append(event)

        emitter.subscribe(ServiceEvent.INITIALIZED, async_handler)
        await emitter.emit_async(ServiceEvent.INITIALIZED, {"async": True})

        assert len(received) == 1
        assert received[0]["async"] is True
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/di/test_events.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write minimal implementation**

```python
# backend/core/di/events.py
"""
Lifecycle Event System
======================

Provides event emission and subscription for service lifecycle events.
Supports both sync and async handlers with service-type filtering.

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Set,
    Type,
    Union,
)

logger = logging.getLogger("jarvis.di.events")


class ServiceEvent(Enum):
    """Lifecycle events emitted by the container."""

    REGISTERED = "registered"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    STARTING = "starting"
    STARTED = "started"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    RECOVERED = "recovered"
    HEALTH_CHANGED = "health_changed"
    DISPOSED = "disposed"


@dataclass
class EventData:
    """Data payload for a lifecycle event."""

    event: ServiceEvent
    service_type: Optional[Type] = None
    service_name: str = ""
    timestamp: float = 0.0
    duration_ms: float = 0.0
    error: Optional[Exception] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


Handler = Callable[[Dict[str, Any]], Any]
AsyncHandler = Callable[[Dict[str, Any]], Any]


@dataclass
class Subscription:
    """A registered event subscription."""

    id: str
    event: Optional[ServiceEvent]  # None means all events
    handler: Union[Handler, AsyncHandler]
    service_type: Optional[Type] = None
    is_async: bool = False


class EventEmitter:
    """
    Event emitter for service lifecycle events.

    Supports:
    - Subscribing to specific events or all events
    - Filtering by service type
    - Sync and async handlers
    - Unsubscription by ID
    """

    def __init__(self):
        self._subscriptions: Dict[str, Subscription] = {}
        self._by_event: Dict[ServiceEvent, Set[str]] = {}
        self._global_subs: Set[str] = set()

    def subscribe(
        self,
        event: ServiceEvent,
        handler: Union[Handler, AsyncHandler],
        service_type: Optional[Type] = None,
    ) -> str:
        """
        Subscribe to a specific event.

        Args:
            event: The event type to subscribe to
            handler: Callback function (sync or async)
            service_type: Optional filter by service type

        Returns:
            Subscription ID for unsubscribing
        """
        sub_id = str(uuid.uuid4())
        is_async = asyncio.iscoroutinefunction(handler)

        subscription = Subscription(
            id=sub_id,
            event=event,
            handler=handler,
            service_type=service_type,
            is_async=is_async,
        )

        self._subscriptions[sub_id] = subscription

        if event not in self._by_event:
            self._by_event[event] = set()
        self._by_event[event].add(sub_id)

        return sub_id

    def subscribe_all(
        self,
        handler: Union[Handler, AsyncHandler],
        service_type: Optional[Type] = None,
    ) -> str:
        """
        Subscribe to all events.

        Args:
            handler: Callback function (sync or async)
            service_type: Optional filter by service type

        Returns:
            Subscription ID for unsubscribing
        """
        sub_id = str(uuid.uuid4())
        is_async = asyncio.iscoroutinefunction(handler)

        subscription = Subscription(
            id=sub_id,
            event=None,  # None means all events
            handler=handler,
            service_type=service_type,
            is_async=is_async,
        )

        self._subscriptions[sub_id] = subscription
        self._global_subs.add(sub_id)

        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """
        Unsubscribe from events.

        Args:
            subscription_id: The subscription ID to remove

        Returns:
            True if unsubscribed, False if not found
        """
        if subscription_id not in self._subscriptions:
            return False

        sub = self._subscriptions.pop(subscription_id)

        if sub.event is not None and sub.event in self._by_event:
            self._by_event[sub.event].discard(subscription_id)

        self._global_subs.discard(subscription_id)

        return True

    def emit(
        self,
        event: ServiceEvent,
        data: Dict[str, Any],
    ) -> None:
        """
        Emit an event synchronously.

        Args:
            event: The event type
            data: Event data payload
        """
        data["event"] = event

        # Get relevant subscriptions
        sub_ids = set()
        if event in self._by_event:
            sub_ids.update(self._by_event[event])
        sub_ids.update(self._global_subs)

        for sub_id in sub_ids:
            sub = self._subscriptions.get(sub_id)
            if sub is None:
                continue

            # Filter by service type if specified
            if sub.service_type is not None:
                event_service = data.get("service_type")
                if event_service != sub.service_type:
                    continue

            try:
                if sub.is_async:
                    # Schedule async handler
                    asyncio.create_task(sub.handler(data))
                else:
                    sub.handler(data)
            except Exception as e:
                logger.error(f"Error in event handler: {e}")

    async def emit_async(
        self,
        event: ServiceEvent,
        data: Dict[str, Any],
    ) -> None:
        """
        Emit an event and await all async handlers.

        Args:
            event: The event type
            data: Event data payload
        """
        data["event"] = event

        # Get relevant subscriptions
        sub_ids = set()
        if event in self._by_event:
            sub_ids.update(self._by_event[event])
        sub_ids.update(self._global_subs)

        tasks = []
        for sub_id in sub_ids:
            sub = self._subscriptions.get(sub_id)
            if sub is None:
                continue

            # Filter by service type if specified
            if sub.service_type is not None:
                event_service = data.get("service_type")
                if event_service != sub.service_type:
                    continue

            try:
                if sub.is_async:
                    tasks.append(sub.handler(data))
                else:
                    sub.handler(data)
            except Exception as e:
                logger.error(f"Error in event handler: {e}")

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def clear(self) -> None:
        """Clear all subscriptions."""
        self._subscriptions.clear()
        self._by_event.clear()
        self._global_subs.clear()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/di/test_events.py -v`
Expected: PASS (5 tests)

**Step 5: Update __init__.py**

```python
# Add to backend/core/di/__init__.py
from backend.core.di.events import (
    EventEmitter,
    ServiceEvent,
    EventData,
    Subscription,
)

# Add to __all__
__all__ = [
    # ... existing exports ...
    "EventEmitter",
    "ServiceEvent",
    "EventData",
    "Subscription",
]
```

**Step 6: Commit**

```bash
git add backend/core/di/events.py tests/core/di/test_events.py backend/core/di/__init__.py
git commit -m "feat(di): add lifecycle event system with sync/async handlers"
```

---

## Task 5: Fix run_supervisor.py Integration

**Files:**
- Modify: `run_supervisor.py` (lines 12620-12820)
- Test: Manual integration test

**Step 1: Read the current buggy section**

Read `run_supervisor.py` lines 12620-12820 to understand the current structure.

**Step 2: Create the service registration module**

```python
# backend/core/di/intelligence_services.py
"""
Intelligence Services Registration
===================================

Registers all intelligence layer services with the DI container.
This replaces the manual instantiation in run_supervisor.py.

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from backend.core.di import (
    ServiceContainer,
    Scope,
    ServiceCriticality,
    DependencySpec,
    DependencyType,
)

logger = logging.getLogger("jarvis.di.intelligence")


def register_intelligence_services(container: ServiceContainer) -> None:
    """
    Register all intelligence layer services with the container.

    This function replaces the manual service instantiation in
    run_supervisor.py lines 12620-12820.

    Args:
        container: The service container to register services with
    """
    # =========================================================================
    # COLLABORATION ENGINE
    # =========================================================================

    if os.getenv("JARVIS_COLLABORATION_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.collaboration_engine import (
                CollaborationConfig,
                CollaborationEngine,
                CrossRepoCollaborationCoordinator,
            )

            # Register config
            container.register(
                CollaborationConfig,
                scope=Scope.SINGLETON,
                factory=CollaborationConfig.from_env,
            )

            # Register engine
            container.register(
                CollaborationEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[CollaborationConfig],
            )

            # Register cross-repo coordinator (if enabled)
            if os.getenv("JARVIS_CROSS_REPO_COLLAB", "true").lower() == "true":
                container.register(
                    CrossRepoCollaborationCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[CollaborationConfig],  # Correct: config, not engine
                )

            logger.info("Registered collaboration services")

        except ImportError as e:
            logger.info(f"Collaboration engine not available: {e}")

    # =========================================================================
    # CODE OWNERSHIP ENGINE
    # =========================================================================

    if os.getenv("JARVIS_CODE_OWNERSHIP_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.code_ownership import (
                OwnershipConfig,
                CodeOwnershipEngine,
                CrossRepoOwnershipCoordinator,
            )

            # Register config
            container.register(
                OwnershipConfig,
                scope=Scope.SINGLETON,
                factory=OwnershipConfig.from_env,
            )

            # Register engine
            container.register(
                CodeOwnershipEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[OwnershipConfig],
            )

            # Register cross-repo coordinator
            if os.getenv("JARVIS_CROSS_REPO_OWNERSHIP", "true").lower() == "true":
                container.register(
                    CrossRepoOwnershipCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[OwnershipConfig],  # Correct: config, not engine
                )

            logger.info("Registered code ownership services")

        except ImportError as e:
            logger.info(f"Code ownership engine not available: {e}")

    # =========================================================================
    # REVIEW WORKFLOW ENGINE
    # =========================================================================

    if os.getenv("JARVIS_REVIEW_WORKFLOW_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.review_workflow import (
                ReviewWorkflowConfig,
                ReviewWorkflowEngine,
                CrossRepoReviewCoordinator,
            )

            # Register config
            container.register(
                ReviewWorkflowConfig,
                scope=Scope.SINGLETON,
                factory=ReviewWorkflowConfig.from_env,
            )

            # Register engine
            container.register(
                ReviewWorkflowEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[ReviewWorkflowConfig],
            )

            # Register cross-repo coordinator
            if os.getenv("JARVIS_CROSS_REPO_REVIEW", "true").lower() == "true":
                container.register(
                    CrossRepoReviewCoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[ReviewWorkflowConfig],  # Correct: config, not engine
                )

            logger.info("Registered review workflow services")

        except ImportError as e:
            logger.info(f"Review workflow engine not available: {e}")

    # =========================================================================
    # LSP SERVER
    # =========================================================================

    if os.getenv("JARVIS_LSP_SERVER_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.lsp_server import (
                LSPServerConfig,
                JARVISLSPServer,
            )

            # Register config
            container.register(
                LSPServerConfig,
                scope=Scope.SINGLETON,
                factory=LSPServerConfig.from_env,
            )

            # Register LSP server
            container.register(
                JARVISLSPServer,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[LSPServerConfig],
            )

            logger.info("Registered LSP server")

        except ImportError as e:
            logger.info(f"LSP server not available: {e}")

    # =========================================================================
    # IDE INTEGRATION ENGINE
    # =========================================================================

    if os.getenv("JARVIS_IDE_INTEGRATION_ENABLED", "true").lower() == "true":
        try:
            from backend.intelligence.ide_integration import (
                IDEIntegrationConfig,
                IDEIntegrationEngine,
                CrossRepoIDECoordinator,
            )

            # Register config
            container.register(
                IDEIntegrationConfig,
                scope=Scope.SINGLETON,
                factory=IDEIntegrationConfig.from_env,
            )

            # Register engine
            container.register(
                IDEIntegrationEngine,
                scope=Scope.SINGLETON,
                criticality=ServiceCriticality.OPTIONAL,
                dependencies=[IDEIntegrationConfig],
            )

            # Register cross-repo coordinator
            if os.getenv("JARVIS_CROSS_REPO_IDE", "true").lower() == "true":
                container.register(
                    CrossRepoIDECoordinator,
                    scope=Scope.SINGLETON,
                    criticality=ServiceCriticality.OPTIONAL,
                    dependencies=[IDEIntegrationConfig],  # Correct: config, not engine
                )

            logger.info("Registered IDE integration services")

        except ImportError as e:
            logger.info(f"IDE integration engine not available: {e}")


async def initialize_intelligence_services(container: ServiceContainer) -> dict:
    """
    Initialize all registered intelligence services.

    Returns:
        Dict of initialization status per service
    """
    await container.initialize_all()

    # Collect status
    status = {}
    for service_type in container.get_all_services():
        try:
            instance = container.resolve(service_type)
            status[service_type.__name__] = "initialized"
        except Exception as e:
            status[service_type.__name__] = f"failed: {e}"

    return status
```

**Step 3: Update run_supervisor.py to use the container**

Replace lines 12620-12820 in run_supervisor.py with:

```python
# =============================================================================
# PHASE 4: INTELLIGENCE SYSTEM INITIALIZATION (Using DI Container)
# =============================================================================

async def _initialize_intelligence_system(self) -> Dict[str, bool]:
    """
    Initialize intelligence layer using the DI container.

    This replaces the manual service instantiation with proper
    dependency injection and lifecycle management.
    """
    from backend.core.di import ServiceContainer, get_container, set_container
    from backend.core.di.intelligence_services import (
        register_intelligence_services,
        initialize_intelligence_services,
    )

    initialized_systems: Dict[str, bool] = {}

    self.logger.info("🧠 Phase 4/5: Initializing Intelligence System...")

    # Get or create the global container
    container = get_container()

    # Register all intelligence services
    register_intelligence_services(container)

    # Initialize all services (handles dependency order automatically)
    try:
        status = await initialize_intelligence_services(container)

        for service_name, result in status.items():
            if result == "initialized":
                initialized_systems[service_name] = True
                self.logger.info(f"  ✓ {service_name}: Ready")
            else:
                initialized_systems[service_name] = False
                self.logger.warning(f"  ⚠️ {service_name}: {result}")

    except Exception as e:
        self.logger.error(f"Intelligence system initialization failed: {e}")

    # Store container reference for shutdown
    self._service_container = container

    return initialized_systems
```

**Step 4: Add shutdown integration**

Add to the shutdown method in run_supervisor.py:

```python
async def _shutdown_intelligence_system(self) -> None:
    """Shutdown intelligence services using the container."""
    if hasattr(self, "_service_container") and self._service_container:
        self.logger.info("Shutting down intelligence services...")
        await self._service_container.shutdown_all()
        self.logger.info("Intelligence services shutdown complete")
```

**Step 5: Test the integration**

Run: `python3 run_supervisor.py`
Expected: No more initialization errors for the 4 engines

**Step 6: Commit**

```bash
git add backend/core/di/intelligence_services.py run_supervisor.py
git commit -m "feat(di): integrate DI container with run_supervisor.py, fixing 4 initialization bugs"
```

---

## Task 6: Update Factory Functions for Backward Compatibility

**Files:**
- Modify: `backend/intelligence/collaboration_engine.py`
- Modify: `backend/intelligence/code_ownership.py`
- Modify: `backend/intelligence/review_workflow.py`
- Modify: `backend/intelligence/ide_integration.py`

**Step 1: Update collaboration_engine.py factory**

Find the `get_collaboration_engine` function and update:

```python
def get_collaboration_engine(
    config: Optional[CollaborationConfig] = None
) -> CollaborationEngine:
    """
    Get the singleton CollaborationEngine instance.

    If the DI container is initialized, delegates to it.
    Otherwise falls back to direct instantiation for standalone use.
    """
    global _collaboration_engine

    # Try DI container first
    try:
        from backend.core.di import get_container
        container = get_container()
        if container.is_initialized and container.is_registered(CollaborationEngine):
            return container.resolve(CollaborationEngine)
    except ImportError:
        pass  # DI not available, use fallback

    # Fallback: direct instantiation
    if _collaboration_engine is None:
        _collaboration_engine = CollaborationEngine(config=config or CollaborationConfig())
    return _collaboration_engine


def get_cross_repo_coordinator() -> CrossRepoCollaborationCoordinator:
    """
    Get the singleton CrossRepoCollaborationCoordinator instance.
    """
    global _cross_repo_coordinator

    # Try DI container first
    try:
        from backend.core.di import get_container
        container = get_container()
        if container.is_initialized and container.is_registered(CrossRepoCollaborationCoordinator):
            return container.resolve(CrossRepoCollaborationCoordinator)
    except ImportError:
        pass

    # Fallback
    if _cross_repo_coordinator is None:
        _cross_repo_coordinator = CrossRepoCollaborationCoordinator()
    return _cross_repo_coordinator
```

**Step 2: Repeat for other engine files**

Apply the same pattern to:
- `code_ownership.py`: `get_ownership_engine`, `get_cross_repo_ownership_coordinator`
- `review_workflow.py`: `get_review_workflow_engine`, `get_cross_repo_review_coordinator`
- `ide_integration.py`: `get_ide_integration_engine`, `get_cross_repo_ide_coordinator`

**Step 3: Commit**

```bash
git add backend/intelligence/collaboration_engine.py
git add backend/intelligence/code_ownership.py
git add backend/intelligence/review_workflow.py
git add backend/intelligence/ide_integration.py
git commit -m "feat(di): update factory functions to delegate to DI container"
```

---

## Task 7: Add Testing Utilities

**Files:**
- Create: `backend/core/di/testing.py`
- Test: `tests/core/di/test_testing.py`

**Step 1: Write the failing test**

```python
# tests/core/di/test_testing.py
"""Tests for DI testing utilities."""

import pytest
from typing import List, Type

from backend.core.di.protocols import BaseAsyncService


class RealService(BaseAsyncService):
    """A real service to be mocked."""

    async def do_work(self) -> str:
        return "real"


class MockService(BaseAsyncService):
    """A mock service."""

    async def do_work(self) -> str:
        return "mock"


class TestTestContainer:
    """Test TestContainer functionality."""

    @pytest.mark.asyncio
    async def test_register_mock(self):
        """Should use mock instead of real service."""
        from backend.core.di.testing import TestContainer

        async with TestContainer() as container:
            container.register_mock(RealService, MockService())

            service = container.resolve(RealService)
            result = await service.do_work()

            assert result == "mock"

    @pytest.mark.asyncio
    async def test_isolation_between_tests(self):
        """Each TestContainer should be isolated."""
        from backend.core.di.testing import TestContainer
        from backend.core.di import Scope

        async with TestContainer() as container1:
            container1.register(RealService, scope=Scope.SINGLETON)
            service1 = container1.resolve(RealService)

        async with TestContainer() as container2:
            container2.register(RealService, scope=Scope.SINGLETON)
            service2 = container2.resolve(RealService)

        # Different containers, different instances
        assert service1 is not service2

    @pytest.mark.asyncio
    async def test_auto_cleanup(self):
        """Should cleanup services on exit."""
        from backend.core.di.testing import TestContainer
        from backend.core.di import Scope

        cleanup_called = []

        class CleanupService(BaseAsyncService):
            async def stop(self) -> None:
                cleanup_called.append(True)

        async with TestContainer() as container:
            container.register(CleanupService, scope=Scope.SINGLETON)
            container.resolve(CleanupService)
            await container.initialize_all()

        # Cleanup should have been called on exit
        assert len(cleanup_called) == 1
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/core/di/test_testing.py -v`
Expected: FAIL with "ModuleNotFoundError"

**Step 3: Write implementation**

```python
# backend/core/di/testing.py
"""
Testing Utilities for DI Container
==================================

Provides TestContainer and mocking utilities for testing
services in isolation.

Author: JARVIS Intelligence System
Version: 1.0.0
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Type, TypeVar

from backend.core.di.container import ServiceContainer
from backend.core.di.protocols import Scope

T = TypeVar("T")


class TestContainer(ServiceContainer):
    """
    Test-specific container with mocking support.

    Usage:
        async with TestContainer() as container:
            container.register_mock(RealService, MockService())
            service = container.resolve(DependentService)
            # DependentService receives MockService
    """

    def __init__(self, name: str = "test"):
        super().__init__(name)
        self._mocks: Dict[Type, Any] = {}

    def register_mock(
        self,
        service_type: Type[T],
        mock_instance: T,
    ) -> "TestContainer":
        """
        Register a mock instance for a service type.

        The mock will be returned whenever this service is resolved.

        Args:
            service_type: The type to mock
            mock_instance: The mock instance to use

        Returns:
            Self for chaining
        """
        self._mocks[service_type] = mock_instance
        self._singletons[service_type] = mock_instance
        return self

    def resolve(self, service_type: Type[T]) -> T:
        """Resolve, returning mock if registered."""
        if service_type in self._mocks:
            return self._mocks[service_type]
        return super().resolve(service_type)

    async def __aenter__(self) -> "TestContainer":
        """Enter async context."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context, cleaning up."""
        await self.shutdown_all()
        self._mocks.clear()

    def clear_mocks(self) -> None:
        """Clear all registered mocks."""
        for mock_type in list(self._mocks.keys()):
            self._singletons.pop(mock_type, None)
        self._mocks.clear()
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/core/di/test_testing.py -v`
Expected: PASS (3 tests)

**Step 5: Update __init__.py**

```python
# Add to backend/core/di/__init__.py
from backend.core.di.testing import TestContainer

__all__ = [
    # ... existing ...
    "TestContainer",
]
```

**Step 6: Commit**

```bash
git add backend/core/di/testing.py tests/core/di/test_testing.py backend/core/di/__init__.py
git commit -m "feat(di): add TestContainer with mocking support for testing"
```

---

## Task 8: Run Full Integration Test

**Step 1: Run all DI tests**

```bash
pytest tests/core/di/ -v
```

Expected: All tests pass

**Step 2: Run supervisor with DI**

```bash
python3 run_supervisor.py
```

Expected:
- No `__init__() got an unexpected keyword argument` errors
- Services initialize in correct order
- Graceful shutdown on Ctrl+C

**Step 3: Verify health checks work**

```bash
curl http://localhost:8010/health
```

Expected: JSON with service health status

**Step 4: Final commit**

```bash
git add -A
git commit -m "feat(di): complete enterprise DI container implementation

- Async-native service container with protocol-based contracts
- Topological sorting for dependency resolution
- Cycle detection with clear error messages
- Parallel initialization by dependency level
- LIFO shutdown ordering
- Health monitoring integration
- Lifecycle events with sync/async handlers
- TestContainer with mocking support
- Backward-compatible factory functions
- Fixed 4 initialization bugs in run_supervisor.py

Closes: JARVIS-XXX"
```

---

## Summary

This implementation plan provides:

1. **9 bite-sized tasks** with TDD approach
2. **Complete code** for each step
3. **Exact file paths** and commands
4. **Frequent commits** after each task

**Files created:**
- `backend/core/di/__init__.py`
- `backend/core/di/protocols.py`
- `backend/core/di/resolution.py`
- `backend/core/di/container.py`
- `backend/core/di/events.py`
- `backend/core/di/intelligence_services.py`
- `backend/core/di/testing.py`
- `tests/core/di/test_protocols.py`
- `tests/core/di/test_resolution.py`
- `tests/core/di/test_container.py`
- `tests/core/di/test_events.py`
- `tests/core/di/test_testing.py`

**Files modified:**
- `run_supervisor.py` (lines 12620-12820)
- `backend/intelligence/collaboration_engine.py`
- `backend/intelligence/code_ownership.py`
- `backend/intelligence/review_workflow.py`
- `backend/intelligence/ide_integration.py`
