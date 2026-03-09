"""Tests for SagaApplyStrategy."""
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

from backend.core.ouroboros.governance.op_context import OperationContext, SagaStepStatus
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
    # Initialize git repo so that `git add` succeeds during apply
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
    )

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
    # Initialize git repo so that `git add` succeeds during apply
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path,
        capture_output=True,
        check=True,
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
    )
    assert order.index("jarvis") < order.index("prime")


async def test_compensation_failure_returns_saga_stuck(tmp_path):
    """When compensation fails for an applied repo, returns SAGA_STUCK.

    Setup: two repos in apply order [repo_a, repo_b].
    - repo_a applies successfully.
    - repo_b fails to apply (missing repo root → FileNotFoundError).
    - Compensation of repo_a raises OSError → compensation_failed.
    Result: SAGA_STUCK, repo_a.status == COMPENSATION_FAILED.
    """
    # repo_a is a real git repo; repo_b's root is intentionally missing to force apply failure
    repo_a_root = tmp_path / "repo_a"
    repo_a_root.mkdir()
    subprocess.run(["git", "init"], cwd=repo_a_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=repo_a_root,
        capture_output=True,
        check=True,
    )
    repo_a_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_a_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Pre-create file so repo_a MODIFY patch succeeds
    (repo_a_root / "a.py").write_bytes(b"old a")

    patch_map = {
        "repo_a": RepoPatch(
            repo="repo_a",
            files=(PatchedFile(path="a.py", op=FileOp.MODIFY, preimage=b"old a"),),
            new_content=(("a.py", b"new a"),),
        ),
        "repo_b": RepoPatch(
            repo="repo_b",
            files=(PatchedFile(path="b.py", op=FileOp.CREATE, preimage=None),),
            new_content=(("b.py", b"new b"),),
        ),
    }

    # repo_b root is missing → _apply_patch will raise FileNotFoundError
    missing_root = tmp_path / "nonexistent_repo_b"

    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(
        repo_roots={"repo_a": repo_a_root, "repo_b": missing_root},
        ledger=ledger,
    )

    ctx = OperationContext.create(
        target_files=("a.py", "b.py"),
        description="stuck test",
        primary_repo="repo_a",
        repo_scope=("repo_a", "repo_b"),
        apply_plan=("repo_a", "repo_b"),
        repo_snapshots=(("repo_a", repo_a_head),),  # no snapshot for repo_b → no TOCTOU check
        saga_id="stuck-001",
    )

    # Make compensation of repo_a raise to simulate COMPENSATION_FAILED
    async def failing_compensate(repo: str, _patch) -> None:  # noqa: ANN001
        raise OSError(f"Cannot restore {repo}: permission denied")

    strategy._compensate_patch = failing_compensate

    result = await strategy.execute(ctx, patch_map)

    assert result.terminal_state == SagaTerminalState.SAGA_STUCK
    assert result.reason_code == "compensation_failed"
    # Per-repo saga_state must record COMPENSATION_FAILED for repo_a
    stuck_statuses = {s.repo: s for s in result.saga_state}
    assert "repo_a" in stuck_statuses
    assert stuck_statuses["repo_a"].status == SagaStepStatus.COMPENSATION_FAILED
    assert stuck_statuses["repo_a"].compensation_attempted is True


async def test_mid_apply_drift_triggers_compensation(tmp_path):
    """TOCTOU guard: HEAD drifts between Phase A and Phase B write → compensation runs.

    This covers the mid-apply drift path (drift_detected_mid_apply reason code).
    The first repo ("prime") applies successfully; the second ("jarvis") detects
    drift and aborts, triggering compensation of "prime".
    """
    # Build two real git repos so HEAD hashes are real
    prime_root = tmp_path / "prime"
    prime_root.mkdir()
    subprocess.run(["git", "init"], cwd=prime_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=prime_root,
        capture_output=True,
        check=True,
    )
    prime_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=prime_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    jarvis_root = tmp_path / "jarvis"
    jarvis_root.mkdir()
    subprocess.run(["git", "init"], cwd=jarvis_root, capture_output=True, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@test.com", "-c", "user.name=Test",
         "commit", "--allow-empty", "-m", "init"],
        cwd=jarvis_root,
        capture_output=True,
        check=True,
    )
    jarvis_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=jarvis_root,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Pre-create files for the patches
    prime_file = prime_root / "p.py"
    prime_file.write_bytes(b"prime old")
    jarvis_file = jarvis_root / "j.py"
    jarvis_file.write_bytes(b"jarvis old")

    patch_map = {
        "prime": RepoPatch(
            repo="prime",
            files=(PatchedFile(path="p.py", op=FileOp.MODIFY, preimage=b"prime old"),),
            new_content=(("p.py", b"prime new"),),
        ),
        "jarvis": RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="j.py", op=FileOp.MODIFY, preimage=b"jarvis old"),),
            new_content=(("j.py", b"jarvis new"),),
        ),
    }

    ledger = MagicMock()
    ledger.append = AsyncMock()

    strategy = SagaApplyStrategy(
        repo_roots={"prime": prime_root, "jarvis": jarvis_root},
        ledger=ledger,
    )

    ctx = OperationContext.create(
        target_files=("p.py", "j.py"),
        description="mid-apply drift test",
        primary_repo="prime",
        repo_scope=("prime", "jarvis"),
        apply_plan=("prime", "jarvis"),  # prime applied first, jarvis second
        repo_snapshots=(("prime", prime_head), ("jarvis", jarvis_head)),
        saga_id="drift-mid-001",
    )

    # Phase A: both pass with correct hashes.
    # During Phase B, prime applies OK; then jarvis HEAD appears drifted.
    call_count = {"n": 0}

    def head_hash_with_mid_drift(repo: str) -> str:
        # Phase A calls prime then jarvis (both correct).
        # Phase B TOCTOU call for prime → correct; for jarvis → drifted.
        call_count["n"] += 1
        if repo == "prime":
            return prime_head
        # jarvis: first call (Phase A) returns real head; second (Phase B) returns drifted
        # The pattern: Phase A iterates [prime, jarvis], then Phase B iterates same order.
        # Prime gets its Phase-B TOCTOU check first (call 3), jarvis gets call 4.
        # We make jarvis always return a drifted hash after the first time it's called.
        if not hasattr(head_hash_with_mid_drift, "_jarvis_seen"):
            head_hash_with_mid_drift._jarvis_seen = True
            return jarvis_head  # Phase A call — return real hash
        return "DRIFTED_HASH"  # Phase B TOCTOU call — simulate drift

    with patch.object(strategy, "_get_head_hash", side_effect=head_hash_with_mid_drift):
        result = await strategy.execute(ctx, patch_map)

    # prime was applied, jarvis drifted → rollback prime → SAGA_ROLLED_BACK
    assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK
    assert result.reason_code == "drift_detected_mid_apply"
    # prime should be compensated (file restored to preimage)
    assert prime_file.read_bytes() == b"prime old"
    # Per-repo statuses
    statuses = {s.repo: s for s in result.saga_state}
    assert statuses["prime"].status == SagaStepStatus.COMPENSATED
    assert statuses["jarvis"].status == SagaStepStatus.FAILED
