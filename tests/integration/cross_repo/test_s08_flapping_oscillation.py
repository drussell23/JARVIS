"""Integration tests for Scenario S8: Rapid Failure/Recovery Oscillation (Flapping).

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, single_routing_target
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s08_flapping_oscillation import S08FlappingOscillation
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS08FlappingOscillation:
    """Verify flap damping engages after threshold and routing holds in fallback."""

    def _build_harness(self):
        """Wire up all harness components for S08."""
        oracle = MockStateOracle()

        # Inner injector mock (not used by S08, but orchestrator requires it)
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("single_routing_target", single_routing_target())

        scenario = S08FlappingOscillation(oracle=oracle)

        config = Config()
        orchestrator = HarnessOrchestrator(
            mode="mock",
            oracle=oracle,
            injector=injector,
            invariants=invariants,
            config=config,
        )

        return orchestrator, scenario, oracle

    async def test_scenario_passes(self) -> None:
        """Full scenario run completes with no violations."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

    async def test_damping_engages_after_threshold(self) -> None:
        """flap_damping_engaged fires after the 3rd flap_detected but before the 4th."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        events = result.event_log

        # Find all flap_detected events
        flap_events = [
            ev for ev in events if ev.event_type == "flap_detected"
        ]
        assert len(flap_events) == 5, (
            f"Expected 5 flap_detected events, got {len(flap_events)}"
        )

        # Find flap_damping_engaged event
        damping_events = [
            ev for ev in events if ev.event_type == "flap_damping_engaged"
        ]
        assert len(damping_events) >= 1, "Expected flap_damping_engaged event"

        damping_seq = damping_events[0].oracle_event_seq

        # Sort flap events by seq
        flap_seqs = sorted(ev.oracle_event_seq for ev in flap_events)

        # Damping must come after the 3rd flap_detected
        assert damping_seq > flap_seqs[2], (
            f"Damping (seq={damping_seq}) must come after 3rd flap "
            f"(seq={flap_seqs[2]})"
        )

        # Damping must come before the 4th flap_detected
        assert damping_seq < flap_seqs[3], (
            f"Damping (seq={damping_seq}) must come before 4th flap "
            f"(seq={flap_seqs[3]})"
        )
