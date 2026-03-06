"""Scenario S2: Prime Latency Spike -> Circuit Breaker Opens.

Simulates a latency spike on the prime transport layer that triggers
a circuit breaker to open, routing traffic to CLOUD_CLAUDE.  Recovery
uses a hysteresis pattern: three consecutive health checks must pass
before the breaker closes and traffic returns to LOCAL_PRIME.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S02PrimeLatencyCircuitBreaker:
    """Latency spike triggers circuit breaker; hysteresis recovery."""

    name = "s02_prime_latency_circuit_breaker"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 15.0,
        "verify": 15.0,
        "recover": 45.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases -- each receives (oracle, injector, trace_root_id) from the
    # HarnessOrchestrator.
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Set prime READY, routing LOCAL_PRIME, epoch 1."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Inject transport latency fault and simulate circuit breaker opening."""
        # 1. Inject latency fault via the scoped injector
        await injector.inject(
            scope=FaultScope.TRANSPORT,
            target="prime",
            fault_type="latency_5s",
            affected=frozenset({"prime"}),
            unaffected=frozenset(),
            trace_root_id=trace_root_id,
        )

        # 2. Circuit breaker detects latency -> prime degrades
        oracle.set_component_status("prime", ComponentStatus.DEGRADED)

        # 3. Breaker opens -> route to cloud
        oracle.set_routing_decision("CLOUD_CLAUDE")

        # 4. Emit breaker_opened event
        oracle.emit_event(
            source="circuit_breaker",
            event_type="breaker_opened",
            component="prime",
            old_value=None,
            new_value="open",
            trace_root_id=trace_root_id,
            trace_id="",
        )

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Confirm routing switched and prime is degraded."""
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.DEGRADED,
            deadline=5.0,
            description="prime status == DEGRADED",
        )

        # Verify breaker_opened event exists
        events = oracle.event_log()
        breaker_events = [
            ev for ev in events if ev.event_type == "breaker_opened"
        ]
        assert len(breaker_events) >= 1, "No breaker_opened event found in event log"

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Simulate hysteresis recovery: 3 consecutive health checks before closing breaker."""
        # Health check 1 of 3
        oracle.emit_event(
            source="circuit_breaker",
            event_type="health_check_passed",
            component="prime",
            old_value=None,
            new_value="1_of_3",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"consecutive": 1},
        )

        # Health check 2 of 3
        oracle.emit_event(
            source="circuit_breaker",
            event_type="health_check_passed",
            component="prime",
            old_value=None,
            new_value="2_of_3",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"consecutive": 2},
        )

        # Health check 3 of 3
        oracle.emit_event(
            source="circuit_breaker",
            event_type="health_check_passed",
            component="prime",
            old_value=None,
            new_value="3_of_3",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"consecutive": 3},
        )

        # Breaker closes after sustained health
        oracle.emit_event(
            source="circuit_breaker",
            event_type="breaker_closed",
            component="prime",
            old_value="open",
            new_value="closed",
            trace_root_id=trace_root_id,
            trace_id="",
        )

        # Restore prime and routing
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")

        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME",
        )
