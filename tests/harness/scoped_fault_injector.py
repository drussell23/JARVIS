"""ScopedFaultInjector -- scope-aware fault injection with re-entrant guards.

Wraps an inner fault injector with:
- Re-entrant guard per target (REJECT / REPLACE / STACK composition policies)
- Pre-fault baseline capture via the StateOracle
- Provenance event emission on inject
- Isolation checking on revert (unaffected components must not be FAILED/LOST)
- Convergence waiting on revert (affected components must reach READY/DEGRADED)

Task 5 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict, FrozenSet
from uuid import uuid4

from tests.harness.types import (
    ComponentStatus,
    FaultComposition,
    FaultHandle,
    FaultScope,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ReentrantFaultError(Exception):
    """Raised when injecting overlapping fault on same target without composition policy."""


class FaultIsolationError(Exception):
    """Raised when fault leaks beyond declared scope."""


# ---------------------------------------------------------------------------
# ScopedFaultInjector
# ---------------------------------------------------------------------------

class ScopedFaultInjector:
    """Scope-aware fault injector with re-entrant guards and isolation checks.

    Parameters
    ----------
    inner:
        The underlying fault injector.  Must expose an async
        ``inject_failure(target, fault_type)`` method returning an object
        with an async ``revert()`` coroutine.
    oracle:
        A StateOracle (or MockStateOracle) used for baseline capture,
        provenance events, isolation checks, and convergence waits.
    """

    def __init__(self, inner: Any, oracle: Any) -> None:
        self._inner = inner
        self._oracle = oracle
        self._active_by_target: Dict[str, FaultHandle] = {}

    # ------------------------------------------------------------------
    # Inject
    # ------------------------------------------------------------------

    async def inject(
        self,
        *,
        scope: FaultScope,
        target: str,
        fault_type: str,
        affected: FrozenSet[str],
        unaffected: FrozenSet[str],
        composition: FaultComposition = FaultComposition.REJECT,
        convergence_deadline_s: float = 30.0,
        trace_root_id: str = "",
        **kwargs: Any,
    ) -> FaultHandle:
        """Inject a fault with scope guards, baseline capture, and provenance.

        Returns a :class:`FaultHandle` that can later be passed to
        :meth:`revert`.
        """
        # 1. Re-entrant guard
        if target in self._active_by_target:
            if composition == FaultComposition.REJECT:
                raise ReentrantFaultError(
                    f"Fault already active on target {target!r} "
                    f"(fault_id={self._active_by_target[target].fault_id}). "
                    f"Use composition=REPLACE or STACK to override."
                )
            elif composition == FaultComposition.REPLACE:
                existing = self._active_by_target.pop(target)
                await existing.revert()
            # STACK: just proceed

        # 2. Capture pre-fault baseline for affected components
        pre_fault_baseline: Dict[str, str] = {}
        for name in affected:
            obs = self._oracle.component_status(name)
            # obs.value is a ComponentStatus enum; get its string value
            status_enum = obs.value
            pre_fault_baseline[name] = status_enum.value

        # 3. Delegate to inner injector
        inner_result = await self._inner.inject_failure(target, fault_type)

        # 4. Build FaultHandle
        fault_id = uuid4().hex[:12]

        handle = FaultHandle(
            fault_id=fault_id,
            scope=scope,
            target=target,
            affected_components=affected,
            unaffected_components=unaffected,
            pre_fault_baseline=pre_fault_baseline,
            convergence_deadline_s=convergence_deadline_s,
            revert=inner_result.revert,
        )

        # 5. Emit provenance event
        self._oracle.emit_event(
            source="scoped_fault_injector",
            event_type="fault_injected",
            component=target,
            old_value=pre_fault_baseline.get(target),
            new_value=fault_type,
            trace_root_id=trace_root_id,
            trace_id=fault_id,
            metadata={
                "fault_id": fault_id,
                "scope": scope.value,
                "affected": sorted(affected),
                "unaffected": sorted(unaffected),
                "composition": composition.value,
            },
        )

        # 6. Store in active map
        self._active_by_target[target] = handle

        # 7. Return handle
        return handle

    # ------------------------------------------------------------------
    # Revert
    # ------------------------------------------------------------------

    async def revert(self, handle: FaultHandle) -> None:
        """Revert a previously injected fault, checking isolation and convergence.

        Raises
        ------
        FaultIsolationError
            If any component declared as *unaffected* is in FAILED or LOST state.
        """
        # 1. Call the inner revert
        await handle.revert()

        # 2. Isolation check: unaffected components must not be FAILED or LOST
        leaked = ComponentStatus.FAILED, ComponentStatus.LOST
        for name in handle.unaffected_components:
            obs = self._oracle.component_status(name)
            if obs.value in leaked:
                raise FaultIsolationError(
                    f"Fault on {handle.target!r} leaked to unaffected component "
                    f"{name!r} (status={obs.value.value})"
                )

        # 3. Convergence: wait until all affected components are READY or DEGRADED
        converged = {ComponentStatus.READY, ComponentStatus.DEGRADED}

        def _all_converged() -> bool:
            return all(
                self._oracle.component_status(name).value in converged
                for name in handle.affected_components
            )

        await self._oracle.wait_until(
            predicate=_all_converged,
            deadline=handle.convergence_deadline_s,
            description=(
                f"convergence after reverting fault {handle.fault_id} "
                f"on {handle.target!r}"
            ),
        )

        # 4. Remove from active map
        self._active_by_target.pop(handle.target, None)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def active_faults(self) -> Dict[str, FaultHandle]:
        """Currently active faults keyed by target."""
        return dict(self._active_by_target)
