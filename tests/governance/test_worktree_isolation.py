# tests/governance/test_worktree_isolation.py
"""WorktreeManager: async git worktree lifecycle for subagent isolation."""
import asyncio
import inspect
import pytest
from pathlib import Path

from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with one empty commit so worktrees work."""
    cmds = [
        ["git", "-C", str(path), "init"],
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        ["git", "-C", str(path), "config", "user.name", "Test"],
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
    ]
    for cmd in cmds:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_and_cleanup(tmp_path: Path) -> None:
    """create() produces a worktree directory; cleanup() removes it."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    wt_path = await mgr.create("test-branch-create-cleanup")

    assert wt_path.exists(), f"Worktree path should exist after create(): {wt_path}"
    assert wt_path.is_dir(), "Worktree path should be a directory"

    await mgr.cleanup(wt_path)

    assert not wt_path.exists(), f"Worktree path should be gone after cleanup(): {wt_path}"


@pytest.mark.asyncio
async def test_cleanup_nonexistent_path_is_safe(tmp_path: Path) -> None:
    """cleanup() on a path that never existed must not raise."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    ghost = tmp_path / "nonexistent-worktree"

    # Must not raise
    await mgr.cleanup(ghost)


def test_worktree_manager_has_create_and_cleanup() -> None:
    """Structural: WorktreeManager exposes async create() and async cleanup()."""
    assert hasattr(WorktreeManager, "create"), "WorktreeManager must have a 'create' method"
    assert hasattr(WorktreeManager, "cleanup"), "WorktreeManager must have a 'cleanup' method"

    assert inspect.iscoroutinefunction(WorktreeManager.create), (
        "WorktreeManager.create must be an async method"
    )
    assert inspect.iscoroutinefunction(WorktreeManager.cleanup), (
        "WorktreeManager.cleanup must be an async method"
    )
