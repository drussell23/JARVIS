"""Cloud Capacity Controller — single decision authority for cloud scaling.

Consumes MCP pressure signals from MemoryBudgetBroker and produces
CloudCapacityAction decisions with hysteresis and cooldowns.

The controller *decides*; GCPVMManager *executes*.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from backend.core.memory_types import (
    CloudCapacityAction,
    PressureTier,
)

logger = logging.getLogger(__name__)

# --- Configuration (all env-var-driven, no hardcoding) ---
_SPOT_CREATE_COOLDOWN_S = float(os.getenv("JARVIS_SPOT_CREATE_COOLDOWN_S", "120"))
_CRITICAL_SUSTAIN_S = float(os.getenv("JARVIS_CRITICAL_SUSTAIN_THRESHOLD_S", "30"))
_QUEUE_DEPTH_OFFLOAD = int(os.getenv("JARVIS_QUEUE_DEPTH_OFFLOAD", "8"))

# Module-level singleton for cross-module access
_instance: Optional["CloudCapacityController"] = None


class CloudCapacityController:
    """Single decision authority for cloud capacity actions.

    Registers as a MemoryBudgetBroker pressure observer.  On each
    pressure tier change it records the tier; callers invoke
    ``evaluate()`` to get the current recommended action.

    Parameters
    ----------
    broker : MemoryBudgetBroker
        The broker to register with as a pressure observer.
    """

    def __init__(self, broker: Any) -> None:
        global _instance
        self._broker = broker
        self._current_tier: PressureTier = PressureTier.OPTIMAL

        # Cooldown tracking (monotonic timestamps; -inf means "never")
        self._last_spot_create: float = float("-inf")

        # Sustained-critical tracking
        self._first_critical_at: Optional[float] = None

        # Spot availability
        self._spot_available: bool = True

        # Decision counter for telemetry
        self._total_decisions: int = 0
        self._decisions_by_action: Dict[str, int] = {}

        # Register with broker
        broker.register_pressure_observer(self._on_pressure_change)
        _instance = self
        logger.info("[CloudCapacity] Registered with MCP broker")

    async def _on_pressure_change(
        self, tier: PressureTier, snapshot: Any,
    ) -> None:
        """Callback from broker when pressure tier changes."""
        prev = self._current_tier
        self._current_tier = tier

        # Track sustained critical
        if tier >= PressureTier.CRITICAL:
            if self._first_critical_at is None:
                self._first_critical_at = time.monotonic()
        else:
            self._first_critical_at = None

        if prev != tier:
            logger.info(
                "[CloudCapacity] Pressure tier change: %s → %s",
                prev.name, tier.name,
            )

    def evaluate(
        self,
        tier: Optional[PressureTier] = None,
        queue_depth: int = 0,
        latency_violations: int = 0,
    ) -> CloudCapacityAction:
        """Evaluate current conditions and return recommended action.

        Parameters
        ----------
        tier : PressureTier, optional
            Override tier (uses broker-tracked tier if None).
        queue_depth : int
            Current inference request backlog.
        latency_violations : int
            Number of recent latency SLO violations.
        """
        if tier is None:
            tier = self._current_tier

        now = time.monotonic()
        action = self._decide(tier, queue_depth, latency_violations, now)

        self._total_decisions += 1
        self._decisions_by_action[action.value] = (
            self._decisions_by_action.get(action.value, 0) + 1
        )

        return action

    def _decide(
        self,
        tier: PressureTier,
        queue_depth: int,
        latency_violations: int,
        now: float,
    ) -> CloudCapacityAction:
        """Core decision logic with hysteresis and cooldowns."""
        # --- STAY_LOCAL: low pressure, short queue ---
        if tier <= PressureTier.ELEVATED and queue_depth < _QUEUE_DEPTH_OFFLOAD:
            return CloudCapacityAction.STAY_LOCAL

        # --- CRITICAL/EMERGENCY: consider Spot VM ---
        if tier >= PressureTier.CRITICAL:
            # Track sustained critical
            if self._first_critical_at is None:
                self._first_critical_at = now

            sustained = now - self._first_critical_at
            spot_cooldown_ok = (now - self._last_spot_create) >= _SPOT_CREATE_COOLDOWN_S

            if sustained >= _CRITICAL_SUSTAIN_S and spot_cooldown_ok:
                if self._spot_available:
                    return CloudCapacityAction.SPIN_SPOT
                else:
                    return CloudCapacityAction.FALLBACK_ONDEMAND

            # Critical but not sustained enough or on cooldown
            if queue_depth >= _QUEUE_DEPTH_OFFLOAD:
                return CloudCapacityAction.OFFLOAD_PARTIAL
            return CloudCapacityAction.FALLBACK_ONDEMAND

        # --- CONSTRAINED: degrade or offload ---
        if tier >= PressureTier.CONSTRAINED:
            if queue_depth >= _QUEUE_DEPTH_OFFLOAD or latency_violations > 0:
                return CloudCapacityAction.OFFLOAD_PARTIAL
            return CloudCapacityAction.DEGRADE_LOCAL

        # Fallback
        return CloudCapacityAction.STAY_LOCAL

    def record_spot_created(self) -> None:
        """Record that a Spot VM was just created (starts cooldown)."""
        self._last_spot_create = time.monotonic()

    def mark_spot_unavailable(self) -> None:
        """Mark Spot VMs as unavailable (preempted/quota exhausted)."""
        self._spot_available = False
        logger.warning("[CloudCapacity] Spot VMs marked unavailable")

    def mark_spot_available(self) -> None:
        """Mark Spot VMs as available again."""
        self._spot_available = True
        logger.info("[CloudCapacity] Spot VMs marked available")

    def get_stats(self) -> Dict[str, Any]:
        """Return telemetry stats."""
        return {
            "current_tier": self._current_tier.name,
            "total_decisions": self._total_decisions,
            "decisions_by_action": dict(self._decisions_by_action),
            "spot_available": self._spot_available,
            "sustained_critical_s": (
                time.monotonic() - self._first_critical_at
                if self._first_critical_at is not None
                else 0.0
            ),
        }


def get_cloud_capacity_controller() -> Optional[CloudCapacityController]:
    """Return the singleton CloudCapacityController, or None if not yet initialized."""
    return _instance
