"""Parity tests for :class:`VALIDATERunner` (Wave 2 (5) Slice 4a.1).

Verbatim transcription of orchestrator.py lines ~4693-5440 (VALIDATE
phase body through advance-to-GATE transition). Covers:

* Seven terminal exit paths with observable state
* Nested retry FSM (validate_retries_remaining loop)
* L2 self-repair dispatch + deadline reconciliation
* Source-drift check (Manifesto §6 tie-in)
* best_candidate artifact threading (CLASSIFY→PLAN pattern)
* Read-only APPLY short-circuit (Manifesto §1)
* Exception swallow invariants

Tests intentionally exercise both short-circuit (no-retries) and
deep paths (L2 converged → GATE) per operator directive.

Authority invariant: no candidate_generator / iron_gate / change_engine / gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.validate_runner import (
    VALIDATERunner,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeSerpent:
    def __init__(self):
        self.updates: List[str] = []
        self.stopped: Optional[bool] = None

    def update_phase(self, p: str):
        self.updates.append(p)

    async def stop(self, success: bool):
        self.stopped = success


class _FakeComm:
    def __init__(self):
        self.heartbeats: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self._transports: List[Any] = []

    async def emit_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)

    async def emit_decision(self, **kwargs):
        self.decisions.append(kwargs)


class _FakeStack:
    def __init__(self):
        self.comm = _FakeComm()
        self.learning_bridge = None


@dataclass
class _FakeConfig:
    project_root: Path
    max_validate_retries: int = 0  # 0 = no retries (short-circuit), >0 = loop
    max_generate_retries: int = 3
    validation_timeout_s: float = 30.0
    repair_engine: Any = None
    shadow_harness: Any = None


@dataclass
class _FakeGeneration:
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    tool_execution_records: List[Any] = field(default_factory=list)
    provider_name: str = "test"
    model_id: str = "test-model"


def _mk_validation(passed: bool, failure_class: str = "") -> ValidationResult:
    return ValidationResult(
        passed=passed,
        best_candidate=None,
        failure_class=failure_class,
        short_summary="ok" if passed else "fail",
        error="" if passed else "some error",
        validation_duration_s=0.01,
        adapter_names_run=("pytest",),
    )


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _pre_action_narrator: Any = None
    _generator: Any = None
    _run_validation_result: Any = None
    _run_validation_raise: bool = False
    _l2_directive: Any = None
    _check_source_drift_result: Optional[str] = None
    ledger_records: List = field(default_factory=list)

    async def _run_validation(self, ctx, cand, remaining_s):
        if self._run_validation_raise:
            raise RuntimeError("validation boom")
        return self._run_validation_result

    async def _record_ledger(self, ctx, state, extra):
        self.ledger_records.append((ctx.phase, state, extra))

    async def _l2_hook(self, ctx, bv, deadline):
        return self._l2_directive

    def _check_source_drift(self, candidate, project_root):
        return self._check_source_drift_result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _validate_ctx(tmp_path: Path) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    return (
        OperationContext.create(
            target_files=(str(tmp_path / "a.py"),),
            description="validate parity",
        )
        .advance(OperationPhase.ROUTE)
        .advance(OperationPhase.GENERATE)
    )


@pytest.fixture
def ctx(tmp_path):
    return _validate_ctx(tmp_path)


def _orch(tmp_path, **overrides):
    cfg_overrides = {k: v for k, v in overrides.items() if k.startswith("cfg_")}
    for k in cfg_overrides:
        del overrides[k]
    cfg = _FakeConfig(project_root=tmp_path, **{k[4:]: v for k, v in cfg_overrides.items()})
    orch_kw = dict(_stack=_FakeStack(), _config=cfg)
    orch_kw.update(overrides)
    return _FakeOrchestrator(**orch_kw)


def _generation(n_cands: int = 1) -> _FakeGeneration:
    return _FakeGeneration(
        candidates=[
            {
                "candidate_id": f"c{i}",
                "candidate_hash": f"hash{i}",
                "file_path": "a.py",
                "full_content": "x = 1\n",
                "source_hash": "src",
                "source_path": "a.py",
            }
            for i in range(n_cands)
        ],
    )


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_validate_runner_is_phase_runner():
    assert issubclass(VALIDATERunner, PhaseRunner)
    assert VALIDATERunner.phase is OperationPhase.VALIDATE


# ---------------------------------------------------------------------------
# (2) Happy path — single candidate passes, advance to GATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_gate(ctx, tmp_path):
    orch = _orch(tmp_path, _run_validation_result=_mk_validation(True))
    gen = _generation(n_cands=1)

    result = await VALIDATERunner(
        orch, None, generation=gen, generate_retries_remaining=3,
    ).run(ctx)

    assert result.status == "ok"
    assert result.reason == "validated"
    assert result.next_phase is OperationPhase.GATE
    assert result.next_ctx.phase is OperationPhase.GATE
    assert result.next_ctx.validation is not None
    assert result.next_ctx.validation.passed is True

    # best_candidate threaded in artifacts
    assert result.artifacts["best_candidate"] is not None
    assert result.artifacts["best_candidate"]["candidate_id"] == "c0"
    assert result.artifacts["best_validation"] is not None


# ---------------------------------------------------------------------------
# (3) Terminal — validation_infra_failure (non-retryable, short-circuit path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_infra_failure_terminates(ctx, tmp_path):
    orch = _orch(
        tmp_path,
        _run_validation_result=_mk_validation(False, failure_class="infra"),
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "fail"
    assert result.reason == "validation_infra_failure"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.POSTMORTEM
    assert result.next_ctx.terminal_reason_code == "validation_infra_failure"


# ---------------------------------------------------------------------------
# (4) Terminal — validation_budget_exhausted (budget class, short-circuit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_failure_terminates(ctx, tmp_path):
    orch = _orch(
        tmp_path,
        _run_validation_result=_mk_validation(False, failure_class="budget"),
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "fail"
    assert result.reason == "validation_budget_exhausted"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (5) Nested retry — 1 retry allowed, test failure → retry → still fail → L2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_nested_retry_loop_exhausts_to_l2_cancel(ctx, tmp_path):
    """Deep-path parity: retry loop runs max_validate_retries iterations,
    exhausts, dispatches L2 which returns 'cancel'."""
    cancelled_ctx = ctx.advance(
        OperationPhase.CANCELLED, terminal_reason_code="l2_cancelled",
    )
    orch = _orch(
        tmp_path,
        cfg_max_validate_retries=1,  # 1 retry → 2 iterations
        cfg_repair_engine=MagicMock(),  # L2 enabled
        _run_validation_result=_mk_validation(False, failure_class="test"),
        _l2_directive=("cancel", cancelled_ctx),
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "fail"
    assert result.reason == "l2_cancelled"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_nested_retry_l2_converges_advances_to_gate(ctx, tmp_path):
    """Deep-path parity: L2 returns 'break' with a fresh candidate/validation;
    runner proceeds past L2 → source-drift (passes) → GATE."""
    _fresh_cand = {
        "candidate_id": "l2_fresh",
        "candidate_hash": "h_fresh",
        "file_path": "a.py",
        "full_content": "x=2\n",
        "source_hash": "src",
        "source_path": "a.py",
    }
    _fresh_val = _mk_validation(True)
    orch = _orch(
        tmp_path,
        cfg_max_validate_retries=0,  # skip retry, straight to L2
        cfg_repair_engine=MagicMock(),
        _run_validation_result=_mk_validation(False, failure_class="test"),
        _l2_directive=("break", _fresh_cand, _fresh_val),
        _check_source_drift_result=None,  # no drift
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "ok"
    assert result.next_phase is OperationPhase.GATE
    assert result.artifacts["best_candidate"]["candidate_id"] == "l2_fresh"


@pytest.mark.asyncio
async def test_retry_exhausted_no_l2_cancels(ctx, tmp_path):
    """Deep-path parity: retries exhausted, no repair_engine → no_candidate_valid."""
    orch = _orch(
        tmp_path,
        cfg_max_validate_retries=0,
        cfg_repair_engine=None,  # no L2
        _run_validation_result=_mk_validation(False, failure_class="test"),
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "fail"
    assert result.reason == "no_candidate_valid"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (6) Terminal — source_drift_detected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_drift_terminates(ctx, tmp_path):
    orch = _orch(
        tmp_path,
        _run_validation_result=_mk_validation(True),
        _check_source_drift_result="different_hash",
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)

    assert result.status == "fail"
    assert result.reason == "source_drift_detected"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (7) Read-only APPLY short-circuit → COMPLETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_only_short_circuit_completes(tmp_path):
    (tmp_path / "a.py").write_text("pass\n")
    ctx = (
        OperationContext.create(
            target_files=(str(tmp_path / "a.py"),),
            description="read only parity",
            is_read_only=True,
        )
        .advance(OperationPhase.ROUTE)
        .advance(OperationPhase.GENERATE)
    )
    orch = _orch(
        tmp_path, _run_validation_result=_mk_validation(True),
    )
    gen = _generation(n_cands=1)
    serpent = _FakeSerpent()
    result = await VALIDATERunner(orch, serpent, gen, 3).run(ctx)

    assert result.status == "ok"
    assert result.reason == "read_only_complete"
    assert result.next_phase is None  # terminal success
    assert result.next_ctx.phase is OperationPhase.COMPLETE
    # Serpent stopped with success=True
    assert serpent.stopped is True
    # emit_decision fired with read_only_complete outcome
    assert any(
        d.get("outcome") == "read_only_complete"
        for d in orch._stack.comm.decisions
    )


# ---------------------------------------------------------------------------
# (8) FSM telemetry — iter_start log fires per iteration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fsm_emits_iter_start_log(ctx, tmp_path, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="Ouroboros.Orchestrator")
    orch = _orch(tmp_path, _run_validation_result=_mk_validation(True))
    gen = _generation(n_cands=1)
    await VALIDATERunner(orch, None, gen, 3).run(ctx)

    iter_lines = [r for r in caplog.records if "[ValidateRetryFSM]" in r.message and "iter_start" in r.message]
    assert iter_lines, "expected at least one iter_start FSM log"


# ---------------------------------------------------------------------------
# (9) Exception swallow — heartbeat raise does not abort the phase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_raise_is_swallowed(ctx, tmp_path):
    orch = _orch(tmp_path, _run_validation_result=_mk_validation(True))

    async def _bad_hb(**kwargs):
        raise RuntimeError("hb boom")

    orch._stack.comm.emit_heartbeat = _bad_hb  # type: ignore[method-assign]
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)
    assert result.status == "ok"
    assert result.next_phase is OperationPhase.GATE


# ---------------------------------------------------------------------------
# (10) best_candidate artifact threading — present even on failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifacts_key_present_on_all_paths(ctx, tmp_path):
    """Every exit path must expose `best_candidate` key (even if None)
    so the orchestrator hook can safely .get() without KeyError."""
    orch = _orch(
        tmp_path,
        _run_validation_result=_mk_validation(False, failure_class="infra"),
    )
    gen = _generation(n_cands=1)
    result = await VALIDATERunner(orch, None, gen, 3).run(ctx)
    assert "best_candidate" in result.artifacts
    assert "best_validation" in result.artifacts


# ---------------------------------------------------------------------------
# (11) Authority invariant
# ---------------------------------------------------------------------------


def test_validate_runner_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance.phase_runners import validate_runner

    src = inspect.getsource(validate_runner)
    for banned in ("candidate_generator", "iron_gate", "change_engine"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"validate_runner.py must not import {banned}: {s}"
                )


__all__ = []
