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
    """Stand-in WorktreeManager: records create() calls, returns a real
    WORK-AREA — a directory carrying ``.git``, which is what distinguishes a
    worktree from a directory that merely exists.

    The `.git` marker is load-bearing. This fake used to `mkdir()` and stop,
    making every test that used it assert behaviour against a HUSK. That is
    how the arming defect reached production twice
    (bt-2026-08-28-061124, -065825): a real `create` returned a marker-only
    directory, the arming seam trusted it, and `effective_execution_root`
    raised at the APPLY boundary for every op that got that far. A linked
    worktree's `.git` is a FILE pointing at the parent repo; either shape
    satisfies :func:`is_valid_git_work_area`.
    """

    def __init__(self, base: Path):
        self._base = base
        self.created: list[str] = []

    async def create(self, branch: str) -> Path:
        self.created.append(branch)
        p = self._base / ("wt_" + branch.replace("/", "__"))
        p.mkdir(parents=True, exist_ok=True)
        (p / ".git").write_text("gitdir: /repo/.git/worktrees/fake\n")
        return p


class _HuskMgr:
    """A create that SUCCEEDS but yields an unusable work-area.

    Not hypothetical — this is the observed production shape: the directory
    exists, carries only a `.jarvis/` marker, and `git worktree list` has
    never heard of it.
    """

    def __init__(self, base: Path):
        self._base = base
        self.created: list[str] = []

    async def create(self, branch: str) -> Path:
        self.created.append(branch)
        p = self._base / ("husk_" + branch.replace("/", "__"))
        p.mkdir(parents=True, exist_ok=True)
        (p / ".jarvis").mkdir(exist_ok=True)  # marker only, no .git
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


async def test_husk_worktree_stays_in_primary_and_never_arms(
    tmp_path, monkeypatch,
):
    """A create that returns an unusable work-area must not become the root.

    This seam is documented as the SINGLE canonical materialization point for
    `JARVIS_AUTO_COMMIT_WORKSPACE`, and the Ledger-Sovereignty boot phase
    explicitly reuses whatever lands here — so an unvalidated husk becomes
    every consumer's execution root. `effective_execution_root` then raises
    `ExecutionRootInvalid` at the APPLY boundary, killing the ops that had
    progressed furthest (8 of 80 in bt-2026-08-28-061124; 5 of 77 in -065825).

    Fail-safe is `return root`, the same move this function already makes when
    `create` raises: staying in the primary checkout is a documented posture,
    pointing every consumer at a husk is not.
    """
    import os
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)
    monkeypatch.delenv("JARVIS_AUTO_COMMIT_WORKSPACE", raising=False)

    mgr = _HuskMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-husk", worktree_manager=mgr,
    )

    assert mgr.created, "create() should still have been attempted"
    assert out == tmp_path, "must fall back to the primary checkout"
    assert "JARVIS_AUTO_COMMIT_WORKSPACE" not in os.environ, (
        "a husk was armed: 'armed' must always imply 'usable'"
    )


async def test_autonomous_route_does_not_clobber_operator_override(
    tmp_path, monkeypatch,
):
    """Task 3 (durability substrate): this is the SINGLE canonical
    materialization seam for JARVIS_AUTO_COMMIT_WORKSPACE (Ledger-Sovereignty
    reuses it via its own already-set check). An operator-supplied value MUST
    win -- setdefault semantics, not unconditional overwrite -- otherwise a
    deliberately pinned workspace gets silently clobbered by whatever
    worktree this call happens to create."""
    import os
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    monkeypatch.setenv("JARVIS_FILE_ISOLATION_ENABLED", "true")
    monkeypatch.setattr(ec, "is_autonomous", lambda *a, **k: True)
    operator_pin = str(tmp_path / "operator-pinned-workspace")
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_WORKSPACE", operator_pin)
    mgr = _FakeMgr(tmp_path)
    out = await aw.resolve_loop_project_root(
        tmp_path, session_id="bt-1", worktree_manager=mgr,
    )
    # A worktree IS still created (routing still happens)...
    assert out != tmp_path
    # ...but the operator's pin survives untouched in the env.
    assert os.environ["JARVIS_AUTO_COMMIT_WORKSPACE"] == operator_pin


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
