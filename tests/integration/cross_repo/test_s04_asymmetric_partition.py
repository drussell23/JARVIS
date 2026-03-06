"""Integration tests for Scenario S4: Asymmetric Network Partition.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import (
    InvariantRegistry,
    fault_isolation,
    single_routing_target,
)
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scenarios.s04_asymmetric_partition import S04AsymmetricPartition
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.state_oracle import MockStateOracle
from tests.harness.types import ComponentStatus


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS04AsymmetricPartition:
    """Verify asymmetric partition -> LOST (not FAILED) -> split-brain prevention."""

    def _build_harness(self):
        """Wire up all harness components for S04."""
        oracle = MockStateOracle()

        # Inner injector mock
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("single_routing_target", single_routing_target())
        invariants.register(
            "fault_isolation",
            fault_isolation(
                affected=frozenset({"prime"}),
                unaffected=frozenset({"backend"}),
            ),
        )

        scenario = S04AsymmetricPartition(oracle=oracle)

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

    async def test_partition_uses_lost_not_failed(self) -> None:
        """During inject phase, prime transitions to LOST, never to FAILED."""
        orchestrator, scenario, oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        # Find state_change events for prime during the inject phase
        inject_state_changes = [
            ev
            for ev in result.event_log
            if ev.event_type == "state_change"
            and ev.component == "prime"
            and ev.scenario_phase == "inject"
        ]
        assert len(inject_state_changes) >= 1, (
            "No state_change events for prime during inject phase"
        )

        # Verify prime went to LOST, not FAILED
        lost_transitions = [
            ev for ev in inject_state_changes if ev.new_value == "LOST"
        ]
        failed_transitions = [
            ev for ev in inject_state_changes if ev.new_value == "FAILED"
        ]

        assert len(lost_transitions) >= 1, (
            "Prime should transition to LOST during partition, "
            f"but found transitions: {[ev.new_value for ev in inject_state_changes]}"
        )
        assert len(failed_transitions) == 0, (
            "Prime should NOT transition to FAILED during partition "
            "(asymmetric partition is ambiguous -- use LOST)"
        )
