"""
JARVIS Unified Readiness Predicate Module
==========================================

This module provides the unified predicate for determining when JARVIS
can be marked FULLY_READY.

Key logic:
- FULLY_READY iff (all critical components healthy) AND
  (optional components can be healthy, skipped, or errored)
- Critical components MUST be healthy to pass
- Optional components do NOT block readiness

Usage:
    from backend.core.readiness_predicate import (
        ReadinessPredicate,
        ReadinessResult,
    )

    predicate = ReadinessPredicate()
    component_states = {
        "backend": "healthy",
        "loading_server": "healthy",
        "preflight": "healthy",
        "jarvis_prime": "skipped",
    }
    result = predicate.evaluate(component_states)
    if result.is_fully_ready:
        print("JARVIS is FULLY_READY!")
    else:
        print(f"Blocked by: {result.blocking_components}")

Author: JARVIS Trinity - Readiness Integrity Fixes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set

from backend.core.readiness_config import (
    ComponentCriticality,
    ReadinessConfig,
    get_readiness_config,
)


# =============================================================================
# Status Classification Constants
# =============================================================================

# Statuses that indicate a component is healthy and ready
HEALTHY_STATUSES: FrozenSet[str] = frozenset({
    "healthy",
    "complete",
    "ready",
    "running",
    # Did its job and was stopped on purpose (the loading page, once the UI
    # took over): not a component that failed to come up.
    "retired",
})

# Statuses that indicate a component is skipped/unavailable (acceptable for optional)
SKIPPED_STATUSES: FrozenSet[str] = frozenset({
    "skipped",
    "unavailable",
})

# Statuses that indicate a component has an error (acceptable for optional, not critical)
ERROR_STATUSES: FrozenSet[str] = frozenset({
    "error",
    "failed",
    "degraded",
})

# All statuses acceptable for optional components (doesn't block readiness)
ACCEPTABLE_OPTIONAL_STATUSES: FrozenSet[str] = frozenset(
    HEALTHY_STATUSES | SKIPPED_STATUSES | ERROR_STATUSES
)


# =============================================================================
# ReadinessResult Dataclass
# =============================================================================

@dataclass
class ReadinessResult:
    """
    Result of evaluating system readiness.

    Attributes:
        is_fully_ready: True if system can be marked FULLY_READY
        message: Human-readable description of readiness state
        blocking_components: Critical components preventing readiness
        degraded_components: Optional components with errors (not blocking)
        skipped_components: Optional components that were skipped/unavailable
        healthy_components: All components that are healthy
        component_states: Original component states passed to evaluate()
    """
    is_fully_ready: bool
    message: str
    blocking_components: List[str] = field(default_factory=list)
    degraded_components: List[str] = field(default_factory=list)
    skipped_components: List[str] = field(default_factory=list)
    healthy_components: List[str] = field(default_factory=list)
    component_states: Dict[str, str] = field(default_factory=dict)


# =============================================================================
# ReadinessPredicate Class
# =============================================================================

class ReadinessPredicate:
    """
    Unified predicate for determining JARVIS system readiness.

    This predicate evaluates component states to determine if the system
    can be marked FULLY_READY.

    Rules:
    1. All critical components must be in a healthy status
    2. Missing critical components count as blocking
    3. Optional components with errors are degraded but don't block
    4. Optional components that are skipped/unavailable are acceptable

    Example:
        predicate = ReadinessPredicate()
        result = predicate.evaluate({
            "backend": "healthy",
            "loading_server": "healthy",
            "preflight": "healthy",
        })
        print(result.is_fully_ready)  # True
    """

    def __init__(self, config: Optional[ReadinessConfig] = None) -> None:
        """
        Initialize the readiness predicate.

        Args:
            config: Optional ReadinessConfig instance. If not provided,
                    the singleton configuration is used.
        """
        self._config = config or get_readiness_config()

    def evaluate(self, component_states: Dict[str, str]) -> ReadinessResult:
        """
        Evaluate system readiness based on component states.

        Args:
            component_states: Dictionary mapping component names to their
                             current status strings.

        Returns:
            ReadinessResult with detailed readiness information.
        """
        blocking_components: List[str] = []
        degraded_components: List[str] = []
        skipped_components: List[str] = []
        healthy_components: List[str] = []

        # Normalize component states (lowercase status values)
        normalized_states: Dict[str, str] = {
            name: status.lower() for name, status in component_states.items()
        }

        # Check all critical components
        for component in self._config.critical_components:
            status = normalized_states.get(component.lower())

            if status is None:
                # Missing critical component is blocking
                blocking_components.append(component)
            elif status in HEALTHY_STATUSES:
                healthy_components.append(component)
            else:
                # Any non-healthy status for critical component is blocking
                blocking_components.append(component)

        # Check all optional components
        for component in self._config.optional_components:
            status = normalized_states.get(component.lower())

            if status is None:
                # Missing optional component is skipped
                skipped_components.append(component)
            elif status in HEALTHY_STATUSES:
                healthy_components.append(component)
            elif status in SKIPPED_STATUSES:
                skipped_components.append(component)
            elif status in ERROR_STATUSES:
                degraded_components.append(component)
            # Other statuses for optional components are ignored

        # Check for any components in states that are not in config
        # These are treated as unknown and don't affect readiness
        known_components: Set[str] = {
            c.lower() for c in self._config.critical_components
        } | {
            c.lower() for c in self._config.optional_components
        }

        for component, status in normalized_states.items():
            if component.lower() not in known_components:
                # Unknown component - check if healthy
                if status in HEALTHY_STATUSES:
                    healthy_components.append(component)
                # Unknown components with errors don't block

        # Determine final readiness
        is_fully_ready = len(blocking_components) == 0

        # Build message
        message = self._build_message(
            is_fully_ready=is_fully_ready,
            blocking_components=blocking_components,
            degraded_components=degraded_components,
            skipped_components=skipped_components,
            healthy_components=healthy_components,
        )

        return ReadinessResult(
            is_fully_ready=is_fully_ready,
            message=message,
            blocking_components=blocking_components,
            degraded_components=degraded_components,
            skipped_components=skipped_components,
            healthy_components=healthy_components,
            component_states=component_states,
        )

    def is_component_ready(self, component: str, status: str) -> bool:
        """
        Check if a specific component status indicates readiness.

        This checks if the status is in HEALTHY_STATUSES, regardless
        of whether the component is critical or optional.

        Args:
            component: Component name (used for potential future logic)
            status: Status string to check

        Returns:
            True if the status indicates the component is ready.
        """
        normalized_status = status.lower()
        return normalized_status in HEALTHY_STATUSES

    def _build_message(
        self,
        is_fully_ready: bool,
        blocking_components: List[str],
        degraded_components: List[str],
        skipped_components: List[str],
        healthy_components: List[str],
    ) -> str:
        """
        Build a human-readable readiness message.

        Args:
            is_fully_ready: Whether the system is fully ready
            blocking_components: Components blocking readiness
            degraded_components: Components in degraded state
            skipped_components: Components that were skipped
            healthy_components: Components that are healthy

        Returns:
            Human-readable message describing readiness state.
        """
        if is_fully_ready:
            if degraded_components:
                return (
                    f"System ready (degraded mode): {len(healthy_components)} "
                    f"components healthy, {len(degraded_components)} components "
                    f"degraded ({', '.join(sorted(degraded_components))})"
                )
            elif skipped_components:
                return (
                    f"System ready: {len(healthy_components)} components healthy, "
                    f"{len(skipped_components)} components skipped"
                )
            else:
                return (
                    f"System fully ready: All {len(healthy_components)} "
                    f"components healthy"
                )
        else:
            blockers_str = ", ".join(sorted(blocking_components))
            return (
                f"System not ready: {len(blocking_components)} critical "
                f"component(s) blocking ({blockers_str})"
            )


# =============================================================================
# Module Exports
# =============================================================================

__all__ = [
    # Constants
    "HEALTHY_STATUSES",
    "SKIPPED_STATUSES",
    "ERROR_STATUSES",
    "ACCEPTABLE_OPTIONAL_STATUSES",
    # Classes
    "ReadinessResult",
    "ReadinessPredicate",
]
