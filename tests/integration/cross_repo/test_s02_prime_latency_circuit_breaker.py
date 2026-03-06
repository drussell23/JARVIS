"""Integration tests for Scenario S2: Prime Latency Circuit Breaker.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, single_routing_target
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scenarios.s02_prime_latency_circuit_breaker import (
    S02PrimeLatencyCircuitBreaker,
)
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS02PrimeLatencyCircuitBreaker:
    """Verify latency spike -> circuit breaker opens -> hysteresis recovery."""

    def _build_harness(self):
        """Wire up all harness components for S02."""
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

        scenario = S02PrimeLatencyCircuitBreaker(oracle=oracle)

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

    async def test_hysteresis_requires_sustained_health(self) -> None:
        """Three health_check_passed events must precede breaker_closed in the event log."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        # Collect health_check_passed and breaker_closed events
        health_events = [
            ev
            for ev in result.event_log
            if ev.event_type == "health_check_passed"
        ]
        breaker_closed_events = [
            ev
            for ev in result.event_log
            if ev.event_type == "breaker_closed"
        ]

        assert len(health_events) == 3, (
            f"Expected 3 health_check_passed events, got {len(health_events)}"
        )
        assert len(breaker_closed_events) >= 1, (
            "No breaker_closed event found in event log"
        )

        # Verify ordering: all 3 health checks must precede breaker_closed
        breaker_closed_seq = breaker_closed_events[0].oracle_event_seq
        for i, health_ev in enumerate(health_events):
            assert health_ev.oracle_event_seq < breaker_closed_seq, (
                f"health_check_passed #{i + 1} (seq={health_ev.oracle_event_seq}) "
                f"must precede breaker_closed (seq={breaker_closed_seq})"
            )
