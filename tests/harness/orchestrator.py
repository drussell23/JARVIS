"""HarnessOrchestrator -- phase-fenced scenario execution engine.

Runs a scenario through four phases (setup, inject, verify, recover),
applying invariant checks at each phase boundary and recording all
violations and timing in a ScenarioResult.

Task 6 of the Disease 9 cross-repo integration test harness.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Literal
from uuid import uuid4

from tests.harness.types import PhaseFailure, PhaseResult, ScenarioResult


# Phase execution order
PHASE_ORDER = ("setup", "inject", "verify", "recover")


class HarnessOrchestrator:
    """Phase-fenced scenario execution engine.

    Parameters
    ----------
    mode:
        ``"mock"`` for in-memory testing, ``"real"`` for live staging.
    oracle:
        A StateOracle (or MockStateOracle) providing state, events, and fencing.
    injector:
        A ScopedFaultInjector (or mock) for fault injection during scenarios.
    invariants:
        An InvariantRegistry whose ``check_all`` method validates oracle state.
    config:
        Configuration object (must have ``strict_mode: bool`` attribute).
    """

    def __init__(
        self,
        mode: Literal["mock", "real"],
        oracle: Any,
        injector: Any,
        invariants: Any,
        config: Any,
    ) -> None:
        self._mode = mode
        self._oracle = oracle
        self._injector = injector
        self._invariants = invariants
        self._config = config

    async def run_scenario(self, scenario: Any) -> ScenarioResult:
        """Execute a scenario through all four phases with fencing and invariant checks.

        Returns a :class:`ScenarioResult` summarising the run.
        """
        trace_root_id = uuid4().hex[:16]
        violations: List[PhaseFailure] = []
        phases: Dict[str, PhaseResult] = {}

        for phase_name in PHASE_ORDER:
            # 1. Transition oracle to this phase
            self._oracle.set_phase(phase_name)

            # 2. Record boundary sequence before phase runs
            boundary_seq = self._oracle.current_seq()

            # 3. Resolve the phase callable from the scenario
            phase_fn = getattr(scenario, phase_name, None)
            if phase_fn is None:
                continue

            # 4. Determine the deadline for this phase
            deadline = scenario.phase_deadlines.get(phase_name, 30.0)

            # 5. Execute the phase with timeout
            phase_start = time.monotonic()
            timed_out = False
            try:
                await asyncio.wait_for(
                    phase_fn(self._oracle, self._injector, trace_root_id),
                    timeout=deadline,
                )
            except asyncio.TimeoutError:
                timed_out = True
                violation = PhaseFailure(
                    phase=phase_name,
                    failure_type="phase_timeout",
                    detail=f"Phase '{phase_name}' exceeded {deadline}s deadline",
                )
                violations.append(violation)
            phase_duration = time.monotonic() - phase_start

            # 6. Run invariant checks (even on timeout, check what we can)
            inv_violations = self._invariants.check_all(self._oracle)
            phase_violations: List[Any] = []
            for inv_detail in inv_violations:
                failure = PhaseFailure(
                    phase=phase_name,
                    failure_type="invariant_violation",
                    detail=inv_detail,
                )
                violations.append(failure)
                phase_violations.append(failure)

            # 7. Fence this phase boundary
            self._oracle.fence_phase(phase_name, boundary_seq)

            # 8. Build PhaseResult
            all_phase_violations = list(phase_violations)
            if timed_out:
                all_phase_violations.insert(
                    0,
                    PhaseFailure(
                        phase=phase_name,
                        failure_type="phase_timeout",
                        detail=f"Phase '{phase_name}' exceeded {deadline}s deadline",
                    ),
                )
            phases[phase_name] = PhaseResult(
                duration_s=phase_duration,
                violations=all_phase_violations,
            )

            # 9. On timeout, stop execution (remaining phases are skipped)
            if timed_out:
                break

        # Build final result
        passed = len(violations) == 0
        event_log = self._oracle.event_log()

        return ScenarioResult(
            scenario_name=scenario.name,
            trace_root_id=trace_root_id,
            passed=passed,
            violations=violations,
            phases=phases,
            event_log=event_log,
        )
