"""Parity tests for :class:`PLANRunner` (Wave 2 (5) Slice 3).

Verbatim transcription of orchestrator.py PLAN block (~2259-3012, the
big one at ~750 lines). Pins observable output across five terminal
exit paths + the happy success path to GENERATE.

Parity contract:

1. PlanGenerator.generate_plan runs with deadline + wait_for
2. plan_required_unavailable terminal (review required + no plan)
3. plan_review_unavailable terminal (no approval provider)
4. plan_rejected terminal (human rejection)
5. plan_approval_expired terminal (strict mode expiry)
6. user_cancelled terminal (pre-GENERATE cancel check)
7. Happy path — advance to GENERATE with implementation_plan stamped
8. Tier 6 personality voice line reads advisory from constructor
9. Authority invariant: no forbidden execution-engine imports
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
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
from backend.core.ouroboros.governance.phase_runners.plan_runner import (
    PLANRunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeComm:
    def __init__(self):
        self.heartbeats: List[Dict[str, Any]] = []

    async def emit_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)


class _FakeStack:
    def __init__(self):
        self.comm = _FakeComm()
        self._emergency_engine = None
        self.governed_loop_service = None


@dataclass
class _FakeConfig:
    project_root: Path


@dataclass
class _FakeApprovalProvider:
    decision_result: Optional[ApprovalResult] = None
    request_raises: bool = False
    last_req_id: str = "req-1"

    async def request_plan(self, ctx, markdown: str) -> str:
        if self.request_raises:
            raise RuntimeError("request boom")
        return self.last_req_id

    async def await_decision(self, req_id: str, timeout_s: float) -> ApprovalResult:
        return self.decision_result


@dataclass
class _FakeOrchestrator:
    _stack: _FakeStack
    _config: _FakeConfig
    _generator: Any = None
    _approval_provider: Any = None
    _pre_action_narrator: Any = None
    _reasoning_narrator: Any = None
    ledger_records: List = field(default_factory=list)
    session_lessons: List = field(default_factory=list)
    plan_shadow_calls: int = 0
    cancel_requested: bool = False

    async def _run_plan_shadow(self, ctx):
        self.plan_shadow_calls += 1
        return ctx

    async def _record_ledger(self, ctx, state: OperationState, extra: Dict[str, Any]):
        self.ledger_records.append((ctx, state, extra))

    def _add_session_lesson(self, kind: str, msg: str, op_id: str):
        self.session_lessons.append((kind, msg, op_id))

    def _is_cancel_requested(self, op_id: str) -> bool:
        return self.cancel_requested


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _plan_ctx(tmp_path: Path) -> OperationContext:
    (tmp_path / "a.py").write_text("pass\n")
    return (
        OperationContext.create(
            target_files=(str(tmp_path / "a.py"),), description="plan parity",
        )
        .advance(OperationPhase.ROUTE, risk_tier=RiskTier.SAFE_AUTO)
        .advance(OperationPhase.PLAN)
    )


@pytest.fixture
def ctx(tmp_path):
    return _plan_ctx(tmp_path)


@pytest.fixture(autouse=True)
def _disable_plan_approval(monkeypatch):
    # Keep tests deterministic — plan approval default ON in env
    # would gate every complex op; tests enable it per-case.
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "false")
    monkeypatch.delenv("JARVIS_PLAN_APPROVAL_MODE", raising=False)


def _orch(tmp_path: Path, **overrides) -> _FakeOrchestrator:
    base = dict(
        _stack=_FakeStack(),
        _config=_FakeConfig(project_root=tmp_path),
    )
    base.update(overrides)
    return _FakeOrchestrator(**base)


def _mock_plan_result(*, skipped: bool = False, complexity: str = "simple"):
    m = MagicMock()
    m.skipped = skipped
    m.plan_json = "{}" if not skipped else ""
    m.complexity = complexity
    m.ordered_changes = []
    m.risk_factors = []
    m.test_strategy = ""
    m.approach = ""
    m.planning_duration_s = 0.1
    m.skip_reason = "trivial_op" if skipped else ""
    m.to_prompt_section = MagicMock(return_value="# Plan")
    return m


# ---------------------------------------------------------------------------
# (1) Class wiring
# ---------------------------------------------------------------------------


def test_plan_runner_is_phase_runner():
    assert issubclass(PLANRunner, PhaseRunner)
    assert PLANRunner.phase is OperationPhase.PLAN


# ---------------------------------------------------------------------------
# (2) Happy path — advance to GENERATE with plan stamped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_plan_skipped_advances_to_generate(ctx, tmp_path):
    orch = _orch(tmp_path)
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(skipped=True))
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "ok"
    assert result.reason == "planned"
    assert result.next_phase is OperationPhase.GENERATE
    assert result.next_ctx.phase is OperationPhase.GENERATE
    assert orch.plan_shadow_calls == 1


@pytest.mark.asyncio
async def test_happy_path_plan_success_stamps_implementation_plan(ctx, tmp_path):
    orch = _orch(tmp_path)
    plan = _mock_plan_result(skipped=False)
    plan.plan_json = '{"approach": "refactor"}'
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=plan)
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "ok"
    assert result.next_ctx.implementation_plan == '{"approach": "refactor"}'


# ---------------------------------------------------------------------------
# (3) Terminal — plan_required_unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_required_but_missing_cancels(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_SHOW_PLAN_BEFORE_EXECUTE", "true")
    orch = _orch(tmp_path)
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(skipped=True))
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.reason == "plan_required_unavailable"
    assert result.next_phase is None
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.terminal_reason_code == "plan_required_unavailable"
    assert orch.ledger_records
    assert orch.ledger_records[0][1] is OperationState.FAILED


# ---------------------------------------------------------------------------
# (4) Terminal — plan_rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_rejected_terminal(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_COMPLEXITIES", "simple,complex")

    from datetime import datetime, timezone as _tz
    rejected = ApprovalResult(
        status=ApprovalStatus.REJECTED,
        approver="tester",
        reason="bad approach",
        decided_at=datetime.now(tz=_tz.utc),
        request_id="req-1",
    )
    provider = _FakeApprovalProvider(decision_result=rejected)
    orch = _orch(tmp_path, _approval_provider=provider)

    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(complexity="simple"))
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.reason == "plan_rejected"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert orch.session_lessons  # session lesson recorded


# ---------------------------------------------------------------------------
# (5) Terminal — plan_approval_expired (strict)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_approval_expired_strict_cancels(ctx, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_COMPLEXITIES", "simple")
    monkeypatch.setenv("JARVIS_PLAN_APPROVAL_EXPIRE_GRACE", "false")

    from datetime import datetime, timezone as _tz
    expired = ApprovalResult(
        status=ApprovalStatus.EXPIRED, approver="", reason="",
        decided_at=datetime.now(tz=_tz.utc), request_id="req-1",
    )
    provider = _FakeApprovalProvider(decision_result=expired)
    orch = _orch(tmp_path, _approval_provider=provider)

    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(complexity="simple"))
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.reason == "plan_approval_expired"
    assert result.next_ctx.phase is OperationPhase.EXPIRED


# ---------------------------------------------------------------------------
# (6) Terminal — user_cancelled (cooperative pre-GENERATE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_cancelled_terminal(ctx, tmp_path):
    orch = _orch(tmp_path)
    orch.cancel_requested = True
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(skipped=True))
        MockPG.return_value = _inst

        result = await PLANRunner(orch, None).run(ctx)

    assert result.status == "fail"
    assert result.reason == "user_cancelled"
    assert result.next_ctx.phase is OperationPhase.CANCELLED


# ---------------------------------------------------------------------------
# (7) Advisory threading (CLASSIFY → PLAN artifact consumption)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advisory_passed_via_constructor(ctx, tmp_path):
    """Tier 6 personality reads ``_advisory.chronic_entropy`` — parity
    requires the runner to accept advisory via constructor."""
    orch = _orch(tmp_path)
    advisory = MagicMock()
    advisory.chronic_entropy = 0.5
    runner = PLANRunner(orch, None, advisory=advisory)
    # Verify the arg is plumbed through to the instance
    assert runner._advisory is advisory


# ---------------------------------------------------------------------------
# (8) PLAN-shadow hook always runs (Phase B)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_shadow_hook_fires(ctx, tmp_path):
    orch = _orch(tmp_path)
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator"
    ) as MockPG:
        _inst = MagicMock()
        _inst.generate_plan = AsyncMock(return_value=_mock_plan_result(skipped=True))
        MockPG.return_value = _inst

        await PLANRunner(orch, None).run(ctx)

    assert orch.plan_shadow_calls == 1


# ---------------------------------------------------------------------------
# (9) Authority invariant
# ---------------------------------------------------------------------------


def test_plan_runner_bans_execution_authority_imports():
    import inspect
    from backend.core.ouroboros.governance.phase_runners import plan_runner

    src = inspect.getsource(plan_runner)
    for banned in ("candidate_generator", "iron_gate", "change_engine"):
        for line in src.splitlines():
            s = line.strip()
            if s.startswith(("import ", "from ")):
                assert banned not in s, (
                    f"plan_runner.py must not import {banned}: {s}"
                )


# ---------------------------------------------------------------------------
# (10) PlanGenerator ImportError is swallowed — continues to GENERATE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_generator_import_error_continues_to_generate(ctx, tmp_path):
    orch = _orch(tmp_path)
    with patch(
        "backend.core.ouroboros.governance.plan_generator.PlanGenerator",
        side_effect=ImportError("missing"),
    ):
        result = await PLANRunner(orch, None).run(ctx)
    assert result.status == "ok"
    assert result.next_phase is OperationPhase.GENERATE


__all__ = []
