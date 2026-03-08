# tests/governance/self_dev/test_candidate_ledger.py
"""Tests for per-candidate ledger entries and new candidate key names."""
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_candidate(candidate_id="c1", content="x = 1\n", file_path="backend/core/foo.py"):
    full = content
    return {
        "candidate_id": candidate_id,
        "file_path": file_path,
        "full_content": full,
        "rationale": "test candidate",
        "candidate_hash": hashlib.sha256(full.encode()).hexdigest(),
        "source_hash": "abc123",
        "source_path": file_path,
    }


def _make_test_result(passed: bool) -> TestResult:
    return TestResult(
        passed=passed, total=1, failed=0 if passed else 1,
        failed_tests=() if passed else ("test_foo",),
        duration_seconds=0.1, stdout="", flake_suspected=False,
    )


def _make_adapter_result(passed: bool, fc: str = "none") -> AdapterResult:
    return AdapterResult(
        adapter="python", passed=passed,
        failure_class=fc,
        test_result=_make_test_result(passed),
        duration_s=0.1,
    )


def _make_multi(passed: bool, failure_class: str = "none") -> MultiAdapterResult:
    ar = _make_adapter_result(passed, failure_class if not passed else "none")
    dominant = None if passed else ar
    return MultiAdapterResult(
        adapter_results=(ar,), passed=passed,
        dominant_failure=dominant,
        total_duration_s=0.1,
    )


def _make_orch(runner):
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")
    mock_stack.change_engine.execute = AsyncMock(return_value=MagicMock(success=True, rolled_back=False))

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=(_make_candidate("c1"),),
        provider_name="test", generation_duration_s=0.1, model_id="llama-3",
    ))

    config = OrchestratorConfig(project_root=REPO_ROOT, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger


def _ctx():
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="test",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


async def test_per_candidate_ledger_entry_on_pass():
    """Passing candidate → ledger entry with candidate_id and outcome=pass."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=True))
    orch, ledger = _make_orch(runner)
    await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("candidate_validated" in e for e in entries), f"No candidate_validated entry: {entries}"
    assert any("c1" in e for e in entries)
    assert any("pass" in e for e in entries)


async def test_per_candidate_ledger_entry_on_fail():
    """Failing candidate → ledger entry with failure_class recorded."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(runner)
    await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("candidate_validated" in e for e in entries)
    assert any("test" in e for e in entries)


async def test_no_candidate_valid_ledger_reason():
    """All candidates fail → CANCELLED with reason_code=no_candidate_valid in ledger."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(runner)
    terminal = await orch.run(_ctx())
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("no_candidate_valid" in e for e in entries)


async def test_run_validation_uses_file_path_and_full_content_keys():
    """_run_validation reads file_path and full_content, not old 'file'/'content' keys."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi(passed=True))
    orch, _ = _make_orch(runner)

    candidate = _make_candidate("c1", "x = 42\n")
    ctx = _ctx()
    result = await orch._run_validation(ctx, candidate, remaining_s=60.0)
    assert result.passed is True
    runner.run.assert_called_once()
