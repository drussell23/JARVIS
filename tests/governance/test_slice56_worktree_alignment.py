"""Slice 56 — cross-worktree commit alignment (Option A).

Confirmed divergence (v47, bt-2026-06-01-220823): ChangeEngine APPLY writes to
the MAIN repo (`self._project_root`), but AutoCommitter checks/commits in the
owned worktree (`JARVIS_AUTO_COMMIT_WORKSPACE`, stamped by harness
ledger-sovereignty so commits never touch the operator's main branch). Result:
the patch lands in main, the committer sees a clean worktree → "No changes
detected" → never commits. AND the autonomous patch leaks into the operator's
real working tree.

Fix (A1): redirect ChangeEngine's write target into the owned worktree when
`JARVIS_AUTO_COMMIT_WORKSPACE` is set — mirroring AutoCommitter's
`_effective_repo_root` env logic so the two are coherent by construction. The
patch then lands in the worktree, AutoCommitter commits it on the
`ouroboros/auto/*` branch, and the operator's main tree is never touched.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from backend.core.ouroboros.governance.change_engine import ChangeEngine


def _engine(root: Path) -> ChangeEngine:
    return ChangeEngine(project_root=Path(root), ledger=MagicMock())


def test_no_redirect_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)
    e = _engine(tmp_path)
    t = tmp_path / "tests" / "foo.py"
    # Byte-identical legacy behavior — writes stay in project_root.
    assert e._redirect_target(t) == t
    assert e._effective_write_root() == tmp_path


def test_effective_write_root_follows_env(tmp_path, monkeypatch):
    ws = tmp_path / ".worktrees" / "owned"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ws))
    assert _engine(tmp_path)._effective_write_root() == ws


def test_redirect_absolute_target_under_project_root(tmp_path, monkeypatch):
    ws = tmp_path / ".worktrees" / "owned"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ws))
    e = _engine(tmp_path)
    t = tmp_path / "tests" / "foo.py"
    assert e._redirect_target(t) == ws / "tests" / "foo.py"


def test_redirect_relative_target(tmp_path, monkeypatch):
    ws = tmp_path / "owned"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ws))
    e = _engine(tmp_path)
    assert e._redirect_target(Path("tests/foo.py")) == ws / "tests" / "foo.py"


def test_target_outside_project_root_is_left_unchanged(tmp_path, monkeypatch):
    # Defensive: a path not under project_root is not rebased (no silent
    # cross-tree write to an unexpected location).
    ws = tmp_path / "owned"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ws))
    e = _engine(tmp_path)
    outside = Path("/tmp/some_other_root/foo.py")
    assert e._redirect_target(outside) == outside


def test_coherent_with_autocommitter_root(tmp_path, monkeypatch):
    """ChangeEngine write root and AutoCommitter commit root must resolve to
    the SAME tree under the env — that coherence is the whole fix."""
    ws = tmp_path / ".worktrees" / "owned"
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", str(ws))
    from backend.core.ouroboros.governance.auto_committer import AutoCommitter

    ac = AutoCommitter(repo_root=tmp_path)
    assert _engine(tmp_path)._effective_write_root() == ac._effective_repo_root()


# Wiring pin (Slice 45 lesson): execute() must actually use the redirect.
def test_execute_uses_redirect_target():
    import inspect

    from backend.core.ouroboros.governance import change_engine as ce

    src = inspect.getsource(ce.ChangeEngine.execute)
    assert "_redirect_target" in src, "execute() must redirect the write target"
