"""StartupOrchestrator — Disease 10 lifecycle coordinator.

Disease 10 Wiring, Task 7.

Coordinates all Disease 10 startup subsystems into a single facade:

* :class:`PhaseGateCoordinator` — dependency-aware phase gates
* :class:`StartupBudgetPolicy` — tiered concurrency budget
* :class:`GCPReadinessLease` — lease-based VM readiness
* :class:`StartupRoutingPolicy` — deadline-based routing fallback
* :class:`BootInvariantChecker` — runtime invariant enforcement
* :class:`RoutingAuthorityFSM` — fail-closed authority state machine
* :class:`StartupEventBus` — event-sourced telemetry

The orchestrator exposes a unified API that the supervisor and TUI can
drive without needing to know about each subsystem's internal wiring.
"""

from __future__ import annotations

import enum
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from uuid import uuid4

from backend.core.boot_invariants import BootInvariantChecker, InvariantResult
from backend.core.gcp_readiness_lease import GCPReadinessLease
from backend.core.routing_authority_fsm import (
    AuthorityState,
    RoutingAuthorityFSM,
    TransitionResult,
)
from backend.core.startup_budget_policy import StartupBudgetPolicy
from backend.core.startup_concurrency_budget import (
    CompletedTask,
    HeavyTaskCategory,
    TaskSlot,
)
from backend.core.startup_config import StartupConfig
from backend.core.startup_phase_gate import (
    GateFailureReason,
    GateResult,
    GateSnapshot,
    GateStatus,
    PhaseGateCoordinator,
    StartupPhase,
)
from backend.core.startup_routing_policy import (
    BootRoutingDecision,
    FallbackReason,
    StartupRoutingPolicy,
)
from backend.core.startup_telemetry import StartupEvent, StartupEventBus

__all__ = [
    "OrchestratorState",
    "StartupOrchestrator",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Orchestrator state enum
# ---------------------------------------------------------------------------


class OrchestratorState(str, enum.Enum):
    """High-level lifecycle state of the orchestrator."""

    BOOTING = "BOOTING"
    HANDOFF_ELIGIBLE = "HANDOFF_ELIGIBLE"
    HANDED_OFF = "HANDED_OFF"
    DEGRADED = "DEGRADED"


# ---------------------------------------------------------------------------
# StartupOrchestrator
# ---------------------------------------------------------------------------


class StartupOrchestrator:
    """Unified coordinator for the Disease 10 startup sequence.

    Parameters
    ----------
    config:
        Declarative startup configuration produced by
        :func:`load_startup_config`.
    prober:
        Duck-typed readiness prober with ``probe_health``,
        ``probe_capabilities``, and ``probe_warm_model`` async methods.
    """

    def __init__(self, config: StartupConfig, prober: Any) -> None:
        self._config = config

        # --- Sub-components ---------------------------------------------------
        self._gate_coordinator = PhaseGateCoordinator()
        self._budget = StartupBudgetPolicy(config.budget)
        self._lease = GCPReadinessLease(
            prober=prober,
            ttl_seconds=config.lease_ttl_s,
        )
        self._routing_policy = StartupRoutingPolicy(
            gcp_deadline_s=config.gcp_deadline_s,
            cloud_fallback_enabled=config.cloud_fallback_enabled,
        )
        self._invariant_checker = BootInvariantChecker()
        self._fsm = RoutingAuthorityFSM(journal_path=None)
        self._event_bus = StartupEventBus(trace_id=uuid4().hex)

        # --- Internal state ---------------------------------------------------
        self._current_phase: Optional[str] = None
        self._hybrid_router: Any = None
        self._prime_router: Any = None

    # -- Properties ------------------------------------------------------------

    @property
    def authority_state(self) -> AuthorityState:
        """Current routing authority FSM state."""
        return self._fsm.state

    @property
    def current_phase(self) -> Optional[str]:
        """Name of the last successfully resolved phase, or None."""
        return self._current_phase

    @property
    def lease_valid(self) -> bool:
        """Whether the GCP readiness lease is currently valid."""
        return self._lease.is_valid

    @property
    def budget_active_count(self) -> int:
        """Number of currently held budget slots."""
        return self._budget.active_count

    @property
    def budget_history(self) -> List[CompletedTask]:
        """Completed budget task history."""
        return self._budget.history

    @property
    def event_history(self) -> List[StartupEvent]:
        """All telemetry events emitted by this orchestrator."""
        return self._event_bus.event_history

    # -- Phase gate methods ----------------------------------------------------

    async def resolve_phase(
        self,
        name: str,
        detail: str = "",
    ) -> GateResult:
        """Resolve a startup phase gate by name.

        Converts *name* to a :class:`StartupPhase` enum member, delegates
        to the gate coordinator, signals the budget policy, and emits a
        telemetry event.
        """
        phase = StartupPhase[name]
        result = self._gate_coordinator.resolve(phase, detail)

        if result.status == GateStatus.PASSED:
            self._current_phase = name
            self._budget.signal_phase_reached(name)

        await self._emit(
            event_type="phase_gate",
            detail={"status": result.status.value, "phase": name},
            phase=name,
        )
        return result

    async def skip_phase(
        self,
        name: str,
        reason: str = "",
    ) -> GateResult:
        """Skip a startup phase gate by name."""
        phase = StartupPhase[name]
        result = self._gate_coordinator.skip(phase, reason)

        await self._emit(
            event_type="phase_gate",
            detail={"status": result.status.value, "phase": name},
            phase=name,
        )
        return result

    async def fail_phase(
        self,
        name: str,
        reason: GateFailureReason,
        detail: str = "",
    ) -> GateResult:
        """Explicitly fail a startup phase gate by name."""
        phase = StartupPhase[name]
        result = self._gate_coordinator.fail(phase, reason, detail)

        await self._emit(
            event_type="phase_gate",
            detail={"status": result.status.value, "phase": name},
            phase=name,
        )
        return result

    def gate_snapshot(self) -> Dict[StartupPhase, GateSnapshot]:
        """Return a point-in-time snapshot of all phase gates."""
        return self._gate_coordinator.snapshot()

    # -- GCP lease methods -----------------------------------------------------

    async def acquire_gcp_lease(self, host: str, port: int) -> bool:
        """Acquire the GCP readiness lease via 3-step handshake.

        On success, signals the routing policy that GCP is ready.
        """
        success = await self._lease.acquire(
            host,
            port,
            timeout_per_step=self._config.probe_timeout_s,
        )

        if success:
            self._routing_policy.signal_gcp_ready(host, port)

        await self._emit(
            event_type="gcp_lease",
            detail={
                "action": "acquire",
                "success": success,
                "host": host,
                "port": port,
            },
        )
        return success

    def revoke_gcp_lease(self, reason: str) -> None:
        """Immediately revoke the GCP readiness lease."""
        self._lease.revoke(reason)
        self._routing_policy.signal_gcp_revoked(reason)

    # -- Routing ---------------------------------------------------------------

    def routing_decide(self) -> Tuple[BootRoutingDecision, FallbackReason]:
        """Compute the current boot routing decision."""
        return self._routing_policy.decide()

    # -- Hybrid router ---------------------------------------------------------

    def set_hybrid_router(self, router: Any) -> None:
        """Store a reference to the hybrid router for authority handoff."""
        self._hybrid_router = router

    def set_prime_router(self, router: Any) -> None:
        """Store a reference to the prime router for mirror-mode control."""
        self._prime_router = router

    # -- Authority handoff -----------------------------------------------------

    async def attempt_handoff(self) -> TransitionResult:
        """Attempt to hand off routing authority from boot policy to hybrid router.

        Checks that the CORE_READY gate has passed, then drives the FSM
        through begin_handoff -> complete_handoff.  On success, activates
        the hybrid router and (optionally) enables mirror mode on the
        prime router.
        """
        # Build begin-handoff guards
        core_ready_status = self._gate_coordinator.status(StartupPhase.CORE_READY)
        core_ready_passed = core_ready_status == GateStatus.PASSED

        begin_guards = {
            "core_ready_passed": core_ready_passed,
            "contracts_valid": True,
            "invariants_clean": len(
                [r for r in self._invariant_checker.check_all(
                    self._build_invariant_state()
                ) if not r.passed]
            ) == 0,
        }

        result = self._fsm.begin_handoff(begin_guards)
        if not result.success:
            await self._emit(
                event_type="authority_handoff",
                detail={
                    "action": "begin_handoff",
                    "success": False,
                    "failed_guard": result.failed_guard,
                },
            )
            return result

        # Build complete-handoff guards
        complete_guards = {
            "contracts_valid": True,
            "invariants_clean": True,
            "hybrid_router_ready": self._hybrid_router is not None,
            "lease_or_local_ready": self._lease.is_valid,
            "readiness_contract_passed": True,
            "no_in_flight_requests": True,
        }

        result = self._fsm.complete_handoff(complete_guards)
        if result.success and self._fsm.state == AuthorityState.HYBRID_ACTIVE:
            if self._hybrid_router is not None:
                self._hybrid_router.set_active(True)
            if self._prime_router is not None and hasattr(self._prime_router, "set_mirror_mode"):
                self._prime_router.set_mirror_mode(True)

        await self._emit(
            event_type="authority_handoff",
            detail={
                "action": "complete_handoff",
                "success": result.success,
                "to_state": result.to_state,
            },
        )
        return result

    # -- Recovery --------------------------------------------------------------

    async def handle_lease_loss(self, cause: str) -> None:
        """Handle loss of the GCP readiness lease.

        Revokes the lease, and if the FSM is in HYBRID_ACTIVE, rolls back
        to BOOT_POLICY_ACTIVE and deactivates the hybrid router.
        """
        self.revoke_gcp_lease(cause)

        if self._fsm.state == AuthorityState.HYBRID_ACTIVE:
            self._fsm.rollback(cause)
            if self._hybrid_router is not None:
                self._hybrid_router.set_active(False)

        await self._emit(
            event_type="lease_loss",
            detail={"cause": cause, "authority_state": self._fsm.state.value},
        )

    # -- Invariant checking ----------------------------------------------------

    def check_invariants(
        self,
        overrides: Optional[Dict[str, Any]] = None,
    ) -> List[InvariantResult]:
        """Run all boot invariants against the current orchestrator state.

        Parameters
        ----------
        overrides:
            Optional dict of state keys to override for testing or
            simulation purposes.
        """
        state = self._build_invariant_state()
        if overrides:
            state.update(overrides)
        return self._invariant_checker.check_all(state)

    # -- Budget ----------------------------------------------------------------

    @asynccontextmanager
    async def budget_acquire(
        self,
        category: HeavyTaskCategory,
        name: str,
        timeout: Optional[float] = None,
    ) -> AsyncIterator[TaskSlot]:
        """Acquire a tiered concurrency budget slot.

        Delegates to the internal :class:`StartupBudgetPolicy`.
        """
        async with self._budget.acquire(category, name, timeout) as slot:
            yield slot

    # -- Internal helpers ------------------------------------------------------

    def _build_invariant_state(self) -> Dict[str, Any]:
        """Build the state dict expected by :class:`BootInvariantChecker`."""
        lease_valid = self._lease.is_valid
        return {
            "routing_target": "gcp" if lease_valid else None,
            "gcp_handshake_complete": lease_valid,
            "gcp_offload_active": lease_valid,
            "gcp_node_ip": self._lease.host if lease_valid else None,
            "gcp_node_reachable": lease_valid,
            "authority_holder": self._fsm.authority_holder,
            "hybrid_router_active": self._fsm.state == AuthorityState.HYBRID_ACTIVE,
            "boot_policy_active": self._fsm.state == AuthorityState.BOOT_POLICY_ACTIVE,
            "fallback_chain_valid": True,
        }

    async def _emit(
        self,
        event_type: str,
        detail: Dict[str, Any],
        phase: Optional[str] = None,
    ) -> None:
        """Create and emit a telemetry event through the event bus."""
        event = self._event_bus.create_event(
            event_type=event_type,
            detail=detail,
            phase=phase,
            authority_state=self._fsm.state.value,
        )
        await self._event_bus.emit(event)
