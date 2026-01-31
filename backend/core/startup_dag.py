"""
StartupDAG - Dependency-ordered startup execution.

Builds a Directed Acyclic Graph from component dependencies and
executes startup in tiers (parallel within tier, sequential between tiers).
"""
from __future__ import annotations

import logging
from typing import List, Dict, Set, Optional
from collections import defaultdict

from backend.core.component_registry import (
    ComponentRegistry, Dependency
)

logger = logging.getLogger("jarvis.startup_dag")


class CycleDetectedError(Exception):
    """Raised when a dependency cycle is detected."""
    pass


class StartupDAG:
    """
    Builds and manages startup order from component dependencies.

    Usage:
        dag = StartupDAG(registry)
        tiers = dag.build()  # Returns [[tier0_components], [tier1_components], ...]
    """

    def __init__(self, registry: ComponentRegistry):
        self.registry = registry
        self._graph: Dict[str, List[str]] = {}  # component -> dependencies
        self._tiers: Optional[List[List[str]]] = None

    def build(self) -> List[List[str]]:
        """
        Build startup tiers from component dependencies.

        Returns list of tiers, where each tier is a list of component names
        that can be started in parallel.

        Raises:
            CycleDetectedError: If dependency cycle detected
        """
        # Build dependency graph
        self._graph = {}
        for defn in self.registry.all_definitions():
            deps = []
            for dep in defn.dependencies:
                dep_name = dep.component if isinstance(dep, Dependency) else dep
                deps.append(dep_name)
            self._graph[defn.name] = deps

        # Check for cycles
        cycle = self._detect_cycles()
        if cycle:
            cycle_str = " -> ".join(cycle)
            raise CycleDetectedError(f"Dependency cycle detected: {cycle_str}")

        # Build tiers via topological sort
        self._tiers = self._topological_tiers()
        return self._tiers

    def _detect_cycles(self) -> Optional[List[str]]:
        """
        Detect cycles using DFS.

        Returns cycle path if found, None otherwise.
        """
        # Collect all nodes (declared + referenced)
        all_nodes: Set[str] = set(self._graph.keys())
        for deps in self._graph.values():
            all_nodes.update(deps)

        UNVISITED, IN_PROGRESS, VISITED = 0, 1, 2
        state = {name: UNVISITED for name in all_nodes}
        path: List[str] = []

        def dfs(node: str) -> Optional[List[str]]:
            if state[node] == VISITED:
                return None
            if state[node] == IN_PROGRESS:
                cycle_start = path.index(node)
                return path[cycle_start:] + [node]

            state[node] = IN_PROGRESS
            path.append(node)

            for dep in self._graph.get(node, []):
                result = dfs(dep)
                if result:
                    return result

            path.pop()
            state[node] = VISITED
            return None

        for node in all_nodes:
            if state[node] == UNVISITED:
                result = dfs(node)
                if result:
                    return result
        return None

    def _topological_tiers(self) -> List[List[str]]:
        """
        Build tiers using Kahn's algorithm variant.

        Components with no unresolved dependencies go in the current tier.
        """
        # Calculate in-degree (number of dependencies)
        in_degree: Dict[str, int] = defaultdict(int)
        all_nodes: Set[str] = set(self._graph.keys())

        for deps in self._graph.values():
            all_nodes.update(deps)

        for node in all_nodes:
            in_degree[node] = 0

        for node, deps in self._graph.items():
            in_degree[node] = len(deps)

        tiers: List[List[str]] = []
        remaining = set(all_nodes)

        while remaining:
            # Find all nodes with no remaining dependencies
            tier = [
                node for node in remaining
                if in_degree[node] == 0
            ]

            if not tier:
                # This shouldn't happen if cycle detection passed
                raise CycleDetectedError("Unable to resolve dependencies")

            tiers.append(sorted(tier))  # Sort for determinism

            # Remove this tier and update in-degrees
            for node in tier:
                remaining.remove(node)
                # Decrease in-degree of dependents
                for other, deps in self._graph.items():
                    if node in deps and other in remaining:
                        in_degree[other] -= 1

        return tiers

    def get_tier(self, component: str) -> int:
        """Get the tier number for a component."""
        if self._tiers is None:
            self.build()
        # After build(), _tiers is guaranteed to be set
        assert self._tiers is not None
        for i, tier in enumerate(self._tiers):
            if component in tier:
                return i
        return -1
