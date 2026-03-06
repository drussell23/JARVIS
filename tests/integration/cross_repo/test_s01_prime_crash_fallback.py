"""Integration tests for Scenario S1: Prime Crash Fallback.

Task 8 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.component_process import MockComponentProcess
from tests.harness.invariants import InvariantRegistry, epoch_monotonic, single_routing_target
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS01PrimeCrashFallback:
    """Verify prime crash -> CLOUD_CLAUDE fallback -> recovery."""

    def _build_harness(self):
        """Wire up all harness components for S01."""
        oracle = MockStateOracle()

        # Inner injector mock
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic())
        invariants.register("single_routing_target", single_routing_target())

        prime = MockComponentProcess(name="prime", oracle=oracle)
        scenario = S01PrimeCrashFallback(prime_process=prime, oracle=oracle)

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

    async def test_causality_chain(self) -> None:
        """fault_injected event has a lower oracle_event_seq than routing_change to CLOUD_CLAUDE."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        # Find the fault_injected event
        fault_events = [
            ev for ev in result.event_log if ev.event_type == "fault_injected"
        ]
        assert len(fault_events) >= 1, "No fault_injected event found in event log"

        # Find the routing_change to CLOUD_CLAUDE
        routing_events = [
            ev
            for ev in result.event_log
            if ev.event_type == "routing_change" and ev.new_value == "CLOUD_CLAUDE"
        ]
        assert len(routing_events) >= 1, "No routing_change to CLOUD_CLAUDE found"

        # Verify causality: fault must come before routing change
        fault_seq = fault_events[0].oracle_event_seq
        routing_seq = routing_events[0].oracle_event_seq
        assert fault_seq < routing_seq, (
            f"fault_injected (seq={fault_seq}) must precede "
            f"routing_change to CLOUD_CLAUDE (seq={routing_seq})"
        )
