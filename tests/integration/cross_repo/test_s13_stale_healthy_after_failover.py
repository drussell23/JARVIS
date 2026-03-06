"""Integration tests for Scenario S13: Stale Healthy After Failover.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import (
    InvariantRegistry,
    epoch_monotonic,
    single_routing_target,
)
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.scenarios.s13_stale_healthy_after_failover import (
    S13StaleHealthyAfterFailover,
)
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS13StaleHealthyAfterFailover:
    """Verify stale prime is not re-promoted until fresh handshake completes."""

    def _build_harness(self):
        """Wire up all harness components for S13."""
        oracle = MockStateOracle()

        # Inner injector mock (not used by S13, but orchestrator requires it)
        inner_revert = AsyncMock()
        inner_result = MagicMock()
        inner_result.revert = inner_revert
        inner_injector = MagicMock()
        inner_injector.inject_failure = AsyncMock(return_value=inner_result)

        injector = ScopedFaultInjector(inner=inner_injector, oracle=oracle)

        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic())
        invariants.register("single_routing_target", single_routing_target())

        scenario = S13StaleHealthyAfterFailover(oracle=oracle)

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

    async def test_stale_prime_not_repromoted(self) -> None:
        """stale_healthy_detected exists, and no routing_change to LOCAL_PRIME between stale detection and handshake."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        events = result.event_log

        # Find stale_healthy_detected event
        stale_events = [
            ev for ev in events if ev.event_type == "stale_healthy_detected"
        ]
        assert len(stale_events) >= 1, "Expected stale_healthy_detected event"
        stale_seq = stale_events[0].oracle_event_seq

        # Find handshake_completed event
        handshake_events = [
            ev for ev in events if ev.event_type == "handshake_completed"
        ]
        assert len(handshake_events) >= 1, "Expected handshake_completed event"
        handshake_seq = handshake_events[0].oracle_event_seq

        # No routing_change to LOCAL_PRIME between stale detection and handshake
        routing_to_local_between = [
            ev for ev in events
            if ev.event_type == "routing_change"
            and ev.new_value == "LOCAL_PRIME"
            and stale_seq < ev.oracle_event_seq < handshake_seq
        ]
        assert len(routing_to_local_between) == 0, (
            f"Expected no routing_change to LOCAL_PRIME between stale detection "
            f"(seq={stale_seq}) and handshake (seq={handshake_seq}), "
            f"but found {len(routing_to_local_between)}"
        )
