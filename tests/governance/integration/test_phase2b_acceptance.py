"""
Phase 2B acceptance tests: enriched generation + multi-candidate sequential validation.

Covers all Phase 2B guarantees end-to-end.
"""
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import (
    OperationContext, OperationPhase,
)
from backend.core.ouroboros.governance.orchestrator import GovernedOrchestrator, OrchestratorConfig
from backend.core.ouroboros.governance.test_runner import AdapterResult, MultiAdapterResult, TestResult

REPO_ROOT = Path(__file__).resolve().parents[3]


# ── shared helpers ────────────────────────────────────────────────────────


def _candidate(cid, content="x = 1\n", file_path="backend/core/foo.py", source_hash=None):
    full = content
    ch = hashlib.sha256(full.encode()).hexdigest()
    sh = source_hash or hashlib.sha256(b"original").hexdigest()
    return {
        "candidate_id": cid,
        "file_path": file_path,
        "full_content": full,
        "rationale": f"approach {cid}",
        "candidate_hash": ch,
        "source_hash": sh,
        "source_path": file_path,
    }


def _make_test_result(passed: bool) -> TestResult:
    return TestResult(
        passed=passed, total=1, failed=0 if passed else 1,
        failed_tests=() if passed else ("test_x",),
        duration_seconds=0.1, stdout="", flake_suspected=False,
    )


def _multi(passed: bool, failure_class: str = "none") -> MultiAdapterResult:
    """Build a MultiAdapterResult with a single python adapter."""
    adapter_fc = failure_class if not passed else "none"
    ar = AdapterResult(
        adapter="python", passed=passed,
        failure_class=adapter_fc,
        test_result=_make_test_result(passed),
        duration_s=0.1,
    )
    dominant = None if passed else ar
    return MultiAdapterResult(
        adapter_results=(ar,), passed=passed,
        dominant_failure=dominant,
        total_duration_s=0.1,
    )


def _make_orch(candidates, runner, project_root=REPO_ROOT):
    """Build a GovernedOrchestrator with mocked stack and provided candidates."""
    mock_ledger = MagicMock()
    mock_ledger.append = AsyncMock()
    mock_stack = MagicMock()
    mock_stack.ledger = mock_ledger
    mock_stack.risk_engine.classify.return_value = MagicMock(
        tier=MagicMock(name="SAFE_AUTO"), reason_code="safe"
    )
    mock_stack.comm.emit_heartbeat = AsyncMock()
    mock_stack.can_write.return_value = (True, "")

    mock_gen = MagicMock()
    mock_gen.generate = AsyncMock(return_value=MagicMock(
        candidates=tuple(candidates),
        provider_name="gcp-jprime",
        generation_duration_s=0.5,
        model_id="llama-3.3-70b",
        is_noop=False,
    ))

    config = OrchestratorConfig(project_root=project_root, max_validate_retries=0)
    orch = GovernedOrchestrator(
        stack=mock_stack, generator=mock_gen,
        approval_provider=MagicMock(), config=config,
        validation_runner=runner,
    )
    return orch, mock_ledger


def _ctx():
    return OperationContext.create(
        target_files=("backend/core/foo.py",),
        description="improve foo",
        pipeline_deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=300),
    )


# ── AC1: First passing candidate wins, stops immediately ─────────────────


async def test_first_passing_candidate_wins():
    """c1 passes → pipeline uses c1, c2 never validated."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    with patch.object(GovernedOrchestrator, "_check_source_drift", return_value=None):
        await orch.run(_ctx())
    # Runner called exactly once (c1 passed, c2 and c3 skipped)
    assert runner.run.call_count == 1


async def test_c1_fails_c2_passes_c3_not_tried():
    """c1 fails tests, c2 passes → c3 never validated."""
    call_count = 0

    async def mock_run(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return _multi(passed=(call_count == 2))  # c1=fail, c2=pass

    runner = MagicMock()
    runner.run = mock_run
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    with patch.object(GovernedOrchestrator, "_check_source_drift", return_value=None):
        await orch.run(_ctx())
    assert call_count == 2  # c1 + c2, not c3


# ── AC2: No valid candidates → CANCELLED(no_candidate_valid) ─────────────


async def test_all_candidates_fail_produces_no_candidate_valid():
    """All 3 candidates fail → CANCELLED + ledger reason_code=no_candidate_valid."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2"), _candidate("c3")],
        runner,
    )
    terminal = await orch.run(_ctx())
    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("no_candidate_valid" in e for e in entries)
    assert runner.run.call_count == 3  # all tried


# ── AC3: Per-candidate ledger provenance ─────────────────────────────────


async def test_per_candidate_ledger_has_required_fields():
    """Each validated candidate has a ledger entry with id, hash, outcome, failure_class."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="test"))
    c1 = _candidate("c1")
    orch, ledger = _make_orch([c1], runner)
    await orch.run(_ctx())

    candidate_entries = [
        call.args[0] for call in ledger.append.call_args_list
        if hasattr(call.args[0], "data") and
        call.args[0].data.get("event") == "candidate_validated"
    ]
    assert len(candidate_entries) >= 1
    entry = candidate_entries[0]
    assert entry.data["candidate_id"] == "c1"
    assert "candidate_hash" in entry.data
    assert entry.data["validation_outcome"] in ("pass", "fail")
    assert "failure_class" in entry.data
    assert "duration_s" in entry.data
    assert "provider" in entry.data
    assert "model" in entry.data


# ── AC4: Winner traceability ──────────────────────────────────────────────


async def test_winner_traceability_in_ledger():
    """Passing candidate → ledger entry with winning_candidate_id + winning_candidate_hash."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    c1 = _candidate("c1")
    orch, ledger = _make_orch([c1], runner)

    with patch.object(GovernedOrchestrator, "_check_source_drift", return_value=None):
        await orch.run(_ctx())

    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("validation_complete" in e for e in entries)
    assert any("winning_candidate_id" in e for e in entries)
    assert any("c1" in e for e in entries)


# ── AC5: Source drift detection ───────────────────────────────────────────


async def test_source_drift_cancels_before_apply():
    """File drifted since generation → CANCELLED(source_drift_detected) before APPLY."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=True))
    c1 = _candidate("c1")
    orch, ledger = _make_orch([c1], runner)

    with patch.object(GovernedOrchestrator, "_check_source_drift",
                      return_value="different_hash_than_expected"):
        terminal = await orch.run(_ctx())

    assert terminal.phase == OperationPhase.CANCELLED
    entries = [str(c) for c in ledger.append.call_args_list]
    assert any("source_drift_detected" in e for e in entries)


# ── AC6: op_id continuity ─────────────────────────────────────────────────


async def test_op_id_in_all_candidate_ledger_entries():
    """All candidate_validated ledger entries share the operation's op_id."""
    runner = MagicMock()
    runner.run = AsyncMock(return_value=_multi(passed=False, failure_class="test"))
    orch, ledger = _make_orch(
        [_candidate("c1"), _candidate("c2")], runner
    )
    ctx = _ctx()
    await orch.run(ctx)

    for call in ledger.append.call_args_list:
        entry = call.args[0]
        assert entry.op_id == ctx.op_id, f"op_id mismatch: {entry.op_id} != {ctx.op_id}"
