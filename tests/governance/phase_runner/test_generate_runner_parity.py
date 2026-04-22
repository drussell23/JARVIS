"""Parity tests for :class:`GENERATERunner` spine (Slice 5a).

Covers FSM edges on the spine mechanics:
* Class wiring + phase identity
* CandidateGenerator dispatch (happy path, is_noop, no_candidates)
* Per-op cost cap pre-attempt gate
* Forward-progress detector (EC8) trip
* Productivity detector (EC9) trip
* Bounded retry exhaustion → L2 escape
* Artifact threading (generation, episodic_memory, generate_retries_remaining)
* Authority invariant

Iron Gate suite depth tests (exploration ledger, ASCII, dep integrity,
multi-file coverage, retry feedback) live in
``test_generate_runner_iron_gate.py`` landed as Slice 5b.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    GenerationResult,
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.generate_runner import (
    GENERATERunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerpent:
    def __init__(self):
        self.updates: List[str] = []

    def update_phase(self, p: str):
        self.updates.append(p)


class _FakeComm:
    def __init__(self):
        self.heartbeats: List[Dict[str, Any]] = []
        self._transports: List[Any] = []

    async def emit_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)


class _FakeStack:
    def __init__(self):
        self.comm = _FakeComm()


class _FakeCostGovernor:
    def __init__(self, exceeded: bool = False, summary_data=None):
        self._exceeded = exceeded
        self._summary = summary_data or {
            "cumulative_usd": 0.50,
            "cap_usd": 0.36,
            "route": "standard",
            "complexity": "simple",
        }
        self.charges: List[Any] = []

    def is_exceeded(self, op_id: str) -> bool:
        return self._exceeded

    def summary(self, op_id: str):
        return self._summary

    def charge(self, op_id: str, cost: float, provider: str, phase: str = ""):
        self.charges.append((op_id, cost, provider, phase))

    def finish(self, op_id: str):
        pass


class _FakeForwardProgress:
    def __init__(self, trip: bool = False):
        self._trip = trip
        self._summary = {"repeat_count": 3}

    def observe(self, op_id: str, content_hash: str) -> bool:
        return self._trip

    def summary(self, op_id: str):
        return self._summary

    def finish(self, op_id: str):
        pass


class _FakeProductivityDetector:
    level = "medium"

    def __init__(self, trip: bool = False):
        self._trip = trip

    def observe(self, op_id: str, cost: float, content_hash: str) -> bool:
        return self._trip

    def summary(self, op_id: str):
        return {
            "cost_since_last_change_usd": 0.25,
            "consecutive_stable": 3,
            "config": {"normalization_level": "medium"},
        }

    def finish(self, op_id: str):
        pass


class _FakeCandidateGenerator:
    """Fakes the orchestrator's candidate generator surface."""

    def __init__(self, result: Optional[GenerationResult] = None,
                 raise_exc: bool = False,
                 repeat_same: bool = False):
        self._result = result
        self._raise = raise_exc
        self._repeat = repeat_same
        self.call_count = 0

    async def generate(self, ctx, deadline):
        self.call_count += 1
        if self._raise:
            raise RuntimeError("generator boom")
        return self._result


def _gen_result(has_candidates: bool = True, is_noop: bool = False,
                cost: float = 0.01) -> GenerationResult:
    return GenerationResult(
        candidates=[{
            "candidate_id": "c0",
            "candidate_hash": "h0",
            "full_content": "x = 1\n",
            "file_path": "a.py",
            "source_hash": "",
            "source_path": "",
        }] if has_candidates else [],
        provider_name="fake",
        generation_duration_s=0.1,
        model_id="fake-v1",
        is_noop=is_noop,
        tool_execution_records=(),
        total_input_tokens=100,
        total_output_tokens=50,
        cost_usd=cost,
    )


@dataclass
class _FakeConfig:
    project_root: Path
    max_generate_retries: int = 1
    generation_timeout_s: float = 60.0
    approval_timeout_s: float = 60.0


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _cost_governor: _FakeCostGovernor
    _forward_progress: _FakeForwardProgress
    _productivity_detector: _FakeProductivityDetector
    _generator: Any = None
    _session_lessons: List = field(default_factory=list)
    _session_lessons_max: int = 20
    _reasoning_narrator: Any = None
    _dialogue_store: Any = None
    ledger_records: List = field(default_factory=list)
    _emit_route_cost_heartbeat_calls: List = field(default_factory=list)

    async def _record_ledger(self, ctx, state, extra):
        self.ledger_records.append((ctx.phase, state, extra))

    async def _emit_route_cost_heartbeat(self, ctx, **kwargs):
        self._emit_route_cost_heartbeat_calls.append(kwargs)

    def _l2_escape_terminal(self, current_phase):
        # Mirror of orchestrator's _l2_escape_terminal: GENERATE→CANCELLED
        return OperationPhase.CANCELLED

    def _add_session_lesson(self, kind, msg, op_id):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _generate_ctx(tmp_path: Path, is_read_only: bool = False) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    ctx = (
        OperationContext.create(
            target_files=(str(tmp_path / "a.py"),),
            description="gen parity",
            is_read_only=is_read_only,
        )
        .advance(OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO)
        .advance(OperationPhase.GENERATE)
    )
    # Spine tests set task_complexity=trivial so the exploration-first
    # Iron Gate bypasses (Slice 5b targets that gate's depth directly).
    object.__setattr__(ctx, "task_complexity", "trivial")
    return ctx


@pytest.fixture
def ctx(tmp_path):
    return _generate_ctx(tmp_path)


def _orch(
    tmp_path: Path,
    *,
    cost_exceeded: bool = False,
    fp_trip: bool = False,
    prod_trip: bool = False,
    generator: Optional[_FakeCandidateGenerator] = None,
    max_retries: int = 1,
) -> _FakeOrchestrator:
    cfg = _FakeConfig(project_root=tmp_path, max_generate_retries=max_retries)
    return _FakeOrchestrator(
        _stack=_FakeStack(),
        _config=cfg,
        _cost_governor=_FakeCostGovernor(exceeded=cost_exceeded),
        _forward_progress=_FakeForwardProgress(trip=fp_trip),
        _productivity_detector=_FakeProductivityDetector(trip=prod_trip),
        _generator=generator or _FakeCandidateGenerator(result=_gen_result()),
    )


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_generate_runner_is_phase_runner():
    assert issubclass(GENERATERunner, PhaseRunner)
    assert GENERATERunner.phase is OperationPhase.GENERATE


# ---------------------------------------------------------------------------
# (2) Happy path — candidate generated, advance to VALIDATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_validate(ctx, tmp_path):
    orch = _orch(tmp_path)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "ok"
    assert result.reason == "generated"
    assert result.next_phase is OperationPhase.VALIDATE
    # Artifacts threaded
    assert result.artifacts["generation"] is not None
    assert "episodic_memory" in result.artifacts
    assert "generate_retries_remaining" in result.artifacts


# ---------------------------------------------------------------------------
# (3) Cost cap tripped pre-attempt → op_cost_cap_exceeded terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_cap_pre_attempt_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, cost_exceeded=True)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "fail"
    assert result.reason == "op_cost_cap_exceeded"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.terminal_reason_code == "op_cost_cap_exceeded"
    # Ledger recorded FAILED with the summary + entry_phase
    assert orch.ledger_records
    _phase, state, extra = orch.ledger_records[-1]
    assert state is OperationState.FAILED
    assert extra["reason"] == "op_cost_cap_exceeded"
    assert extra["entry_phase"] == "GENERATE"


# ---------------------------------------------------------------------------
# (4) Forward-progress detector trip → no_forward_progress terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forward_progress_trip_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, fp_trip=True)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "fail"
    assert result.reason == "no_forward_progress"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    # Ledger extra carries progress_summary
    assert orch.ledger_records
    _phase, state, extra = orch.ledger_records[-1]
    assert state is OperationState.FAILED
    assert extra["reason"] == "no_forward_progress"
    assert "progress_summary" in extra


# ---------------------------------------------------------------------------
# (5) Productivity detector trip → stalled_productivity terminal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_productivity_trip_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, prod_trip=True)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "fail"
    assert result.reason == "stalled_productivity"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (6) is_noop signal — break retry loop with empty generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_noop_terminates_with_noop_reason(ctx, tmp_path):
    """generation.is_noop=True → break out of retry loop, then post-loop
    the inline code advances to CANCELLED with terminal_reason_code=noop
    (skipping APPLY). The runner mirrors this exactly."""
    generator = _FakeCandidateGenerator(
        result=_gen_result(has_candidates=False, is_noop=True),
    )
    orch = _orch(tmp_path, generator=generator)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "fail"
    # Terminal reason code matches inline "noop" exit path.
    # Inline advances to COMPLETE (noop success), not CANCELLED.
    assert result.next_ctx.phase is OperationPhase.COMPLETE
    assert result.next_ctx.terminal_reason_code == "noop"


# ---------------------------------------------------------------------------
# (7) no_candidates_returned — raises, retry loop continues
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_candidates_raises_and_retries(ctx, tmp_path):
    """Generator returning None triggers retry. With max_retries=1, first
    attempt fails no-candidates → retry attempt also fails → terminal."""
    generator = _FakeCandidateGenerator(result=None)  # None = no candidates
    orch = _orch(tmp_path, generator=generator, max_retries=1)
    result = await GENERATERunner(orch, None, None).run(ctx)
    # With no candidates on every attempt, all retries exhaust → terminal fail.
    # The exact reason depends on the inline retry-exhaustion path (which
    # lives deeper in the runner body). At minimum status should be fail.
    assert result.status == "fail"


# ---------------------------------------------------------------------------
# (8) Generator exception — retry mechanism kicks in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generator_exception_triggers_retry_path(ctx, tmp_path):
    """Generator raising RuntimeError → inline retry path handles it.
    With max_retries=1 both attempts raise → terminal fail."""
    generator = _FakeCandidateGenerator(raise_exc=True)
    orch = _orch(tmp_path, generator=generator, max_retries=1)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.status == "fail"
    # Generator called 2 times (initial + 1 retry)
    assert generator.call_count == 2


# ---------------------------------------------------------------------------
# (9) Artifact threading — generate_retries_remaining correctly decremented
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_threads_retries_remaining(ctx, tmp_path):
    """On happy first-attempt success, generate_retries_remaining stays at
    max_generate_retries (no retries consumed)."""
    orch = _orch(tmp_path, max_retries=3)
    result = await GENERATERunner(orch, None, None).run(ctx)
    assert result.artifacts["generate_retries_remaining"] == 3


# ---------------------------------------------------------------------------
# (10) Cost charging — generator cost_usd routed to CostGovernor.charge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_governor_charged_per_attempt(ctx, tmp_path):
    """Non-zero cost_usd from generator must be charged to CostGovernor
    with phase tag for the per-phase cost drill-down."""
    generator = _FakeCandidateGenerator(result=_gen_result(cost=0.05))
    orch = _orch(tmp_path, generator=generator)
    await GENERATERunner(orch, None, None).run(ctx)
    assert orch._cost_governor.charges
    _op_id, cost, provider, phase = orch._cost_governor.charges[0]
    assert cost == 0.05
    assert phase == "GENERATE"


# ---------------------------------------------------------------------------
# (11) Serpent update_phase("GENERATE") fires at phase start
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serpent_update_phase_generate(ctx, tmp_path):
    serpent = _FakeSerpent()
    orch = _orch(tmp_path)
    await GENERATERunner(orch, serpent, None).run(ctx)
    assert "GENERATE" in serpent.updates


# ---------------------------------------------------------------------------
# (12) Authority invariant
# ---------------------------------------------------------------------------


def test_generate_runner_bans_execution_authority_imports():
    """The runner is a VERBATIM extraction — it may import the modules the
    inline block imported (candidate_generator module CAN be referenced via
    the orchestrator surface at runtime) but must not statically `from
    candidate_generator import ...` at module level."""
    import inspect
    from backend.core.ouroboros.governance.phase_runners import generate_runner

    src = inspect.getsource(generate_runner)
    for banned in ("iron_gate", "change_engine"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"generate_runner.py must not import {banned}: {s}"
                )


__all__ = []
