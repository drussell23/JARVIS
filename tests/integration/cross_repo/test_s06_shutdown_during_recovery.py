"""Integration tests for Scenario S6: Shutdown During Active Recovery.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, terminal_is_final
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s06_shutdown_during_recovery import S06ShutdownDuringRecovery
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS06ShutdownDuringRecovery:
    """Verify shutdown wins over active recovery and all components reach terminal state."""

    def _build_harness(self):
        """Wire up all harness components for S06."""
        oracle = MockStateOracle()

        # Inner injector mock (not used by S06, but orchestrator requires it)
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("terminal_is_final", terminal_is_final())

        scenario = S06ShutdownDuringRecovery(oracle=oracle)

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

    async def test_shutdown_wins_over_recovery(self) -> None:
        """shutdown_requested comes after recovery_started, and prime ends STOPPED not STARTING."""
        orchestrator, scenario, oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        events = result.event_log

        # Find recovery_started and shutdown_requested events
        recovery_events = [
            ev for ev in events if ev.event_type == "recovery_started"
        ]
        shutdown_events = [
            ev for ev in events if ev.event_type == "shutdown_requested"
        ]

        assert len(recovery_events) >= 1, "Expected recovery_started event"
        assert len(shutdown_events) >= 1, "Expected shutdown_requested event"

        # shutdown_requested must come after recovery_started
        assert shutdown_events[0].oracle_event_seq > recovery_events[0].oracle_event_seq, (
            "shutdown_requested must have higher seq than recovery_started"
        )

        # Prime must end in STOPPED, not STARTING
        from tests.harness.types import ComponentStatus
        prime_status = oracle.component_status("prime").value
        assert prime_status == ComponentStatus.STOPPED, (
            f"Prime should be STOPPED, not {prime_status.value}"
        )
