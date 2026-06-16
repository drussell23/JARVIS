"""Slice 257 — exclude ``.claude/worktrees`` + any nested git boundary.

Root cause (bt-2026-06-16-042304, exit 137 ``ExternalWatchdog`` SIGKILL):
the Oracle's ``should_exclude`` does a *substring* match against
``EXCLUDE_PATTERNS``. Slice 44 added ``.worktrees`` but the path
``/repo/.claude/worktrees/<wt>/...`` contains ``/worktrees`` (slash), not
``.worktrees`` (dot), so the pattern never matched. Result: the Oracle
recursed into all 6 agent worktrees under ``.claude/worktrees`` — each a
full 29k-file checkout — and ast-parsed 6× the tree. The process pool
saturated, the main asyncio loop starved (LoopSink:
``posture_observer.run_one_cycle blocked_ms=73191``), the heartbeat went
stale, and the out-of-process ``ExternalWatchdog`` SIGKILLed the session
before it could reach ``_generate_report`` → summary frozen at ``in_flight``.

Two layers of fix, both pinned here:
  §1  STATIC — ``.claude`` joins the Oracle EXCLUDE_PATTERNS + the miner
      ``_WALK_PRUNE_SEGMENTS`` (cheap fast-path, mirrors ``.jarvis``).
  §2  DYNAMIC — the Oracle never recurses into a directory that is itself a
      *linked* git worktree (``<dir>/.git`` is a FILE). This is the general,
      no-hardcoding guard: any future ``git worktree add`` location is
      auto-excluded, while embedded clones that ARE real source
      (``backend/vision`` — ``.git`` is a DIR) are preserved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.oracle import (
    OracleConfig,
    _is_linked_git_worktree,
)
from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    _WALK_PRUNE_SEGMENTS,
    _iter_python_files_pruned,
)


# ── §1 static pattern coverage ──────────────────────────────────────────
def test_oracle_excludes_dot_claude() -> None:
    assert ".claude" in set(OracleConfig.EXCLUDE_PATTERNS)


def test_oracle_substring_excludes_claude_worktrees() -> None:
    path = "/repo/.claude/worktrees/deploy256/backend/core/trinity_monitoring.py"
    assert any(p in path for p in OracleConfig.EXCLUDE_PATTERNS)


def test_miner_prune_segments_include_dot_claude() -> None:
    assert ".claude" in _WALK_PRUNE_SEGMENTS


def test_miner_walk_prunes_claude_worktrees(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "real.py").write_text("x = 1\n")
    wt = tmp_path / ".claude" / "worktrees" / "deploy256" / "backend"
    wt.mkdir(parents=True)
    (wt / "dup.py").write_text("y = 2\n")

    found = _iter_python_files_pruned(tmp_path, _WALK_PRUNE_SEGMENTS)
    names = {p.name for p in found}
    assert "real.py" in names
    assert "dup.py" not in names
    assert all(".claude" not in p.parts for p in found)


# ── §2 dynamic linked-worktree guard (file-.git only) ───────────────────
def test_linked_worktree_detected_by_gitfile(tmp_path: Path) -> None:
    # A linked worktree (``git worktree add``) has a ``.git`` *file*.
    wt = tmp_path / "some-worktree"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: /repo/.git/worktrees/some-worktree\n")
    assert _is_linked_git_worktree(wt) is True


def test_embedded_clone_with_gitdir_is_NOT_skipped(tmp_path: Path) -> None:
    # A nested clone (``.git`` is a DIRECTORY) may hold first-class tracked
    # source (e.g. backend/vision: 229 real .py). It must NOT be skipped —
    # only file-.git linked worktrees are duplicate checkouts.
    sub = tmp_path / "vendored"
    (sub / ".git").mkdir(parents=True)
    assert _is_linked_git_worktree(sub) is False


def test_linked_worktree_false_for_plain_source_dir(tmp_path: Path) -> None:
    pkg = tmp_path / "backend" / "core"
    pkg.mkdir(parents=True)
    (pkg / "real.py").write_text("x = 1\n")
    assert _is_linked_git_worktree(pkg) is False


def test_linked_worktree_fail_soft_on_bad_path() -> None:
    # Never raises — a non-existent / odd path resolves to "not a worktree"
    # so the guard can only ever *add* exclusions, never crash the walk.
    assert _is_linked_git_worktree(Path("/nonexistent/zzz/qqq")) is False


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
