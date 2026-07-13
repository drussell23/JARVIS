"""Slice 11 Task 3 — WorktreeManager promotion primitives (RED first).

Pure git layer, no governance policy: ``WorktreeManager.promote_commits``
moves verified workspace commits onto a target checkout via ``merge
--ff-only`` (when the target head is an ancestor) or ``cherry-pick`` —
NEVER force flags, never ``reset --hard`` on the target. Fail-closed typed
states (``PromotionError.state``); a conflict aborts cleanly and leaves the
target tree byte-identical. This is the mechanism half of the Run-21
promotion gap ("nothing in this codebase merges or cherry-picks it back",
worktree_manager.py quarantine note) — the governance half (LiveWork/drift
gating, AutoCommit 8b integration) is Task 4's WorkspacePromoter.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.worktree_manager import (
    PromotionError,
    WorktreeManager,
)


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "base.txt").write_text("base\n")
    (root / "other.txt").write_text("other\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "base")
    return root


def _workspace_commit(
    repo_root: Path, branch: str, relpath: str, content: str, msg: str,
    create: bool = True,
) -> str:
    """Commit into a workspace worktree of ``repo_root`` on ``branch``;
    return the commit sha (mirrors the ledger-sovereignty workspace)."""
    ws = repo_root.parent / f"ws-{branch.replace('/', '_')}"
    if create:
        _git(repo_root, "worktree", "add", "-b", branch, str(ws))
    target = ws / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", msg)
    return _git(ws, "rev-parse", "HEAD")


def _tree_hash(root: Path) -> str:
    """Content hash of the WORKING TREE (tracked + untracked, minus .git)."""
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and ".git" not in p.parts
    )
    import hashlib
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


class TestPromoteCommitsHappyPaths:
    async def test_ff_when_target_head_is_ancestor(self, repo):
        sha = _workspace_commit(repo, "ouroboros/auto/s1", "fix.py",
                                "x = 1\n", "repair")
        mgr = WorktreeManager(repo_root=repo)
        result = await mgr.promote_commits(repo, "ouroboros/auto/s1", [sha])
        assert result.mode == "ff"
        assert (repo / "fix.py").read_text() == "x = 1\n"
        assert _git(repo, "rev-parse", "HEAD") == sha

    async def test_cherry_pick_when_target_advanced(self, repo):
        sha = _workspace_commit(repo, "ouroboros/auto/s2", "fix.py",
                                "x = 2\n", "repair")
        # Advance the target independently -> ff impossible, cherry-pick.
        (repo / "unrelated.txt").write_text("moved on\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "operator work")
        mgr = WorktreeManager(repo_root=repo)
        result = await mgr.promote_commits(repo, "ouroboros/auto/s2", [sha])
        assert result.mode == "cherry-pick"
        assert (repo / "fix.py").read_text() == "x = 2\n"
        assert (repo / "unrelated.txt").exists()

    async def test_multi_commit_chain_in_order(self, repo):
        s1 = _workspace_commit(repo, "ouroboros/auto/s3", "fix.py",
                               "v1\n", "step1")
        s2 = _workspace_commit(repo, "ouroboros/auto/s3", "fix.py",
                               "v2\n", "step2", create=False)
        (repo / "unrelated.txt").write_text("moved on\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "operator work")
        mgr = WorktreeManager(repo_root=repo)
        result = await mgr.promote_commits(
            repo, "ouroboros/auto/s3", [s1, s2],
        )
        assert result.mode == "cherry-pick"
        assert (repo / "fix.py").read_text() == "v2\n"
        assert len(result.promoted_shas) == 2

    async def test_empty_shas_is_noop(self, repo):
        mgr = WorktreeManager(repo_root=repo)
        before = _git(repo, "rev-parse", "HEAD")
        result = await mgr.promote_commits(repo, "main", [])
        assert result.mode == "none"
        assert result.promoted_shas == ()
        assert _git(repo, "rev-parse", "HEAD") == before


class TestPromoteCommitsFailClosed:
    async def test_conflict_aborts_and_target_tree_byte_identical(self, repo):
        # Workspace edits base.txt; target ALSO commits a different base.txt
        # -> guaranteed conflict.
        sha = _workspace_commit(repo, "ouroboros/auto/s4", "base.txt",
                                "workspace version\n", "repair")
        (repo / "base.txt").write_text("operator version\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "operator edit")
        before = _tree_hash(repo)
        mgr = WorktreeManager(repo_root=repo)
        with pytest.raises(PromotionError) as ei:
            await mgr.promote_commits(repo, "ouroboros/auto/s4", [sha])
        assert ei.value.state == "conflict_aborted"
        assert _tree_hash(repo) == before, (
            "conflict must leave the target working tree byte-identical"
        )
        assert not (repo / ".git" / "CHERRY_PICK_HEAD").exists(), (
            "cherry-pick must be aborted, not left in progress"
        )

    async def test_dirty_touched_path_refused_before_any_git(self, repo):
        sha = _workspace_commit(repo, "ouroboros/auto/s5", "base.txt",
                                "workspace version\n", "repair")
        (repo / "base.txt").write_text("uncommitted human edit\n")
        before = _tree_hash(repo)
        mgr = WorktreeManager(repo_root=repo)
        with pytest.raises(PromotionError) as ei:
            await mgr.promote_commits(repo, "ouroboros/auto/s5", [sha])
        assert ei.value.state == "target_dirty"
        assert _tree_hash(repo) == before

    async def test_unrelated_dirty_file_is_allowed(self, repo):
        sha = _workspace_commit(repo, "ouroboros/auto/s6", "fix.py",
                                "x = 6\n", "repair")
        (repo / "other.txt").write_text("unrelated human edit\n")
        mgr = WorktreeManager(repo_root=repo)
        result = await mgr.promote_commits(repo, "ouroboros/auto/s6", [sha])
        assert result.promoted_shas == (sha,)
        assert (repo / "other.txt").read_text() == "unrelated human edit\n", (
            "touched-paths-only scoping: unrelated operator dirt survives"
        )

    async def test_missing_branch(self, repo):
        mgr = WorktreeManager(repo_root=repo)
        with pytest.raises(PromotionError) as ei:
            await mgr.promote_commits(repo, "ouroboros/auto/ghost", ["a" * 40])
        assert ei.value.state == "branch_missing"

    async def test_commit_budget_exceeded(self, repo, monkeypatch):
        monkeypatch.setenv("JARVIS_PROMOTION_MAX_COMMITS", "2")
        sha = _workspace_commit(repo, "ouroboros/auto/s7", "fix.py",
                                "x\n", "repair")
        mgr = WorktreeManager(repo_root=repo)
        with pytest.raises(PromotionError) as ei:
            await mgr.promote_commits(
                repo, "ouroboros/auto/s7", [sha, sha, sha],
            )
        assert ei.value.state == "commit_budget_exceeded"


class TestPromotionPurityPins:
    def test_no_force_flags_or_hard_resets_in_source(self):
        src = inspect.getsource(WorktreeManager.promote_commits)
        for forbidden in ("--force", "reset", "--hard", "push"):
            assert forbidden not in src, (
                f"promotion must never compose {forbidden!r} — "
                "non-destructive git only (mandate 2)"
            )

    def test_promote_is_async(self):
        assert inspect.iscoroutinefunction(WorktreeManager.promote_commits)
