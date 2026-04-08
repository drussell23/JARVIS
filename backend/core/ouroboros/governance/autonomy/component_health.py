"""backend/core/ouroboros/governance/autonomy/component_health.py

Standalone component health tracking types for L3 SafetyNet.

Extracted and adapted from legacy ``system_states.py`` with modern design:
- Uses ``time.monotonic_ns()`` for all timestamps (consistent with C+ architecture)
- Tracks per-component state, health score, error count, and transition history
- Bounded history to prevent unbounded memory growth
- Pure in-memory state tracking -- no async needed
"""
from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Ouroboros.ComponentHealth")

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ComponentState(enum.Enum):
    """Lifecycle state of a tracked component."""

    NOT_INITIALIZED = enum.auto()
    READY = enum.auto()
    ACTIVE = enum.auto()
    BUSY = enum.auto()
    ERROR = enum.auto()
    OFFLINE = enum.auto()


class TransitionReason(str, enum.Enum):
    """Reason for a component state transition."""

    USER_REQUEST = "user_request"
    AUTOMATIC = "automatic"
    ERROR = "error"
    RECOVERY = "recovery"
    TIMEOUT = "timeout"
    COMPLETION = "completion"
    EXTERNAL_TRIGGER = "external_trigger"


# ---------------------------------------------------------------------------
# Valid transitions state machine
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: Dict[ComponentState, frozenset] = {
    ComponentState.NOT_INITIALIZED: frozenset({
        ComponentState.READY,
        ComponentState.ACTIVE,   # Components that boot directly into active (e.g. health probe)
        ComponentState.ERROR,
        ComponentState.OFFLINE,
    }),
    ComponentState.READY: frozenset({
        ComponentState.ACTIVE,
        ComponentState.BUSY,
        ComponentState.ERROR,
        ComponentState.OFFLINE,
    }),
    ComponentState.ACTIVE: frozenset({
        ComponentState.READY,
        ComponentState.BUSY,
        ComponentState.ERROR,
        ComponentState.OFFLINE,
    }),
    ComponentState.BUSY: frozenset({
        ComponentState.READY,
        ComponentState.ACTIVE,
        ComponentState.ERROR,
        ComponentState.OFFLINE,
    }),
    ComponentState.ERROR: frozenset({
        ComponentState.READY,
        ComponentState.ACTIVE,
        ComponentState.NOT_INITIALIZED,
        ComponentState.OFFLINE,
    }),
    ComponentState.OFFLINE: frozenset({
        ComponentState.NOT_INITIALIZED,
        ComponentState.READY,
        ComponentState.ERROR,
    }),
}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ComponentStatus:
    """Current status snapshot of a single tracked component."""

    name: str
    state: ComponentState
    last_update_ns: int = field(default_factory=time.monotonic_ns)
    health_score: float = 1.0  # 0.0 to 1.0
    error_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_healthy(self) -> bool:
        """Component is healthy when in a good state with adequate score."""
        return (
            self.state in (ComponentState.READY, ComponentState.ACTIVE)
            and self.health_score > 0.7
        )

    @property
    def needs_attention(self) -> bool:
        """Component needs attention when in error, low score, or many errors."""
        return (
            self.state == ComponentState.ERROR
            or self.health_score < 0.5
            or self.error_count > 5
        )


@dataclass(frozen=True)
class StateTransition:
    """Immutable record of a component state transition."""

    component_name: str
    from_state: ComponentState
    to_state: ComponentState
    reason: TransitionReason
    timestamp_ns: int = field(default_factory=time.monotonic_ns)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# ComponentHealthTracker
# ---------------------------------------------------------------------------


class ComponentHealthTracker:
    """Tracks health of multiple named components for L3 SafetyNet.

    Thread-safe for single-writer scenarios (L3 is single-threaded).
    History is bounded to ``max_history`` entries to prevent unbounded growth.
    """

    def __init__(self, max_history: int = 200) -> None:
        self._components: Dict[str, ComponentStatus] = {}
        self._history: List[StateTransition] = []
        self._max_history: int = max_history

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        initial_state: ComponentState = ComponentState.NOT_INITIALIZED,
    ) -> None:
        """Register a component for tracking.

        If the component is already registered, this is a no-op.
        """
        if name in self._components:
            return
        self._components[name] = ComponentStatus(
            name=name,
            state=initial_state,
        )

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def update(
        self,
        name: str,
        state: ComponentState,
        health_score: Optional[float] = None,
        reason: TransitionReason = TransitionReason.AUTOMATIC,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Update a component's state. Records transition in history.

        Auto-registers the component if it was not previously registered.
        Validates the transition against ``VALID_TRANSITIONS``; invalid
        transitions are logged as warnings but still applied (best-effort).
        """
        if name not in self._components:
            self.register(name)

        comp = self._components[name]
        old_state = comp.state

        # Validate transition
        allowed = VALID_TRANSITIONS.get(old_state, frozenset())
        if state != old_state and state not in allowed:
            logger.warning(
                "Invalid transition for %s: %s -> %s (allowed: %s). Applying anyway.",
                name,
                old_state.name,
                state.name,
                [s.name for s in allowed],
            )

        # Record transition
        transition = StateTransition(
            component_name=name,
            from_state=old_state,
            to_state=state,
            reason=reason,
            metadata=metadata or {},
        )
        self._history.append(transition)

        # Trim history if over limit
        if len(self._history) > self._max_history:
            excess = len(self._history) - self._max_history
            self._history = self._history[excess:]

        # Apply updates
        comp.state = state
        comp.last_update_ns = time.monotonic_ns()
        if health_score is not None:
            comp.health_score = max(0.0, min(1.0, health_score))
        if metadata is not None:
            comp.metadata.update(metadata)
        if state == ComponentState.ERROR:
            comp.error_count += 1

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_status(self, name: str) -> Optional[ComponentStatus]:
        """Get current status of a named component, or None if unknown."""
        return self._components.get(name)

    def get_unhealthy(self) -> List[ComponentStatus]:
        """Return all components where ``is_healthy`` is False."""
        return [c for c in self._components.values() if not c.is_healthy]

    def get_needs_attention(self) -> List[ComponentStatus]:
        """Return all components where ``needs_attention`` is True."""
        return [c for c in self._components.values() if c.needs_attention]

    def get_aggregate_health(self) -> float:
        """Return average health_score across all components (0.0 if none)."""
        if not self._components:
            return 0.0
        total = sum(c.health_score for c in self._components.values())
        return total / len(self._components)

    def get_history(
        self,
        name: Optional[str] = None,
        limit: int = 50,
    ) -> List[StateTransition]:
        """Return transition history, optionally filtered by component name.

        Results are ordered oldest-first (insertion order), limited to
        the most recent ``limit`` entries after filtering.
        """
        if name is not None:
            filtered = [t for t in self._history if t.component_name == name]
        else:
            filtered = list(self._history)
        return filtered[-limit:]

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Snapshot of all components for telemetry/logging."""
        components: Dict[str, Any] = {}
        for name, comp in self._components.items():
            components[name] = {
                "state": comp.state.name,
                "health_score": comp.health_score,
                "error_count": comp.error_count,
                "is_healthy": comp.is_healthy,
                "needs_attention": comp.needs_attention,
                "last_update_ns": comp.last_update_ns,
                "metadata": comp.metadata,
            }
        return {"components": components}
