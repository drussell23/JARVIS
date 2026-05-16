"""Behavioral spine for P1 Slice 2 — Ledger Sovereignty wiring.

Three integration surfaces, each tested in isolation:

  1. :class:`WorktreeManager.create` stamps the marker when
     master is on AND uses the env-bound session_id; no-op when
     master is off (byte-identical legacy worktree).
  2. :class:`AutoCommitter` resolves its commit cwd from
     ``JARVIS_AUTO_COMMIT_WORKSPACE`` and refuses (typed) when
     master is on AND the cwd is not an owned work-area —
     surfaced via ``CommitResult.skipped_reason=ledger_sovereignty_refused``
     so the public "Never raises" contract stays intact.
  3. :class:`BattleTestHarness._boot_ledger_sovereignty_workspace`
     creates the per-session worktree under master, sets the env
     var, no-ops under master-off, and runs in the correct boot
     order (after the readiness gate, before tier boot).

A full end-to-end test (real `git commit` against a worktree-
isolated path) is also included to prove the structural fix
under realistic conditions.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest

from backend.core.ouroboros.battle_test.harness import (
    BattleTestHarness,
    HarnessConfig,
)
from backend.core.ouroboros.governance.auto_committer import (
    AutoCommitter,
    CommitResult,
)
from backend.core.ouroboros.governance.ledger_sovereignty import (
    is_owned,
    mark_owned,
    marker_path,
    read_ownership,
)
from backend.core.ouroboros.governance.worktree_manager import (
    WorktreeManager,
)


_MASTER_FLAG = "JARVIS_LEDGER_SOVEREIGNTY_ENABLED"
_WORKSPACE_FLAG = "JARVIS_AUTO_COMMIT_WORKSPACE"
_SESSION_FLAG = "JARVIS_OUROBOROS_SESSION_ID"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for flag in (_MASTER_FLAG, _WORKSPACE_FLAG, _SESSION_FLAG):
        monkeypatch.delenv(flag, raising=False)
    yield


def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_MASTER_FLAG, "true")


# ---------------------------------------------------------------------------
# Fixtures — minimal git repos / worktrees
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Initialize a minimal git repo at tmp_path with one
    committed file (so worktree creation has a base ref).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q"], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@t"], cwd=repo,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=repo,
        check=True,
    )
    (repo / "README").write_text("hi\n")
    subprocess.run(
        ["git", "add", "."], cwd=repo, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=repo,
        check=True,
    )
    return repo


# ===========================================================================
# Surface 1 — WorktreeManager.create stamps marker
# ===========================================================================


class TestWorktreeManagerStampsMarker:
    @pytest.mark.asyncio
    async def test_master_off_does_not_stamp(self, git_repo):
        # Default: master OFF — legacy worktree, no marker.
        mgr = WorktreeManager(repo_root=git_repo)
        wt_path = await mgr.create("ouroboros/test/off")
        try:
            assert wt_path.exists()
            assert not is_owned(wt_path)
            assert not marker_path(wt_path).exists()
        finally:
            await mgr.cleanup(wt_path)

    @pytest.mark.asyncio
    async def test_master_on_stamps_marker(
        self, git_repo, monkeypatch,
    ):
        _enable(monkeypatch)
        monkeypatch.setenv(_SESSION_FLAG, "bt-test-on")
        mgr = WorktreeManager(repo_root=git_repo)
        wt_path = await mgr.create("ouroboros/test/on")
        try:
            assert wt_path.exists()
            assert is_owned(wt_path)
            rec = read_ownership(wt_path)
            assert rec is not None
            assert rec.session_id == "bt-test-on"
            assert rec.branch_name == "ouroboros/test/on"
        finally:
            await mgr.cleanup(wt_path)

    @pytest.mark.asyncio
    async def test_master_on_missing_session_id_still_stamps(
        self, git_repo, monkeypatch,
    ):
        """Absence of JARVIS_OUROBOROS_SESSION_ID must not crash
        the worktree creation — the marker stamps with empty
        session_id; downstream session-id mismatch check just
        won't fire for this marker."""
        _enable(monkeypatch)
        # session_id NOT set.
        mgr = WorktreeManager(repo_root=git_repo)
        wt_path = await mgr.create("ouroboros/test/no-sess")
        try:
            rec = read_ownership(wt_path)
            assert rec is not None
            assert rec.session_id == ""
        finally:
            await mgr.cleanup(wt_path)


# ===========================================================================
# Surface 2 — AutoCommitter resolves + asserts
# ===========================================================================


class TestAutoCommitterEffectiveRoot:
    def test_no_env_falls_back_to_repo_root(self, tmp_path):
        ac = AutoCommitter(repo_root=tmp_path)
        assert ac._effective_repo_root() == tmp_path

    def test_env_override_takes_precedence(
        self, tmp_path, monkeypatch,
    ):
        override = tmp_path / "alt-workspace"
        override.mkdir()
        monkeypatch.setenv(_WORKSPACE_FLAG, str(override))
        ac = AutoCommitter(repo_root=tmp_path / "primary")
        assert ac._effective_repo_root() == override


class TestAutoCommitterSovereigntyAssertion:
    def test_master_off_assertion_is_noop(self, tmp_path):
        # Default: master OFF. Assertion must NOT raise even
        # against an unowned path.
        ac = AutoCommitter(repo_root=tmp_path)
        ac._assert_commit_target_sovereign()  # no raise

    def test_master_on_unowned_raises(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        ac = AutoCommitter(repo_root=tmp_path)
        from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
            LedgerSovereigntyError,
        )
        with pytest.raises(LedgerSovereigntyError):
            ac._assert_commit_target_sovereign()

    def test_master_on_owned_passes(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        mark_owned(
            tmp_path,
            session_id="s-pass", branch_name="b",
        )
        monkeypatch.setenv(_SESSION_FLAG, "s-pass")
        ac = AutoCommitter(repo_root=tmp_path)
        ac._assert_commit_target_sovereign()  # no raise

    def test_master_on_session_mismatch_raises(
        self, tmp_path, monkeypatch,
    ):
        _enable(monkeypatch)
        mark_owned(
            tmp_path,
            session_id="real-session", branch_name="b",
        )
        monkeypatch.setenv(_SESSION_FLAG, "expected-different")
        ac = AutoCommitter(repo_root=tmp_path)
        from backend.core.ouroboros.governance.ledger_sovereignty import (  # noqa: E501
            LedgerSovereigntyError,
        )
        with pytest.raises(LedgerSovereigntyError):
            ac._assert_commit_target_sovereign()


class TestAutoCommitterCommitContract:
    @pytest.mark.asyncio
    async def test_sovereignty_refusal_surfaces_as_skipped_reason(
        self, tmp_path, monkeypatch,
    ):
        """Public `commit` MUST never raise (line 199 contract).
        Sovereignty violation surfaces as CommitResult with
        skipped_reason=ledger_sovereignty_refused.
        """
        _enable(monkeypatch)
        # tmp_path has no marker — assertion will raise inside
        # commit() and get converted to CommitResult.
        ac = AutoCommitter(repo_root=tmp_path)
        result = await ac.commit(
            op_id="op-x",
            description="test",
            target_files=("a.py",),
        )
        assert isinstance(result, CommitResult)
        assert result.committed is False
        assert (
            result.skipped_reason
            == "ledger_sovereignty_refused"
        )
        assert "sovereignty" in result.error.lower()

    @pytest.mark.asyncio
    async def test_master_off_legacy_path_byte_identical(
        self, tmp_path, monkeypatch,
    ):
        """Master OFF + no target_files → must skip with the
        existing legacy reason, NOT be intercepted by sovereignty.
        Proves the pre-substrate code path is byte-identical.
        """
        # Master flag deliberately unset.
        ac = AutoCommitter(repo_root=tmp_path)
        result = await ac.commit(
            op_id="op-x",
            description="test",
            target_files=(),  # empty -> legacy skip path
        )
        assert result.skipped_reason == "no_target_files"


# ===========================================================================
# Surface 3 — Harness boot phase
# ===========================================================================


@pytest.fixture
def tmp_harness(tmp_path: Path) -> Iterator[BattleTestHarness]:
    session_dir = (
        tmp_path / ".ouroboros" / "sessions" / "bt-sov-test"
    )
    session_dir.mkdir(parents=True, exist_ok=True)
    config = HarnessConfig(
        repo_path=tmp_path,
        cost_cap_usd=0.05,
        idle_timeout_s=10.0,
        session_dir=session_dir,
    )
    h = BattleTestHarness(config)
    yield h
    import atexit
    try:
        atexit.unregister(h._atexit_fallback_write)
    except Exception:
        pass


class TestHarnessWorkspaceBoot:
    def test_method_exists(self):
        assert hasattr(
            BattleTestHarness,
            "_boot_ledger_sovereignty_workspace",
        )
        m = (
            BattleTestHarness
            ._boot_ledger_sovereignty_workspace
        )
        assert inspect.iscoroutinefunction(m)

    def test_master_off_is_noop(
        self, tmp_harness, monkeypatch,
    ):
        # Default: master OFF.
        called = {"create": 0}

        async def _spy(self, branch_name):  # noqa: ARG001
            called["create"] += 1
            return Path("/should-not-be-called")

        with patch.object(
            WorktreeManager, "create", _spy,
        ):
            asyncio.run(
                tmp_harness
                ._boot_ledger_sovereignty_workspace()
            )

        assert called["create"] == 0
        # Env var NOT set.
        assert _WORKSPACE_FLAG not in os.environ

    def test_master_on_creates_workspace_and_sets_env(
        self, tmp_harness, monkeypatch, tmp_path,
    ):
        _enable(monkeypatch)
        fake_wt = tmp_path / "fake-worktree"
        fake_wt.mkdir()

        async def _fake_create(self, branch_name):  # noqa: ARG001
            return fake_wt

        with patch.object(
            WorktreeManager, "create", _fake_create,
        ):
            asyncio.run(
                tmp_harness
                ._boot_ledger_sovereignty_workspace()
            )

        assert os.environ.get(_WORKSPACE_FLAG) == str(fake_wt)
        assert os.environ.get(_SESSION_FLAG) == (
            tmp_harness._session_id
        )
        assert tmp_harness._auto_commit_workspace == fake_wt

    def test_master_on_create_failure_is_fail_open(
        self, tmp_harness, monkeypatch,
    ):
        """If WorktreeManager.create raises, boot continues
        without raising and without setting the env var. The
        downstream AutoCommitter assertion will catch the
        problem at commit time."""
        _enable(monkeypatch)

        async def _boom(self, branch_name):  # noqa: ARG001
            raise RuntimeError("disk full")

        with patch.object(WorktreeManager, "create", _boom):
            # MUST NOT raise.
            asyncio.run(
                tmp_harness
                ._boot_ledger_sovereignty_workspace()
            )

        # Env var NOT set on failure path.
        assert _WORKSPACE_FLAG not in os.environ


# ===========================================================================
# Surface 4 — boot-sequence positional invariant
# ===========================================================================


def test_boot_sequence_position():
    """The sovereignty workspace boot phase must run AFTER the
    readiness gate (so the gate's verdict short-circuits a
    refused soak before any worktree is created) and BEFORE
    boot_jarvis_tiers (so any tier that touches the commit
    cwd sees the env var set)."""
    src = Path(
        inspect.getfile(BattleTestHarness)
    ).read_text(encoding="utf-8")
    gate_idx = src.find(
        '_BootPhase("boot_provider_readiness_gate")'
    )
    ws_idx = src.find(
        '_BootPhase("boot_ledger_sovereignty_workspace")'
    )
    tiers_idx = src.find('_BootPhase("boot_jarvis_tiers")')
    assert gate_idx > 0
    assert ws_idx > 0
    assert tiers_idx > 0
    assert gate_idx < ws_idx < tiers_idx, (
        f"boot ordering drift: gate={gate_idx} "
        f"workspace={ws_idx} tiers={tiers_idx}"
    )


# ===========================================================================
# Surface 5 — full end-to-end with real git
# ===========================================================================


class TestEndToEndStructuralProtection:
    """The whole point of this arc — prove the operator's main
    checkout is structurally protected even when the loop tries
    to commit to it."""

    @pytest.mark.asyncio
    async def test_master_on_unowned_main_refuses_commit(
        self, git_repo, monkeypatch,
    ):
        """Simulate the v18 franken-commit scenario: master ON,
        AutoCommitter pointed at the operator's main checkout
        (no marker). The commit must be structurally refused —
        no `git commit` subprocess ever fires."""
        _enable(monkeypatch)
        monkeypatch.setenv(_SESSION_FLAG, "bt-e2e-prot")
        # AutoCommitter pointed at git_repo (operator's main —
        # no marker exists there).
        ac = AutoCommitter(repo_root=git_repo)
        # Write a file the auto-committer would normally commit.
        (git_repo / "ghost.py").write_text(
            "# malicious autonomous edit\n"
        )
        result = await ac.commit(
            op_id="e2e-prot-1",
            description="ghost commit attempt",
            target_files=("ghost.py",),
        )
        assert result.committed is False
        assert (
            result.skipped_reason
            == "ledger_sovereignty_refused"
        )
        # Critical: NO commit landed in git_repo.
        log = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=git_repo, check=True,
            capture_output=True, text=True,
        ).stdout
        # Only the initial "init" commit — no autonomous trace.
        assert "ghost" not in log.lower()
        assert log.count("\n") == 1  # exactly the init commit

    @pytest.mark.asyncio
    async def test_master_on_owned_worktree_commits(
        self, git_repo, monkeypatch,
    ):
        """Same setup but routed to an owned worktree: commit
        lands. Proves the boundary protects unowned paths
        without blocking legitimate auto-commits."""
        _enable(monkeypatch)
        monkeypatch.setenv(_SESSION_FLAG, "bt-e2e-ok")
        mgr = WorktreeManager(repo_root=git_repo)
        wt_path = await mgr.create("ouroboros/auto/bt-e2e-ok")
        try:
            assert is_owned(wt_path)
            monkeypatch.setenv(_WORKSPACE_FLAG, str(wt_path))
            # Stage a file in the worktree.
            (wt_path / "feature.py").write_text(
                "# legit autonomous change\n"
            )
            ac = AutoCommitter(repo_root=git_repo)
            result = await ac.commit(
                op_id="e2e-ok-1",
                description="legit auto-commit",
                target_files=("feature.py",),
                rationale="legit",
            )
            # Should NOT be the sovereignty refusal — it may
            # succeed or fail for other reasons (missing
            # git-config, etc.) but the sovereignty path is
            # cleared.
            assert (
                result.skipped_reason
                != "ledger_sovereignty_refused"
            )
        finally:
            await mgr.cleanup(wt_path)


# ===========================================================================
# Surface 6 — marker payload survives a worktree creation lifecycle
# ===========================================================================


def test_marker_persists_after_create_lifecycle(
    git_repo, monkeypatch,
):
    """The marker stamped by WorktreeManager.create persists
    across read_ownership calls and survives JSON roundtrips —
    proves Slice 1 substrate + Slice 2 wiring compose lossless."""
    _enable(monkeypatch)
    monkeypatch.setenv(_SESSION_FLAG, "bt-persist")
    mgr = WorktreeManager(repo_root=git_repo)
    wt_path = asyncio.run(
        mgr.create("ouroboros/test/persist"),
    )
    try:
        # First read.
        rec1 = read_ownership(wt_path)
        # Second read.
        rec2 = read_ownership(wt_path)
        assert rec1 is not None
        assert rec1 == rec2
        # Raw payload is well-formed JSON.
        raw = marker_path(wt_path).read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert payload["session_id"] == "bt-persist"
        assert payload["branch_name"] == "ouroboros/test/persist"
    finally:
        asyncio.run(mgr.cleanup(wt_path))
