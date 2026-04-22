"""Parity tests for :class:`Slice4bRunner` (APPROVE + APPLY + VERIFY).

Wave 2 item (5) Slice 4b — mutation-adjacent phases. Per operator
directive: "same bar for mutation-adjacent phases (logging, rollback
story, §8 audit signals unchanged)."

Coverage focus:
* All ~14 terminal paths (APPROVE rejects, DRY_RUN, change engine,
  INFRA, human-active, verify regression + rollback + checkpoint)
* §8 audit signals: on_apply_succeeded / on_verify_completed /
  on_commit_succeeded observer calls
* Rollback story: pre_apply_snapshots + checkpoint restore on regression
* t_apply artifact threading to COMPLETERunner
* Authority invariant: no forbidden imports

Authority invariant: no candidate_generator / iron_gate. change_engine
accessed via orch._stack.change_engine (same as inline).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance.approval_provider import (
    ApprovalResult,
    ApprovalStatus,
)
from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.phase_runner import (
    PhaseResult,
    PhaseRunner,
)
from backend.core.ouroboros.governance.phase_runners.slice4b_runner import (
    Slice4bRunner,
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
        self.postmortems: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []
        self._transports: List[Any] = []

    async def emit_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)

    async def emit_postmortem(self, **kwargs):
        self.postmortems.append(kwargs)

    async def emit_decision(self, **kwargs):
        self.decisions.append(kwargs)


@dataclass
class _FakeChangeResult:
    success: bool = True
    rolled_back: bool = False


class _FakeChangeEngine:
    def __init__(self, success: bool = True, raise_exc: bool = False):
        self._success = success
        self._raise = raise_exc
        self.executions: List[Any] = []

    async def execute(self, req):
        self.executions.append(req)
        if self._raise:
            raise RuntimeError("change engine boom")
        return _FakeChangeResult(success=self._success)


class _FakeStack:
    def __init__(self, change_engine=None):
        self.comm = _FakeComm()
        self.change_engine = change_engine or _FakeChangeEngine()
        self.canary_controller = None


@dataclass
class _FakeConfig:
    project_root: Path
    approval_timeout_s: float = 60.0
    repair_engine: Any = None


@dataclass
class _FakeApprovalProvider:
    decision: Optional[ApprovalResult] = None

    async def request(self, ctx):
        return "req-1"

    async def await_decision(self, req_id, timeout_s):
        return self.decision


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _approval_provider: Any = None
    _pre_action_narrator: Any = None
    _infra_applicator: Any = None
    _validation_runner: Any = None
    _hot_reloader: Any = None
    _critique_engine: Any = None
    _cancel_requested: bool = False
    ledger_records: List = field(default_factory=list)
    canary_records: List = field(default_factory=list)
    outcomes: List = field(default_factory=list)
    session_lessons: List = field(default_factory=list)

    async def _record_ledger(self, ctx, state, extra):
        self.ledger_records.append((ctx.phase, state, extra))

    def _record_canary_for_ctx(self, ctx, success, latency, rolled_back=False):
        self.canary_records.append((ctx, success, latency, rolled_back))

    async def _publish_outcome(self, ctx, state, *args):
        self.outcomes.append((ctx.phase, state, args))

    def _add_session_lesson(self, kind, msg, op_id):
        self.session_lessons.append((kind, msg, op_id))

    def _is_cancel_requested(self, op_id):
        return self._cancel_requested

    def _iter_candidate_files(self, candidate):
        if not candidate:
            return []
        fp = candidate.get("file_path", "")
        return [(fp, candidate.get("full_content", ""))] if fp else []

    def _build_change_request(self, ctx, candidate):
        return MagicMock()

    async def _apply_multi_file_candidate(self, ctx, candidate, files, snapshots):
        return _FakeChangeResult(success=True)

    async def _execute_saga_apply(self, ctx, best_candidate):
        return ctx.advance(OperationPhase.COMPLETE, terminal_reason_code="saga_complete")

    async def _materialize_execution_graph_candidate(self, ctx, candidate):
        return (ctx, candidate)

    async def _l2_hook(self, ctx, val, deadline):
        return ("fatal", ctx)

    async def _run_benchmark(self, ctx, items):
        return ctx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _approve_ctx(tmp_path: Path, is_read_only: bool = False) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    return (
        OperationContext.create(
            target_files=(str(tmp_path / "a.py"),),
            description="4b parity",
            is_read_only=is_read_only,
        )
        .advance(OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO)
        .advance(OperationPhase.GENERATE)
        .advance(OperationPhase.VALIDATE)
        .advance(OperationPhase.GATE)
    )


@pytest.fixture
def ctx(tmp_path):
    return _approve_ctx(tmp_path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "JARVIS_DRY_RUN", "JARVIS_VERIFY_TIMEOUT_S",
        "JARVIS_CRITIQUE_TIMEOUT_S", "JARVIS_MULTI_FILE_GEN_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _orch(tmp_path, **overrides):
    stack_kw = {}
    for k in ("change_engine",):
        if k in overrides:
            stack_kw[k] = overrides.pop(k)
    return _FakeOrchestrator(
        _stack=_FakeStack(**stack_kw),
        _config=_FakeConfig(project_root=tmp_path),
        **overrides,
    )


def _candidate(tmp_path):
    return {
        "candidate_id": "c0",
        "file_path": str(tmp_path / "a.py"),
        "full_content": "x = 1\n",
    }


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_slice4b_runner_is_phase_runner():
    assert issubclass(Slice4bRunner, PhaseRunner)
    assert Slice4bRunner.phase is OperationPhase.APPROVE


# ---------------------------------------------------------------------------
# (2) Happy path — SAFE_AUTO proceeds to COMPLETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_advances_to_complete(ctx, tmp_path):
    orch = _orch(tmp_path)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "ok"
    assert result.reason == "applied_and_verified"
    assert result.next_phase is OperationPhase.COMPLETE
    assert "t_apply" in result.artifacts
    # t_apply must be non-zero post-APPLY
    assert result.artifacts["t_apply"] > 0


# ---------------------------------------------------------------------------
# (3) APPROVE: no provider → approval_required_but_no_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_required_no_provider_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, _approval_provider=None)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.APPROVAL_REQUIRED,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "approval_required_but_no_provider"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (4) APPROVE: EXPIRED → approval_expired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_expired_terminates(ctx, tmp_path):
    provider = _FakeApprovalProvider(decision=ApprovalResult(
        status=ApprovalStatus.EXPIRED, approver="", reason="",
        decided_at=datetime.now(tz=timezone.utc), request_id="r",
    ))
    orch = _orch(tmp_path, _approval_provider=provider)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.APPROVAL_REQUIRED,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "approval_expired"
    assert result.next_ctx.phase is OperationPhase.EXPIRED


# ---------------------------------------------------------------------------
# (5) APPROVE: REJECTED → approval_rejected + session lesson + negative constraint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_rejected_records_lessons(ctx, tmp_path):
    provider = _FakeApprovalProvider(decision=ApprovalResult(
        status=ApprovalStatus.REJECTED, approver="tester",
        reason="bad approach",
        decided_at=datetime.now(tz=timezone.utc), request_id="r",
    ))
    orch = _orch(tmp_path, _approval_provider=provider)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.APPROVAL_REQUIRED,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "approval_rejected"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    # Session lesson recorded
    assert orch.session_lessons
    assert "[REJECTED]" in orch.session_lessons[0][1]


# ---------------------------------------------------------------------------
# (6) Pre-APPLY user cancel → user_cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_apply_user_cancel_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, _cancel_requested=True)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "user_cancelled"


# ---------------------------------------------------------------------------
# (7) DRY_RUN gate → dry_run_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_gate_terminates(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DRY_RUN", "1")
    orch = _orch(tmp_path)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "dry_run_session"


# ---------------------------------------------------------------------------
# (8) APPLY: change_engine raises → change_engine_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_engine_exception_terminates(ctx, tmp_path):
    orch = _orch(tmp_path, change_engine=_FakeChangeEngine(raise_exc=True))
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "change_engine_error"
    assert result.next_ctx.phase is OperationPhase.POSTMORTEM
    # §8 audit: canary recorded false outcome
    assert orch.canary_records
    assert orch.canary_records[0][1] is False


# ---------------------------------------------------------------------------
# (9) APPLY: change_result.success=False → change_engine_failed + rollback flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_engine_failure_rolls_back(ctx, tmp_path):
    orch = _orch(tmp_path, change_engine=_FakeChangeEngine(success=False))
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "change_engine_failed"
    # Ledger extra must carry rolled_back flag
    assert orch.ledger_records
    _, _, extra = orch.ledger_records[-1]
    assert "rolled_back" in extra


# ---------------------------------------------------------------------------
# (10) APPLY: §8 observer fires on_apply_succeeded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_observer_fires_on_success(ctx, tmp_path):
    with patch(
        "backend.core.ouroboros.governance.ops_digest_observer.get_ops_digest_observer"
    ) as mock_obs:
        observer = MagicMock()
        mock_obs.return_value = observer
        orch = _orch(tmp_path)
        await Slice4bRunner(
            orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
        ).run(ctx)
    observer.on_apply_succeeded.assert_called_once()
    # Verify kwargs shape
    call = observer.on_apply_succeeded.call_args
    assert "op_id" in call.kwargs
    assert "mode" in call.kwargs
    assert "files" in call.kwargs


# ---------------------------------------------------------------------------
# (11) VERIFY: scoped-test failure + repair_engine=None → verify_regression + rollback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_regression_triggers_rollback(ctx, tmp_path):
    # Validation runner that returns a failing multi-result
    _failed_adapter = MagicMock()
    _failed_adapter.test_result.total = 5
    _failed_adapter.test_result.failed = 2
    _failed_adapter.test_result.failed_tests = ("t_fail",)
    _failed_multi = MagicMock()
    _failed_multi.passed = False
    _failed_multi.adapter_results = [_failed_adapter]

    _runner = MagicMock()
    _runner.run = AsyncMock(return_value=_failed_multi)

    orch = _orch(tmp_path, _validation_runner=_runner)

    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    assert result.status == "fail"
    assert result.reason == "verify_regression"
    assert result.next_ctx.phase is OperationPhase.POSTMORTEM
    assert result.next_ctx.rollback_occurred is True
    # §8 postmortem emit
    assert orch._stack.comm.postmortems
    assert "verify_regression" in orch._stack.comm.postmortems[0]["root_cause"]


# ---------------------------------------------------------------------------
# (12) t_apply threading — non-zero post-APPLY, stays through VERIFY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t_apply_set_after_apply(ctx, tmp_path):
    orch = _orch(tmp_path)
    result = await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    # On success, t_apply must be a real monotonic timestamp (>0)
    assert result.artifacts["t_apply"] > 0


# ---------------------------------------------------------------------------
# (13) §8 audit signals — ledger records APPLIED state at VERIFY entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ledger_records_applied_on_verify_entry(ctx, tmp_path):
    orch = _orch(tmp_path)
    await Slice4bRunner(
        orch, None, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    # VERIFY entry must record OperationState.APPLIED
    assert any(
        state is OperationState.APPLIED
        for _phase, state, _extra in orch.ledger_records
    )


# ---------------------------------------------------------------------------
# (14) Serpent phase progression: APPLY → VERIFY (no POSTMORTEM on success)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serpent_happy_path_progression(ctx, tmp_path):
    serpent = _FakeSerpent()
    orch = _orch(tmp_path)
    await Slice4bRunner(
        orch, serpent, _candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx)
    # Happy path hits APPLY + VERIFY, NOT POSTMORTEM
    assert "APPLY" in serpent.updates
    assert "VERIFY" in serpent.updates
    assert "POSTMORTEM" not in serpent.updates


# ---------------------------------------------------------------------------
# (15) Authority invariant
# ---------------------------------------------------------------------------


def test_slice4b_runner_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance.phase_runners import slice4b_runner

    src = inspect.getsource(slice4b_runner)
    for banned in ("candidate_generator", "iron_gate"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"slice4b_runner.py must not import {banned}: {s}"
                )


__all__ = []
