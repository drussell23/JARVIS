"""Tests for B+ branch lifecycle in SagaApplyStrategy."""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict, Tuple

import pytest

from backend.core.ouroboros.governance.op_context import (
    OperationContext,
    RepoSagaStatus,
    SagaStepStatus,
)
from backend.core.ouroboros.governance.saga.saga_apply_strategy import (
    SagaApplyStrategy,
    _safe_branch_name,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)


def _init_repo(path: Path) -> str:
    """Initialize a git repo with one commit on branch 'main'. Returns HEAD SHA."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(path), check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=str(path), check=True,
    )
    (path / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=str(path), check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init", "--no-verify"],
        cwd=str(path), check=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path), capture_output=True, text=True, check=True,
    )
    (path / ".jarvis").mkdir(exist_ok=True)
    return result.stdout.strip()


@pytest.fixture
def git_repos(tmp_path: Path) -> Tuple[Dict[str, Path], Dict[str, str]]:
    """Create two git repos. Returns (repo_roots, base_shas)."""
    roots: Dict[str, Path] = {}
    shas: Dict[str, str] = {}
    for name in ("jarvis", "prime"):
        root = tmp_path / name
        root.mkdir()
        sha = _init_repo(root)
        roots[name] = root
        shas[name] = sha
    return roots, shas


def _make_ctx(
    repo_scope: Tuple[str, ...] = ("jarvis", "prime"),
    repo_snapshots: Tuple[Tuple[str, str], ...] = (),
    op_id: str = "test-op-001",
) -> OperationContext:
    return OperationContext.create(
        target_files=("test.py",),
        description="Test saga operation",
        op_id=op_id,
        repo_scope=repo_scope,
        repo_snapshots=repo_snapshots,
        saga_id=f"saga-{op_id}",
    )


def _make_patch(repo: str, file_path: str = "src/test.py", content: str = "# new\n") -> RepoPatch:
    return RepoPatch(
        repo=repo,
        files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
        new_content=((file_path, content.encode()),),
    )


class TestSafeBranchName:
    def test_normal_op_id(self) -> None:
        result = _safe_branch_name("op-20260311-abc123", "jarvis")
        assert result == "ouroboros/saga-op-20260311-abc123/jarvis"

    def test_truncates_long_op_id(self) -> None:
        long_id = "a" * 100
        result = _safe_branch_name(long_id, "jarvis")
        parts = result.split("/")
        assert len(parts) == 3
        assert parts[0] == "ouroboros"

    def test_sanitizes_special_chars(self) -> None:
        result = _safe_branch_name("op id/with spaces", "repo.name")
        assert " " not in result
        assert result.count("/") == 2


class TestCleanTreeCheck:
    async def test_clean_tree_passes(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        await strategy._assert_clean_worktree("jarvis")

    async def test_dirty_tree_fails(self, git_repos) -> None:
        roots, shas = git_repos
        (roots["jarvis"] / "README.md").write_text("# modified\n")
        subprocess.run(["git", "add", "README.md"], cwd=str(roots["jarvis"]), check=True)

        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        with pytest.raises(RuntimeError, match="dirty_worktree"):
            await strategy._assert_clean_worktree("jarvis")

    async def test_untracked_files_allowed(self, git_repos) -> None:
        roots, shas = git_repos
        (roots["jarvis"] / "untracked.txt").write_text("untracked\n")
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        await strategy._assert_clean_worktree("jarvis")


class TestEphemeralBranchLifecycle:
    async def test_create_and_checkout(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-001")
        assert branch.startswith("ouroboros/saga-")

        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == branch

    async def test_promote_ff_only(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-002")
        strategy._saga_branches = {"jarvis": branch}

        (roots["jarvis"] / "new.py").write_text("# new\n")
        subprocess.run(["git", "add", "new.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "test commit", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )

        promoted_sha = await strategy._promote_ephemeral_branch("jarvis")
        assert promoted_sha != shas["jarvis"]

        result = subprocess.run(
            ["git", "rev-parse", "main"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == promoted_sha

    async def test_target_moved_aborts_promote(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-003")
        strategy._saga_branches = {"jarvis": branch}

        (roots["jarvis"] / "new.py").write_text("# new\n")
        subprocess.run(["git", "add", "new.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "saga commit", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )

        # Advance main (simulate external push)
        subprocess.run(["git", "checkout", "main"], cwd=str(roots["jarvis"]), check=True)
        (roots["jarvis"] / "other.py").write_text("# other\n")
        subprocess.run(["git", "add", "other.py"], cwd=str(roots["jarvis"]), check=True)
        subprocess.run(
            ["git", "commit", "-m", "external", "--no-verify"],
            cwd=str(roots["jarvis"]), check=True,
        )
        subprocess.run(["git", "checkout", branch], cwd=str(roots["jarvis"]), check=True)

        with pytest.raises(RuntimeError, match="TARGET_MOVED"):
            await strategy._check_promote_safe("jarvis")


class TestCompensationViaBranchDelete:
    async def test_cleanup_deletes_branch(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
            keep_failed_saga_branches=False,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        strategy._original_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-004")
        strategy._saga_branches = {"jarvis": branch}

        await strategy._cleanup_ephemeral_branch("jarvis")

        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(roots["jarvis"]), capture_output=True, text=True,
        )
        assert branch not in result.stdout

        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=str(roots["jarvis"]), capture_output=True, text=True, check=True,
        )
        assert result.stdout.strip() == "main"

    async def test_forensics_keeps_branch(self, git_repos) -> None:
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=True,
            keep_failed_saga_branches=True,
        )
        strategy._base_shas = {"jarvis": shas["jarvis"]}
        strategy._original_branches = {"jarvis": "main"}
        strategy._original_shas = {"jarvis": shas["jarvis"]}
        branch = await strategy._create_ephemeral_branch("jarvis", "op-005")
        strategy._saga_branches = {"jarvis": branch}

        await strategy._cleanup_ephemeral_branch("jarvis")

        result = subprocess.run(
            ["git", "branch", "--list", branch],
            cwd=str(roots["jarvis"]), capture_output=True, text=True,
        )
        assert branch in result.stdout.strip()


class TestFeatureFlagFallback:
    async def test_branch_isolation_false_uses_legacy_path(self, git_repos) -> None:
        """When feature flag is off, execute() uses old direct-to-HEAD behavior."""
        roots, shas = git_repos
        strategy = SagaApplyStrategy(
            repo_roots=roots, ledger=None,
            branch_isolation=False,
        )
        ctx = _make_ctx(
            repo_scope=("jarvis",),
            repo_snapshots=(("jarvis", shas["jarvis"]),),
        )
        patch_map = {"jarvis": _make_patch("jarvis")}
        result = await strategy.execute(ctx, patch_map)
        assert result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
