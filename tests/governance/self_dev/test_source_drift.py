# tests/governance/self_dev/test_source_drift.py
"""Tests for source-drift check and winner traceability."""
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.op_context import (
    OperationContext, OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


def _make_multi_pass():
    ar = AdapterResult(
        adapter="python", passed=True, failure_class="none",
        test_result=TestResult(passed=True, total=1, failed=0,
                               failed_tests=(), duration_seconds=0.1, stdout="", flake_suspected=False),
        duration_s=0.1,
    )
    return MultiAdapterResult(adapter_results=(ar,), passed=True,
                              dominant_failure=None, total_duration_s=0.1)


def _make_orch_with_drift(source_hash_matches: bool, tmp_path: Path):
    """Build orchestrator where the target file's current hash may or may not match."""
    target_file = tmp_path / "foo.py"
    current_content = "x = 1\n"
    target_file.write_text(current_content)
    current_hash = hashlib.sha256(current_content.encode()).hexdigest()

    # The candidate's source_hash either matches or was set from a different version
    candidate_source_hash = current_hash if source_hash_matches else "deadbeef" * 8

    full_content = "x = 2\n"  # the new content
    candidate = {
        "candidate_id": "c1",
        "file_path": "foo.py",
        "full_content": full_content,
        "rationale": "increment x",
        "candidate_hash": hashlib.sha256(full_content.encode()).hexdigest(),
        "source_hash": candidate_source_hash,
        "source_path": "foo.py",
    }

    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    runner = MagicMock()
    runner.run = AsyncMock(return_value=_make_multi_pass())

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=(candidate,),
        provider_name="test", generation_duration_s=0.1, model_id="llama-3",
        is_noop=False,
    ))

    config = OrchestratorConfig(project_root=tmp_path, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger, target_file


def _ctx(tmp_path):
    return OperationContext.create(
        target_files=("foo.py",),
        description="increment x",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


async def test_source_drift_cancels_op(tmp_path):
    """File changed since generation → CANCELLED with source_drift_detected."""
    orch, ledger, target_file = _make_orch_with_drift(source_hash_matches=False, tmp_path=tmp_path)
    terminal = await orch.run(_ctx(tmp_path))
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("source_drift_detected" in e for e in entries), \
        f"No source_drift_detected in ledger: {entries}"


async def test_no_source_drift_proceeds_to_apply(tmp_path):
    """File unchanged → drift check passes, pipeline does NOT cancel with source_drift_detected."""
    orch, ledger, _ = _make_orch_with_drift(source_hash_matches=True, tmp_path=tmp_path)
    terminal = await orch.run(_ctx(tmp_path))
    entries = [str(c) for c in ledger.append.call_args_list]
    assert not any("source_drift_detected" in e for e in entries), \
        f"source_drift_detected should not appear: {entries}"


async def test_winner_ledger_contains_candidate_id_and_hash(tmp_path):
    """Winning candidate's id and hash appear in the validation_complete ledger entry."""
    orch, ledger, _ = _make_orch_with_drift(source_hash_matches=True, tmp_path=tmp_path)
    await orch.run(_ctx(tmp_path))
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("validation_complete" in e for e in entries), \
        f"No validation_complete entry: {entries}"
    assert any("winning_candidate_id" in e for e in entries)
    assert any("winning_candidate_hash" in e for e in entries)
