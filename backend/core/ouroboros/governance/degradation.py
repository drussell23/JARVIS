# backend/core/ouroboros/governance/degradation.py
"""
Degradation Controller — 4-Mode Autonomy State Machine
========================================================

Manages transitions between 4 degradation modes based on resource pressure,
GCP availability, and rollback history::

    FULL_AUTONOMY       All tiers active, GCP available, all gates green
    REDUCED_AUTONOMY    GCP unavailable or elevated pressure -> safe_auto local only
    READ_ONLY_PLANNING  Critical pressure or incident mode -> analyze + plan only
    EMERGENCY_STOP      Emergency pressure or 3+ rollbacks/hour -> all autonomy halted

EMERGENCY_STOP requires explicit human reset (:meth:`explicit_reset`).
All other modes auto-recover when pressure drops.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.core.ouroboros.governance.resource_monitor import (
    PressureLevel,
    ResourceSnapshot,
)

logger = logging.getLogger("Ouroboros.Degradation")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class DegradationMode(enum.IntEnum):
    """Autonomy degradation modes (ordered by restriction level)."""

    FULL_AUTONOMY = 0
    REDUCED_AUTONOMY = 1
    READ_ONLY_PLANNING = 2
    EMERGENCY_STOP = 3


class DegradationReason(enum.Enum):
    """Why a degradation transition occurred."""

    PRESSURE_ELEVATED = "pressure_elevated"
    PRESSURE_CRITICAL = "pressure_critical"
    PRESSURE_EMERGENCY = "pressure_emergency"
    GCP_UNAVAILABLE = "gcp_unavailable"
    ROLLBACK_THRESHOLD = "rollback_threshold_exceeded"
    PRESSURE_RECOVERED = "pressure_recovered"
    EXPLICIT_RESET = "explicit_reset"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ModeTransition:
    """Record of a degradation mode transition."""

    from_mode: DegradationMode
    to_mode: DegradationMode
    reason: DegradationReason
    timestamp: float = field(default_factory=time.time)
    details: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ROLLBACK_WINDOW_S: float = 3600.0  # 1 hour
ROLLBACK_THRESHOLD: int = 3


# ---------------------------------------------------------------------------
# DegradationController
# ---------------------------------------------------------------------------


class DegradationController:
    """4-mode degradation state machine for Ouroboros autonomy."""

    def __init__(self) -> None:
        self._mode: DegradationMode = DegradationMode.FULL_AUTONOMY
        self._gcp_available: bool = True
        self._rollback_timestamps: List[float] = []
        self._transition_history: List[ModeTransition] = []

    @property
    def mode(self) -> DegradationMode:
        """Current degradation mode."""
        return self._mode

    @property
    def safe_auto_allowed(self) -> bool:
        """Whether SAFE_AUTO tasks are permitted."""
        return self._mode in (
            DegradationMode.FULL_AUTONOMY,
            DegradationMode.REDUCED_AUTONOMY,
        )

    @property
    def heavy_tasks_allowed(self) -> bool:
        """Whether heavy tasks (multi-file, cross-repo, codegen) are permitted."""
        return self._mode == DegradationMode.FULL_AUTONOMY

    def set_gcp_available(self, available: bool) -> None:
        """Update GCP availability status."""
        self._gcp_available = available

    def record_rollback(self) -> None:
        """Record a rollback event for threshold tracking."""
        self._rollback_timestamps.append(time.time())

    async def evaluate(
        self, snapshot: ResourceSnapshot
    ) -> Optional[ModeTransition]:
        """Evaluate resource state and transition mode if needed."""
        # EMERGENCY_STOP is sticky — requires explicit reset
        if self._mode == DegradationMode.EMERGENCY_STOP:
            return None

        # Determine target mode from signals
        target = self._compute_target_mode(snapshot)

        if target == self._mode:
            return None

        reason = self._classify_reason(snapshot, target)
        transition = ModeTransition(
            from_mode=self._mode,
            to_mode=target,
            reason=reason,
            details={
                "ram_percent": snapshot.ram_percent,
                "cpu_percent": snapshot.cpu_percent,
                "pressure": snapshot.overall_pressure.name,
                "gcp_available": self._gcp_available,
            },
        )

        previous = self._mode
        self._mode = target
        self._transition_history.append(transition)

        logger.info(
            "Degradation: %s -> %s (reason=%s)",
            previous.name, target.name, reason.value,
        )

        return transition

    def _compute_target_mode(self, snapshot: ResourceSnapshot) -> DegradationMode:
        """Determine target mode from all signals."""
        pressure = snapshot.overall_pressure

        # Check rollback threshold
        now = time.time()
        recent_rollbacks = [
            t for t in self._rollback_timestamps
            if now - t < ROLLBACK_WINDOW_S
        ]
        self._rollback_timestamps = recent_rollbacks

        if len(recent_rollbacks) >= ROLLBACK_THRESHOLD:
            return DegradationMode.EMERGENCY_STOP

        # Pressure-based mode
        if pressure >= PressureLevel.EMERGENCY:
            return DegradationMode.EMERGENCY_STOP
        elif pressure >= PressureLevel.CRITICAL:
            return DegradationMode.READ_ONLY_PLANNING
        elif pressure >= PressureLevel.ELEVATED or not self._gcp_available:
            return DegradationMode.REDUCED_AUTONOMY
        else:
            return DegradationMode.FULL_AUTONOMY

    def _classify_reason(
        self,
        snapshot: ResourceSnapshot,
        target: DegradationMode,
    ) -> DegradationReason:
        """Classify the reason for a mode transition."""
        if target == DegradationMode.FULL_AUTONOMY:
            return DegradationReason.PRESSURE_RECOVERED
        if target == DegradationMode.EMERGENCY_STOP:
            now = time.time()
            recent = [t for t in self._rollback_timestamps if now - t < ROLLBACK_WINDOW_S]
            if len(recent) >= ROLLBACK_THRESHOLD:
                return DegradationReason.ROLLBACK_THRESHOLD
            return DegradationReason.PRESSURE_EMERGENCY
        if target == DegradationMode.READ_ONLY_PLANNING:
            return DegradationReason.PRESSURE_CRITICAL
        if not self._gcp_available:
            return DegradationReason.GCP_UNAVAILABLE
        return DegradationReason.PRESSURE_ELEVATED

    async def explicit_reset(self) -> None:
        """Explicitly reset from EMERGENCY_STOP to FULL_AUTONOMY."""
        if self._mode == DegradationMode.EMERGENCY_STOP:
            transition = ModeTransition(
                from_mode=DegradationMode.EMERGENCY_STOP,
                to_mode=DegradationMode.FULL_AUTONOMY,
                reason=DegradationReason.EXPLICIT_RESET,
            )
            self._mode = DegradationMode.FULL_AUTONOMY
            self._transition_history.append(transition)
            self._rollback_timestamps.clear()
            logger.info("Degradation: EMERGENCY_STOP -> FULL_AUTONOMY (explicit reset)")

    def get_transition_history(self) -> List[ModeTransition]:
        """Return all mode transitions."""
        return list(self._transition_history)
