"""Tests for Wave 3 (6) Slice 4 — enforce-mode + scheduler submit.

Scope: memory/project_wave3_item6_scope.md §9 + operator Slice 4
authorization (2026-04-23):

- Gating: master + enforce both required. Shadow flag independent.
  Defaults stay false.
- Sovereignty: MemoryPressureGate re-consulted RIGHT BEFORE
  scheduler.submit(); widening beyond eligibility.n_allowed is denied.
- §6: per-unit Iron Gate semantics unchanged (scheduler-side contract).
- Parity: enforce-off ≡ legacy sequential; enforce-on vs shadow-on
  build same graph (differ only in submit side-effect).
- Lifecycle: cooperative cancellation; no retry loops; Ticket A1
  wall-clock budget honored via per-graph wait timeout.
- Observability: INFO/WARN per submit start + terminal + clamp/deny.
- Narrow error handling: only known-safe exceptions caught on hot path
  (asyncio.CancelledError, asyncio.TimeoutError, scheduler.submit
  returning False). Unknown exceptions propagate — fail loud.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision as MemoryFanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutOutcome,
    FanoutResult,
    ShadowEvaluation,
    enforce_evaluate_fanout,
    evaluate_shadow_fanout,
    parallel_dispatch_wait_timeout_s,
)
from backend.core.ouroboros.governance.posture import Posture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeGeneration:
    candidates: Tuple[Dict[str, Any], ...] = ()


def _multi_file_candidates(n: int = 3) -> Tuple[Dict[str, Any], ...]:
    return (
        {
            "files": [
                {
                    "file_path": f"pkg/mod_{i}.py",
                    "full_content": f"# module {i}\npass\n",
                    "rationale": f"unit {i}",
                }
                for i in range(n)
            ],
        },
    )


def _ok_gate(level: PressureLevel = PressureLevel.OK,
             cap_override: Optional[int] = None) -> MemoryPressureGate:
    gate = MagicMock(spec=MemoryPressureGate)

    def _cf(n: int) -> MemoryFanoutDecision:
        allowed_n = n if cap_override is None else min(n, cap_override)
        if level == PressureLevel.CRITICAL:
            allowed_n = 1
        return MemoryFanoutDecision(
            allowed=level != PressureLevel.CRITICAL,
            n_requested=n,
            n_allowed=allowed_n,
            level=level,
            free_pct=60.0,
            reason_code=f"mock_{level.value}",
            source="test",
        )

    gate.can_fanout.side_effect = _cf
    return gate


def _posture(p: Posture = Posture.MAINTAIN, c: float = 0.9):
    def _fn() -> Tuple[Optional[Posture], Optional[float]]:
        return p, c

    return _fn


class _FakeScheduler:
    """Async mock implementing the SubagentScheduler contract surface
    enforce_evaluate_fanout depends on.

    Callers parameterize ``submit_returns`` + ``terminal_phase`` +
    ``unit_tallies`` + ``submit_raises`` + ``wait_raises`` to drive
    each test scenario.
    """

    def __init__(
        self,
        *,
        submit_returns: bool = True,
        submit_raises: Optional[BaseException] = None,
        wait_raises: Optional[BaseException] = None,
        terminal_phase: GraphExecutionPhase = GraphExecutionPhase.COMPLETED,
        unit_tallies: Tuple[int, int, int] = (0, 0, 0),  # completed, failed, cancelled
        last_error: str = "",
        wait_delay_s: float = 0.0,
    ) -> None:
        self.submit_returns = submit_returns
        self.submit_raises = submit_raises
        self.wait_raises = wait_raises
        self.terminal_phase = terminal_phase
        self.unit_tallies = unit_tallies
        self.last_error = last_error
        self.wait_delay_s = wait_delay_s
        self.submitted_graphs: list = []
        self.wait_calls: list = []

    async def submit(self, graph: ExecutionGraph) -> bool:
        self.submitted_graphs.append(graph)
        if self.submit_raises is not None:
            raise self.submit_raises
        return self.submit_returns

    async def wait_for_graph(self, graph_id: str, timeout_s: Optional[float] = None) -> GraphExecutionState:
        self.wait_calls.append((graph_id, timeout_s))
        if self.wait_delay_s > 0:
            await asyncio.sleep(self.wait_delay_s)
        if self.wait_raises is not None:
            raise self.wait_raises
        graph = self.submitted_graphs[-1] if self.submitted_graphs else None
        if graph is None:
            raise RuntimeError("test helper: wait_for_graph called without prior submit")
        n_c, n_f, n_x = self.unit_tallies
        completed = tuple(u.unit_id for u in graph.units[:n_c])
        failed = tuple(u.unit_id for u in graph.units[n_c:n_c + n_f])
        cancelled = tuple(u.unit_id for u in graph.units[n_c + n_f:n_c + n_f + n_x])
        return GraphExecutionState(
            graph=graph,
            phase=self.terminal_phase,
            completed_units=completed,
            failed_units=failed,
            cancelled_units=cancelled,
            last_error=self.last_error,
        )


# ---------------------------------------------------------------------------
# Asyncio runner
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture
def master_and_enforce_on(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")


# ===========================================================================
# (1) Gate matrix — master + enforce gates
# ===========================================================================


def test_master_off_skips_with_master_off_reason(monkeypatch):
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    result = _run(enforce_evaluate_fanout(
        op_id="op-001",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=_FakeScheduler(),
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason == "master_off"
    assert result.graph is None
    assert result.state is None


def test_enforce_off_skips_with_enforce_off_reason(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    scheduler = _FakeScheduler()
    result = _run(enforce_evaluate_fanout(
        op_id="op-002",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason == "enforce_off"
    # Scheduler was NOT touched.
    assert scheduler.submitted_graphs == []
    assert scheduler.wait_calls == []


def test_shadow_flag_does_not_engage_enforce(monkeypatch):
    """Shadow=true + enforce=false → enforce_evaluate_fanout skips."""
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    scheduler = _FakeScheduler()
    result = _run(enforce_evaluate_fanout(
        op_id="op-003",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason == "enforce_off"
    assert scheduler.submitted_graphs == []


# ===========================================================================
# (2) Shape / eligibility short-circuits
# ===========================================================================


def test_unrecognized_generation_shape_skips(master_and_enforce_on):
    scheduler = _FakeScheduler()
    result = _run(enforce_evaluate_fanout(
        op_id="op-004",
        generation="garbage",
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason == "unrecognized_shape"
    assert scheduler.submitted_graphs == []


def test_single_file_generation_skips_as_ineligible(master_and_enforce_on):
    scheduler = _FakeScheduler()
    single_file_gen = _FakeGeneration(candidates=(
        {"file_path": "pkg/solo.py", "full_content": "# solo\n"},
    ))
    result = _run(enforce_evaluate_fanout(
        op_id="op-005",
        generation=single_file_gen,
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason.startswith("ineligible:")
    assert scheduler.submitted_graphs == []


def test_memory_critical_skips_via_ineligible(master_and_enforce_on):
    scheduler = _FakeScheduler()
    result = _run(enforce_evaluate_fanout(
        op_id="op-006",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(level=PressureLevel.CRITICAL),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert "memory_critical" in result.skip_reason
    assert scheduler.submitted_graphs == []


# ===========================================================================
# (3) §2 Sovereignty — MemoryPressureGate re-check right before submit
# ===========================================================================


def test_gate_is_reconsulted_right_before_submit(master_and_enforce_on):
    """Operator invariant: MemoryPressureGate.can_fanout(n) consulted
    immediately before scheduler.submit(). Verify via MagicMock call count."""
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    gate = _ok_gate()
    result = _run(enforce_evaluate_fanout(
        op_id="op-007",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=gate,
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.COMPLETED
    # gate.can_fanout called at least twice: once inside
    # is_fanout_eligible, once in the §2 sovereignty re-check.
    assert gate.can_fanout.call_count >= 2


def test_sovereignty_clamp_denies_submit(master_and_enforce_on):
    """If memory gate clamps below graph.concurrency_limit at submit time,
    outcome=SUBMIT_DENIED and scheduler.submit is NOT called.

    Note: is_fanout_eligible consults the gate TWICE internally (once at
    n_requested for the CRITICAL check, once at posture_clamped). The
    sovereignty re-check inside enforce_evaluate_fanout is therefore the
    3rd call. Tests parameterize the stateful gate to pass calls 1+2
    (eligibility phase) and clamp only on call 3 (sovereignty re-check).
    """
    scheduler = _FakeScheduler()

    gate = MagicMock(spec=MemoryPressureGate)
    calls = [0]

    def _cf(n: int) -> MemoryFanoutDecision:
        calls[0] += 1
        # Calls 1+2: eligibility phase probes — return OK allowing n.
        if calls[0] <= 2:
            return MemoryFanoutDecision(
                allowed=True, n_requested=n, n_allowed=n,
                level=PressureLevel.OK, free_pct=50.0,
                reason_code="mock_ok", source="test",
            )
        # Call 3: sovereignty re-check — pressure rose to HIGH, clamp to 1.
        return MemoryFanoutDecision(
            allowed=True, n_requested=n, n_allowed=1,
            level=PressureLevel.HIGH, free_pct=15.0,
            reason_code="mock_high", source="test",
        )

    gate.can_fanout.side_effect = _cf

    result = _run(enforce_evaluate_fanout(
        op_id="op-008",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=gate,
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SUBMIT_DENIED
    assert result.skip_reason == "sovereignty_clamp"
    assert scheduler.submitted_graphs == []
    assert scheduler.wait_calls == []
    assert result.graph is not None  # graph was built
    assert "n_allowed=1" in result.error


def test_sovereignty_clamp_to_critical_denies_submit(master_and_enforce_on):
    """CRITICAL at sovereignty re-check (call 3) → SUBMIT_DENIED."""
    scheduler = _FakeScheduler()
    gate = MagicMock(spec=MemoryPressureGate)
    calls = [0]

    def _cf(n: int) -> MemoryFanoutDecision:
        calls[0] += 1
        # Calls 1+2: eligibility phase — OK.
        if calls[0] <= 2:
            return MemoryFanoutDecision(
                allowed=True, n_requested=n, n_allowed=n,
                level=PressureLevel.OK, free_pct=50.0,
                reason_code="mock_ok", source="test",
            )
        # Call 3: sovereignty re-check — CRITICAL.
        return MemoryFanoutDecision(
            allowed=False, n_requested=n, n_allowed=1,
            level=PressureLevel.CRITICAL, free_pct=2.0,
            reason_code="mock_critical", source="test",
        )

    gate.can_fanout.side_effect = _cf
    result = _run(enforce_evaluate_fanout(
        op_id="op-009",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=gate,
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SUBMIT_DENIED
    assert result.skip_reason == "sovereignty_clamp"
    assert scheduler.submitted_graphs == []


# ===========================================================================
# (4) Submit + wait — success + terminal phases
# ===========================================================================


def test_completed_happy_path(master_and_enforce_on):
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    result = _run(enforce_evaluate_fanout(
        op_id="op-010",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.COMPLETED
    assert result.state is not None
    assert result.n_units_requested == 3
    assert result.n_units_completed == 3
    assert result.n_units_failed == 0
    assert result.n_units_cancelled == 0
    assert scheduler.submitted_graphs[0] is result.graph
    assert scheduler.wait_calls[0][0] == result.graph.graph_id


def test_failed_phase_returns_failed_outcome(master_and_enforce_on):
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.FAILED,
        unit_tallies=(1, 2, 0),
        last_error="unit-A validation failed",
    )
    result = _run(enforce_evaluate_fanout(
        op_id="op-011",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.FAILED
    assert result.n_units_completed == 1
    assert result.n_units_failed == 2
    assert "unit-A validation failed" in result.error


def test_cancelled_phase_returns_cancelled_outcome(master_and_enforce_on):
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.CANCELLED,
        unit_tallies=(1, 0, 2),
    )
    result = _run(enforce_evaluate_fanout(
        op_id="op-012",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.CANCELLED
    assert result.n_units_completed == 1
    assert result.n_units_cancelled == 2


def test_scheduler_submit_returns_false_denies(master_and_enforce_on):
    scheduler = _FakeScheduler(submit_returns=False)
    result = _run(enforce_evaluate_fanout(
        op_id="op-013",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SUBMIT_DENIED
    assert result.skip_reason == "scheduler_refused"
    # Submit WAS called, but wait was NOT.
    assert len(scheduler.submitted_graphs) == 1
    assert scheduler.wait_calls == []


# ===========================================================================
# (5) Cooperative cancellation (Ticket A1 discipline)
# ===========================================================================


def test_cancellation_during_submit_propagates(master_and_enforce_on):
    scheduler = _FakeScheduler(submit_raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _run(enforce_evaluate_fanout(
            op_id="op-014",
            generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
            scheduler=scheduler,
            gate=_ok_gate(),
            posture_fn=_posture(),
        ))


def test_cancellation_during_wait_propagates(master_and_enforce_on):
    scheduler = _FakeScheduler(wait_raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        _run(enforce_evaluate_fanout(
            op_id="op-015",
            generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
            scheduler=scheduler,
            gate=_ok_gate(),
            posture_fn=_posture(),
        ))


def test_wait_timeout_classified_as_timeout_outcome(master_and_enforce_on):
    scheduler = _FakeScheduler(wait_raises=asyncio.TimeoutError())
    result = _run(enforce_evaluate_fanout(
        op_id="op-016",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
        wait_timeout_s=0.01,
    ))
    assert result.outcome == FanoutOutcome.TIMEOUT
    assert "timeout" in result.error.lower()


# ===========================================================================
# (6) Narrow error handling — unknown exceptions PROPAGATE (fail loud)
# ===========================================================================


def test_unknown_submit_exception_propagates_unchanged(master_and_enforce_on):
    """Operator directive: enforce path must fail loud. An unknown
    exception from scheduler.submit must NOT be caught+classified."""
    class _UnusualError(RuntimeError):
        pass

    scheduler = _FakeScheduler(submit_raises=_UnusualError("boom"))
    with pytest.raises(_UnusualError, match="boom"):
        _run(enforce_evaluate_fanout(
            op_id="op-017",
            generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
            scheduler=scheduler,
            gate=_ok_gate(),
            posture_fn=_posture(),
        ))


def test_unknown_wait_exception_propagates_unchanged(master_and_enforce_on):
    class _UnusualError(RuntimeError):
        pass

    scheduler = _FakeScheduler(wait_raises=_UnusualError("wait-boom"))
    with pytest.raises(_UnusualError, match="wait-boom"):
        _run(enforce_evaluate_fanout(
            op_id="op-018",
            generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
            scheduler=scheduler,
            gate=_ok_gate(),
            posture_fn=_posture(),
        ))


def test_non_terminal_phase_raises_loud(master_and_enforce_on):
    """Scheduler returning a non-terminal GraphExecutionPhase is a
    contract violation — raise loud rather than silently continuing."""
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.RUNNING,  # non-terminal
        unit_tallies=(0, 0, 0),
    )
    with pytest.raises(RuntimeError, match="non-terminal GraphExecutionPhase"):
        _run(enforce_evaluate_fanout(
            op_id="op-019",
            generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
            scheduler=scheduler,
            gate=_ok_gate(),
            posture_fn=_posture(),
        ))


# ===========================================================================
# (7) Parity — enforce-on vs shadow-on build the same graph
# ===========================================================================


def test_enforce_and_shadow_build_same_graph(monkeypatch):
    """(b) enforce-on vs shadow-on where both build same graph."""
    gen = _FakeGeneration(candidates=_multi_file_candidates(3))

    # Shadow run.
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", "true")
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    shadow_result = evaluate_shadow_fanout(
        op_id="op-parity-001",
        generation=gen,
        gate=_ok_gate(),
        posture_fn=_posture(),
    )
    assert shadow_result.graph is not None

    # Enforce run — same op_id + inputs.
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_SHADOW", raising=False)
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", "true")
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    enforce_result = _run(enforce_evaluate_fanout(
        op_id="op-parity-001",
        generation=gen,
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert enforce_result.graph is not None

    # Both build the same graph on deterministic inputs.
    assert enforce_result.graph.graph_id == shadow_result.graph.graph_id
    assert enforce_result.graph.plan_digest == shadow_result.graph.plan_digest
    shadow_unit_ids = [u.unit_id for u in shadow_result.graph.units]
    enforce_unit_ids = [u.unit_id for u in enforce_result.graph.units]
    assert shadow_unit_ids == enforce_unit_ids


def test_enforce_off_is_zero_side_effect(monkeypatch):
    """(a) enforce-off ≡ legacy sequential: scheduler is not touched."""
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENFORCE", raising=False)
    scheduler = _FakeScheduler()
    result = _run(enforce_evaluate_fanout(
        op_id="op-020",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    assert result.outcome == FanoutOutcome.SKIPPED
    assert result.skip_reason == "master_off"
    assert scheduler.submitted_graphs == []
    assert scheduler.wait_calls == []


# ===========================================================================
# (8) FanoutResult immutability + FanoutOutcome enum stability
# ===========================================================================


def test_fanout_result_is_frozen():
    r = FanoutResult(outcome=FanoutOutcome.SKIPPED, skip_reason="test")
    with pytest.raises((AttributeError, Exception)):
        r.outcome = FanoutOutcome.COMPLETED  # type: ignore[misc]


def test_fanout_outcome_enum_values_stable():
    """Outcome codes are used in telemetry — pin the enum value set."""
    expected = {
        "skipped",
        "submit_denied",
        "submit_failed",
        "completed",
        "failed",
        "cancelled",
        "timeout",
    }
    assert {o.value for o in FanoutOutcome} == expected


def test_fanout_result_graph_id_and_plan_digest_properties():
    r_empty = FanoutResult(outcome=FanoutOutcome.SKIPPED)
    assert r_empty.graph_id == ""
    assert r_empty.plan_digest == ""


# ===========================================================================
# (9) Telemetry — submit_start / completed / denied lines emitted
# ===========================================================================


def test_emits_submit_start_and_completed_logs(master_and_enforce_on, caplog):
    caplog.set_level(logging.INFO, logger="Ouroboros.ParallelDispatch")
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    _run(enforce_evaluate_fanout(
        op_id="op-021-telemetry",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
    ))
    msgs = [r.message for r in caplog.records]
    assert any("enforce_submit_start" in m for m in msgs), msgs
    assert any("enforce_completed" in m for m in msgs), msgs


def test_emits_submit_denied_log_on_sovereignty_clamp(master_and_enforce_on, caplog):
    caplog.set_level(logging.WARNING, logger="Ouroboros.ParallelDispatch")

    gate = MagicMock(spec=MemoryPressureGate)
    calls = [0]

    def _cf(n: int) -> MemoryFanoutDecision:
        calls[0] += 1
        # Calls 1+2: eligibility phase — OK.
        if calls[0] <= 2:
            return MemoryFanoutDecision(
                allowed=True, n_requested=n, n_allowed=n,
                level=PressureLevel.OK, free_pct=50.0,
                reason_code="mock_ok", source="test",
            )
        # Call 3: sovereignty re-check — clamp.
        return MemoryFanoutDecision(
            allowed=True, n_requested=n, n_allowed=1,
            level=PressureLevel.HIGH, free_pct=15.0,
            reason_code="mock_high", source="test",
        )

    gate.can_fanout.side_effect = _cf
    _run(enforce_evaluate_fanout(
        op_id="op-022-telemetry",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=_FakeScheduler(),
        gate=gate,
        posture_fn=_posture(),
    ))
    msgs = [r.message for r in caplog.records]
    assert any("enforce_submit_denied" in m and "sovereignty_clamp" in m for m in msgs), msgs


# ===========================================================================
# (10) Wait-timeout env knob
# ===========================================================================


def test_wait_timeout_default():
    """Default returned when env unset."""
    assert parallel_dispatch_wait_timeout_s() == 900.0


def test_wait_timeout_env_override(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", "60")
    assert parallel_dispatch_wait_timeout_s() == 60.0


def test_wait_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", "not-a-number")
    assert parallel_dispatch_wait_timeout_s() == 900.0


def test_wait_timeout_non_positive_falls_back(monkeypatch):
    for bad in ("0", "-10"):
        monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_WAIT_TIMEOUT_S", bad)
        assert parallel_dispatch_wait_timeout_s() == 900.0


def test_wait_timeout_threaded_to_scheduler(master_and_enforce_on):
    """wait_for_graph receives the configured timeout_s."""
    scheduler = _FakeScheduler(
        terminal_phase=GraphExecutionPhase.COMPLETED,
        unit_tallies=(3, 0, 0),
    )
    _run(enforce_evaluate_fanout(
        op_id="op-023",
        generation=_FakeGeneration(candidates=_multi_file_candidates(3)),
        scheduler=scheduler,
        gate=_ok_gate(),
        posture_fn=_posture(),
        wait_timeout_s=42.0,
    ))
    assert scheduler.wait_calls[0][1] == 42.0


# ===========================================================================
# (11) Authority-import ban re-verified after Slice 4 additions
# ===========================================================================


def test_parallel_dispatch_authority_ban_after_slice4():
    module_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "parallel_dispatch.py"
    )
    source = module_path.read_text()
    banned_patterns = [
        r"from\s+backend\.core\.ouroboros\.governance\.orchestrator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.policy\b",
        r"from\s+backend\.core\.ouroboros\.governance\.iron_gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.risk_tier\b",
        r"from\s+backend\.core\.ouroboros\.governance\.change_engine\b",
        r"from\s+backend\.core\.ouroboros\.governance\.candidate_generator\b",
        r"from\s+backend\.core\.ouroboros\.governance\.gate\b",
        r"from\s+backend\.core\.ouroboros\.governance\.phase_runners\.gate_runner\b",
    ]
    for pattern in banned_patterns:
        matches = re.findall(pattern, source)
        assert not matches, (
            f"parallel_dispatch.py Slice 4 violates ban: {pattern!r} "
            f"matched {matches!r}"
        )


def test_phase_dispatcher_still_no_candidate_generator_import():
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    matches = re.findall(
        r"backend\.core\.ouroboros\.governance\.candidate_generator",
        source,
    )
    assert not matches, (
        f"phase_dispatcher.py Slice 4 must not add candidate_generator import: "
        f"matched {matches!r}"
    )


# ===========================================================================
# (12) phase_dispatcher wiring — enforce branch present + gated
# ===========================================================================


def test_phase_dispatcher_has_enforce_branch():
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()
    # Enforce branch + lazy import of enforce_evaluate_fanout.
    assert "enforce_evaluate_fanout" in source
    assert "_master_on()" in source and "_enforce_on()" in source
    # Fanout result stored in extras (not mutating phase flow yet).
    assert "parallel_dispatch_fanout_result" in source


def test_phase_dispatcher_enforce_branch_uses_narrow_catches():
    """Operator directive: enforce path fails loud. The enforce branch
    should NOT have a broad 'except Exception' wrapper like the shadow
    branch does."""
    dispatcher_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "phase_dispatcher.py"
    )
    source = dispatcher_path.read_text()

    # Pattern: find the enforce branch body (between "if _master_on() and _enforce_on():"
    # and the next elif/else at the same indentation level). Simple
    # string search — the branch should NOT contain a broad
    # 'except Exception' wrap around the enforce call.
    enforce_idx = source.find("if _master_on() and _enforce_on():")
    shadow_elif_idx = source.find("elif _master_on() and _shadow_on():", enforce_idx)
    assert enforce_idx >= 0 and shadow_elif_idx > enforce_idx, (
        "enforce branch structure not found as expected"
    )
    enforce_body = source[enforce_idx:shadow_elif_idx]
    # Enforce body does NOT contain 'except Exception' wrap around the
    # enforce_evaluate_fanout call. (The broad catch lives only in the
    # shadow branch per operator's loud-fail directive.)
    broad_catches = re.findall(r"except\s+Exception", enforce_body)
    assert not broad_catches, (
        f"enforce branch violates loud-fail directive with broad exception: "
        f"{broad_catches}"
    )
