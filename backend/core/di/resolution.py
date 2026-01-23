"""
DI Resolution v1.0 - Enterprise-Grade Dependency Resolution Engine
===================================================================

Advanced dependency resolution with graph algorithms and visualization.

This module provides:
- Tarjan's algorithm for O(V+E) strongly connected component detection
- Kahn's algorithm for stable topological sort with priority weighting
- Parallel group computation for concurrent initialization
- Thread-safe graph operations with invalidation support
- Rich visualization exports (DOT, Mermaid, JSON)

Performance Characteristics:
- Cycle detection: O(V+E) via Tarjan's SCC
- Topological sort: O(V+E) via Kahn's algorithm
- Parallel groups: O(V+E) with level assignment
- Graph building: O(V+E) with memoization
- All results cached with LRU and invalidation support

Thread Safety:
- Graph building uses RLock for reentrant safety
- Cache operations are atomic
- Invalidation propagates to all cached results

Architecture:
    +------------------------------------------------------------------+
    |                    DependencyResolver                            |
    +------------------------------------------------------------------+
    |  build_graph()     |  detect_cycles()  |  get_init_order()       |
    +------------------------------------------------------------------+
    |  Tarjan SCC        |  Kahn Topo Sort   |  Level Assignment       |
    +------------------------------------------------------------------+
    |                    Graph Cache Layer                             |
    +------------------------------------------------------------------+

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import json
import threading
import weakref
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from functools import lru_cache
from typing import (
    Any,
    Callable,
    DefaultDict,
    Deque,
    Dict,
    FrozenSet,
    Generic,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
    Type,
    TypeVar,
    Union,
)

from backend.core.di.protocols import (
    CircularDependencyError,
    DependencySpec,
    DependencyType,
    InvalidGraphStateError,
    ServiceDefinition,
    Scope,
)


# =============================================================================
# Type Variables and Aliases
# =============================================================================

T = TypeVar("T")
NodeType = Type  # Graph nodes are types
EdgeType = Tuple[Type, Type]  # (from, to) directed edge


# =============================================================================
# Graph Edge Metadata
# =============================================================================


@dataclass(frozen=True)
class EdgeMetadata:
    """
    Metadata for dependency graph edges.

    Captures additional information about the dependency relationship
    for advanced resolution strategies.
    """
    source: Type
    target: Type
    dependency_type: DependencyType
    is_lazy: bool = False
    is_optional: bool = False
    qualifier: Optional[str] = None
    weight: float = 1.0

    @property
    def is_weak(self) -> bool:
        """Check if this is a weak dependency (lazy or optional)."""
        return self.is_lazy or self.is_optional

    @property
    def can_break_cycle(self) -> bool:
        """Check if this edge can be ignored for cycle detection."""
        return self.is_lazy

    def __hash__(self) -> int:
        return hash((self.source, self.target, self.qualifier))


# =============================================================================
# Strongly Connected Component (SCC) Result
# =============================================================================


@dataclass
class StronglyConnectedComponent:
    """
    Represents a strongly connected component in the dependency graph.

    A SCC with more than one node indicates a circular dependency.
    """
    nodes: FrozenSet[Type]
    is_cycle: bool = field(init=False)
    cycle_path: Optional[List[Type]] = None

    def __post_init__(self):
        self.is_cycle = len(self.nodes) > 1

    @property
    def node_names(self) -> List[str]:
        """Get sorted list of node type names."""
        return sorted(t.__name__ for t in self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    def __contains__(self, item: Type) -> bool:
        return item in self.nodes


# =============================================================================
# Parallel Group
# =============================================================================


@dataclass
class ParallelGroup:
    """
    Group of services that can be initialized concurrently.

    Services within a group have no interdependencies and can
    safely be started in parallel.
    """
    level: int
    services: FrozenSet[Type]
    dependencies_satisfied: bool = True

    @property
    def size(self) -> int:
        return len(self.services)

    @property
    def service_names(self) -> List[str]:
        return sorted(t.__name__ for t in self.services)


# =============================================================================
# Graph Statistics
# =============================================================================


@dataclass
class GraphStatistics:
    """Statistics about the dependency graph."""
    node_count: int
    edge_count: int
    max_depth: int
    avg_dependencies: float
    max_dependencies: int
    cycle_count: int
    scc_count: int
    parallel_levels: int
    computed_at: datetime = field(default_factory=datetime.utcnow)


# =============================================================================
# Cache Entry
# =============================================================================


@dataclass
class CacheEntry(Generic[T]):
    """Thread-safe cache entry with versioning."""
    value: T
    version: int
    computed_at: datetime = field(default_factory=datetime.utcnow)
    access_count: int = 0
    last_access: datetime = field(default_factory=datetime.utcnow)

    def touch(self) -> None:
        """Record an access to this cache entry."""
        self.access_count += 1
        self.last_access = datetime.utcnow()


# =============================================================================
# Dependency Resolver
# =============================================================================


class DependencyResolver:
    """
    Enterprise-grade dependency resolver with advanced algorithms.

    Provides O(V+E) cycle detection via Tarjan's algorithm and
    stable topological sorting via Kahn's algorithm with priority
    weighting for deterministic initialization order.

    Features:
    - Thread-safe graph building with RLock
    - Lazy resolution support (defers cycle check for lazy deps)
    - Optional dependency handling (no failure if missing)
    - Factory dependency support
    - Memoized results with version-based invalidation
    - Visualization exports (DOT, Mermaid, JSON)

    Example:
        resolver = DependencyResolver(definitions)
        resolver.validate_all()  # Raises if cycles exist

        init_order = resolver.get_initialization_order()
        parallel_groups = resolver.get_parallel_groups()

        # Visualize
        print(resolver.to_mermaid())
    """

    __slots__ = (
        "_definitions",
        "_definitions_by_type",
        "_adjacency",
        "_reverse_adjacency",
        "_edge_metadata",
        "_lock",
        "_version",
        "_cache",
        "_lazy_edges",
        "_optional_edges",
        "_is_built",
    )

    def __init__(
        self,
        definitions: Optional[List[ServiceDefinition]] = None,
    ):
        """
        Initialize the resolver with service definitions.

        Args:
            definitions: List of service definitions to analyze.
                        Can be empty for dynamic registration.
        """
        self._definitions: List[ServiceDefinition] = list(definitions or [])
        self._definitions_by_type: Dict[Type, ServiceDefinition] = {}
        self._adjacency: DefaultDict[Type, Set[Type]] = defaultdict(set)
        self._reverse_adjacency: DefaultDict[Type, Set[Type]] = defaultdict(set)
        self._edge_metadata: Dict[EdgeType, EdgeMetadata] = {}
        self._lock = threading.RLock()
        self._version = 0
        self._cache: Dict[str, CacheEntry[Any]] = {}
        self._lazy_edges: Set[EdgeType] = set()
        self._optional_edges: Set[EdgeType] = set()
        self._is_built = False

        # Build graph if definitions provided
        if definitions:
            self.build_graph()

    # =========================================================================
    # Graph Building
    # =========================================================================

    def build_graph(self) -> Dict[Type, Set[Type]]:
        """
        Build the dependency adjacency list from definitions.

        Thread-safe operation that constructs:
        - Forward adjacency list (who I depend on)
        - Reverse adjacency list (who depends on me)
        - Edge metadata for all dependencies

        Returns:
            Dict mapping each type to its dependencies.

        Complexity: O(V + E) where V = services, E = dependencies
        """
        with self._lock:
            # Clear existing state
            self._adjacency.clear()
            self._reverse_adjacency.clear()
            self._edge_metadata.clear()
            self._definitions_by_type.clear()
            self._lazy_edges.clear()
            self._optional_edges.clear()

            # Index definitions by interface type
            for definition in self._definitions:
                self._definitions_by_type[definition.interface] = definition

                # Ensure node exists even with no dependencies
                if definition.interface not in self._adjacency:
                    self._adjacency[definition.interface] = set()

            # Build edges from dependencies
            for definition in self._definitions:
                source = definition.interface

                for dep_spec in definition.dependencies:
                    # Support both service_type (new) and target_type (legacy)
                    target = getattr(dep_spec, 'service_type', None) or getattr(dep_spec, 'target_type', None)
                    if target is None:
                        continue

                    # Add to adjacency (source depends on target)
                    self._adjacency[source].add(target)

                    # Add to reverse adjacency (target is depended on by source)
                    self._reverse_adjacency[target].add(source)

                    # Store edge metadata
                    edge: EdgeType = (source, target)
                    self._edge_metadata[edge] = EdgeMetadata(
                        source=source,
                        target=target,
                        dependency_type=dep_spec.dependency_type,
                        is_lazy=dep_spec.is_lazy,
                        is_optional=dep_spec.is_optional,
                        qualifier=dep_spec.qualifier,
                        weight=1.0 if not dep_spec.is_optional else 0.5,
                    )

                    # Track special edge types
                    if dep_spec.is_lazy:
                        self._lazy_edges.add(edge)
                    if dep_spec.is_optional:
                        self._optional_edges.add(edge)

            self._is_built = True
            self._invalidate_cache()

            return dict(self._adjacency)

    def register(self, definition: ServiceDefinition) -> None:
        """
        Dynamically register a new service definition.

        Invalidates cached results and rebuilds the graph.

        Args:
            definition: The service definition to register.
        """
        with self._lock:
            self._definitions.append(definition)
            self.build_graph()

    def unregister(self, service_type: Type) -> bool:
        """
        Remove a service definition.

        Args:
            service_type: The interface type to remove.

        Returns:
            True if removed, False if not found.
        """
        with self._lock:
            original_len = len(self._definitions)
            self._definitions = [
                d for d in self._definitions
                if d.interface != service_type
            ]
            if len(self._definitions) < original_len:
                self.build_graph()
                return True
            return False

    # =========================================================================
    # Cycle Detection (Tarjan's Algorithm)
    # =========================================================================

    def detect_cycles(
        self,
        include_lazy: bool = False,
    ) -> List[List[Type]]:
        """
        Detect circular dependencies using Tarjan's SCC algorithm.

        Uses Tarjan's algorithm for O(V+E) strongly connected component
        detection. SCCs with more than one node represent cycles.

        Args:
            include_lazy: If True, include lazy dependencies in cycle detection.
                         By default, lazy deps are excluded as they can break cycles.

        Returns:
            List of cycles, where each cycle is a list of types in the cycle.
            Empty list if no cycles detected.

        Complexity: O(V + E)
        """
        cache_key = f"cycles_{include_lazy}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            if not self._is_built:
                self.build_graph()

            # Tarjan's algorithm state
            index_counter = [0]  # Mutable counter
            stack: List[Type] = []
            lowlinks: Dict[Type, int] = {}
            index: Dict[Type, int] = {}
            on_stack: Set[Type] = set()
            sccs: List[StronglyConnectedComponent] = []

            def strongconnect(node: Type) -> None:
                """Recursive DFS for Tarjan's algorithm."""
                index[node] = index_counter[0]
                lowlinks[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack.add(node)

                # Consider successors
                for successor in self._adjacency.get(node, set()):
                    edge = (node, successor)

                    # Skip lazy edges unless explicitly included
                    if not include_lazy and edge in self._lazy_edges:
                        continue

                    if successor not in index:
                        # Successor not visited yet
                        strongconnect(successor)
                        lowlinks[node] = min(lowlinks[node], lowlinks[successor])
                    elif successor in on_stack:
                        # Successor is on stack, part of current SCC
                        lowlinks[node] = min(lowlinks[node], index[successor])

                # If node is root of SCC, pop the SCC
                if lowlinks[node] == index[node]:
                    scc_nodes: List[Type] = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        scc_nodes.append(w)
                        if w == node:
                            break

                    scc = StronglyConnectedComponent(
                        nodes=frozenset(scc_nodes),
                        cycle_path=scc_nodes if len(scc_nodes) > 1 else None,
                    )
                    sccs.append(scc)

            # Run Tarjan's on all nodes
            for node in self._adjacency:
                if node not in index:
                    strongconnect(node)

            # Extract cycles (SCCs with more than one node)
            cycles = [
                list(scc.nodes) for scc in sccs if scc.is_cycle
            ]

            self._set_cached(cache_key, cycles)
            return cycles

    def get_strongly_connected_components(
        self,
        include_lazy: bool = False,
    ) -> List[StronglyConnectedComponent]:
        """
        Get all strongly connected components.

        Returns all SCCs, not just cycles. Useful for understanding
        the full graph structure.

        Returns:
            List of all SCCs (including single-node components).
        """
        cache_key = f"sccs_{include_lazy}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            if not self._is_built:
                self.build_graph()

            # Same Tarjan's algorithm but return all SCCs
            index_counter = [0]
            stack: List[Type] = []
            lowlinks: Dict[Type, int] = {}
            index: Dict[Type, int] = {}
            on_stack: Set[Type] = set()
            sccs: List[StronglyConnectedComponent] = []

            def strongconnect(node: Type) -> None:
                index[node] = index_counter[0]
                lowlinks[node] = index_counter[0]
                index_counter[0] += 1
                stack.append(node)
                on_stack.add(node)

                for successor in self._adjacency.get(node, set()):
                    edge = (node, successor)
                    if not include_lazy and edge in self._lazy_edges:
                        continue

                    if successor not in index:
                        strongconnect(successor)
                        lowlinks[node] = min(lowlinks[node], lowlinks[successor])
                    elif successor in on_stack:
                        lowlinks[node] = min(lowlinks[node], index[successor])

                if lowlinks[node] == index[node]:
                    scc_nodes: List[Type] = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        scc_nodes.append(w)
                        if w == node:
                            break

                    scc = StronglyConnectedComponent(
                        nodes=frozenset(scc_nodes),
                        cycle_path=scc_nodes if len(scc_nodes) > 1 else None,
                    )
                    sccs.append(scc)

            for node in self._adjacency:
                if node not in index:
                    strongconnect(node)

            self._set_cached(cache_key, sccs)
            return sccs

    # =========================================================================
    # Topological Sort (Kahn's Algorithm)
    # =========================================================================

    def get_initialization_order(
        self,
        use_priority: bool = True,
    ) -> List[Type]:
        """
        Get stable topological initialization order using Kahn's algorithm.

        Uses Kahn's algorithm with priority-based tie-breaking for
        deterministic ordering when multiple services have no dependencies.

        Services with lower priority values are initialized first.
        Priority is taken from ServiceDefinition.priority.

        Args:
            use_priority: If True, use priority for tie-breaking.
                         If False, use alphabetical order.

        Returns:
            List of types in initialization order (dependencies first).

        Raises:
            CircularDependencyError: If cycles exist (Kahn's fails).

        Complexity: O(V + E) with O(V log V) for priority sorting
        """
        cache_key = f"init_order_{use_priority}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            if not self._is_built:
                self.build_graph()

            # Calculate in-degrees (ignoring lazy edges for ordering)
            in_degree: Dict[Type, int] = defaultdict(int)
            all_nodes: Set[Type] = set(self._adjacency.keys())

            for node in all_nodes:
                if node not in in_degree:
                    in_degree[node] = 0

            for source, targets in self._adjacency.items():
                for target in targets:
                    edge = (source, target)
                    # Only count non-lazy edges for ordering
                    if edge not in self._lazy_edges:
                        in_degree[source] += 0  # Ensure source exists
                        all_nodes.add(target)

            # Recalculate considering actual dependencies
            in_degree = defaultdict(int)
            for node in all_nodes:
                in_degree[node] = 0

            for source, targets in self._adjacency.items():
                for target in targets:
                    edge = (source, target)
                    if edge not in self._lazy_edges:
                        # source depends on target, so source's in-degree increases
                        # Wait, that's wrong. Let me reconsider.
                        # If A depends on B, then B must come before A
                        # In topological sort, we need to track "what must come before me"
                        # So if A depends on B, the edge is A -> B (A needs B)
                        # For topo sort, we want B before A
                        # in_degree should be: number of things I depend on
                        pass

            # Recompute correctly:
            # adjacency[A] contains B means A depends on B
            # For topo sort, we need: for each node, count how many other nodes depend on it
            # That's the reverse adjacency's size

            # Actually, let's be more careful:
            # Kahn's algorithm processes nodes with in-degree 0 first
            # in-degree = number of incoming edges
            # If A depends on B, edge is A -> B
            # For initialization, we want B before A
            # So we should reverse the edges: edge B -> A means "B must init before A"
            # Then in-degree[A] = number of things A depends on

            # Let's compute this correctly
            in_degree = {node: 0 for node in all_nodes}

            for source, targets in self._adjacency.items():
                for target in targets:
                    edge = (source, target)
                    if edge not in self._lazy_edges:
                        # source depends on target
                        # so source cannot start until target is ready
                        # edge direction for topo sort: target -> source
                        # this means in_degree[source] += 1
                        in_degree[source] = in_degree.get(source, 0) + 1

            # Priority function for tie-breaking
            def get_priority(node: Type) -> Tuple[int, str]:
                defn = self._definitions_by_type.get(node)
                priority = defn.priority if defn and use_priority else 100
                name = node.__name__
                return (priority, name)

            # Kahn's algorithm with priority queue
            # Start with nodes that have in-degree 0 (no dependencies)
            queue: List[Type] = [n for n in all_nodes if in_degree[n] == 0]
            queue.sort(key=get_priority)

            result: List[Type] = []

            while queue:
                # Pop node with lowest priority (first in sorted list)
                node = queue.pop(0)
                result.append(node)

                # For each node that depends on this node, decrement in-degree
                for dependent in self._reverse_adjacency.get(node, set()):
                    edge = (dependent, node)
                    if edge in self._lazy_edges:
                        continue

                    in_degree[dependent] -= 1
                    if in_degree[dependent] == 0:
                        # Insert in sorted position
                        priority = get_priority(dependent)
                        insert_pos = 0
                        for i, q_node in enumerate(queue):
                            if get_priority(q_node) > priority:
                                insert_pos = i
                                break
                            insert_pos = i + 1
                        queue.insert(insert_pos, dependent)

            # Check for remaining nodes (indicates cycle)
            if len(result) < len(all_nodes):
                remaining = all_nodes - set(result)
                # Find cycle among remaining
                cycles = self.detect_cycles()
                if cycles:
                    raise CircularDependencyError(cycles[0])
                else:
                    raise InvalidGraphStateError(
                        f"Topological sort incomplete: {len(remaining)} nodes unreachable",
                        {"remaining": [t.__name__ for t in remaining]},
                    )

            self._set_cached(cache_key, result)
            return result

    def get_shutdown_order(self) -> List[Type]:
        """
        Get shutdown order (reverse of initialization).

        Returns:
            List of types in shutdown order (dependents first).
        """
        cache_key = "shutdown_order"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        init_order = self.get_initialization_order()
        shutdown_order = list(reversed(init_order))

        self._set_cached(cache_key, shutdown_order)
        return shutdown_order

    # =========================================================================
    # Parallel Groups (Level Assignment)
    # =========================================================================

    def get_parallel_groups(self) -> List[Set[Type]]:
        """
        Get groups of services that can initialize concurrently.

        Uses level assignment algorithm to partition services into
        groups where all services in a group have their dependencies
        satisfied by previous groups.

        Returns:
            List of sets, where each set contains types that can
            be initialized in parallel. Groups are ordered by level.

        Complexity: O(V + E)
        """
        cache_key = "parallel_groups"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            if not self._is_built:
                self.build_graph()

            # Assign levels using BFS from nodes with no dependencies
            levels: Dict[Type, int] = {}
            all_nodes = set(self._adjacency.keys())

            # Add nodes that are only targets (no outgoing edges)
            for targets in self._adjacency.values():
                all_nodes.update(targets)

            # Find nodes with no dependencies (level 0)
            def get_effective_deps(node: Type) -> Set[Type]:
                """Get non-lazy dependencies."""
                deps = self._adjacency.get(node, set())
                return {
                    d for d in deps
                    if (node, d) not in self._lazy_edges
                }

            level_0 = {
                n for n in all_nodes
                if not get_effective_deps(n)
            }

            for node in level_0:
                levels[node] = 0

            # BFS to assign levels
            queue: Deque[Type] = deque(level_0)
            visited = set(level_0)

            while queue:
                node = queue.popleft()
                node_level = levels[node]

                # Process dependents
                for dependent in self._reverse_adjacency.get(node, set()):
                    edge = (dependent, node)
                    if edge in self._lazy_edges:
                        continue

                    # Dependent's level is max of all dependency levels + 1
                    deps = get_effective_deps(dependent)
                    if all(d in levels for d in deps):
                        dep_level = max(levels[d] for d in deps) + 1
                        if dependent not in levels or levels[dependent] < dep_level:
                            levels[dependent] = dep_level

                        if dependent not in visited:
                            visited.add(dependent)
                            queue.append(dependent)

            # Handle any orphaned nodes (no dependencies or dependents)
            for node in all_nodes:
                if node not in levels:
                    levels[node] = 0

            # Group by level
            level_groups: DefaultDict[int, Set[Type]] = defaultdict(set)
            for node, level in levels.items():
                level_groups[level].add(node)

            # Convert to sorted list of sets
            max_level = max(level_groups.keys()) if level_groups else 0
            result = [level_groups[i] for i in range(max_level + 1)]

            self._set_cached(cache_key, result)
            return result

    def get_parallel_groups_detailed(self) -> List[ParallelGroup]:
        """
        Get detailed parallel group information.

        Returns:
            List of ParallelGroup objects with full metadata.
        """
        groups = self.get_parallel_groups()
        return [
            ParallelGroup(
                level=i,
                services=frozenset(group),
                dependencies_satisfied=True,
            )
            for i, group in enumerate(groups)
        ]

    # =========================================================================
    # Dependency Queries
    # =========================================================================

    def get_dependents(self, service_type: Type) -> Set[Type]:
        """
        Get all services that depend on the given type.

        Args:
            service_type: The type to check dependents for.

        Returns:
            Set of types that depend on service_type.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()
            return set(self._reverse_adjacency.get(service_type, set()))

    def get_dependencies(self, service_type: Type) -> Set[Type]:
        """
        Get all types that the given service depends on.

        Args:
            service_type: The type to check dependencies for.

        Returns:
            Set of types that service_type depends on.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()
            return set(self._adjacency.get(service_type, set()))

    def get_transitive_dependents(self, service_type: Type) -> Set[Type]:
        """
        Get all transitive dependents (recursive).

        Returns all types that directly or indirectly depend on service_type.
        """
        cache_key = f"trans_dependents_{service_type.__name__}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            result: Set[Type] = set()
            queue: Deque[Type] = deque([service_type])

            while queue:
                current = queue.popleft()
                for dependent in self._reverse_adjacency.get(current, set()):
                    if dependent not in result:
                        result.add(dependent)
                        queue.append(dependent)

            self._set_cached(cache_key, result)
            return result

    def get_transitive_dependencies(self, service_type: Type) -> Set[Type]:
        """
        Get all transitive dependencies (recursive).

        Returns all types that service_type directly or indirectly depends on.
        """
        cache_key = f"trans_deps_{service_type.__name__}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            result: Set[Type] = set()
            queue: Deque[Type] = deque([service_type])

            while queue:
                current = queue.popleft()
                for dep in self._adjacency.get(current, set()):
                    if dep not in result:
                        result.add(dep)
                        queue.append(dep)

            self._set_cached(cache_key, result)
            return result

    def get_depth(self, service_type: Type) -> int:
        """
        Get the depth of a service in the dependency graph.

        Depth is the longest path from this node to a leaf (no dependencies).
        """
        deps = self.get_dependencies(service_type)
        if not deps:
            return 0
        return 1 + max(self.get_depth(d) for d in deps)

    # =========================================================================
    # Validation
    # =========================================================================

    def validate_all(self) -> None:
        """
        Validate the dependency graph.

        Raises:
            CircularDependencyError: If cycles exist (excluding lazy deps).
            InvalidGraphStateError: If graph is in invalid state.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()

            # Check for cycles (excluding lazy dependencies)
            cycles = self.detect_cycles(include_lazy=False)
            if cycles:
                # Find the first meaningful cycle
                cycle = cycles[0]
                raise CircularDependencyError(cycle=cycle)

            # Validate all dependencies exist (for non-optional deps)
            all_types = set(self._definitions_by_type.keys())
            for defn in self._definitions:
                for dep_spec in defn.dependencies:
                    # Support both service_type (new) and target_type (legacy)
                    target_type = getattr(dep_spec, 'service_type', None) or getattr(dep_spec, 'target_type', None)
                    if target_type is None:
                        continue

                    if (
                        not dep_spec.is_optional
                        and target_type not in all_types
                    ):
                        raise InvalidGraphStateError(
                            f"Missing dependency: {defn.interface.__name__} requires "
                            f"{target_type.__name__} which is not registered",
                            {
                                "service": defn.interface.__name__,
                                "missing": target_type.__name__,
                            },
                        )

    def is_valid(self) -> Tuple[bool, List[str]]:
        """
        Check if the graph is valid without raising.

        Returns:
            Tuple of (is_valid, list_of_issues).
        """
        issues: List[str] = []

        try:
            self.validate_all()
        except CircularDependencyError as e:
            issues.append(str(e))
        except InvalidGraphStateError as e:
            issues.append(str(e))
        except Exception as e:
            issues.append(f"Unexpected validation error: {e}")

        return len(issues) == 0, issues

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_statistics(self) -> GraphStatistics:
        """
        Get statistics about the dependency graph.

        Returns:
            GraphStatistics with computed metrics.
        """
        cache_key = "statistics"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        with self._lock:
            if not self._is_built:
                self.build_graph()

            node_count = len(self._adjacency)
            edge_count = sum(len(deps) for deps in self._adjacency.values())

            # Calculate dependency counts
            dep_counts = [len(deps) for deps in self._adjacency.values()]
            avg_deps = sum(dep_counts) / len(dep_counts) if dep_counts else 0.0
            max_deps = max(dep_counts) if dep_counts else 0

            # Max depth via DFS
            def get_max_depth(node: Type, visited: Set[Type]) -> int:
                if node in visited:
                    return 0
                visited.add(node)
                deps = self._adjacency.get(node, set())
                if not deps:
                    return 0
                return 1 + max(
                    (get_max_depth(d, visited) for d in deps),
                    default=0,
                )

            max_depth = max(
                (get_max_depth(n, set()) for n in self._adjacency),
                default=0,
            )

            cycles = self.detect_cycles(include_lazy=False)
            sccs = self.get_strongly_connected_components(include_lazy=False)
            parallel = self.get_parallel_groups()

            stats = GraphStatistics(
                node_count=node_count,
                edge_count=edge_count,
                max_depth=max_depth,
                avg_dependencies=avg_deps,
                max_dependencies=max_deps,
                cycle_count=len(cycles),
                scc_count=len(sccs),
                parallel_levels=len(parallel),
            )

            self._set_cached(cache_key, stats)
            return stats

    # =========================================================================
    # Visualization Export
    # =========================================================================

    def to_dot(
        self,
        highlight_cycles: bool = True,
        include_metadata: bool = True,
    ) -> str:
        """
        Export to DOT format for Graphviz visualization.

        Args:
            highlight_cycles: Color cycle nodes red.
            include_metadata: Include edge labels for dependency type.

        Returns:
            DOT format string.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()

            lines = [
                "digraph DependencyGraph {",
                "  rankdir=TB;",
                "  node [shape=box, style=rounded];",
                "",
            ]

            # Find cycle nodes for highlighting
            cycle_nodes: Set[Type] = set()
            if highlight_cycles:
                for cycle in self.detect_cycles(include_lazy=True):
                    cycle_nodes.update(cycle)

            # Add nodes
            for node in self._adjacency:
                name = node.__name__
                attrs = []

                if node in cycle_nodes:
                    attrs.append('color="red"')
                    attrs.append('style="filled,rounded"')
                    attrs.append('fillcolor="lightpink"')

                if node in self._definitions_by_type:
                    defn = self._definitions_by_type[node]
                    attrs.append(f'tooltip="Priority: {defn.priority}"')

                attr_str = f" [{', '.join(attrs)}]" if attrs else ""
                lines.append(f'  "{name}"{attr_str};')

            lines.append("")

            # Add edges
            for source, targets in self._adjacency.items():
                for target in targets:
                    source_name = source.__name__
                    target_name = target.__name__

                    edge = (source, target)
                    attrs = []

                    if edge in self._lazy_edges:
                        attrs.append('style="dashed"')
                        attrs.append('color="blue"')
                        if include_metadata:
                            attrs.append('label="lazy"')
                    elif edge in self._optional_edges:
                        attrs.append('style="dotted"')
                        attrs.append('color="gray"')
                        if include_metadata:
                            attrs.append('label="optional"')

                    if edge in self._edge_metadata and highlight_cycles:
                        meta = self._edge_metadata[edge]
                        if meta.source in cycle_nodes and meta.target in cycle_nodes:
                            attrs.append('color="red"')
                            attrs.append('penwidth="2"')

                    attr_str = f" [{', '.join(attrs)}]" if attrs else ""
                    lines.append(f'  "{source_name}" -> "{target_name}"{attr_str};')

            lines.append("}")
            return "\n".join(lines)

    def to_mermaid(
        self,
        direction: str = "TB",
        highlight_cycles: bool = True,
    ) -> str:
        """
        Export to Mermaid diagram format.

        Args:
            direction: Graph direction (TB, BT, LR, RL).
            highlight_cycles: Style cycle edges differently.

        Returns:
            Mermaid format string.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()

            lines = [
                f"graph {direction}",
            ]

            # Find cycle nodes
            cycle_nodes: Set[Type] = set()
            if highlight_cycles:
                for cycle in self.detect_cycles(include_lazy=True):
                    cycle_nodes.update(cycle)

            # Add nodes with styling
            for node in self._adjacency:
                name = node.__name__
                if node in cycle_nodes:
                    lines.append(f"    {name}[{name}]:::cycle")
                else:
                    lines.append(f"    {name}[{name}]")

            lines.append("")

            # Add edges
            edge_id = 0
            for source, targets in self._adjacency.items():
                for target in targets:
                    source_name = source.__name__
                    target_name = target.__name__
                    edge = (source, target)

                    if edge in self._lazy_edges:
                        lines.append(f"    {source_name} -.->|lazy| {target_name}")
                    elif edge in self._optional_edges:
                        lines.append(f"    {source_name} -.-> {target_name}")
                    else:
                        lines.append(f"    {source_name} --> {target_name}")

                    edge_id += 1

            # Add styling
            lines.append("")
            lines.append("    classDef cycle fill:#ffcccc,stroke:#cc0000")

            return "\n".join(lines)

    def to_json(
        self,
        include_metadata: bool = True,
        include_statistics: bool = True,
    ) -> Dict[str, Any]:
        """
        Export as JSON-serializable dictionary.

        Args:
            include_metadata: Include edge metadata.
            include_statistics: Include graph statistics.

        Returns:
            Dictionary representation of the graph.
        """
        with self._lock:
            if not self._is_built:
                self.build_graph()

            result: Dict[str, Any] = {
                "nodes": [],
                "edges": [],
            }

            # Nodes
            for node in self._adjacency:
                node_data = {
                    "type": node.__name__,
                    "module": node.__module__,
                    "dependencies": [t.__name__ for t in self._adjacency.get(node, set())],
                    "dependents": [t.__name__ for t in self._reverse_adjacency.get(node, set())],
                }

                if node in self._definitions_by_type:
                    defn = self._definitions_by_type[node]
                    node_data["priority"] = defn.priority
                    node_data["scope"] = defn.scope.value
                    node_data["lazy"] = defn.lazy
                    node_data["async_init"] = defn.async_init

                result["nodes"].append(node_data)

            # Edges
            for (source, target), meta in self._edge_metadata.items():
                edge_data = {
                    "source": source.__name__,
                    "target": target.__name__,
                    "is_lazy": meta.is_lazy,
                    "is_optional": meta.is_optional,
                }

                if include_metadata:
                    edge_data["dependency_type"] = meta.dependency_type.value
                    edge_data["qualifier"] = meta.qualifier
                    edge_data["weight"] = meta.weight

                result["edges"].append(edge_data)

            # Cycles
            cycles = self.detect_cycles(include_lazy=False)
            result["cycles"] = [
                [t.__name__ for t in cycle]
                for cycle in cycles
            ]

            # Statistics
            if include_statistics:
                stats = self.get_statistics()
                result["statistics"] = {
                    "node_count": stats.node_count,
                    "edge_count": stats.edge_count,
                    "max_depth": stats.max_depth,
                    "avg_dependencies": stats.avg_dependencies,
                    "max_dependencies": stats.max_dependencies,
                    "cycle_count": stats.cycle_count,
                    "scc_count": stats.scc_count,
                    "parallel_levels": stats.parallel_levels,
                }

            # Initialization order
            try:
                init_order = self.get_initialization_order()
                result["initialization_order"] = [t.__name__ for t in init_order]
            except CircularDependencyError:
                result["initialization_order"] = None

            # Parallel groups
            try:
                parallel = self.get_parallel_groups()
                result["parallel_groups"] = [
                    [t.__name__ for t in group]
                    for group in parallel
                ]
            except Exception:
                result["parallel_groups"] = None

            return result

    def to_json_string(self, indent: int = 2, **kwargs: Any) -> str:
        """
        Export as formatted JSON string.

        Args:
            indent: JSON indentation level.
            **kwargs: Additional arguments for to_json().

        Returns:
            JSON string.
        """
        return json.dumps(self.to_json(**kwargs), indent=indent)

    # =========================================================================
    # Cache Management
    # =========================================================================

    def _get_cached(self, key: str) -> Optional[Any]:
        """Get a cached result if version matches."""
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if entry.version == self._version:
                    entry.touch()
                    return entry.value
        return None

    def _set_cached(self, key: str, value: Any) -> None:
        """Cache a result with current version."""
        with self._lock:
            self._cache[key] = CacheEntry(
                value=value,
                version=self._version,
            )

    def _invalidate_cache(self) -> None:
        """Invalidate all cached results."""
        with self._lock:
            self._version += 1
            # Keep cache entries but they'll be ignored due to version mismatch
            # This allows for LRU-style cleanup if needed

    def clear_cache(self) -> None:
        """Clear all cached results."""
        with self._lock:
            self._cache.clear()
            self._version += 1

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def __len__(self) -> int:
        """Get number of registered services."""
        return len(self._definitions)

    def __contains__(self, service_type: Type) -> bool:
        """Check if a service type is registered."""
        return service_type in self._definitions_by_type

    def __iter__(self) -> Iterator[ServiceDefinition]:
        """Iterate over service definitions."""
        return iter(self._definitions)

    def __repr__(self) -> str:
        stats = self.get_statistics()
        return (
            f"DependencyResolver("
            f"services={stats.node_count}, "
            f"edges={stats.edge_count}, "
            f"cycles={stats.cycle_count}, "
            f"levels={stats.parallel_levels})"
        )


# =============================================================================
# Convenience Functions
# =============================================================================


def create_resolver(
    definitions: List[ServiceDefinition],
    validate: bool = True,
) -> DependencyResolver:
    """
    Create a dependency resolver with optional validation.

    Args:
        definitions: Service definitions to analyze.
        validate: If True, validate for cycles during creation.

    Returns:
        Configured DependencyResolver.

    Raises:
        CircularDependencyError: If validate=True and cycles exist.
    """
    resolver = DependencyResolver(definitions)
    if validate:
        resolver.validate_all()
    return resolver


def detect_cycles_in_definitions(
    definitions: List[ServiceDefinition],
) -> List[List[Type]]:
    """
    Quick cycle detection without full resolver.

    Args:
        definitions: Service definitions to check.

    Returns:
        List of cycles (empty if none found).
    """
    resolver = DependencyResolver(definitions)
    return resolver.detect_cycles()


def get_init_order(
    definitions: List[ServiceDefinition],
) -> List[Type]:
    """
    Quick initialization order without full resolver.

    Args:
        definitions: Service definitions to order.

    Returns:
        List of types in initialization order.

    Raises:
        CircularDependencyError: If cycles exist.
    """
    resolver = DependencyResolver(definitions)
    return resolver.get_initialization_order()


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Main class
    "DependencyResolver",

    # Supporting types
    "EdgeMetadata",
    "StronglyConnectedComponent",
    "ParallelGroup",
    "GraphStatistics",
    "CacheEntry",

    # Convenience functions
    "create_resolver",
    "detect_cycles_in_definitions",
    "get_init_order",
]
