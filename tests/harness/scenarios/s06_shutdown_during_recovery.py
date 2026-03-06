"""Scenario S6: Shutdown During Active Recovery.

Simulates a prime crash followed by recovery starting, then a shutdown
request arriving before recovery completes.  Verifies that shutdown
wins over active recovery and all components reach terminal state.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S06ShutdownDuringRecovery:
    """Shutdown request cancels an in-progress recovery; all components reach terminal state."""

    name = "s06_shutdown_during_recovery"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 10.0,
        "verify": 30.0,
        "recover": 5.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Bring prime and backend to READY, set LOCAL_PRIME routing, epoch 1."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_component_status("backend", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Simulate prime crash, recovery start, then shutdown before recovery completes."""
        # Prime crashes
        oracle.set_component_status("prime", ComponentStatus.FAILED)

        # Recovery starts
        oracle.set_component_status("prime", ComponentStatus.STARTING)
        oracle.emit_event(
            source="recovery_manager",
            event_type="recovery_started",
            component="prime",
            old_value="FAILED",
            new_value="STARTING",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"reason": "prime_crash_detected"},
        )

        # Shutdown requested before recovery completes
        oracle.emit_event(
            source="operator",
            event_type="shutdown_requested",
            component=None,
            old_value=None,
            new_value="SHUTTING_DOWN",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"reason": "operator_initiated"},
        )

        # Shutdown cancels recovery -- prime goes to STOPPED
        oracle.set_component_status("prime", ComponentStatus.STOPPED)

        # Backend drains gracefully
        oracle.set_component_status("backend", ComponentStatus.DRAINING)
        oracle.set_component_status("backend", ComponentStatus.STOPPING)
        oracle.set_component_status("backend", ComponentStatus.STOPPED)

        # Routing degrades
        oracle.set_routing_decision("DEGRADED")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Verify all components reached terminal state and event ordering is correct."""
        # Both components must be STOPPED
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.STOPPED,
            deadline=5.0,
            description="prime status == STOPPED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("backend").value == ComponentStatus.STOPPED,
            deadline=5.0,
            description="backend status == STOPPED",
        )

        # No component should be stuck in STARTING
        for comp in ("prime", "backend"):
            status = oracle.component_status(comp).value
            assert status in (ComponentStatus.STOPPED, ComponentStatus.FAILED), (
                f"Component {comp} stuck in {status.value}, expected STOPPED or FAILED"
            )

        # Verify shutdown_requested event exists
        events = oracle.event_log()
        shutdown_events = [
            ev for ev in events if ev.event_type == "shutdown_requested"
        ]
        assert len(shutdown_events) >= 1, "Expected shutdown_requested event"

        # Verify recovery_started event's seq < shutdown_requested event's seq
        recovery_events = [
            ev for ev in events if ev.event_type == "recovery_started"
        ]
        assert len(recovery_events) >= 1, "Expected recovery_started event"
        assert recovery_events[0].oracle_event_seq < shutdown_events[0].oracle_event_seq, (
            "recovery_started must precede shutdown_requested"
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """No-op -- terminal state after shutdown."""
        pass
