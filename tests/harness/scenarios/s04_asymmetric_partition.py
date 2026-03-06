"""Scenario S4: Asymmetric Network Partition -> Split-Brain Prevention.

Simulates an asymmetric network partition where JARVIS cannot reach prime
but the backend remains healthy.  The partition detector marks prime as
LOST (not FAILED, since the partition is ambiguous -- prime may still be
running).  Routing switches to CLOUD_CLAUDE while the backend remains
unaffected, preventing split-brain.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S04AsymmetricPartition:
    """Asymmetric partition isolates prime; backend stays healthy."""

    name = "s04_asymmetric_partition"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 15.0,
        "verify": 15.0,
        "recover": 30.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases -- each receives (oracle, injector, trace_root_id) from the
    # HarnessOrchestrator.
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Set prime READY, backend READY, routing LOCAL_PRIME, epoch 1."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_component_status("backend", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Inject asymmetric partition: prime is LOST, backend unaffected."""
        # 1. Inject transport fault via the scoped injector
        await injector.inject(
            scope=FaultScope.TRANSPORT,
            target="prime",
            fault_type="asymmetric_partition",
            affected=frozenset({"prime"}),
            unaffected=frozenset({"backend"}),
            trace_root_id=trace_root_id,
        )

        # 2. Partition detector marks prime as LOST (ambiguous -- not FAILED)
        oracle.set_component_status("prime", ComponentStatus.LOST)

        # 3. Emit partition_detected event
        oracle.emit_event(
            source="partition_detector",
            event_type="partition_detected",
            component="prime",
            old_value=None,
            new_value="LOST",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={
                "type": "asymmetric",
                "direction": "jarvis_to_prime_blocked",
            },
        )

        # 4. Route to cloud
        oracle.set_routing_decision("CLOUD_CLAUDE")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Confirm prime is LOST, routing is CLOUD_CLAUDE, backend is READY."""
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.LOST,
            deadline=5.0,
            description="prime status == LOST",
        )
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE",
        )

        # Backend must remain unaffected
        backend_obs = oracle.component_status("backend")
        assert backend_obs.value == ComponentStatus.READY, (
            f"Backend should be READY, got {backend_obs.value}"
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Heal partition: prime recovers, epoch increments."""
        # 1. Restore prime
        oracle.set_component_status("prime", ComponentStatus.READY)

        # 2. Emit partition_healed event
        oracle.emit_event(
            source="partition_detector",
            event_type="partition_healed",
            component="prime",
            old_value="LOST",
            new_value="READY",
            trace_root_id=trace_root_id,
            trace_id="",
        )

        # 3. Restore routing and increment epoch
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(oracle.epoch() + 1)

        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME",
        )
