"""BootInvariantChecker — runtime invariant enforcement with causal tracing.

Disease 10 — Startup Sequencing, Task 4.

Provides a set of boot-time invariants that guard against unsafe state
combinations during the startup sequence.  Each invariant produces an
``InvariantResult`` with an optional ``CausalTrace`` when a violation is
detected, enabling deterministic root-cause analysis of boot failures.
"""

from __future__ import annotations

import enum
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum: severity levels
# ---------------------------------------------------------------------------


@enum.unique
class InvariantSeverity(str, enum.Enum):
    """Severity of an invariant violation."""

    CRITICAL = "critical"
    WARNING = "warning"
    ADVISORY = "advisory"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CausalTrace:
    """Describes the causal chain that led to an invariant violation."""

    trigger: str
    decision: str
    outcome: str
    timestamp: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class InvariantResult:
    """Outcome of evaluating a single boot invariant."""

    invariant_id: str
    description: str
    passed: bool
    severity: InvariantSeverity
    trace: Optional[CausalTrace] = None
    detail: str = ""


# ---------------------------------------------------------------------------
# Type alias for invariant check functions
# ---------------------------------------------------------------------------

_InvariantFn = Callable[[Dict[str, Any]], InvariantResult]


# ---------------------------------------------------------------------------
# BootInvariantChecker
# ---------------------------------------------------------------------------


class BootInvariantChecker:
    """Runs registered boot-time invariants against a state snapshot.

    All four invariants are registered at construction time.  Call
    :meth:`check_all` with a state dict to evaluate every invariant and
    receive a list of :class:`InvariantResult` objects.
    """

    _VALID_ROUTING_TARGETS = frozenset({None, "local", "gcp", "cloud"})

    def __init__(self) -> None:
        self._invariants: List[_InvariantFn] = [
            self._inv1_no_routing_without_handshake,
            self._inv2_no_offload_without_reachable,
            self._inv3_no_dual_authority,
            self._inv4_no_dead_end_fallback,
        ]

    # -- Public API ----------------------------------------------------------

    def check_all(self, state: Dict[str, Any]) -> List[InvariantResult]:
        """Evaluate all registered invariants against *state*.

        Parameters
        ----------
        state:
            A snapshot of boot-relevant system state.  Expected keys are
            documented per invariant.

        Returns
        -------
        List[InvariantResult]
            One result per invariant, in registration order.
        """
        results: List[InvariantResult] = []
        for inv_fn in self._invariants:
            result = inv_fn(state)
            if not result.passed:
                logger.warning(
                    "Boot invariant VIOLATED: %s — %s [detail=%s]",
                    result.invariant_id,
                    result.description,
                    result.detail or "(none)",
                )
            results.append(result)
        return results

    # -- Invariant implementations -------------------------------------------

    @staticmethod
    def _inv1_no_routing_without_handshake(
        state: Dict[str, Any],
    ) -> InvariantResult:
        """INV-1: No routing to GCP without a completed handshake."""
        routing_target = state.get("routing_target")
        handshake_complete = state.get("gcp_handshake_complete", False)

        if routing_target != "gcp":
            # Not routing to GCP — invariant is trivially satisfied.
            return InvariantResult(
                invariant_id="INV-1",
                description="No routing to GCP without completed handshake",
                passed=True,
                severity=InvariantSeverity.CRITICAL,
            )

        if not handshake_complete:
            return InvariantResult(
                invariant_id="INV-1",
                description="No routing to GCP without completed handshake",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                trace=CausalTrace(
                    trigger=f"routing_target={routing_target!r} but gcp_handshake_complete=False",
                    decision="Routing to GCP requires a completed handshake",
                    outcome="Invariant violated — traffic would reach an unverified node",
                ),
                detail="GCP handshake has not completed",
            )

        return InvariantResult(
            invariant_id="INV-1",
            description="No routing to GCP without completed handshake",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    @staticmethod
    def _inv2_no_offload_without_reachable(
        state: Dict[str, Any],
    ) -> InvariantResult:
        """INV-2: No offload_active without a reachable node."""
        offload_active = state.get("gcp_offload_active", False)
        node_ip = state.get("gcp_node_ip")
        node_reachable = state.get("gcp_node_reachable", False)

        if not offload_active:
            return InvariantResult(
                invariant_id="INV-2",
                description="No offload active without reachable node",
                passed=True,
                severity=InvariantSeverity.CRITICAL,
            )

        if node_ip is None or not node_reachable:
            reason_parts: List[str] = []
            if node_ip is None:
                reason_parts.append("gcp_node_ip is None")
            if not node_reachable:
                reason_parts.append("gcp_node_reachable is False")
            reason = "; ".join(reason_parts)

            return InvariantResult(
                invariant_id="INV-2",
                description="No offload active without reachable node",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                trace=CausalTrace(
                    trigger=f"gcp_offload_active=True but {reason}",
                    decision="Offload requires an IP-addressed and reachable GCP node",
                    outcome="Invariant violated — offload traffic has no viable destination",
                ),
                detail=reason,
            )

        return InvariantResult(
            invariant_id="INV-2",
            description="No offload active without reachable node",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    @staticmethod
    def _inv3_no_dual_authority(
        state: Dict[str, Any],
    ) -> InvariantResult:
        """INV-3: No dual authority — routing_target must be a known value."""
        routing_target = state.get("routing_target")

        if routing_target not in BootInvariantChecker._VALID_ROUTING_TARGETS:
            return InvariantResult(
                invariant_id="INV-3",
                description="No dual authority — routing_target must be a known value",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                trace=CausalTrace(
                    trigger=f"routing_target={routing_target!r} is not in {sorted(str(t) for t in BootInvariantChecker._VALID_ROUTING_TARGETS)}",
                    decision="Routing must target exactly one of: None, local, gcp, cloud",
                    outcome="Invariant violated — ambiguous or unknown routing authority",
                ),
                detail=f"Unknown routing_target: {routing_target!r}",
            )

        return InvariantResult(
            invariant_id="INV-3",
            description="No dual authority — routing_target must be a known value",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )

    @staticmethod
    def _inv4_no_dead_end_fallback(
        state: Dict[str, Any],
    ) -> InvariantResult:
        """INV-4: No dead-end fallback — at least one inference path must exist."""
        local_loaded = state.get("local_model_loaded", False)
        gcp_handshake = state.get("gcp_handshake_complete", False)
        cloud_enabled = state.get("cloud_fallback_enabled", False)

        if not local_loaded and not gcp_handshake and not cloud_enabled:
            return InvariantResult(
                invariant_id="INV-4",
                description="No dead-end fallback — at least one inference path required",
                passed=False,
                severity=InvariantSeverity.CRITICAL,
                trace=CausalTrace(
                    trigger=(
                        "local_model_loaded=False, "
                        "gcp_handshake_complete=False, "
                        "cloud_fallback_enabled=False"
                    ),
                    decision="At least one of local, GCP, or cloud must be available",
                    outcome="Invariant violated — no inference path available, system is dead-ended",
                ),
                detail="All three inference paths are unavailable",
            )

        return InvariantResult(
            invariant_id="INV-4",
            description="No dead-end fallback — at least one inference path required",
            passed=True,
            severity=InvariantSeverity.CRITICAL,
        )
