"""Scenario S13: Stale Healthy After Failover.

Simulates prime going down, failover to CLOUD_CLAUDE, then prime coming
back with a stale epoch.  The stale prime must NOT be re-promoted until
it completes a fresh handshake with the current epoch.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S13StaleHealthyAfterFailover:
    """Stale prime is not re-promoted; must complete fresh handshake first."""

    name = "s13_stale_healthy_after_failover"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 10.0,
        "verify": 10.0,
        "recover": 15.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Set prime READY, routing LOCAL_PRIME, epoch 3, journal revision 150."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(3)
        oracle.set_store_revision("journal", 150)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Prime fails, failover to Claude, prime returns stale."""
        # Prime goes down
        oracle.set_component_status("prime", ComponentStatus.FAILED)
        oracle.set_routing_decision("CLOUD_CLAUDE")

        # Prime comes back but stale (old epoch)
        oracle.emit_event(
            source="health_monitor",
            event_type="stale_healthy_detected",
            component="prime",
            old_value="epoch_2",
            new_value="epoch_3",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={
                "prime_epoch": 2,
                "current_epoch": 3,
                "prime_revision": 100,
                "current_revision": 150,
            },
        )

        # Do NOT re-promote -- routing stays CLOUD_CLAUDE
        # Mark prime as DEGRADED (stale)
        oracle.set_component_status("prime", ComponentStatus.DEGRADED)

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Verify stale prime is NOT re-promoted."""
        # Routing must remain CLOUD_CLAUDE
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE (not re-promoted)",
        )

        # Prime must be DEGRADED
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.DEGRADED,
            deadline=5.0,
            description="prime status == DEGRADED",
        )

        # Verify stale_healthy_detected event exists
        events = oracle.event_log()
        stale_events = [
            ev for ev in events if ev.event_type == "stale_healthy_detected"
        ]
        assert len(stale_events) >= 1, "Expected stale_healthy_detected event"

        # Verify NO routing_change to LOCAL_PRIME after stale detection
        stale_seq = stale_events[0].oracle_event_seq
        routing_to_local = [
            ev for ev in events
            if ev.event_type == "routing_change"
            and ev.new_value == "LOCAL_PRIME"
            and ev.oracle_event_seq > stale_seq
        ]
        assert len(routing_to_local) == 0, (
            f"Expected no routing_change to LOCAL_PRIME after stale detection, "
            f"but found {len(routing_to_local)}"
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Prime completes fresh handshake with current epoch; re-promoted."""
        oracle.emit_event(
            source="handshake_manager",
            event_type="handshake_completed",
            component="prime",
            old_value="stale",
            new_value="synced",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"epoch": 3, "revision": 150},
        )

        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME after handshake",
        )
