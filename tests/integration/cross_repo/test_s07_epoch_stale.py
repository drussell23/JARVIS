"""Integration tests for Scenario S7: Epoch Stale.

Task 10 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, epoch_monotonic
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s07_epoch_stale import S07EpochStale
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS07EpochStale:
    """Verify stale-epoch write is rejected without mutating state."""

    def _build_harness(self):
        """Wire up all harness components for S07."""
        oracle = MockStateOracle()

        # Inner injector mock (not used by S07, but orchestrator requires it)
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic())

        scenario = S07EpochStale(oracle=oracle)

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

    async def test_stale_write_rejected(self) -> None:
        """At least one stale_epoch_rejected event is present in the log."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        stale_events = [
            ev
            for ev in result.event_log
            if ev.event_type == "stale_epoch_rejected"
        ]
        assert len(stale_events) >= 1, (
            "Expected at least one stale_epoch_rejected event in event log"
        )
