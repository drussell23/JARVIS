"""Tests for HarnessOrchestrator with phase fencing and invariant checks.

TDD tests for Task 6 of the Disease 9 cross-repo integration test harness.
asyncio_mode = auto in pytest.ini -- no @pytest.mark.asyncio required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Literal
from unittest.mock import MagicMock

from tests.harness.invariants import InvariantRegistry, epoch_monotonic
from tests.harness.orchestrator import HarnessOrchestrator
from tests.harness.state_oracle import MockStateOracle
from tests.harness.types import ComponentStatus


# ---------------------------------------------------------------------------
# Config stub
# ---------------------------------------------------------------------------

@dataclass
class Config:
    strict_mode: bool = False


# ---------------------------------------------------------------------------
# Scenario helpers
# ---------------------------------------------------------------------------

class DummyScenario:
    """Scenario that records which phases ran."""

    name = "dummy"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 5.0,
        "verify": 5.0,
        "recover": 5.0,
    }

    def __init__(self) -> None:
        self.phases_run: list[str] = []

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        self.phases_run.append("setup")

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        self.phases_run.append("inject")

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        self.phases_run.append("verify")

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        self.phases_run.append("recover")


class TimeoutScenario:
    """Scenario whose inject phase sleeps forever (triggers timeout)."""

    name = "timeout_test"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 0.2,
        "verify": 5.0,
        "recover": 5.0,
    }

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        pass

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        await asyncio.sleep(10)

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        pass

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        pass


class InvariantFailScenario:
    """Scenario that sets epoch=5 in inject, then epoch=2 in verify (violates monotonic)."""

    name = "invariant_fail"
    phase_deadlines: Dict[str, float] = {
        "setup": 5.0,
        "inject": 5.0,
        "verify": 5.0,
        "recover": 5.0,
    }

    async def setup(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        pass

    async def inject(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        oracle.set_epoch(5)

    async def verify(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        oracle.set_epoch(2)

    async def recover(self, oracle: Any, injector: Any, trace_root_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(
    oracle: MockStateOracle | None = None,
    invariants: InvariantRegistry | None = None,
) -> tuple[HarnessOrchestrator, MockStateOracle]:
    """Build an orchestrator with sensible defaults."""
    oracle = oracle or MockStateOracle()
    injector = MagicMock()
    invariants = invariants or InvariantRegistry()
    config = Config()
    orch = HarnessOrchestrator(
        mode="mock",
        oracle=oracle,
        injector=injector,
        invariants=invariants,
        config=config,
    )
    return orch, oracle


# ---------------------------------------------------------------------------
# TestOrchestratorBasics (5 tests)
# ---------------------------------------------------------------------------

class TestOrchestratorBasics:
    """Core orchestrator behaviour: phase execution, timeouts, invariants."""

    async def test_runs_all_four_phases(self) -> None:
        scenario = DummyScenario()
        orch, oracle = _make_orchestrator()

        result = await orch.run_scenario(scenario)

        assert scenario.phases_run == ["setup", "inject", "verify", "recover"]
        assert result.passed is True
        assert result.scenario_name == "dummy"
        assert len(result.trace_root_id) == 16

    async def test_phase_timeout_stops_execution(self) -> None:
        scenario = TimeoutScenario()
        orch, oracle = _make_orchestrator()

        result = await orch.run_scenario(scenario)

        assert result.passed is False
        # Should have a phase_timeout violation on "inject"
        timeout_violations = [
            v for v in result.violations if v.failure_type == "phase_timeout"
        ]
        assert len(timeout_violations) >= 1
        assert timeout_violations[0].phase == "inject"
        # Execution should have stopped; verify and recover should NOT be in phases
        assert "verify" not in result.phases
        assert "recover" not in result.phases

    async def test_invariant_violation_recorded(self) -> None:
        scenario = InvariantFailScenario()
        invariants = InvariantRegistry()
        invariants.register("epoch_monotonic", epoch_monotonic(), suppress_flapping=False)
        orch, oracle = _make_orchestrator(invariants=invariants)

        result = await orch.run_scenario(scenario)

        invariant_violations = [
            v for v in result.violations if v.failure_type == "invariant_violation"
        ]
        assert len(invariant_violations) >= 1
        assert result.passed is False

    async def test_phase_boundary_seq_recorded(self) -> None:
        scenario = DummyScenario()
        orch, oracle = _make_orchestrator()

        await orch.run_scenario(scenario)

        # The oracle should have phase boundaries for each executed phase
        assert len(oracle._phase_boundaries) > 0

    async def test_result_includes_event_log(self) -> None:
        oracle = MockStateOracle()
        # Set a component status to generate events before running
        oracle.set_component_status("prime", ComponentStatus.READY)
        orch, _ = _make_orchestrator(oracle=oracle)
        scenario = DummyScenario()

        result = await orch.run_scenario(scenario)

        assert len(result.event_log) > 0
