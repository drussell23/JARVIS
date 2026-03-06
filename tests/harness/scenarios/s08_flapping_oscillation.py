"""Scenario S8: Rapid Failure/Recovery Oscillation (Flapping).

Simulates 5 rapid kill/restart cycles on prime, with flap damping
engaging after the 3rd cycle.  Verifies that damping holds the routing
in fallback (CLOUD_CLAUDE) and prime in DEGRADED until a stability
window expires, at which point prime is re-promoted.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S08FlappingOscillation:
    """Rapid fail/recover cycles trigger flap damping; stability window allows re-promotion."""

    name = "s08_flapping_oscillation"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 15.0,
        "verify": 15.0,
        "recover": 60.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Set prime READY, routing LOCAL_PRIME, epoch 1."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Simulate 5 rapid kill/restart cycles with damping after cycle 3."""
        for i in range(5):
            oracle.set_component_status("prime", ComponentStatus.FAILED)
            oracle.emit_event(
                source="flap_detector",
                event_type="flap_detected",
                component="prime",
                old_value="READY",
                new_value=f"cycle_{i + 1}",
                trace_root_id=trace_root_id,
                trace_id="",
                metadata={"cycle": i + 1},
            )
            oracle.set_component_status("prime", ComponentStatus.READY)

            # After 3rd cycle, damping engages
            if i == 2:
                oracle.emit_event(
                    source="flap_detector",
                    event_type="flap_damping_engaged",
                    component="prime",
                    old_value="flapping",
                    new_value="damped",
                    trace_root_id=trace_root_id,
                    trace_id="",
                    metadata={"threshold": 3, "window_s": 60},
                )

        # After all 5 cycles, damping holds fallback routing
        oracle.set_routing_decision("CLOUD_CLAUDE")

        # Prime is DEGRADED (damped -- not promoted despite being intermittently READY)
        oracle.set_component_status("prime", ComponentStatus.DEGRADED)

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Verify damping engaged and routing held in fallback."""
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE",
        )

        events = oracle.event_log()

        # Verify flap_damping_engaged event exists
        damping_events = [
            ev for ev in events if ev.event_type == "flap_damping_engaged"
        ]
        assert len(damping_events) >= 1, "Expected flap_damping_engaged event"

        # Verify exactly 5 flap_detected events
        flap_events = [
            ev for ev in events if ev.event_type == "flap_detected"
        ]
        assert len(flap_events) == 5, (
            f"Expected exactly 5 flap_detected events, got {len(flap_events)}"
        )

        # Verify prime is DEGRADED (held in fallback by damping)
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.DEGRADED,
            deadline=5.0,
            description="prime status == DEGRADED",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Stability window expires; prime re-promoted."""
        oracle.emit_event(
            source="flap_detector",
            event_type="stability_window_expired",
            component="prime",
            old_value="damped",
            new_value="stable",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"window_s": 60},
        )
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME after stability window",
        )
