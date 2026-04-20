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


def test_worktree_manager_has_reap_orphans() -> None:
    """Structural: WorktreeManager exposes async reap_orphans()."""
    assert hasattr(WorktreeManager, "reap_orphans"), (
        "WorktreeManager must have a 'reap_orphans' method"
    )
    assert inspect.iscoroutinefunction(WorktreeManager.reap_orphans), (
        "WorktreeManager.reap_orphans must be an async method"
    )


# ---------------------------------------------------------------------------
# Reaper helpers
# ---------------------------------------------------------------------------

async def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run git against repo; return (rc, stdout+stderr). Test harness only."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, (out + err).decode()


async def _list_branches(repo: Path) -> list[str]:
    _, blob = await _git(
        repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"
    )
    return [ln.strip() for ln in blob.splitlines() if ln.strip()]


async def _list_worktree_paths(repo: Path) -> list[str]:
    _, blob = await _git(repo, "worktree", "list", "--porcelain")
    paths: list[str] = []
    for ln in blob.splitlines():
        if ln.startswith("worktree "):
            paths.append(ln.split(None, 1)[1].strip())
    return paths


# ---------------------------------------------------------------------------
# Reaper behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reap_orphans_on_clean_repo_returns_zero(tmp_path: Path) -> None:
    """Idempotent: a repo with no orphans reaps nothing and does not raise."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    reaped = await mgr.reap_orphans()
    assert reaped == 0


@pytest.mark.asyncio
async def test_reap_orphans_removes_registered_unit_worktree(tmp_path: Path) -> None:
    """A worktree with 'unit-' branch is reaped (dir + branch + registration)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    wt_path = await mgr.create("unit-abc-graph-xyz")
    assert wt_path.exists()

    pre_branches = await _list_branches(repo_root)
    assert "unit-abc-graph-xyz" in pre_branches

    reaped = await mgr.reap_orphans()
    assert reaped == 1

    assert not wt_path.exists(), "worktree directory must be gone"
    post_branches = await _list_branches(repo_root)
    assert "unit-abc-graph-xyz" not in post_branches, "branch must be deleted"
    post_paths = await _list_worktree_paths(repo_root)
    assert all("unit-abc-graph-xyz" not in p for p in post_paths), (
        "git worktree registration must be gone"
    )


@pytest.mark.asyncio
async def test_reap_orphans_removes_unregistered_on_disk_dir(tmp_path: Path) -> None:
    """A leftover directory under worktree_base that git never knew about is reaped."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    wt_base = tmp_path / "worktrees"
    mgr = WorktreeManager(repo_root=repo_root, worktree_base=wt_base)

    orphan = wt_base / "unit-leftover-from-crash"
    orphan.mkdir(parents=True)
    (orphan / "stale-file.txt").write_text("from a prior run")
    assert orphan.exists()

    reaped = await mgr.reap_orphans()
    assert reaped == 1
    assert not orphan.exists(), "unregistered on-disk orphan must be removed"


@pytest.mark.asyncio
async def test_reap_orphans_preserves_non_unit_worktrees(tmp_path: Path) -> None:
    """A non-'unit-' worktree (e.g. a user-created feature branch) is left alone."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    user_wt = await mgr.create("feature-do-not-touch")
    unit_wt = await mgr.create("unit-xyz-graph-1")

    reaped = await mgr.reap_orphans()
    assert reaped == 1
    assert user_wt.exists(), "non-unit worktree must be preserved"
    assert not unit_wt.exists(), "unit-prefixed worktree must be reaped"

    branches = await _list_branches(repo_root)
    assert "feature-do-not-touch" in branches
    assert "unit-xyz-graph-1" not in branches


@pytest.mark.asyncio
async def test_reap_orphans_deletes_orphan_branch_with_no_worktree(tmp_path: Path) -> None:
    """A 'unit-' branch left behind after its worktree was removed is deleted.

    Without this, resubmitting the same unit_id in a later session would
    fail with 'branch already exists' from git worktree add -b.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    rc, _ = await _git(repo_root, "branch", "unit-stale-branch-only")
    assert rc == 0
    pre = await _list_branches(repo_root)
    assert "unit-stale-branch-only" in pre

    mgr = WorktreeManager(repo_root=repo_root)
    await mgr.reap_orphans()

    post = await _list_branches(repo_root)
    assert "unit-stale-branch-only" not in post


@pytest.mark.asyncio
async def test_reap_orphans_idempotent(tmp_path: Path) -> None:
    """Calling reap twice on an already-reaped state returns 0 the second time."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    await _init_git_repo(repo_root)

    mgr = WorktreeManager(repo_root=repo_root)
    await mgr.create("unit-first")

    first = await mgr.reap_orphans()
    second = await mgr.reap_orphans()

    assert first == 1
    assert second == 0
