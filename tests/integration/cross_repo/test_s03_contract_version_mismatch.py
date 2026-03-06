"""Integration tests for Scenario S3: Contract Version Mismatch.

Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.harness.invariants import InvariantRegistry, single_routing_target
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.scenarios.s03_contract_version_mismatch import (
    S03ContractVersionMismatch,
)
from tests.harness.scoped_fault_injector import ScopedFaultInjector
from tests.harness.state_oracle import MockStateOracle


@dataclass
class Config:
    strict_mode: bool = False


@pytest.mark.integration_mock
class TestS03ContractVersionMismatch:
    """Verify contract version mismatch -> degradation -> recovery."""

    def _build_harness(self):
        """Wire up all harness components for S03."""
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

        scenario = S03ContractVersionMismatch(oracle=oracle)

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

    async def test_contract_reason_code_captured(self) -> None:
        """The contract_incompatible event carries reason 'version_window' in metadata."""
        orchestrator, scenario, _oracle = self._build_harness()
        result = await orchestrator.run_scenario(scenario)
        assert result.passed, f"Scenario failed with violations: {result.violations}"

        # Find the contract_incompatible event
        contract_events = [
            ev
            for ev in result.event_log
            if ev.event_type == "contract_incompatible"
        ]
        assert len(contract_events) >= 1, (
            "No contract_incompatible event found in event log"
        )

        event = contract_events[0]
        assert event.metadata.get("reason") == "version_window", (
            f"Expected reason 'version_window' in metadata, got {event.metadata}"
        )
        assert event.metadata.get("detail") == "v2.1 vs v3.0", (
            f"Expected detail 'v2.1 vs v3.0' in metadata, got {event.metadata}"
        )
