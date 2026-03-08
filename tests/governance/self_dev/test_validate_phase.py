# tests/governance/self_dev/test_validate_phase.py
"""Tests for the VALIDATE phase with TestRunner integration."""
import pytest
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
    ValidationResult,
)
from backend.core.ouroboros.governance.orchestrator import (
    GovernedOrchestrator,
    OrchestratorConfig,
)
from backend.core.ouroboros.governance.test_runner import (
    AdapterResult,
    MultiAdapterResult,
    TestResult,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_test_result(passed: bool, stdout: str = "") -> TestResult:
    return TestResult(
        passed=passed,
        total=1,
        failed=0 if passed else 1,
        failed_tests=() if passed else ("test_foo::test_bar",),
        duration_seconds=0.1,
        stdout=stdout,
        flake_suspected=False,
    )


def _make_adapter_result(adapter: str, passed: bool, failure_class=None) -> AdapterResult:
    return AdapterResult(
        adapter=adapter,
        passed=passed,
        failure_class=failure_class,
        test_result=_make_test_result(passed),
        duration_s=0.1,
    )


def _make_multi(passed: bool, failure_class=None, adapters=("python",)) -> MultiAdapterResult:
    adapter_results = tuple(
        _make_adapter_result(a, passed, failure_class) for a in adapters
    )
    dominant = next((r for r in adapter_results if not r.passed), None)
    return MultiAdapterResult(
        adapter_results=adapter_results,
        passed=passed,
        dominant_failure=dominant,
        total_duration_s=0.1,
    )


def _make_orch(validation_runner=None, max_retries=0):
    config = OrchestratorConfig(project_root=REPO_ROOT, max_validate_retries=max_retries)
    mock_stack = MagicMock()
    mock_stack.ledger.append = AsyncMock()
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.can_write.return_value = (False, "gate_blocked_for_test")
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=({"file": "backend/core/foo.py", "content": "x = 1\n"},),
        provider_name="test",
        generation_duration_s=0.1,
    ))
    return GovernedOrchestrator(
        stack=mock_stack,
        generator=mock_gen,
        approval_provider=MagicMock(),
        config=config,
        validation_runner=validation_runner,
    )


def _ctx(deadline_s: float = 300.0):
    dl = datetime.now(tz=timezone.utc) + timedelta(seconds=deadline_s)
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="test op",
        pipeline_deadline=dl,
    )


# ── _run_validation unit tests ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_validation_syntax_error_no_subprocess():
    """Candidate with SyntaxError -> ValidationResult.passed=False, runner NOT called."""
    runner = MagicMock()
    runner.run = AsyncMock()
    orch = _make_orch(validation_runner=runner)

    candidate = {"file": "backend/core/foo.py", "content": "def broken(:\n    pass"}
    result = await orch._run_validation(_ctx(), candidate, remaining_s=60.0)

    assert result.passed is False
    assert result.failure_class == "test"
    assert "syntax" in result.short_summary.lower() or "SyntaxError" in result.short_summary
    runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_run_validation_budget_exhausted():
    """remaining_s <= 0 -> ValidationResult with failure_class='budget', runner NOT called."""
    runner = MagicMock()
    runner.run = AsyncMock()
    orch = _make_orch(validation_runner=runner)

    candidate = {"file": "backend/core/foo.py", "content": "x = 1\n"}
    result = await orch._run_validation(_ctx(), candidate, remaining_s=0.0)

    assert result.passed is False
    assert result.failure_class == "budget"
    runner.run.assert_not_called()


@pytest.mark.asyncio
async def test_run_validation_passes_op_id_to_runner():
    """_run_validation passes ctx.op_id as op_id kwarg to validation_runner.run()."""
    multi = _make_multi(passed=True)
    runner = MagicMock()
    runner.run = AsyncMock(return_value=multi)
    orch = _make_orch(validation_runner=runner)

    ctx = _ctx()
    candidate = {"file": "backend/core/foo.py", "content": "x = 1\n"}
    await orch._run_validation(ctx, candidate, remaining_s=60.0)

    call_kwargs = runner.run.call_args
    assert call_kwargs is not None
    passed_op_id = call_kwargs.kwargs.get("op_id")
    assert passed_op_id == ctx.op_id


@pytest.mark.asyncio
async def test_run_validation_maps_pass_result():
    """MultiAdapterResult.passed=True -> ValidationResult.passed=True."""
    multi = _make_multi(passed=True, adapters=("python",))
    runner = MagicMock()
    runner.run = AsyncMock(return_value=multi)
    orch = _make_orch(validation_runner=runner)

    result = await orch._run_validation(_ctx(), {"file": "backend/core/foo.py", "content": "x = 1\n"}, remaining_s=60.0)

    assert result.passed is True
    assert result.failure_class is None
    assert "python" in result.adapter_names_run


@pytest.mark.asyncio
async def test_run_validation_maps_infra_failure():
    """MultiAdapterResult.failure_class='infra' -> ValidationResult.failure_class='infra'."""
    multi = _make_multi(passed=False, failure_class="infra")
    runner = MagicMock()
    runner.run = AsyncMock(return_value=multi)
    orch = _make_orch(validation_runner=runner)

    result = await orch._run_validation(_ctx(), {"file": "backend/core/foo.py", "content": "x = 1\n"}, remaining_s=60.0)

    assert result.passed is False
    assert result.failure_class == "infra"


# ── VALIDATE phase integration tests ─────────────────────────────────────

@pytest.mark.asyncio
async def test_validate_infra_failure_reaches_postmortem():
    """VALIDATE: infra failure -> terminal phase = POSTMORTEM."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="infra"))
    orch = _make_orch(validation_runner=runner)
    terminal_ctx = await orch.run(_ctx())
    assert terminal_ctx.phase == OperationPhase.POSTMORTEM


@pytest.mark.asyncio
async def test_validate_test_failure_reaches_cancelled():
    """VALIDATE: test failure -> terminal phase = CANCELLED."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="test"))
    orch = _make_orch(validation_runner=runner)
    terminal_ctx = await orch.run(_ctx())
    assert terminal_ctx.phase == OperationPhase.CANCELLED


@pytest.mark.asyncio
async def test_validate_pass_advances_past_validate():
    """VALIDATE: pass -> context.validation.passed=True; pipeline advances past VALIDATE."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=True))
    orch = _make_orch(validation_runner=runner)
    terminal_ctx = await orch.run(_ctx())
    # Gate blocks it but validation result must be set and passing
    assert terminal_ctx.validation is not None
    assert terminal_ctx.validation.passed is True
