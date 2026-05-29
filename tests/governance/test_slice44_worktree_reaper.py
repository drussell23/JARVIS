"""Slice 44 — watchdog/index exclusion + dynamic reaper coverage.

Root cause (v38/v39 SidecarProfiler): 62 orphaned ouroboros__auto__bt-* worktrees
(492k files / 13GB full-repo checkouts) were never reaped (reaper only swept
unit-*) and Oracle's _find_python_files indexed them (EXCLUDE_PATTERNS lacked
.worktrees) → recursive scan_dir held the GIL → asyncio loop starvation +
51s shutdown join. Fixes: (1) reaper matches a multi-prefix array; (2) Oracle
excludes .worktrees + .ouroboros.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from backend.core.ouroboros.governance.worktree_manager import (
    WorktreeManager,
    _resolve_reap_prefixes,
    _DEFAULT_REAP_EXTRA_PREFIXES,
)
from backend.core.ouroboros.oracle import OracleConfig


async def _init_git_repo(path: Path) -> None:
    for cmd in (
        ["git", "-C", str(path), "init"],
        ["git", "-C", str(path), "config", "user.email", "t@t.com"],
        ["git", "-C", str(path), "config", "user.name", "T"],
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
    ):
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await p.wait()


async def _git_worktree_add(repo: Path, branch: str, wt_dir: Path) -> None:
    p = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), "worktree", "add", "-b", branch, str(wt_dir),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await p.wait()


# --- reaper prefix resolution ---------------------------------------------


def test_default_includes_primary_and_campaign_extras(monkeypatch):
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    out = _resolve_reap_prefixes("unit-")
    assert out[0] == "unit-"  # primary first
    # BOTH forms: branch (slash) — load-bearing for base-independent reaping —
    # and the on-disk dir (__) form.
    assert "ouroboros/auto/bt-" in out
    assert "ouroboros__auto__bt-" in out
    assert "soak-" in out


def test_env_override_replaces_extras(monkeypatch):
    monkeypatch.setenv("JARVIS_WORKTREE_REAP_PREFIXES", "foo-, bar- ,")
    out = _resolve_reap_prefixes("unit-")
    assert out == ("unit-", "foo-", "bar-")  # whitespace/empty dropped


def test_primary_not_duplicated(monkeypatch):
    monkeypatch.setenv("JARVIS_WORKTREE_REAP_PREFIXES", "unit-,ouroboros__auto__bt-")
    out = _resolve_reap_prefixes("unit-")
    assert out.count("unit-") == 1


def test_never_raises_on_blank_primary(monkeypatch):
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    out = _resolve_reap_prefixes("")  # blank primary dropped, extras remain
    assert "ouroboros__auto__bt-" in out and "" not in out


def test_default_extras_constant_shape():
    assert "ouroboros__auto__bt-" in _DEFAULT_REAP_EXTRA_PREFIXES


# --- reaper prefix MATCHING (the dir-name logic the reaper applies) --------


def _matches(name: str, prefixes) -> bool:
    return any(name.startswith(p) for p in prefixes)


def test_auto_soak_worktree_dir_now_matches(monkeypatch):
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    pfx = _resolve_reap_prefixes("unit-")
    assert _matches("ouroboros__auto__bt-2026-05-24-053214", pfx) is True
    assert _matches("unit-abc123", pfx) is True  # legacy still works
    # real feature worktrees are NOT reaped
    assert _matches("feat-ouroboros-gap-fixes-tier3", pfx) is False


# --- Oracle indexing exclusion --------------------------------------------


def test_oracle_excludes_worktrees_and_ouroboros():
    assert ".worktrees" in OracleConfig.EXCLUDE_PATTERNS
    assert ".ouroboros" in OracleConfig.EXCLUDE_PATTERNS


def _oracle_should_exclude(path_str: str) -> bool:
    # Mirrors oracle._find_python_files.should_exclude (substring match).
    return any(p in path_str for p in OracleConfig.EXCLUDE_PATTERNS)


def test_oracle_skips_worktree_paths_keeps_source():
    assert _oracle_should_exclude("/repo/.worktrees/ouroboros__auto__bt-x/backend/a.py") is True
    assert _oracle_should_exclude("/repo/.ouroboros/sessions/bt-x/y.py") is True
    # genuine source still indexed
    assert _oracle_should_exclude("/repo/backend/core/ouroboros/oracle.py") is False


# --- END-TO-END reap (the Critical-bug regression: slash branch / base) ----


async def test_reap_orphans_removes_auto_soak_slash_branch_worktree(tmp_path, monkeypatch):
    """The real debris shape: branch ``ouroboros/auto/bt-*`` (slashes), dir
    ``ouroboros__auto__bt-*`` under .worktrees. Must be reaped via the
    repo-global porcelain branch_matches — independent of worktree_base."""
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()
    wt_dir = base / "ouroboros__auto__bt-2026-05-24-053214"
    await _git_worktree_add(repo, "ouroboros/auto/bt-2026-05-24-053214", wt_dir)
    assert wt_dir.exists(), "setup: worktree should exist"

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    reaped = await mgr.reap_orphans()  # default prefixes incl. ouroboros/auto/bt-
    assert reaped >= 1, "auto-soak worktree must be reaped"
    assert not wt_dir.exists(), "worktree dir must be removed"


async def test_reap_orphans_preserves_real_feature_worktree(tmp_path, monkeypatch):
    """A genuine feature worktree (branch feat/...) must NEVER be reaped."""
    monkeypatch.delenv("JARVIS_WORKTREE_REAP_PREFIXES", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    await _init_git_repo(repo)
    base = repo / ".worktrees"
    base.mkdir()
    real = base / "feat-real-work"
    await _git_worktree_add(repo, "feat/real-work", real)
    assert real.exists()

    mgr = WorktreeManager(repo_root=repo, worktree_base=base)
    await mgr.reap_orphans()
    assert real.exists(), "real feature worktree must be preserved"
