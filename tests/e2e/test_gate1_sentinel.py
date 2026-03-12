"""Gate 1 -- Deterministic sentinel E2E tests for B+ saga lifecycle.

These tests use real git repos (created in tmp_path) and exercise the full
SagaApplyStrategy B+ path without requiring J-Prime.

Tests cover:
1. Ephemeral branch creation and checkout
2. Commit identity (JARVIS Ouroboros author)
3. FF-only promotion to target branch
4. Rollback on apply failure (branch deletion)
5. TARGET_MOVED abort when target branch advances
6. Dirty worktree rejection
7. Deterministic lock acquisition order
8. Orphan branch detection
9. Full two-repo apply + promote cycle
10. Feature flag fallback to legacy path
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.repo_lock import RepoLockManager
from backend.core.ouroboros.governance.saga.saga_apply_strategy import SagaApplyStrategy
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)


def _make_ctx(
    repo_scope: Tuple[str, ...],
    repo_snapshots: Tuple[Tuple[str, str], ...] = (),
    op_id: str = "e2e-op-001",
) -> OperationContext:
    return OperationContext.create(
        target_files=("sentinel.py",),
        description="E2E saga test operation",
        op_id=op_id,
        repo_scope=repo_scope,
        repo_snapshots=repo_snapshots,
        saga_id=f"saga-{op_id}",
    )


def _make_patch(repo: str, file_path: str = "src/new_feature.py", content: str = "# generated\n") -> RepoPatch:
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
        new_content=((file_path, content.encode()),),
    )


class TestG1EphemeralBranchCreation:
    """Test 1: Ephemeral branch is created and checked out."""

    async def test_saga_creates_ephemeral_branch(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Should be on ephemeral branch
        git_result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        current_branch = git_result.stdout.strip()
        assert current_branch.startswith("ouroboros/saga-")


class TestG1CommitIdentity:
    """Test 2: Commits use JARVIS Ouroboros identity."""

    async def test_commit_author_is_jarvis(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        await strategy.execute(ctx, patch_map)

        git_result = subprocess.run(
            ["git", "log", "-1", "--format=%an <%ae>"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        author = git_result.stdout.strip()
        assert "JARVIS Ouroboros" in author
        assert "ouroboros@jarvis.local" in author


class TestG1FFOnlyPromote:
    """Test 3: Promote uses ff-only merge."""

    async def test_promote_advances_main(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Promote
        state, promoted = await strategy.promote_all(
            apply_order=["jarvis"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert state == SagaTerminalState.SAGA_SUCCEEDED
        assert "jarvis" in promoted

        # Main should have advanced
        git_result = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert git_result.stdout.strip() == promoted["jarvis"]
        assert promoted["jarvis"] != shas["jarvis"]


class TestG1RollbackOnFailure:
    """Test 4: Failed apply rolls back (cleans ephemeral branches)."""

    async def test_rollback_cleans_branches(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True, keep_failed_saga_branches=False,
        )
        # Create a patch that will fail -- MODIFY requires preimage and the
        # target file must exist. Using a path that does not exist will cause
        # write_bytes to raise FileNotFoundError (parent dir does not exist for
        # MODIFY since it doesn't mkdir).
        bad_patch = RepoPatch(
            repo="jarvis",
            files=(PatchedFile(path="nonexistent_dir/deep/path.py", op=FileOp.MODIFY, preimage=b"old"),),
            new_content=(("nonexistent_dir/deep/path.py", b"new"),),
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        result = await strategy.execute(ctx, {"jarvis": bad_patch})
        assert result.terminal_state == SagaTerminalState.SAGA_ROLLED_BACK

        # Should be back on main
        git_result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert git_result.stdout.strip() == "main"


class TestG1TargetMovedAbort:
    """Test 5: TARGET_MOVED aborts promote when target branch advances."""

    async def test_promote_aborts_on_target_moved(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Record the saga branch name for re-checkout later
        saga_branch = list(strategy._saga_branches.values())[0]

        # Advance main externally (simulate another push)
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )
        (roots["jarvis"] / "external.py").write_text("# external\n")
        subprocess.run(
            ["git", "add", "external.py"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "external push", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )

        # Switch back to saga branch so promote_all finds us in the right state
        subprocess.run(
            ["git", "checkout", saga_branch],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )

        state, promoted = await strategy.promote_all(
            apply_order=["jarvis"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert state == SagaTerminalState.SAGA_PARTIAL_PROMOTE
        assert "jarvis" not in promoted


class TestG1DirtyTreeRejection:
    """Test 6: Dirty worktree is rejected."""

    async def test_dirty_tree_prevents_saga(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        # Make jarvis dirty (staged change)
        (roots["jarvis"] / "sentinel.py").write_text("# modified\n")
        subprocess.run(
            ["git", "add", "sentinel.py"],
            cwd=str(roots["jarvis"]), check=True, capture_output=True,
        )

        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}

        with pytest.raises(RuntimeError, match="dirty_worktree"):
            await strategy.execute(ctx, patch_map)


class TestG1DeterministicLockOrder:
    """Test 7: Locks are acquired in sorted order."""

    async def test_lock_order_is_sorted(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        mgr = RepoLockManager()
        order: list = []
        orig = mgr._acquire_single

        async def track(repo, root):
            order.append(repo)
            await orig(repo, root)

        mgr._acquire_single = track  # type: ignore[assignment]
        await mgr.acquire(["reactor-core", "jarvis", "prime"], roots)
        assert order == ["jarvis", "prime", "reactor-core"]
        await mgr.release(["jarvis", "prime", "reactor-core"])


class TestG1OrphanBranchDetection:
    """Test 8: Detect leftover saga branches."""

    def test_detect_orphan_branches(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        # Create an orphan branch in jarvis
        subprocess.run(
            ["git", "branch", "ouroboros/saga-orphaned/jarvis"],
            cwd=str(roots["jarvis"]), check=True,
        )
        mgr = RepoLockManager()
        orphans = mgr.detect_orphan_branches(roots)
        assert any("ouroboros/saga-orphaned" in b for b in orphans)


class TestG1FullTwoRepoApplyPromote:
    """Test 9: Full two-repo apply + promote cycle."""

    async def test_two_repo_full_cycle(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=True,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis", "prime"),
            repo_snapshots=(("jarvis", shas["jarvis"]), ("prime", shas["prime"])),
            op_id="e2e-two-repo-001",
        )
        patch_map = {
            "jarvis": _make_patch("jarvis", "src/jarvis_new.py", "# jarvis feature\n"),
            "prime": _make_patch("prime", "src/prime_new.py", "# prime feature\n"),
        }
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Promote both
        state, promoted = await strategy.promote_all(
            apply_order=["jarvis", "prime"],
            saga_id=result.saga_id,
            op_id=ctx.op_id,
        )
        assert state == SagaTerminalState.SAGA_SUCCEEDED
        assert "jarvis" in promoted
        assert "prime" in promoted

        # Both mains advanced
        for repo in ("jarvis", "prime"):
            git_result = subprocess.run(
                ["git", "rev-parse", "main"],
                cwd=str(roots[repo]), capture_output=True, text=True, check=True,
            )
            assert git_result.stdout.strip() != shas[repo]


class TestG1FeatureFlagFallback:
    """Test 10: Feature flag off falls back to legacy path."""

    async def test_legacy_path_when_flag_off(self, e2e_repo_roots) -> None:
        roots, shas = e2e_repo_roots
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None, branch_isolation=False,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED

        # Should NOT have created any saga branches
        git_result = subprocess.run(
            ["git", "branch", "--list", "ouroboros/saga-*"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True,
        )
        assert "ouroboros/saga-" not in git_result.stdout
