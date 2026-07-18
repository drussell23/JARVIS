"""Slice 2 — workspace-arming integrity (the true APPLY-boundary kill chain).

bt-2026-07-18-200502 postmortem: an op survived exploration, GENERATE, VALIDATE
(first pass), GATE, the universal floor, auto-approval, and the LiveWork gate —
then died at APPLY on ``JARVIS_AUTO_COMMIT_WORKSPACE ... armed but unusable
(no .git in workspace dir)``. Five dominoes: Stage-B marker stamped with
session="" (id not yet exported) → boot reaper ate the just-armed workspace →
the surviving session-nonced BRANCH collided with the later create ("branch
already exists") → the fail-open path left the stale env armed → every op
reaching APPLY died fail-closed.

These tests pin the three arming-layer fixes (the read-side fail-closed seam in
``effective_execution_root`` is deliberately untouched — it must keep failing
LOUD):
  * ``is_valid_git_work_area`` — the C1 validity rule as a reusable predicate.
  * ``WorktreeManager.create_or_reclaim`` — self-heals the session-nonced
    self-debris branch collision; non-collision failures propagate unchanged.
  * armed ⇒ usable — a husk env is cleared, never reused.
"""
from __future__ import annotations

import subprocess

import pytest

from backend.core.ouroboros.governance.autonomous_workspace import (
    is_valid_git_work_area,
)
from backend.core.ouroboros.governance.worktree_manager import WorktreeManager


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    subprocess.run(["git", "init", "-q", str(d)], check=True)
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    (d / "seed.txt").write_text("seed\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-qm", "base")
    return d


# ===========================================================================
# A. is_valid_git_work_area — the C1 predicate
# ===========================================================================


def test_full_checkout_is_valid(repo):
    assert is_valid_git_work_area(repo) is True          # .git DIR


def test_linked_worktree_is_valid(repo, tmp_path):
    wt = tmp_path / "wt"
    r = _git(repo, "worktree", "add", "-b", "t/wt", str(wt))
    assert r.returncode == 0, r.stderr
    assert (wt / ".git").is_file()                        # linked worktree = .git FILE
    assert is_valid_git_work_area(wt) is True


def test_marker_only_husk_is_invalid(tmp_path):
    """THE bt-2026-07-18-200502 shape: a dir containing only .jarvis/."""
    husk = tmp_path / "husk"
    (husk / ".jarvis").mkdir(parents=True)
    assert is_valid_git_work_area(husk) is False


def test_missing_and_file_paths_are_invalid(tmp_path):
    assert is_valid_git_work_area(tmp_path / "nope") is False
    f = tmp_path / "f.txt"
    f.write_text("x")
    assert is_valid_git_work_area(f) is False
    assert is_valid_git_work_area(None) is False          # never raises


# ===========================================================================
# B. create_or_reclaim — self-debris collision healing
# ===========================================================================


@pytest.mark.asyncio
async def test_clean_create_unchanged(repo):
    mgr = WorktreeManager(repo_root=repo)
    wt = await mgr.create_or_reclaim("ouroboros/auto/test-clean-1a2b3c")
    assert is_valid_git_work_area(wt)


@pytest.mark.asyncio
async def test_reclaims_self_debris_branch(repo):
    """The kill-chain collision: the session's branch exists but its worktree
    was reaped. create_or_reclaim deletes the debris branch and succeeds."""
    branch = "ouroboros/auto/test-debris-9f8e7d"
    r = _git(repo, "branch", branch)                      # debris branch, no worktree
    assert r.returncode == 0, r.stderr
    mgr = WorktreeManager(repo_root=repo)

    with pytest.raises(RuntimeError, match="already exists"):
        await mgr.create(branch)                          # plain create dies (the bug)

    wt = await mgr.create_or_reclaim(branch)              # the fix
    assert is_valid_git_work_area(wt)
    assert branch in _git(repo, "branch", "--list", branch).stdout


@pytest.mark.asyncio
async def test_reclaims_husk_directory_too(repo):
    """Debris branch + marker-only husk dir at the target path — reclaim clears
    both and the retry lands a real worktree."""
    branch = "ouroboros/auto/test-husk-5c6d7e"
    _git(repo, "branch", branch)
    mgr = WorktreeManager(repo_root=repo)
    husk = mgr._worktree_base / branch.replace("/", "__")
    (husk / ".jarvis").mkdir(parents=True)                # the husk shape

    wt = await mgr.create_or_reclaim(branch)
    assert is_valid_git_work_area(wt)
    assert (wt / "seed.txt").exists()                     # real checkout, not husk


@pytest.mark.asyncio
async def test_non_collision_failure_propagates_unchanged(tmp_path):
    """A failure that is NOT the branch-collision class must NOT be retried or
    swallowed (no blanket retry — root-cause mandate)."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()
    mgr = WorktreeManager(repo_root=not_a_repo)
    with pytest.raises(RuntimeError) as exc:
        await mgr.create_or_reclaim("ouroboros/auto/test-fail-000000")
    assert "already exists" not in str(exc.value)


# ===========================================================================
# C. armed ⇒ usable (the env contract the harness now enforces)
# ===========================================================================


def test_husk_env_fails_closed_at_read_side(tmp_path, monkeypatch):
    """Pin the READ side unchanged: an armed husk still fails LOUD at
    effective_execution_root — Slice 2 fixes ARMING, never weakens the seam."""
    from backend.core.ouroboros.governance.autonomous_workspace import (
        ExecutionRootInvalid,
        effective_execution_root,
    )
    husk = tmp_path / "husk"
    (husk / ".jarvis").mkdir(parents=True)
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(husk))
    with pytest.raises(ExecutionRootInvalid):
        effective_execution_root(tmp_path)
    # ...and with the env cleared (what the harness now guarantees on any
    # arming failure), the root cleanly falls back to project_root.
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE")
    assert effective_execution_root(tmp_path) == tmp_path
