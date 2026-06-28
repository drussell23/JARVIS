"""Tests for L3 worktree full hydration and authoritative-root translation.

Covers the bug where WorktreeManager.create() returned a relative or
symlink-containing path, and where advisor/coverage/test-resolver scans
against an empty .worktrees/<name>/ root returned 0% coverage and blocked
every op.

Five test groups:
  1. WorktreeManager.create() returns an absolute path with full checkout.
  2. authoritative_repo_root() translates .worktrees/<name> to parent repo.
  3. authoritative_repo_root() leaves non-worktree paths unchanged.
  4. file_has_test_coverage() translates worktree root → real repo root.
  5. OperationAdvisor._compute_test_coverage() is not blocked under a
     worktree root when the real repo has tests.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/governance/ → repo


def _git(*args: str, cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or _REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# 1. WorktreeManager.create() — integration (requires git)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.integration
async def test_worktree_create_fully_populated(tmp_path):
    """WorktreeManager.create() must return an ABSOLUTE path whose checkout
    contains both 'tests/' and 'backend/', and the worktree must appear in
    ``git worktree list``."""
    from backend.core.ouroboros.governance.worktree_manager import WorktreeManager

    branch = "ouroboros/auto/test-hydration-check-001"
    wm = WorktreeManager(repo_root=_REPO_ROOT, worktree_base=tmp_path / "wt")
    wt_path: Optional[Path] = None
    try:
        wt_path = await wm.create(branch)

        # Must be absolute
        assert wt_path.is_absolute(), f"create() returned relative path: {wt_path}"

        # Must have a full checkout
        assert (wt_path / "tests").is_dir(), f"tests/ missing in worktree {wt_path}"
        assert (wt_path / "backend").is_dir(), f"backend/ missing in worktree {wt_path}"

        # Must appear in git worktree list
        result = _git("worktree", "list", "--porcelain")
        assert str(wt_path) in result.stdout, (
            f"worktree {wt_path} not in git worktree list:\n{result.stdout}"
        )
    finally:
        if wt_path is not None:
            await wm.cleanup(wt_path)
        _git("branch", "-D", branch)


# ---------------------------------------------------------------------------
# 2. authoritative_repo_root — .worktrees/<name> → parent repo
# ---------------------------------------------------------------------------

def test_authoritative_repo_root_inside_worktrees():
    """Path directly under .worktrees/<name> must resolve to the parent."""
    from backend.core.ouroboros.governance.execution_context import (
        authoritative_repo_root,
    )
    fake_repo = Path("/repo/project")
    worktree_path = fake_repo / ".worktrees" / "some-session"
    result = authoritative_repo_root(worktree_path)
    assert result == fake_repo, f"Expected {fake_repo}, got {result}"


def test_authoritative_repo_root_deep_inside_worktrees():
    """A file deep inside .worktrees/<name>/... must resolve to the parent."""
    from backend.core.ouroboros.governance.execution_context import (
        authoritative_repo_root,
    )
    fake_repo = Path("/repo/project")
    deep_path = fake_repo / ".worktrees" / "some-session" / "backend" / "foo.py"
    result = authoritative_repo_root(deep_path)
    assert result == fake_repo, f"Expected {fake_repo}, got {result}"


# ---------------------------------------------------------------------------
# 3. authoritative_repo_root — non-worktree path unchanged
# ---------------------------------------------------------------------------

def test_authoritative_repo_root_non_worktree_path():
    """A path NOT inside .worktrees must be returned unchanged (resolved)."""
    from backend.core.ouroboros.governance.execution_context import (
        authoritative_repo_root,
    )
    non_worktree = Path("/repo/project/backend/core/foo.py")
    result = authoritative_repo_root(non_worktree)
    # resolve() may differ on case-insensitive filesystems; compare resolved
    assert result == non_worktree.resolve(), (
        f"Expected {non_worktree.resolve()}, got {result}"
    )


def test_authoritative_repo_root_repo_root_itself():
    """The repo root itself must be returned unchanged."""
    from backend.core.ouroboros.governance.execution_context import (
        authoritative_repo_root,
    )
    result = authoritative_repo_root(_REPO_ROOT)
    assert result == _REPO_ROOT.resolve()


# ---------------------------------------------------------------------------
# 4. file_has_test_coverage — worktree root → real repo lookup
# ---------------------------------------------------------------------------

def test_coverage_lookup_translates_to_authoritative_root(tmp_path):
    """file_has_test_coverage(tested_file, empty_worktree_root) must return
    True because the worktree root is translated to the real repo root where
    the test actually lives.

    Target: operation_advisor.py — covered by test_operation_advisor_*.py
    files (suffix-prefix strategy), which live in tests/governance/.
    """
    from backend.core.ouroboros.governance.target_stratification import (
        file_has_test_coverage,
    )

    # Build a fake .worktrees/<name>/ directory with the _REPO_ROOT as parent
    # (EMPTY — simulates the bug: only .jarvis/ survives after git worktree remove)
    wt_base = _REPO_ROOT / ".worktrees" / "ouroboros__auto__bt-test-session-cov"
    wt_base.mkdir(parents=True, exist_ok=True)
    # No tests/ inside — exactly the broken state described in the bug report.

    # operation_advisor.py is covered by test_operation_advisor_*.py files.
    target_file = "backend/core/ouroboros/governance/operation_advisor.py"
    assert (_REPO_ROOT / target_file).exists(), (
        f"{target_file} not found in repo — pick a different sentinel"
    )
    assert any(
        (_REPO_ROOT / "tests").rglob("test_operation_advisor_*.py")
    ), "No test_operation_advisor_*.py found — test sentinel is wrong"

    try:
        # Passing the EMPTY worktree root should still succeed because
        # authoritative_repo_root translates wt_base → _REPO_ROOT.
        result = file_has_test_coverage(target_file, wt_base)
        assert result is True, (
            "file_has_test_coverage returned False for a covered file when "
            "given an empty .worktrees/ root — authoritative_repo_root "
            "translation is not working"
        )
    finally:
        import shutil
        if wt_base.exists():
            shutil.rmtree(wt_base)


def test_coverage_lookup_real_repo_root_unchanged():
    """file_has_test_coverage works identically when given the real repo root
    (no .worktrees component — the translate is a no-op)."""
    from backend.core.ouroboros.governance.target_stratification import (
        file_has_test_coverage,
    )
    # operation_advisor.py has test_operation_advisor_*.py coverage
    target_file = "backend/core/ouroboros/governance/operation_advisor.py"
    assert (_REPO_ROOT / target_file).exists()
    result = file_has_test_coverage(target_file, _REPO_ROOT)
    assert result is True


# ---------------------------------------------------------------------------
# 5. OperationAdvisor._compute_test_coverage — not blocked under worktree root
# ---------------------------------------------------------------------------

def test_advisor_coverage_not_blocked_under_worktree_root(tmp_path):
    """OperationAdvisor._compute_test_coverage must return > 0 for a tested
    file even when the scan root is an empty .worktrees/<name>/ directory."""
    from backend.core.ouroboros.governance.operation_advisor import OperationAdvisor

    # Empty worktree root under the REAL repo — simulates the exact bug state
    wt_base = _REPO_ROOT / ".worktrees" / "ouroboros__auto__bt-advisor-test"
    wt_base.mkdir(parents=True, exist_ok=True)

    try:
        advisor = OperationAdvisor(project_root=wt_base)
        # operation_advisor.py has test_operation_advisor_*.py coverage
        target = "backend/core/ouroboros/governance/operation_advisor.py"
        coverage = advisor._compute_test_coverage((target,), root=wt_base)
        assert coverage > 0.0, (
            f"Advisor returned 0% coverage for {target!r} when given an empty "
            f".worktrees/ root — authoritative_repo_root translation in "
            f"_compute_test_coverage is not working (coverage={coverage})"
        )
    finally:
        import shutil
        if wt_base.exists():
            shutil.rmtree(wt_base)
