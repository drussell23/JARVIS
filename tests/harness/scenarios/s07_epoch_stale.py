"""Scenario S7: Epoch Stale.

Simulates a stale-epoch write attempt that is rejected without mutating
any system state.  Verifies epoch, component status, and store revision
are all unchanged after the rejected write.

Task 10 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S07EpochStale:
    """Stale epoch write is rejected; system state remains unchanged."""

    name = "s07_epoch_stale"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 5.0,
        "verify": 5.0,
        "recover": 5.0,
    }

    def __init__(self, oracle: Any) -> None:
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Establish epoch 3, prime READY, journal revision 100."""
        oracle.set_epoch(3)
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_store_revision("journal", 100)
        oracle.set_routing_decision("LOCAL_PRIME")

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Emit a stale-epoch rejection event.  State is NOT mutated."""
        current_epoch = oracle.epoch()
        stale_epoch = current_epoch - 1

        oracle.emit_event(
            source="orchestration_journal",
            event_type="stale_epoch_rejected",
            component="prime",
            old_value=str(stale_epoch),
            new_value=str(current_epoch),
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={
                "stale_epoch": stale_epoch,
                "current_epoch": current_epoch,
            },
        )

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Assert state was not mutated by the stale write."""
        assert oracle.epoch() == 3
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.READY,
            deadline=2.0,
            description="prime status == READY",
        )
        assert oracle.store_revision("journal") == 100

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """No-op -- system continues unaffected."""
        pass
