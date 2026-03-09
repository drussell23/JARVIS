"""Tests for SagaApplyStrategy."""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy


def _make_ctx(
    repo_scope=("jarvis", "prime"),
    apply_plan=("prime", "jarvis"),
    snapshots=(("jarvis", "abc123"), ("prime", "def456")),
):
    return OperationContext.create(
        target_files=("backend/x.py",),
        description="test saga",
        repo_scope=repo_scope,
        primary_repo="jarvis",
        apply_plan=apply_plan,
        repo_snapshots=snapshots,
        saga_id="test-saga-001",
    )


async def test_happy_path_all_repos_applied(tmp_path):
    """All repos apply successfully → SAGA_APPLY_COMPLETED."""
    ctx = _make_ctx()
    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"old content")

    prime_file = tmp_path / "backend" / "y.py"
    prime_file.parent.mkdir(parents=True, exist_ok=True)
    prime_file.write_bytes(b"old prime")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.MODIFY, preimage=b"old prime"),),
            new_content=(("backend/y.py", b"new prime"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"old content"),),
            new_content=(("backend/x.py", b"new content"),),
        ),
    }

    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
    assert jarvis_file.read_bytes() == b"new content"
    assert prime_file.read_bytes() == b"new prime"


async def test_drift_aborts_before_any_apply(tmp_path):
    """HEAD drift detected in pre-flight → SAGA_ABORTED, no files written."""
    ctx = _make_ctx()
    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="backend/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/y.py", b"new"),),
        ),
        "jarvis": RepoPatch(repo="jarvis", files=(), new_content=()),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    # prime HEAD has drifted
    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "DRIFTED"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ABORTED
    assert result.reason_code == "drift_detected"


async def test_apply_failure_triggers_compensation(tmp_path):
    """Second repo apply fails → first repo is compensated."""
    ctx = _make_ctx()

    jarvis_file = tmp_path / "backend" / "x.py"
    jarvis_file.parent.mkdir(parents=True)
    jarvis_file.write_bytes(b"original")

    # prime patch tries to write to a non-existent deep path — will fail
    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="does/not/exist/deeply/y.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("does/not/exist/deeply/y.py", b"content"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.MODIFY, preimage=b"original"),),
            new_content=(("backend/x.py", b"modified"),),
        ),
    }

    # Make prime apply fail by crippling its root path
    bad_prime_root = tmp_path / "nonexistent_prime"
    repo_roots = {"jarvis": tmp_path, "prime": bad_prime_root}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK
    assert jarvis_file.read_bytes() == b"original"


async def test_skipped_repo_with_empty_patch(tmp_path):
    """Repo with empty patch is skipped, result is SAGA_APPLY_COMPLETED."""
    ctx = _make_ctx(
        repo_scope=("jarvis", "prime"),
        apply_plan=("prime", "jarvis"),
    )
    patch_map = {
        "prime": RepoPatch(repo="prime", files=(), new_content=()),  # empty
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="backend/x.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("backend/x.py", b"new"),),
        ),
    }
    repo_roots = {"jarvis": tmp_path, "prime": tmp_path}
    ledger = MagicMock()
    ledger.append = AsyncMock()
    strategy = SagaApplyStrategy(repo_roots=repo_roots, ledger=ledger)

    with patch.object(strategy, "_get_head_hash", side_effect=lambda repo: {"jarvis": "abc123", "prime": "def456"}[repo]):
        result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED


def test_topological_sort_respects_dependency_edges():
    """_topological_sort returns correct apply order from dependency_edges."""
    strategy = SagaApplyStrategy(repo_roots={}, ledger=MagicMock())
    # edge (prime, jarvis) means prime depends on jarvis → jarvis applied first
    order = strategy._topological_sort(
        repo_scope=("jarvis", "prime"),
        edges=(("prime", "jarvis"),),
        apply_plan=(),
    )
    assert order.index("jarvis") < order.index("prime")
