"""Scenario S1: Prime Crash Fallback.

Simulates a LOCAL_PRIME crash (SIGKILL) and verifies the system falls
back to CLOUD_CLAUDE, then recovers when prime comes back online.

Task 8 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus, FaultScope


class S01PrimeCrashFallback:
    """Prime process crashes; routing falls back to CLOUD_CLAUDE then recovers."""

    name = "s01_prime_crash_fallback"

    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 10.0,
        "verify": 10.0,
        "recover": 30.0,
    }

    def __init__(self, prime_process: Any, oracle: Any) -> None:
        self._prime = prime_process
        self._oracle = oracle

    # ------------------------------------------------------------------
    # Phases — each receives (oracle, injector, trace_root_id) from the
    # HarnessOrchestrator.
    # ------------------------------------------------------------------

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Start prime and establish LOCAL_PRIME routing at epoch 1."""
        await self._prime.start()
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Kill prime, inject fault, and simulate router switching to CLOUD_CLAUDE."""
        await self._prime.kill()
        await injector.inject(
            scope=FaultScope.PROCESS,
            target="prime",
            fault_type="sigkill",
            affected=frozenset({"prime"}),
            unaffected=frozenset(),
            trace_root_id=trace_root_id,
        )
        oracle.set_routing_decision("CLOUD_CLAUDE")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Wait until routing is CLOUD_CLAUDE and prime status is FAILED."""
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "CLOUD_CLAUDE",
            deadline=5.0,
            description="routing == CLOUD_CLAUDE",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("prime").value == ComponentStatus.FAILED,
            deadline=5.0,
            description="prime status == FAILED",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Bring prime back, restore LOCAL_PRIME routing, bump epoch."""
        oracle.set_component_status("prime", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(oracle.epoch() + 1)
        await oracle.wait_until(
            lambda: oracle.routing_decision().value == "LOCAL_PRIME",
            deadline=5.0,
            description="routing == LOCAL_PRIME",
        )
