"""
JARVIS Startup DAG v149.2
=========================

Explicit dependency graph for deterministic startup ordering.

This module defines:
- Component dependencies as a directed acyclic graph (DAG)
- Topological sort for initialization waves
- Dependency readiness checking
- Shutdown order (reverse of startup)

Usage:
    from backend.core.startup.startup_dag import get_startup_dag
    
    dag = get_startup_dag()
    
    # Get initialization order as waves
    waves = dag.get_initialization_waves()
    # [["database", "redis"], ["cloudsql"], ["jarvis_prime"], ...]
    
    # Check if a component can start
    if dag.are_dependencies_ready("jarvis_prime"):
        await start_jarvis_prime()
"""

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set

from .component_registry import get_registry

logger = logging.getLogger(__name__)


# =============================================================================
# STARTUP PHASES
# =============================================================================

class StartupPhase(Enum):
    """
    High-level startup phases for organization.
    
    Components are grouped into phases for clarity.
    Within each phase, components are ordered by dependencies.
    """
    INFRASTRUCTURE = "infrastructure"  # Database, Redis, ports
    NETWORKING = "networking"          # IPC, WebSocket, HTTP
    CLOUD = "cloud"                    # GCP VM, CloudSQL
    EXTERNAL_REPOS = "external_repos"  # Prime, Reactor
    FEATURES = "features"              # Voice, AI features
    FINALIZATION = "finalization"      # Final checks, announcements


# =============================================================================
# DEPENDENCY DEFINITIONS
# =============================================================================

# Explicit dependency declarations
# Format: component -> [list of dependencies that must be READY first]
COMPONENT_DEPENDENCIES: Dict[str, List[str]] = {
    # === INFRASTRUCTURE (no dependencies) ===
    "database": [],
    "redis": [],
    "ipc_server": [],
    "port_manager": [],
    
    # === NETWORKING (depends on infrastructure) ===
    "websocket": ["ipc_server"],
    "http_server": ["database"],
    
    # === CLOUD (depends on infrastructure) ===
    "gcp_auth": [],
    "cloudsql": ["database", "gcp_auth"],
    "gcp_vm": ["gcp_auth"],
    "secret_manager": ["gcp_auth"],
    
    # === EXTERNAL REPOS (depends on cloud) ===
    "jarvis_prime": ["gcp_vm", "database"],
    "reactor_core": ["jarvis_prime", "database"],
    
    # === FEATURES (depends on repos) ===
    "voice_system": ["reactor_core"],
    "biometric_auth": ["voice_system"],
    "cost_tracker": ["redis"],
    "hardware_optimizer": [],
    
    # === SUPERVISOR (orchestration) ===
    "supervisor": ["ipc_server", "port_manager"],
    "backend": ["database", "websocket"],
}

# Phase assignments for organizational clarity
COMPONENT_PHASES: Dict[str, StartupPhase] = {
    "database": StartupPhase.INFRASTRUCTURE,
    "redis": StartupPhase.INFRASTRUCTURE,
    "ipc_server": StartupPhase.INFRASTRUCTURE,
    "port_manager": StartupPhase.INFRASTRUCTURE,
    
    "websocket": StartupPhase.NETWORKING,
    "http_server": StartupPhase.NETWORKING,
    
    "gcp_auth": StartupPhase.CLOUD,
    "cloudsql": StartupPhase.CLOUD,
    "gcp_vm": StartupPhase.CLOUD,
    "secret_manager": StartupPhase.CLOUD,
    
    "jarvis_prime": StartupPhase.EXTERNAL_REPOS,
    "reactor_core": StartupPhase.EXTERNAL_REPOS,
    
    "voice_system": StartupPhase.FEATURES,
    "biometric_auth": StartupPhase.FEATURES,
    "cost_tracker": StartupPhase.FEATURES,
    "hardware_optimizer": StartupPhase.FEATURES,
    
    "supervisor": StartupPhase.FINALIZATION,
    "backend": StartupPhase.FINALIZATION,
}


# =============================================================================
# STARTUP DAG
# =============================================================================

class StartupDAG:
    """
    Directed Acyclic Graph for startup dependencies.
    
    Provides:
    - Dependency validation (cycle detection)
    - Topological sort for initialization waves
    - Readiness checking for individual components
    - Shutdown order generation (reverse of startup)
    """
    
    _instance: Optional["StartupDAG"] = None
    
    def __new__(cls) -> "StartupDAG":
        """Singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the DAG."""
        if self._initialized:
            return
        
        self._dependencies = dict(COMPONENT_DEPENDENCIES)
        self._phases = dict(COMPONENT_PHASES)
        self._reverse_deps: Dict[str, Set[str]] = defaultdict(set)
        
        # Build reverse dependency map
        for component, deps in self._dependencies.items():
            for dep in deps:
                self._reverse_deps[dep].add(component)
        
        # Validate no cycles
        self._validate_no_cycles()
        
        self._initialized = True
    
    # =========================================================================
    # VALIDATION
    # =========================================================================
    
    def _validate_no_cycles(self) -> None:
        """Validate that the dependency graph has no cycles."""
        visited = set()
        rec_stack = set()
        
        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            
            for dep in self._dependencies.get(node, []):
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    logger.error(f"[StartupDAG] Cycle detected: {node} -> {dep}")
                    return True
            
            rec_stack.remove(node)
            return False
        
        for node in self._dependencies:
            if node not in visited:
                if dfs(node):
                    raise ValueError(f"Cycle detected in startup dependencies")
    
    # =========================================================================
    # DEPENDENCY QUERIES
    # =========================================================================
    
    def get_dependencies(self, component: str) -> Set[str]:
        """Get direct dependencies of a component."""
        return set(self._dependencies.get(component, []))
    
    def get_all_dependencies(self, component: str) -> Set[str]:
        """Get all transitive dependencies of a component."""
        result = set()
        queue = deque(self._dependencies.get(component, []))
        
        while queue:
            dep = queue.popleft()
            if dep not in result:
                result.add(dep)
                queue.extend(self._dependencies.get(dep, []))
        
        return result
    
    def get_dependents(self, component: str) -> Set[str]:
        """Get components that depend on this component."""
        return self._reverse_deps.get(component, set())
    
    def get_phase(self, component: str) -> Optional[StartupPhase]:
        """Get the startup phase for a component."""
        return self._phases.get(component)
    
    # =========================================================================
    # READINESS CHECKING
    # =========================================================================
    
    def are_dependencies_ready(self, component: str) -> bool:
        """
        Check if all dependencies of a component are ready.
        
        Uses the ComponentRegistry to check readiness.
        """
        registry = get_registry()
        deps = self.get_dependencies(component)
        
        for dep in deps:
            if not registry.is_available(dep):
                logger.debug(
                    f"[StartupDAG] {component} waiting for dependency: {dep}"
                )
                return False
        
        return True
    
    def get_blocking_dependencies(self, component: str) -> List[str]:
        """Get list of dependencies that are not yet ready."""
        registry = get_registry()
        deps = self.get_dependencies(component)
        
        return [dep for dep in deps if not registry.is_available(dep)]
    
    # =========================================================================
    # INITIALIZATION ORDER
    # =========================================================================
    
    def get_initialization_waves(
        self,
        components: Optional[Set[str]] = None,
    ) -> List[List[str]]:
        """
        Get components grouped into initialization waves.
        
        Each wave contains components that can be initialized in parallel.
        Components in wave N+1 depend on components in waves 0..N.
        
        Args:
            components: Optional subset of components to order.
                       If None, orders all known components.
        
        Returns:
            List of waves, each wave is a list of component names.
        
        Example:
            [
                ["database", "redis", "ipc_server"],  # Wave 0: no deps
                ["websocket", "cloudsql"],             # Wave 1: depends on wave 0
                ["jarvis_prime"],                      # Wave 2: depends on wave 1
                ["reactor_core"],                      # Wave 3: depends on wave 2
            ]
        """
        # Use specified components or all known
        target_components = components or set(self._dependencies.keys())
        
        # Calculate in-degree for each component
        in_degree: Dict[str, int] = defaultdict(int)
        for comp in target_components:
            for dep in self._dependencies.get(comp, []):
                if dep in target_components:
                    in_degree[comp] += 1
        
        # Start with components that have no dependencies
        waves: List[List[str]] = []
        remaining = set(target_components)
        
        while remaining:
            # Find all components with in-degree 0
            wave = [
                comp for comp in remaining
                if in_degree[comp] == 0
            ]
            
            if not wave:
                # This shouldn't happen if DAG is valid
                logger.error(f"[StartupDAG] Unable to order: {remaining}")
                break
            
            waves.append(sorted(wave))  # Sort for determinism
            
            # Remove these from remaining and update in-degrees
            for comp in wave:
                remaining.remove(comp)
                for dependent in self._reverse_deps.get(comp, set()):
                    if dependent in remaining:
                        in_degree[dependent] -= 1
        
        return waves
    
    def get_flat_initialization_order(
        self,
        components: Optional[Set[str]] = None,
    ) -> List[str]:
        """Get a flat list of components in initialization order."""
        waves = self.get_initialization_waves(components)
        return [comp for wave in waves for comp in wave]
    
    # =========================================================================
    # SHUTDOWN ORDER
    # =========================================================================
    
    def get_shutdown_order(
        self,
        components: Optional[Set[str]] = None,
    ) -> List[str]:
        """
        Get shutdown order (reverse of initialization).
        
        Components that depend on others should shut down first.
        """
        init_order = self.get_flat_initialization_order(components)
        return list(reversed(init_order))
    
    def get_shutdown_waves(
        self,
        components: Optional[Set[str]] = None,
    ) -> List[List[str]]:
        """Get shutdown waves (reverse of initialization waves)."""
        init_waves = self.get_initialization_waves(components)
        return list(reversed(init_waves))
    
    # =========================================================================
    # DYNAMIC REGISTRATION
    # =========================================================================
    
    def register_component(
        self,
        name: str,
        dependencies: Optional[List[str]] = None,
        phase: Optional[StartupPhase] = None,
    ) -> None:
        """
        Register a new component with dependencies.
        
        Useful for dynamic component discovery.
        """
        if name not in self._dependencies:
            self._dependencies[name] = dependencies or []
            if phase:
                self._phases[name] = phase
            
            # Update reverse deps
            for dep in (dependencies or []):
                self._reverse_deps[dep].add(name)
            
            logger.debug(f"[StartupDAG] Registered {name} with deps: {dependencies}")
    
    # =========================================================================
    # VISUALIZATION
    # =========================================================================
    
    def get_graph_summary(self) -> dict:
        """Get a summary of the dependency graph for logging/debugging."""
        waves = self.get_initialization_waves()
        
        return {
            "total_components": len(self._dependencies),
            "total_waves": len(waves),
            "waves": [
                {
                    "wave": i,
                    "components": wave,
                    "can_parallelize": len(wave),
                }
                for i, wave in enumerate(waves)
            ],
            "phases": {
                phase.value: [
                    comp for comp in self._dependencies
                    if self._phases.get(comp) == phase
                ]
                for phase in StartupPhase
            },
        }
    
    def log_initialization_plan(self) -> None:
        """Log the planned initialization order."""
        waves = self.get_initialization_waves()
        
        logger.info("[StartupDAG] Initialization Plan:")
        for i, wave in enumerate(waves):
            components = ", ".join(wave)
            parallel = "parallel" if len(wave) > 1 else "sequential"
            logger.info(f"  Wave {i}: [{components}] ({parallel})")


# =============================================================================
# SINGLETON ACCESSOR
# =============================================================================

def get_startup_dag() -> StartupDAG:
    """Get the global StartupDAG singleton."""
    return StartupDAG()


# =============================================================================
# MODULE EXPORTS
# =============================================================================

__all__ = [
    "StartupPhase",
    "StartupDAG",
    "get_startup_dag",
    "COMPONENT_DEPENDENCIES",
]
