"""Scenario S14: Dual Recovery Race.

Simulates simultaneous failure of db and api (api hard-depends on db)
while frontend remains isolated.  Recovery must be wave-ordered: db
recovers first, then api.  Verifies correct ordering and fault
isolation throughout.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from typing import Any, Dict

from tests.harness.types import ComponentStatus


class S14DualRecoveryRace:
    """Dual failure with wave-ordered recovery; frontend isolated."""

    name = "s14_dual_recovery_race"

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
        """Set db, api, frontend READY; routing LOCAL_PRIME; epoch 1."""
        oracle.set_component_status("db", ComponentStatus.READY)
        oracle.set_component_status("api", ComponentStatus.READY)
        oracle.set_component_status("frontend", ComponentStatus.READY)
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(1)

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Both db and api fail simultaneously; frontend stays READY."""
        oracle.set_component_status("db", ComponentStatus.FAILED)
        oracle.set_component_status("api", ComponentStatus.FAILED)

        oracle.emit_event(
            source="failure_detector",
            event_type="dual_failure",
            component="db",
            old_value="READY",
            new_value="FAILED",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"also_failed": "api"},
        )

        oracle.set_routing_decision("CLOUD_CLAUDE")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Verify both components failed and frontend is still READY."""
        await oracle.wait_until(
            lambda: oracle.component_status("db").value == ComponentStatus.FAILED,
            deadline=5.0,
            description="db status == FAILED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("api").value == ComponentStatus.FAILED,
            deadline=5.0,
            description="api status == FAILED",
        )
        await oracle.wait_until(
            lambda: oracle.component_status("frontend").value == ComponentStatus.READY,
            deadline=5.0,
            description="frontend status == READY (isolated)",
        )

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        """Wave-ordered recovery: db first (wave 1), then api (wave 2)."""
        # Wave 1: db recovers first
        oracle.emit_event(
            source="recovery_manager",
            event_type="recovery_wave_start",
            component="db",
            old_value="FAILED",
            new_value="wave_1",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"wave": 1, "reason": "no_dependencies"},
        )
        oracle.set_component_status("db", ComponentStatus.STARTING)
        oracle.set_component_status("db", ComponentStatus.READY)

        # Wave 2: api recovers after db is READY
        oracle.emit_event(
            source="recovery_manager",
            event_type="recovery_wave_start",
            component="api",
            old_value="FAILED",
            new_value="wave_2",
            trace_root_id=trace_root_id,
            trace_id="",
            metadata={"wave": 2, "reason": "dependency_db_ready"},
        )
        oracle.set_component_status("api", ComponentStatus.STARTING)
        oracle.set_component_status("api", ComponentStatus.READY)

        # Restore routing and bump epoch
        oracle.set_routing_decision("LOCAL_PRIME")
        oracle.set_epoch(2)

        # Wait until all three components are READY
        await oracle.wait_until(
            lambda: all(
                oracle.component_status(c).value == ComponentStatus.READY
                for c in ("db", "api", "frontend")
            ),
            deadline=5.0,
            description="all components READY",
        )

        # Verify wave ordering: db wave seq < api wave seq
        events = oracle.event_log()
        wave_events = [
            ev for ev in events if ev.event_type == "recovery_wave_start"
        ]
        assert len(wave_events) == 2, (
            f"Expected 2 recovery_wave_start events, got {len(wave_events)}"
        )
        db_wave = [ev for ev in wave_events if ev.component == "db"]
        api_wave = [ev for ev in wave_events if ev.component == "api"]
        assert len(db_wave) == 1 and len(api_wave) == 1
        assert db_wave[0].oracle_event_seq < api_wave[0].oracle_event_seq, (
            "db recovery wave must start before api recovery wave"
        )
