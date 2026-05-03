"""AutoCommitterIgnoreGuard Slice 2 -- regression spine.

Pins the two-layer fail-closed contract:
  * Layer 1: ``AutoCommitter._stage_files`` consults
    ``gitignore_guard.find_ignored_targets`` BEFORE each
    ``git add``; refuses ignored paths; surfaces them in
    ``CommitResult.skipped_ignored``.
  * Layer 2: post-stage ``_validate_no_ignored_staged`` runs
    ``git diff --cached --name-only`` and cross-checks via the
    same guard; if anything slipped past Layer 1, the commit
    is ABORTED, the index is reset, and the breach is surfaced
    in ``CommitResult.aborted_validator_breach``.

Coverage:
  * CommitResult dataclass additive fields default empty
  * Layer 1 short-circuit when guard master flag off
    (existing pre-Slice-2 behavior preserved)
  * Layer 1 refuses ignored paths + records them
  * Layer 1 mixes ignored + clean correctly (clean staged,
    ignored skipped)
  * Layer 1 fail-open when guard subprocess fails (defensive)
  * Layer 2 catches breach not covered by Layer 1 (e.g.,
    Layer 1 guard subprocess fails fail-open, Layer 2 catches
    it)
  * Layer 2 ABORTS commit + resets index when breach detected
  * Layer 2 returns clean tuple when no breach
  * End-to-end on a real git repo with .gitignore + tracked
    legacy file: AutoCommitter refuses to commit modifications
    to the tracked-but-ignored file
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.auto_committer import (
    AutoCommitter,
    CommitResult,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
        "JARVIS_GITIGNORE_CHECK_TIMEOUT_S",
        "JARVIS_AUTO_COMMIT_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    # Re-import auto_committer to refresh _ENABLED reading
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_ENABLED", "true")


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Real git repo with .gitignore + tracked legacy + clean."""
    if not shutil.which("git"):
        pytest.skip("git binary not available")
    monkeypatch.setenv(
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_AUTO_COMMIT_ENABLED", "true")

    def _run(*args):
        return subprocess.run(
            ["git", *args],
            cwd=str(tmp_path),
            capture_output=True, text=True, check=True,
        )

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "test@example.com")
    _run("config", "user.name", "test")

    # .gitignore: ignore *.pyc + secrets/
    (tmp_path / ".gitignore").write_text(
        "*.pyc\nsecrets/\n"
    )

    # src.py = clean tracked
    (tmp_path / "src.py").write_text("x = 1\n")
    # tracked_legacy.pyc = tracked-but-ignored (force-added)
    (tmp_path / "tracked_legacy.pyc").write_text("legacy")

    _run("add", ".gitignore", "src.py")
    _run("add", "-f", "tracked_legacy.pyc")
    _run("commit", "-q", "-m", "initial")
    return tmp_path


# ---------------------------------------------------------------------------
# CommitResult additive fields
# ---------------------------------------------------------------------------


class TestCommitResultDefaults:
    def test_skipped_ignored_default_empty(self):
        r = CommitResult(committed=False)
        assert r.skipped_ignored == ()
        assert isinstance(r.skipped_ignored, tuple)

    def test_aborted_validator_breach_default_empty(self):
        r = CommitResult(committed=False)
        assert r.aborted_validator_breach == ()
        assert isinstance(r.aborted_validator_breach, tuple)

    def test_default_committed_true_clean_state(self):
        r = CommitResult(
            committed=True, commit_hash="abc12345",
        )
        assert r.skipped_ignored == ()
        assert r.aborted_validator_breach == ()


# ---------------------------------------------------------------------------
# Layer 1 — _stage_files pre-check
# ---------------------------------------------------------------------------


class TestLayer1StageFiles:
    @pytest.mark.asyncio
    async def test_master_off_no_filtering(
        self, tmp_path, monkeypatch,
    ):
        """When guard master flag is off, _stage_files should
        behave identically to pre-Slice-2: no filtering, no
        skipped_ignored entries."""
        if not shutil.which("git"):
            pytest.skip("git binary not available")
        monkeypatch.delenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            raising=False,
        )
        # Build a tiny repo
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "x@y.z"],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "x"],
            cwd=str(tmp_path), check=True,
        )
        (tmp_path / ".gitignore").write_text("*.pyc\n")
        (tmp_path / "ok.py").write_text("x")
        committer = AutoCommitter(repo_root=tmp_path)
        staged, skipped = await committer._stage_files(("ok.py",))
        assert staged is True
        assert skipped == ()

    @pytest.mark.asyncio
    async def test_layer1_refuses_ignored_path(self, git_repo):
        """When master flag on + path is ignored, Layer 1 skips
        it + records in skipped_ignored."""
        # Modify the tracked-but-ignored file so git status sees it.
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)
        staged, skipped = await committer._stage_files(
            ("tracked_legacy.pyc",),
        )
        # Nothing staged (the only target was ignored)
        assert staged is False
        assert "tracked_legacy.pyc" in skipped

    @pytest.mark.asyncio
    async def test_layer1_partitions_ignored_vs_clean(self, git_repo):
        """Mixed inputs: clean files staged, ignored files
        refused + recorded."""
        (git_repo / "src.py").write_text("y = 2")  # modify clean
        (git_repo / "tracked_legacy.pyc").write_text("v2")
        committer = AutoCommitter(repo_root=git_repo)
        staged, skipped = await committer._stage_files((
            "src.py",
            "tracked_legacy.pyc",
        ))
        assert staged is True  # src.py staged
        assert "tracked_legacy.pyc" in skipped
        assert "src.py" not in skipped

    @pytest.mark.asyncio
    async def test_layer1_fail_open_on_guard_exception(
        self, git_repo,
    ):
        """If the guard's find_ignored_targets raises, Layer 1
        should fail-open (skip the filter, attempt the staging
        normally). Layer 2 is the safety net."""
        (git_repo / "src.py").write_text("y = 2")
        committer = AutoCommitter(repo_root=git_repo)
        with mock.patch(
            "backend.core.ouroboros.governance."
            "gitignore_guard.find_ignored_targets",
            side_effect=RuntimeError("guard boom"),
        ):
            staged, skipped = await committer._stage_files(
                ("src.py",),
            )
        # src.py is clean so it stages despite the guard raising.
        assert staged is True
        assert skipped == ()


# ---------------------------------------------------------------------------
# Layer 2 — post-stage validator
# ---------------------------------------------------------------------------


class TestLayer2Validator:
    @pytest.mark.asyncio
    async def test_validator_clean_returns_empty(self, git_repo):
        """Stage a clean file, validator finds no breach."""
        (git_repo / "src.py").write_text("y = 2")
        subprocess.run(
            ["git", "add", "src.py"],
            cwd=str(git_repo), check=True,
        )
        committer = AutoCommitter(repo_root=git_repo)
        breach = await committer._validate_no_ignored_staged()
        assert breach == ()

    @pytest.mark.asyncio
    async def test_validator_catches_force_staged_ignored(
        self, git_repo,
    ):
        """Bypass Layer 1 entirely by force-staging an ignored
        file directly via git, then run Layer 2 -- it must
        catch the breach."""
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        # Force-stage past .gitignore (bypasses Layer 1 since we
        # skip it and call git add -f directly)
        subprocess.run(
            ["git", "add", "-f", "tracked_legacy.pyc"],
            cwd=str(git_repo), check=True,
        )
        committer = AutoCommitter(repo_root=git_repo)
        breach = await committer._validate_no_ignored_staged()
        assert "tracked_legacy.pyc" in breach

    @pytest.mark.asyncio
    async def test_validator_subprocess_failure_returns_empty(
        self, tmp_path,
    ):
        """Validator NEVER raises; subprocess failure -> ()."""
        committer = AutoCommitter(repo_root=tmp_path)
        # Simulate subprocess failure by patching create_subprocess_exec
        # to raise.
        with mock.patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("no git"),
        ):
            breach = await committer._validate_no_ignored_staged()
        assert breach == ()


# ---------------------------------------------------------------------------
# _reset_index helper
# ---------------------------------------------------------------------------


class TestResetIndex:
    @pytest.mark.asyncio
    async def test_reset_unstages_changes(self, git_repo):
        # Stage src.py
        (git_repo / "src.py").write_text("y = 2")
        subprocess.run(
            ["git", "add", "src.py"],
            cwd=str(git_repo), check=True,
        )
        # Confirm staged
        diff_proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert "src.py" in diff_proc.stdout

        # Reset
        committer = AutoCommitter(repo_root=git_repo)
        ok = await committer._reset_index()
        assert ok is True

        # Confirm un-staged
        diff_proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert "src.py" not in diff_proc.stdout

    @pytest.mark.asyncio
    async def test_reset_subprocess_failure_returns_false(
        self, tmp_path,
    ):
        """Reset NEVER raises; subprocess failure -> False."""
        committer = AutoCommitter(repo_root=tmp_path)
        with mock.patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("no git"),
        ):
            assert await committer._reset_index() is False


# ---------------------------------------------------------------------------
# End-to-end via commit() — the load-bearing test
# ---------------------------------------------------------------------------


class TestCommitEndToEnd:
    @pytest.mark.asyncio
    async def test_commit_refuses_only_ignored_target(
        self, git_repo, monkeypatch,
    ):
        """commit() called with only a tracked-but-ignored file
        as target -> Layer 1 skips it -> nothing_to_stage outcome
        with the path surfaced in skipped_ignored."""
        # Reload _ENABLED at module level
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-test-1",
            description="should not commit ignored",
            target_files=("tracked_legacy.pyc",),
        )
        assert result.committed is False
        assert result.skipped_reason == "nothing_to_stage"
        assert "tracked_legacy.pyc" in result.skipped_ignored

    @pytest.mark.asyncio
    async def test_commit_clean_path_succeeds(
        self, git_repo, monkeypatch,
    ):
        """commit() with a clean path succeeds + leaves
        skipped_ignored / aborted_validator_breach empty."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        (git_repo / "src.py").write_text("y = 2")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-clean-1",
            description="clean change",
            target_files=("src.py",),
        )
        assert result.committed is True
        assert result.skipped_ignored == ()
        assert result.aborted_validator_breach == ()

    @pytest.mark.asyncio
    async def test_commit_partitions_mixed_inputs(
        self, git_repo, monkeypatch,
    ):
        """commit() with mixed clean + ignored: stages clean,
        skips ignored, commit succeeds with skipped surfaced."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        (git_repo / "src.py").write_text("y = 2")
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-mixed-1",
            description="mixed",
            target_files=("src.py", "tracked_legacy.pyc"),
        )
        assert result.committed is True
        assert "tracked_legacy.pyc" in result.skipped_ignored
        assert "src.py" not in result.skipped_ignored

    @pytest.mark.asyncio
    async def test_layer2_aborts_when_layer1_fails_open(
        self, git_repo, monkeypatch,
    ):
        """Simulate Layer 1 fail-open (guard subprocess fails ->
        empty refused list) but Layer 2 still catches the breach.
        commit() must abort + reset index + populate
        aborted_validator_breach."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        (git_repo / "tracked_legacy.pyc").write_text("modified")
        committer = AutoCommitter(repo_root=git_repo)

        # Patch find_ignored_targets to fail-open at Layer 1 only
        # by RAISING (which the Layer 1 try/except catches and
        # treats as no-refusal). Layer 2 calls the same function
        # via _validate_no_ignored_staged which uses a fresh
        # import path -- we need to patch the SOURCE module so
        # both call sites see the same patch initially, then
        # selectively unblock Layer 2.
        original_find = (
            __import__(
                "backend.core.ouroboros.governance.gitignore_guard",
                fromlist=["find_ignored_targets"],
            ).find_ignored_targets
        )

        call_count = {"n": 0}

        def _selectively_fail(repo_root, target_files, **_kwargs):
            call_count["n"] += 1
            # First call (Layer 1 pre-stage) raises
            if call_count["n"] == 1:
                raise RuntimeError("simulated Layer 1 failure")
            # Subsequent calls (Layer 2 validator) use real impl
            return original_find(repo_root, target_files)

        with mock.patch(
            "backend.core.ouroboros.governance."
            "gitignore_guard.find_ignored_targets",
            side_effect=_selectively_fail,
        ):
            result = await committer.commit(
                op_id="op-layer2-test",
                description="layer 2 catch",
                target_files=("tracked_legacy.pyc",),
            )

        # Layer 1 fail-open let it through staging. Layer 2 caught
        # the breach + aborted + reset.
        assert result.committed is False
        assert "gitignore_breach_blocked" in result.error
        assert "tracked_legacy.pyc" in result.aborted_validator_breach

        # Index should be clean post-reset.
        diff_proc = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(git_repo), capture_output=True, text=True,
        )
        assert "tracked_legacy.pyc" not in diff_proc.stdout

    @pytest.mark.asyncio
    async def test_no_target_files_short_circuits(
        self, git_repo, monkeypatch,
    ):
        """Pre-existing short-circuit preserved (no target_files
        -> early return)."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-empty",
            description="x",
            target_files=(),
        )
        assert result.committed is False
        assert result.skipped_reason == "no_target_files"


# ---------------------------------------------------------------------------
# Backward-compat: pre-Slice-2 behavior preserved when guard off
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    @pytest.mark.asyncio
    async def test_master_off_full_commit_still_works(
        self, git_repo, monkeypatch,
    ):
        """When guard master flag is off, AutoCommitter should
        commit clean files normally + not block ignored paths
        either (pre-Slice-2 behavior). Layer 2 also short-
        circuits when guard is off because find_ignored_targets
        returns empty."""
        import backend.core.ouroboros.governance.auto_committer as ac
        monkeypatch.setattr(ac, "_ENABLED", True)
        monkeypatch.delenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            raising=False,
        )
        (git_repo / "src.py").write_text("y = 2")
        committer = AutoCommitter(repo_root=git_repo)
        result = await committer.commit(
            op_id="op-bw-1",
            description="clean change with guard off",
            target_files=("src.py",),
        )
        assert result.committed is True
        # No filtering happened (guard off)
        assert result.skipped_ignored == ()
        assert result.aborted_validator_breach == ()
