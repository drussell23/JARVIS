"""Scenario S5: Cascading Failure.

Simulates a database failure that cascades to dependent services (api
hard-depends on db, cache soft-depends on db) while verifying that
the frontend remains isolated.

Task 9 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S05CascadingFailure:
    """DB failure cascades to api (hard dep) and cache (soft dep); frontend isolated."""

    name = "s05_cascading_failure"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 10.0,
        "verify": 10.0,
        "recover": 60.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Bring all four components to READY and set LOCAL_PRIME routing."""
        for component in ("db", "api", "cache", "frontend"):
            oracle.set_component_status(component, ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Inject db fault, cascade to api (FAILED) and cache (DEGRADED)."""
        await injector.inject(
            scope=FaultScope.COMPONENT,
            target="db",
            fault_type="crash",
            affected=frozenset({"db", "api", "cache"}),
            unaffected=frozenset({"frontend"}),
            convergence_deadline_s=60.0,
            trace_root_id=trace_root_id,
        )
        # db crashes
        oracle.set_component_status("db", ComponentStatus.FAILED)
        # api hard-depends on db -> FAILED
        oracle.set_component_status("api", ComponentStatus.FAILED)
        # cache soft-depends on db -> DEGRADED
        oracle.set_component_status("cache", ComponentStatus.DEGRADED)
        # frontend is unaffected -- no status change

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Verify cascade propagation and frontend isolation."""
        await oracle.wait_until(
            lambda: oracle.component_status("api").value == ComponentStatus.FAILED,
            deadline=5.0,
            description="api status == FAILED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("cache").value == ComponentStatus.DEGRADED,
            deadline=5.0,
            description="cache status == DEGRADED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("frontend").value == ComponentStatus.READY,
            deadline=5.0,
            description="frontend status == READY",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Restore all components to READY."""
        oracle.set_component_status("db", ComponentStatus.READY)
        oracle.set_component_status("api", ComponentStatus.READY)
        oracle.set_component_status("cache", ComponentStatus.READY)
        await oracle.wait_until(
            lambda: all(
                oracle.component_status(c).value == ComponentStatus.READY
                for c in ("db", "api", "cache", "frontend")
            ),
            deadline=5.0,
            description="all components READY",
        )
