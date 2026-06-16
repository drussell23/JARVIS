"""Sovereign Execution Boundary (Stage B) — autonomous file isolation.

`autonomous_workspace.resolve_loop_project_root` is the single dynamic
project_root injection (no os.chdir, no global cwd mutation): when file
isolation is enabled AND the session is autonomous, the loop's project_root
resolves to an isolated worktree (same `ouroboros/auto/<session>` naming the
Ledger-Sovereignty phase uses, so the two converge on ONE worktree and the
existing reaper sweeps it). All 4 delegates (ChangeEngine/BranchManager/
TestRunner/ToolExecutor) inherit project_root, so this one redirect routes
every mutation into the quarantine zone.

Gated by JARVIS_FILE_ISOLATION_ENABLED (default off → returns repo_root,
byte-identical boot). NEVER raises → repo_root fallback.

TDD red: written before the module exists.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import execution_context as ec


class _FakeMgr:
    """Stand-in WorktreeManager: records create() calls, returns a real dir."""

    def __init__(self, base: Path):
        self._base = base
        self.created: list[str] = []

    async def create(self, branch: str) -> Path:
        self.created.append(branch)
        p = self._base / ("wt_" + branch.replace("/", "__"))
        p.mkdir(parents=True, exist_ok=True)
        return p


class _BoomMgr:
    async def create(self, branch: str) -> Path:
        raise RuntimeError("disk full")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_FILE_ISOLATION_ENABLED",
        "JARVIS_AUTO_COMMIT_WORKSPACE",
        "JARVIS_OUROBOROS_SESSION_ID",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


async def test_off_returns_repo_root_no_create(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    mgr = _FakeMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-1", worktree_manager=mgr,
    )
    assert out == tmp_path
    assert mgr.created == []  # inert when flag off — byte-identical boot


async def test_on_but_not_autonomous_returns_repo_root(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: False)
    mgr = _FakeMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-1", worktree_manager=mgr,
    )
    assert out == tmp_path  # human session → primary checkout
    assert mgr.created == []


async def test_on_and_autonomous_routes_to_worktree(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)
    mgr = _FakeMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-xyz", worktree_manager=mgr,
    )
    assert out != tmp_path
    assert out.is_dir()
    # Same naming as the Ledger Sovereignty phase → one unified worktree,
    # swept by the existing ouroboros/auto/* reaper.
    assert mgr.created == ["ouroboros/auto/bt-xyz"]


async def test_autonomous_route_unifies_commit_workspace_env(
    tmp_path, monkeypatch,
):
    import os
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)
    mgr = _FakeMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-1", worktree_manager=mgr,
    )
    # Reuses the existing commit-workspace handoff env so AutoCommitter +
    # ChangeEngine converge on the SAME worktree (not new global cwd state).
    assert os.environ["JARVIS_AUTO_COMMIT_WORKSPACE"] == str(out)


async def test_create_failure_falls_back_to_repo_root(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-1", worktree_manager=_BoomMgr(),
    )
    # Fail-safe: stay in primary (the Stage A commit-gate still blocks any
    # autonomous commit there, so no silent harm).
    assert out == tmp_path


def test_file_isolation_flag_default_off(monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.delenv("JARVIS_FILE_ISOLATION_ENABLED", raising=False)
    assert aw.file_isolation_enabled() is False
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    assert aw.file_isolation_enabled() is True
