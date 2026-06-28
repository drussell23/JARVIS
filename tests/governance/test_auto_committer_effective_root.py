"""Regression for the A1 dead-cwd fix in ``AutoCommitter._effective_repo_root``.

The cloud A1 run reached ``state=applied`` but the ledger recorded
``written=False`` because ``JARVIS_AUTO_COMMIT_WORKSPACE`` pointed at a
worktree path that did not exist (a branch-named ``.worktrees/ouroboros/auto/...``
the producer never materialized). Every git subprocess then died with
``fatal: cannot change to '<path>'`` (exit 128), so APPLIED never durably
committed and the auditor's ``fsm_classify_to_applied`` stayed False.

The fix: validate the override is a real, usable git working tree; otherwise
fall back to ``repo_root`` so the commit still lands durably. Fail-safe.
"""
from __future__ import annotations

import os
from pathlib import Path

from backend.core.ouroboros.governance.auto_committer import AutoCommitter

_ENV = "JARVIS_AUTO_COMMIT_WORKSPACE"


def _ac(repo_root: Path) -> AutoCommitter:
    ac = AutoCommitter.__new__(AutoCommitter)  # bypass __init__ — unit-isolate
    ac._repo_root = repo_root
    return ac


def test_unset_override_returns_repo_root(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV, raising=False)
    ac = _ac(tmp_path)
    assert ac._effective_repo_root() == tmp_path


def test_nonexistent_override_falls_back_to_repo_root(monkeypatch, tmp_path):
    # The exact A1 failure: a worktree path that was never created.
    monkeypatch.setenv(_ENV, str(tmp_path / ".worktrees" / "ouroboros" / "nope"))
    ac = _ac(tmp_path)
    assert ac._effective_repo_root() == tmp_path  # fail-safe fallback


def test_existing_dir_without_git_falls_back(monkeypatch, tmp_path):
    # Exists + is a dir but is NOT a git worktree -> git ops would fail -> fall back.
    plain = tmp_path / "plain_dir"
    plain.mkdir()
    monkeypatch.setenv(_ENV, str(plain))
    ac = _ac(tmp_path)
    assert ac._effective_repo_root() == tmp_path


def test_valid_git_worktree_override_is_used(monkeypatch, tmp_path):
    # A real git working tree (has a .git entry) -> adopted as the cwd.
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    monkeypatch.setenv(_ENV, str(wt))
    ac = _ac(tmp_path)
    assert ac._effective_repo_root() == wt


def test_file_dot_git_worktree_form_is_accepted(monkeypatch, tmp_path):
    # Git worktrees use a .git FILE (not dir); the check must accept both.
    wt = tmp_path / "wt2"
    (wt / ".git").parent.mkdir(parents=True, exist_ok=True)
    wt.mkdir(exist_ok=True)
    (wt / ".git").mkdir()  # bare .git dir form (main checkout)
    monkeypatch.setenv(_ENV, str(wt))
    ac = _ac(tmp_path)
    assert ac._effective_repo_root() == wt
