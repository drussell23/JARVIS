"""Sovereign Execution Boundary (Stage B) — inheritance + wiring pins.

Stage B's single-redirect design (route the orchestrator's project_root into
an isolated worktree) only quarantines mutations IF every delegate derives its
working dir from the injected root. These pins LOCK that property: if a future
refactor makes a delegate use os.getcwd()/an independent `git rev-parse`
instead of its passed root, the isolation silently breaks — and these fail.

Source-inspection pins (the established AST/source-pin idiom): cheap, robust,
no heavy object construction.
"""
from __future__ import annotations

import inspect


def _src(modpath: str) -> str:
    import importlib
    return inspect.getsource(importlib.import_module(modpath))


def test_branch_manager_git_runs_in_injected_repo_path():
    src = _src("backend.core.ouroboros.battle_test.branch_manager")
    # git checkout -b runs against the injected repo_path, not ambient cwd.
    assert 'self._repo_path = Path(repo_path)' in src
    assert '"-C", str(self._repo_path)' in src


def test_test_runner_pytest_cwd_falls_back_to_injected_repo_root():
    src = _src("backend.core.ouroboros.governance.test_runner")
    assert "self._repo_root" in src
    assert "else str(self._repo_root)" in src  # pytest effective_cwd


def test_change_engine_writes_under_injected_project_root():
    src = _src("backend.core.ouroboros.governance.change_engine")
    assert "self._project_root = Path(project_root)" in src
    # _effective_write_root falls back to project_root.
    assert "self._project_root" in src


def test_tool_executor_bash_cwd_is_injected_repo_root():
    src = _src("backend.core.ouroboros.governance.tool_executor")
    assert "self._repo_root = repo_root" in src
    assert "cwd=self._repo_root" in src or "cwd=str(self._repo_root)" in src


def test_harness_redirects_project_root_via_resolver():
    src = _src("backend.core.ouroboros.battle_test.harness")
    # The single dynamic injection point feeds GLS config from the resolver.
    assert "resolve_loop_project_root(" in src
    assert "project_root=_loop_root" in src
    # And the Ledger phase yields to a Stage-B-created worktree (no dup).
    assert "reusing file-isolation worktree" in src


def test_resolver_uses_same_branch_naming_as_ledger_sovereignty():
    # File + commit isolation MUST converge on one worktree (swept by the
    # existing ouroboros/auto/* reaper).
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    assert aw.workspace_branch("bt-1") == "ouroboros/auto/bt-1"
