"""MVP gate test for the Disease 9 cross-repo integration test harness.

Verifies that every harness module is importable, wired correctly,
and that a full mock-mode scenario can execute end-to-end.

Task 11 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest


@dataclass
class Config:
    """Minimal orchestrator configuration for gate tests."""

    strict_mode: bool = False


class TestHarnessGate:
    """Go/No-Go gate: every harness component must be importable and functional."""

    # ------------------------------------------------------------------
    # 1. Core types importable with correct enum cardinalities
    # ------------------------------------------------------------------

    def test_core_types_importable(self) -> None:
        from tests.harness.types import (
            ComponentStatus,
            ContractReasonCode,
            ContractStatus,
            FaultComposition,
            FaultHandle,
            FaultScope,
            ObservedEvent,
            OracleObservation,
            PhaseFailure,
            PhaseResult,
            ScenarioResult,
        )

        assert len(ComponentStatus) == 11
        assert len(FaultScope) == 5
        assert len(ContractReasonCode) == 6

    # ------------------------------------------------------------------
    # 2. StateOracle protocol and mock satisfy isinstance
    # ------------------------------------------------------------------

    def test_state_oracle_protocol_exists(self) -> None:
        from tests.harness.state_oracle import MockStateOracle, StateOracleProtocol

        assert isinstance(MockStateOracle(), StateOracleProtocol)

    # ------------------------------------------------------------------
    # 3. InvariantRegistry + 4 MVP invariant factories
    # ------------------------------------------------------------------

    def test_invariant_registry_has_mvp_invariants(self) -> None:
        from tests.harness.invariants import (
            InvariantRegistry,
            epoch_monotonic,
            fault_isolation,
            single_routing_target,
            terminal_is_final,
        )

        registry = InvariantRegistry()
        registry.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        registry.register("single_routing_target", single_routing_target(), suppress_flapping=False)
        registry.register(
            "fault_isolation",
            fault_isolation(
                affected=frozenset({"prime"}),
                unaffected=frozenset({"frontend"}),
            ),
            suppress_flapping=False,
        )
        registry.register("terminal_is_final", terminal_is_final(), suppress_flapping=False)

        assert len(registry._invariants) == 4

    # ------------------------------------------------------------------
    # 4. ScopedFaultInjector + exceptions importable
    # ------------------------------------------------------------------

    def test_scoped_fault_injector_importable(self) -> None:
        from tests.harness.scoped_fault_injector import (
            FaultIsolationError,
            ReentrantFaultError,
            ScopedFaultInjector,
        )

    # ------------------------------------------------------------------
    # 5. HarnessOrchestrator importable
    # ------------------------------------------------------------------

    def test_orchestrator_importable(self) -> None:
        from tests.harness.orchestrator import HarnessOrchestrator

    # ------------------------------------------------------------------
    # 6. ComponentProcess + MockComponentProcess importable
    # ------------------------------------------------------------------

    def test_component_process_importable(self) -> None:
        from tests.harness.component_process import ComponentProcess, MockComponentProcess

    # ------------------------------------------------------------------
    # 7. Scenario S01 importable
    # ------------------------------------------------------------------

    def test_scenario_s01_importable(self) -> None:
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback

    # ------------------------------------------------------------------
    # 8. Scenario S05 importable
    # ------------------------------------------------------------------

    def test_scenario_s05_importable(self) -> None:
        from tests.harness.scenarios.s05_cascading_failure import S05CascadingFailure

    # ------------------------------------------------------------------
    # 9. Scenario S07 importable
    # ------------------------------------------------------------------

    def test_scenario_s07_importable(self) -> None:
        from tests.harness.scenarios.s07_epoch_stale import S07EpochStale

    # ------------------------------------------------------------------
    # 10. Full end-to-end mock-mode scenario S01
    # ------------------------------------------------------------------

    async def test_full_scenario_s01_mock_mode(self) -> None:
        from tests.harness.component_process import MockComponentProcess
        from tests.harness.invariants import (
            InvariantRegistry,
            epoch_monotonic,
            single_routing_target,
        )
        from tests.harness.orchestrator import HarnessOrchestrator
        from tests.harness.scenarios.s01_prime_crash_fallback import S01PrimeCrashFallback
        from tests.harness.scoped_fault_injector import ScopedFaultInjector
        from tests.harness.state_oracle import MockStateOracle

        # --- Oracle ---
        oracle = MockStateOracle()

        # --- Mock inner injector ---
        mock_revert = AsyncMock()
        mock_inner_result = MagicMock()
        mock_inner_result.revert = mock_revert

        mock_inner = MagicMock()
        mock_inner.inject_failure = AsyncMock(return_value=mock_inner_result)

        # --- ScopedFaultInjector wrapping mock inner ---
        injector = ScopedFaultInjector(inner=mock_inner, oracle=oracle)

        # --- Invariants (no flapping suppression for deterministic gate) ---
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        invariants.register("single_routing_target", single_routing_target(), suppress_flapping=False)

        # --- Prime component process ---
        prime = MockComponentProcess(name="prime", oracle=oracle)

        # --- Scenario ---
        scenario = S01PrimeCrashFallback(prime_process=prime, oracle=oracle)

        # --- Orchestrator ---
        config = Config(strict_mode=False)
        orchestrator = HarnessOrchestrator(
            mode="mock",
            oracle=oracle,
            injector=injector,
            invariants=invariants,
            config=config,
        )

        # --- Run ---
        result = await orchestrator.run_scenario(scenario)

        # --- Assertions ---
        assert result.passed, f"Scenario failed with violations: {result.violations}"
        assert len(result.event_log) > 0, "Event log must not be empty"
        assert len(result.trace_root_id) > 0, "trace_root_id must not be empty"
