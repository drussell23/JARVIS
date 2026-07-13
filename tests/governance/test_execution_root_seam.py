"""Slice 11 Task 1 — canonical execution-root seam (RED first).

Contract under test (the three consumers of ONE resolver):

  * ``autonomous_workspace.effective_execution_root(project_root)`` — the
    single source of truth: ``JARVIS_AUTO_COMMIT_WORKSPACE`` when present
    AND a real directory, else ``project_root``. Never raises.
  * ``GovernedLoopConfig.execution_root`` — fully dynamic property resolving
    through the seam AT READ TIME. The ledger-sovereignty bootloader exports
    the env var AFTER config construction (harness.py:2854 vs :3478), so any
    value captured at ``from_env`` time — or cached on first read — is a bug
    this file must catch.
  * ``ChangeEngine._effective_write_root`` — pure pass-through delegate
    (per-request ``write_root`` precedence stays local; env/legacy resolution
    is the seam's job — no duplicated env read or validity check).

Run-21 evidence anchor: the correct chaos repair landed in the workspace
worktree while VERIFY judged the boot-time ``project_root`` (pass_rate=0.75
terminal on a correct fix). The seam is the mandate-1 root-cause fix.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.change_engine import ChangeEngine
from backend.core.ouroboros.governance.governed_loop_service import (
    GovernedLoopConfig,
)

ENV = "JARVIS_AUTO_COMMIT_WORKSPACE"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts with the workspace env ABSENT (legacy posture)."""
    monkeypatch.delenv(ENV, raising=False)
    yield


def _engine(project_root: Path) -> ChangeEngine:
    """Resolver-method pin only — bypass __init__ (ledger/comm/lock deps are
    irrelevant to path resolution and heavy to construct)."""
    eng = object.__new__(ChangeEngine)
    eng._project_root = Path(project_root)
    return eng


def _resolver():
    from backend.core.ouroboros.governance.autonomous_workspace import (
        effective_execution_root,
    )

    return effective_execution_root


# ---------------------------------------------------------------------------
# 1. The canonical resolver (autonomous_workspace.effective_execution_root)
# ---------------------------------------------------------------------------


class TestEffectiveExecutionRootResolver:
    def test_env_unset_returns_project_root(self, tmp_path):
        assert _resolver()(tmp_path) == Path(tmp_path)

    def test_env_valid_dir_returns_workspace(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv(ENV, str(ws))
        assert _resolver()(tmp_path / "repo") == ws

    def test_env_nonexistent_path_falls_back(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, str(tmp_path / "does-not-exist"))
        assert _resolver()(tmp_path) == Path(tmp_path)

    def test_env_blank_returns_project_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, "   ")
        assert _resolver()(tmp_path) == Path(tmp_path)

    def test_env_points_to_file_falls_back(self, tmp_path, monkeypatch):
        f = tmp_path / "a-file"
        f.write_text("not a dir")
        monkeypatch.setenv(ENV, str(f))
        assert _resolver()(tmp_path) == Path(tmp_path)

    def test_never_raises_on_unresolvable_override(self, tmp_path, monkeypatch):
        # A component beyond NAME_MAX → OSError(ENAMETOOLONG) inside is_dir();
        # the seam must swallow it and fall back. (A NUL byte is untestable
        # here — os.environ itself rejects embedded NULs at setenv time.)
        monkeypatch.setenv(ENV, "/" + "x" * 4096)
        assert _resolver()(tmp_path) == Path(tmp_path)

    def test_accepts_str_project_root(self, tmp_path):
        # Callers pass both Path and str today (harness vs orchestrator).
        assert _resolver()(str(tmp_path)) == Path(tmp_path)


# ---------------------------------------------------------------------------
# 2. GovernedLoopConfig.execution_root — lazy, dynamic, never cached
# ---------------------------------------------------------------------------


class TestGovernedLoopConfigExecutionRoot:
    def test_lazy_sequencing_env_set_after_construction(
        self, tmp_path, monkeypatch
    ):
        """THE mandate-4 pin: config built BEFORE the env exists (the real
        harness ordering) must still resolve the workspace afterwards."""
        ws = tmp_path / "ws"
        ws.mkdir()
        cfg = GovernedLoopConfig(project_root=tmp_path / "repo")
        # Env injected AFTER instantiation — ledger-sovereignty bootloader.
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws

    def test_not_cached_on_first_read(self, tmp_path, monkeypatch):
        """A first read pre-injection must not freeze the value (kills
        functools.cached_property / memoization shortcuts)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        cfg = GovernedLoopConfig(project_root=repo)
        assert cfg.execution_root == repo  # read #1: legacy
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws  # read #2: must see the injection

    def test_env_cleared_reverts_to_project_root(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        cfg = GovernedLoopConfig(project_root=repo)
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws
        monkeypatch.delenv(ENV)
        assert cfg.execution_root == repo

    def test_observation_root_untouched(self, tmp_path, monkeypatch):
        """Role split: project_root (observation) must NEVER follow the env —
        sensors/TestWatcher keep watching the operator's real tree."""
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        monkeypatch.setenv(ENV, str(ws))
        cfg = GovernedLoopConfig(project_root=repo)
        assert cfg.project_root == repo
        assert cfg.execution_root == ws

    def test_from_env_does_not_capture_workspace(self, tmp_path, monkeypatch):
        """from_env must not bake the workspace into project_root even when
        the env is ALREADY set at construction time (frozen-field hygiene) —
        and execution_root must stay dynamic on the built object."""
        ws = tmp_path / "ws"
        ws.mkdir()
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv(ENV, str(ws))
        cfg = GovernedLoopConfig.from_env(project_root=repo)
        assert cfg.project_root == repo
        assert cfg.execution_root == ws
        monkeypatch.delenv(ENV)
        assert cfg.execution_root == repo


# ---------------------------------------------------------------------------
# 3. ChangeEngine._effective_write_root — pure delegate (mandate 3)
# ---------------------------------------------------------------------------


class TestChangeEngineDelegation:
    def test_request_write_root_wins_over_env(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        per_op = tmp_path / "per-op"
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(tmp_path / "repo")
        assert eng._effective_write_root(per_op) == per_op

    def test_env_valid_dir_returns_workspace(self, tmp_path, monkeypatch):
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(tmp_path / "repo")
        assert eng._effective_write_root() == ws

    def test_legacy_no_env_returns_project_root(self, tmp_path):
        repo = tmp_path / "repo"
        eng = _engine(repo)
        assert eng._effective_write_root() == repo

    def test_invalid_env_falls_back_to_project_root(self, tmp_path, monkeypatch):
        """NEW contract (mandate 1: presence AND validity): a nonexistent
        workspace override must not become a raw write root — writing into it
        would create a rogue tree outside any git worktree. Pre-Slice-11
        ChangeEngine returned Path(override) unchecked; the seam validates."""
        repo = tmp_path / "repo"
        monkeypatch.setenv(ENV, str(tmp_path / "vanished-ws"))
        eng = _engine(repo)
        assert eng._effective_write_root() == repo

    def test_pure_delegate_no_local_env_read(self):
        """Structural mandate-3 pin: the method must delegate to the seam and
        must not read the env or duplicate validity checks locally."""
        src = inspect.getsource(ChangeEngine._effective_write_root)
        assert "effective_execution_root" in src, (
            "_effective_write_root must delegate to "
            "autonomous_workspace.effective_execution_root"
        )
        assert "os.environ" not in src, (
            "env resolution belongs to the canonical seam, not ChangeEngine"
        )
        assert "is_dir" not in src, (
            "validity checking belongs to the canonical seam, not ChangeEngine"
        )

    # -- _redirect_target behavior must survive the refactor ----------------

    def test_redirect_rebases_absolute_target(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        ws = tmp_path / "ws"
        ws.mkdir()
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(repo)
        target = repo / "pkg" / "mod.py"
        assert eng._redirect_target(target) == ws / "pkg" / "mod.py"

    def test_redirect_outside_project_root_unchanged(
        self, tmp_path, monkeypatch
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        elsewhere = tmp_path / "elsewhere" / "x.py"
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(repo)
        assert eng._redirect_target(elsewhere) == elsewhere

    def test_redirect_noop_when_env_unset(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        eng = _engine(repo)
        target = repo / "pkg" / "mod.py"
        assert eng._redirect_target(target) == target
