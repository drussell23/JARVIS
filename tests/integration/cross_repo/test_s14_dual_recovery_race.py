"""Integration tests for Scenario S14: Dual Recovery Race.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import (
    InvariantRegistry,
    epoch_monotonic,
    fault_isolation,
)
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s14_dual_recovery_race import S14DualRecoveryRace
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS14DualRecoveryRace:
    """Verify wave-ordered recovery and fault isolation for dual failure."""

    def _build_harness(self):
        """Wire up all harness components for S14."""
        oracle = MockStateOracle()

        # Inner injector mock (not used by S14, but orchestrator requires it)
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
                affected=frozenset({"db", "api"}),
                unaffected=frozenset({"frontend"}),
            ),
        )
        invariants.register("epoch_monotonic", epoch_monotonic())

        scenario = S14DualRecoveryRace(oracle=oracle)

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

    async def test_wave_ordered_recovery(self) -> None:
        """db recovery_wave_start has lower seq than api recovery_wave_start."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        events = result.event_log

        # Find recovery_wave_start events
        wave_events = [
            ev for ev in events if ev.event_type == "recovery_wave_start"
        ]
        assert len(wave_events) == 2, (
            f"Expected 2 recovery_wave_start events, got {len(wave_events)}"
        )

        db_waves = [ev for ev in wave_events if ev.component == "db"]
        api_waves = [ev for ev in wave_events if ev.component == "api"]

        assert len(db_waves) == 1, "Expected exactly 1 db recovery_wave_start"
        assert len(api_waves) == 1, "Expected exactly 1 api recovery_wave_start"

        assert db_waves[0].oracle_event_seq < api_waves[0].oracle_event_seq, (
            f"db wave (seq={db_waves[0].oracle_event_seq}) must start before "
            f"api wave (seq={api_waves[0].oracle_event_seq})"
        )
