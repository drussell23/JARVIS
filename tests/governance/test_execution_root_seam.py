"""Slice 11 Task 1 — canonical execution-root seam.

Contract v2 (post-review, findings C1 + C2):

  * env absent/blank → ``project_root`` (byte-identical legacy).
  * env set to a real git work-area (dir containing ``.git`` — FILE for
    linked worktrees, DIR for full checkouts) → that tree.
  * env set to ANYTHING else → raise ``ExecutionRootInvalid`` (fail
    CLOSED). Review C2: the v1 silent fallback routed APPLY bytes into
    the operator's live tree unquarantined when a workspace vanished
    mid-session; review C1: the ``.git`` requirement previously lived
    only in AutoCommitter, so a plain dir made VERIFY judge one tree
    while the commit fell back to another.

Consumers: ``ChangeEngine._effective_write_root`` and
``AutoCommitter._effective_repo_root`` (pure delegates),
``GovernedLoopConfig.execution_root`` / ``OrchestratorConfig.execution_root``
(lazy read-time properties — the ledger-sovereignty bootloader exports the
env AFTER config construction, so caching is a bug this file must catch).
"""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomous_workspace import (
    ExecutionRootInvalid,
    effective_execution_root,
)
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


def _mk_ws(path: Path) -> Path:
    """A valid workspace: dir + .git FILE (linked-worktree shape, exactly
    what WorktreeManager.create produces)."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text("gitdir: /nonexistent/worktrees/x\n")
    return path


def _engine(project_root: Path) -> ChangeEngine:
    """Resolver-method pin only — bypass __init__ (ledger/comm/lock deps are
    irrelevant to path resolution and heavy to construct)."""
    eng = object.__new__(ChangeEngine)
    eng._project_root = Path(project_root)
    return eng


# ---------------------------------------------------------------------------
# 1. The canonical resolver
# ---------------------------------------------------------------------------


class TestEffectiveExecutionRootResolver:
    def test_env_unset_returns_project_root(self, tmp_path):
        assert effective_execution_root(tmp_path) == Path(tmp_path)

    def test_env_blank_returns_project_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV, "   ")
        assert effective_execution_root(tmp_path) == Path(tmp_path)

    def test_env_valid_worktree_returns_workspace(self, tmp_path, monkeypatch):
        ws = _mk_ws(tmp_path / "ws")
        monkeypatch.setenv(ENV, str(ws))
        assert effective_execution_root(tmp_path / "repo") == ws

    def test_env_git_dir_checkout_returns_workspace(
        self, tmp_path, monkeypatch,
    ):
        ws = tmp_path / "ws"
        (ws / ".git").mkdir(parents=True)  # full-checkout shape
        monkeypatch.setenv(ENV, str(ws))
        assert effective_execution_root(tmp_path) == ws

    def test_env_nonexistent_path_raises(self, tmp_path, monkeypatch):
        """Review C2: a vanished workspace must fail LOUD — the v1 fallback
        silently routed autonomous bytes into the operator's live tree."""
        monkeypatch.setenv(ENV, str(tmp_path / "vanished"))
        with pytest.raises(ExecutionRootInvalid):
            effective_execution_root(tmp_path)

    def test_env_dir_without_git_raises(self, tmp_path, monkeypatch):
        """Review C1: a plain dir split the truth — seam accepted it while
        AutoCommitter fell back; the .git rule now lives in the seam."""
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv(ENV, str(plain))
        with pytest.raises(ExecutionRootInvalid):
            effective_execution_root(tmp_path)

    def test_env_points_to_file_raises(self, tmp_path, monkeypatch):
        f = tmp_path / "a-file"
        f.write_text("not a dir")
        monkeypatch.setenv(ENV, str(f))
        with pytest.raises(ExecutionRootInvalid):
            effective_execution_root(tmp_path)

    def test_unresolvable_override_raises_typed(self, tmp_path, monkeypatch):
        # Component beyond NAME_MAX → OSError inside is_dir() → wrapped
        # into the typed refusal, never a raw OSError.
        monkeypatch.setenv(ENV, "/" + "x" * 4096)
        with pytest.raises(ExecutionRootInvalid):
            effective_execution_root(tmp_path)

    def test_accepts_str_project_root(self, tmp_path):
        assert effective_execution_root(str(tmp_path)) == Path(tmp_path)


# ---------------------------------------------------------------------------
# 2. GovernedLoopConfig.execution_root — lazy, dynamic, never cached
# ---------------------------------------------------------------------------


class TestGovernedLoopConfigExecutionRoot:
    def test_lazy_sequencing_env_set_after_construction(
        self, tmp_path, monkeypatch,
    ):
        ws = _mk_ws(tmp_path / "ws")
        cfg = GovernedLoopConfig(project_root=tmp_path / "repo")
        monkeypatch.setenv(ENV, str(ws))  # injected AFTER instantiation
        assert cfg.execution_root == ws

    def test_not_cached_on_first_read(self, tmp_path, monkeypatch):
        ws = _mk_ws(tmp_path / "ws")
        repo = tmp_path / "repo"
        cfg = GovernedLoopConfig(project_root=repo)
        assert cfg.execution_root == repo  # read #1: legacy
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws  # read #2: must see the injection

    def test_env_cleared_reverts_to_project_root(self, tmp_path, monkeypatch):
        ws = _mk_ws(tmp_path / "ws")
        repo = tmp_path / "repo"
        cfg = GovernedLoopConfig(project_root=repo)
        monkeypatch.setenv(ENV, str(ws))
        assert cfg.execution_root == ws
        monkeypatch.delenv(ENV)
        assert cfg.execution_root == repo

    def test_observation_root_untouched(self, tmp_path, monkeypatch):
        ws = _mk_ws(tmp_path / "ws")
        repo = tmp_path / "repo"
        monkeypatch.setenv(ENV, str(ws))
        cfg = GovernedLoopConfig(project_root=repo)
        assert cfg.project_root == repo
        assert cfg.execution_root == ws

    def test_invalid_env_raises_through_property(self, tmp_path, monkeypatch):
        cfg = GovernedLoopConfig(project_root=tmp_path)
        monkeypatch.setenv(ENV, str(tmp_path / "vanished"))
        with pytest.raises(ExecutionRootInvalid):
            _ = cfg.execution_root

    def test_from_env_does_not_capture_workspace(self, tmp_path, monkeypatch):
        ws = _mk_ws(tmp_path / "ws")
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
        ws = _mk_ws(tmp_path / "ws")
        per_op = tmp_path / "per-op"
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(tmp_path / "repo")
        assert eng._effective_write_root(per_op) == per_op

    def test_env_valid_worktree_returns_workspace(
        self, tmp_path, monkeypatch,
    ):
        ws = _mk_ws(tmp_path / "ws")
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(tmp_path / "repo")
        assert eng._effective_write_root() == ws

    def test_legacy_no_env_returns_project_root(self, tmp_path):
        repo = tmp_path / "repo"
        eng = _engine(repo)
        assert eng._effective_write_root() == repo

    def test_invalid_env_raises_never_writes_either_tree(
        self, tmp_path, monkeypatch,
    ):
        """Review C2: armed-but-invalid must refuse — the v1 fallback wrote
        APPLY bytes into the operator's live tree unquarantined."""
        monkeypatch.setenv(ENV, str(tmp_path / "vanished-ws"))
        eng = _engine(tmp_path / "repo")
        with pytest.raises(ExecutionRootInvalid):
            eng._effective_write_root()

    def test_pure_delegate_no_local_env_read(self):
        src = inspect.getsource(ChangeEngine._effective_write_root)
        assert "effective_execution_root" in src
        assert "os.environ" not in src
        assert "is_dir" not in src

    # -- _redirect_target behavior must survive the refactor ----------------

    def test_redirect_rebases_absolute_target(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        (repo / "pkg").mkdir(parents=True)
        ws = _mk_ws(tmp_path / "ws")
        monkeypatch.setenv(ENV, str(ws))
        eng = _engine(repo)
        target = repo / "pkg" / "mod.py"
        assert eng._redirect_target(target) == ws / "pkg" / "mod.py"

    def test_redirect_outside_project_root_unchanged(
        self, tmp_path, monkeypatch,
    ):
        repo = tmp_path / "repo"
        repo.mkdir()
        ws = _mk_ws(tmp_path / "ws")
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


# ---------------------------------------------------------------------------
# 4. AutoCommitter delegation (review C1 — the fourth consumer unified)
# ---------------------------------------------------------------------------


class TestAutoCommitterDelegation:
    def test_pure_delegate_no_local_env_read(self):
        from backend.core.ouroboros.governance.auto_committer import (
            AutoCommitter,
        )

        src = inspect.getsource(AutoCommitter._effective_repo_root)
        assert "effective_execution_root" in src, (
            "AutoCommitter must resolve through the canonical seam — its "
            "private stricter copy was the C1 split-truth"
        )
        assert "os.environ" not in src
        assert "is_dir" not in src

    def test_resolves_same_root_as_change_engine(self, tmp_path, monkeypatch):
        from backend.core.ouroboros.governance.auto_committer import (
            AutoCommitter,
        )

        ws = _mk_ws(tmp_path / "ws")
        repo = tmp_path / "repo"
        repo.mkdir()
        monkeypatch.setenv(ENV, str(ws))
        committer = AutoCommitter(repo_root=repo)
        eng = _engine(repo)
        assert committer._effective_repo_root() == eng._effective_write_root()
