"""Integration tests for Scenario S5: Cascading Failure.

Task 9 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, fault_isolation
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s05_cascading_failure import S05CascadingFailure
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS05CascadingFailure:
    """Verify db crash cascades to api/cache while frontend stays isolated."""

    def _build_harness(self):
        """Wire up all harness components for S05."""
        oracle = MockStateOracle()

        # Inner injector mock
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register(
            "fault_isolation",
            fault_isolation(
                affected=frozenset({"db", "api", "cache"}),
                unaffected=frozenset({"frontend"}),
            ),
        )

        scenario = S05CascadingFailure(oracle=oracle)

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

    async def test_hard_dep_fails_soft_dep_degrades(self) -> None:
        """api transitions to FAILED, cache to DEGRADED, and frontend has no changes during inject."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        # Gather inject-phase events (scenario_phase == "inject")
        inject_events = [
            ev for ev in result.event_log if ev.scenario_phase == "inject"
        ]

        # api must have a state_change to FAILED during inject
        api_failed_events = [
            ev
            for ev in inject_events
            if ev.event_type == "state_change"
            and ev.component == "api"
            and ev.new_value == "FAILED"
        ]
        assert len(api_failed_events) >= 1, (
            "Expected api -> FAILED state_change during inject phase"
        )

        # cache must have a state_change to DEGRADED during inject
        cache_degraded_events = [
            ev
            for ev in inject_events
            if ev.event_type == "state_change"
            and ev.component == "cache"
            and ev.new_value == "DEGRADED"
        ]
        assert len(cache_degraded_events) >= 1, (
            "Expected cache -> DEGRADED state_change during inject phase"
        )

        # frontend must NOT have any state_change events during inject
        frontend_changes = [
            ev
            for ev in inject_events
            if ev.event_type == "state_change" and ev.component == "frontend"
        ]
        assert len(frontend_changes) == 0, (
            f"Frontend should have no state changes during inject, "
            f"but found: {frontend_changes}"
        )
