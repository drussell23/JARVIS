# tests/scripts/test_a1_zero_state_lock.py
"""
TDD suite for scripts/a1_zero_state_lock.py — A1 Zero-State Lock.

All tests use temporary git repositories (created via subprocess git init) so
they never touch the live JARVIS repo.  WorktreeManager / _sweep are never
invoked against a real repo; tests that check sweep behaviour use
``sweep=False`` or patch ``_sweep`` directly.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure the scripts/ directory is importable regardless of PYTHONPATH.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from a1_zero_state_lock import (  # noqa: E402
    ZeroStateResult,
    assert_pristine,
    compute_state_digest,
    main,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with one initial commit in ``tmp_path``."""
    run = lambda *args: subprocess.run(  # noqa: E731
        list(args), cwd=str(tmp_path), check=True,
        capture_output=True, text=True,
    )
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test Bot")
    (tmp_path / "README.md").write_text("initial commit")
    run("git", "add", "README.md")
    run("git", "commit", "-m", "init")
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1 — clean repo is pristine, stable digest
# ---------------------------------------------------------------------------

class TestCleanRepo:
    def test_assert_pristine_returns_pristine(self, tmp_path):
        """A freshly initialised repo with one commit is pristine."""
        repo = _init_repo(tmp_path)
        result = assert_pristine(repo, sweep=False)
        assert isinstance(result, ZeroStateResult)
        assert result.pristine is True
        assert result.deviations == []
        assert result.actual_digest == result.expected_digest

    def test_digest_is_64_hex_chars(self, tmp_path):
        """compute_state_digest returns a valid SHA256 hex string."""
        repo = _init_repo(tmp_path)
        digest = compute_state_digest(repo)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_digest_is_stable_across_two_calls(self, tmp_path):
        """Same clean state → same digest (determinism check)."""
        repo = _init_repo(tmp_path)
        d1 = compute_state_digest(repo)
        d2 = compute_state_digest(repo)
        assert d1 == d2


# ---------------------------------------------------------------------------
# Test 2 — untracked file makes it NOT pristine
# ---------------------------------------------------------------------------

class TestUntrackedFile:
    def test_untracked_file_not_pristine(self, tmp_path):
        """An untracked file causes a deviation and pristine=False."""
        repo = _init_repo(tmp_path)
        (repo / "ghost.py").write_text("# ghost state")
        result = assert_pristine(repo, sweep=False)
        assert result.pristine is False
        assert any("ghost.py" in d for d in result.deviations), (
            f"Expected ghost.py in deviations but got: {result.deviations}"
        )

    def test_untracked_file_digest_differs(self, tmp_path):
        """Digest with untracked file differs from clean-state digest."""
        repo = _init_repo(tmp_path)
        clean_digest = compute_state_digest(repo)
        (repo / "ghost.py").write_text("# ghost state")
        dirty_digest = compute_state_digest(repo)
        assert clean_digest != dirty_digest

    def test_staged_modification_not_pristine(self, tmp_path):
        """A staged (but not committed) change is also not pristine."""
        repo = _init_repo(tmp_path)
        (repo / "README.md").write_text("modified")
        subprocess.run(
            ["git", "-C", str(repo), "add", "README.md"],
            check=True, capture_output=True,
        )
        result = assert_pristine(repo, sweep=False)
        assert result.pristine is False


# ---------------------------------------------------------------------------
# Test 3 — orphan worktree dir under .worktrees/ makes it NOT pristine
# ---------------------------------------------------------------------------

class TestOrphanWorktreeDir:
    def test_orphan_unit_dir_not_pristine(self, tmp_path):
        """A unit- orphan directory under .worktrees/ is a deviation."""
        repo = _init_repo(tmp_path)
        orphan = repo / ".worktrees" / "unit-abc123"
        orphan.mkdir(parents=True)
        result = assert_pristine(repo, sweep=False)
        assert result.pristine is False
        assert any("unit-abc123" in d for d in result.deviations), (
            f"Expected unit-abc123 in deviations but got: {result.deviations}"
        )

    def test_orphan_auto_bt_dir_not_pristine(self, tmp_path):
        """An ouroboros__auto__bt- orphan dir is also caught."""
        repo = _init_repo(tmp_path)
        orphan = repo / ".worktrees" / "ouroboros__auto__bt-sess42"
        orphan.mkdir(parents=True)
        result = assert_pristine(repo, sweep=False)
        assert result.pristine is False
        assert any("ouroboros__auto__bt-sess42" in d for d in result.deviations)

    def test_unrelated_dir_under_worktrees_is_ignored(self, tmp_path):
        """A non-orphan-prefix directory under .worktrees/ does NOT trigger."""
        repo = _init_repo(tmp_path)
        legit = repo / ".worktrees" / "myproject-checkout"
        legit.mkdir(parents=True)
        result = assert_pristine(repo, sweep=False)
        # .worktrees/myproject-checkout is untracked by git → shows in status
        # but is NOT in orphan_dirs.  It may or may not appear in status
        # depending on git config, but the deviation list must not say
        # "orphan_dir: myproject-checkout".
        assert not any(
            "orphan_dir" in d and "myproject-checkout" in d
            for d in result.deviations
        )


# ---------------------------------------------------------------------------
# Test 4 — .jarvis/chaos_manifest.json makes it NOT pristine
# ---------------------------------------------------------------------------

class TestChaosManifest:
    def test_chaos_manifest_not_pristine(self, tmp_path):
        """Presence of .jarvis/chaos_manifest.json is a deviation."""
        repo = _init_repo(tmp_path)
        jarvis_dir = repo / ".jarvis"
        jarvis_dir.mkdir(exist_ok=True)
        (jarvis_dir / "chaos_manifest.json").write_text(
            json.dumps({"active": True, "session": "test-123"})
        )
        result = assert_pristine(repo, sweep=False)
        assert result.pristine is False
        assert any("chaos_manifest" in d for d in result.deviations), (
            f"Expected chaos_manifest in deviations but got: {result.deviations}"
        )

    def test_chaos_manifest_gone_is_pristine(self, tmp_path):
        """Removing the chaos manifest restores pristine state."""
        repo = _init_repo(tmp_path)
        jarvis_dir = repo / ".jarvis"
        jarvis_dir.mkdir(exist_ok=True)
        cm = jarvis_dir / "chaos_manifest.json"
        cm.write_text("{}")
        assert assert_pristine(repo, sweep=False).pristine is False
        cm.unlink()
        # .jarvis/ directory is still there but empty → may appear as untracked
        # depending on git version; the chaos_manifest deviation must be gone.
        result2 = assert_pristine(repo, sweep=False)
        assert not any("chaos_manifest" in d for d in result2.deviations)


# ---------------------------------------------------------------------------
# Test 5 — --no-sweep skips the reaper
# ---------------------------------------------------------------------------

class TestNoSweep:
    def test_sweep_false_does_not_call_sweep(self, tmp_path):
        """With sweep=False, the _sweep coroutine is never invoked."""
        repo = _init_repo(tmp_path)
        with patch("a1_zero_state_lock._sweep") as mock_sweep:
            result = assert_pristine(repo, sweep=False)
        mock_sweep.assert_not_called()
        assert result.pristine is True

    def test_sweep_true_calls_sweep(self, tmp_path):
        """With sweep=True (default), _sweep IS called (even if it's a no-op)."""
        repo = _init_repo(tmp_path)
        # _sweep is async; patch it so it returns 0 without touching any real repo.
        import asyncio

        async def _fake_sweep(repo_root):  # noqa: ARG001
            return 0

        with patch("a1_zero_state_lock._sweep", side_effect=_fake_sweep) as mock_sweep:
            result = assert_pristine(repo, sweep=True)
        mock_sweep.assert_called_once_with(repo)
        assert result.pristine is True


# ---------------------------------------------------------------------------
# Test 6 — determinism: identical digest across multiple runs
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_three_successive_digests_are_equal(self, tmp_path):
        """compute_state_digest returns identical output on repeated calls."""
        repo = _init_repo(tmp_path)
        digests = [compute_state_digest(repo) for _ in range(3)]
        assert len(set(digests)) == 1, (
            f"Expected identical digests but got {digests}"
        )

    def test_expected_digest_tracks_head(self, tmp_path):
        """After a new commit, the expected digest changes (it tracks HEAD)."""
        repo = _init_repo(tmp_path)
        result_before = assert_pristine(repo, sweep=False)

        (repo / "newfile.py").write_text("# new")
        subprocess.run(["git", "-C", str(repo), "add", "newfile.py"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "second commit"],
            check=True, capture_output=True,
        )
        result_after = assert_pristine(repo, sweep=False)

        # Both should be pristine, but at different HEADs → different digests.
        assert result_before.pristine is True
        assert result_after.pristine is True
        assert result_before.actual_digest != result_after.actual_digest


# ---------------------------------------------------------------------------
# Test — CLI (main()) return codes
# ---------------------------------------------------------------------------

class TestCLI:
    def test_cli_returns_0_on_pristine(self, tmp_path):
        """main() exits 0 and prints ZERO_STATE_ASSERTED for a clean repo."""
        repo = _init_repo(tmp_path)
        rc = main(["--repo-root", str(repo), "--no-sweep"])
        assert rc == 0

    def test_cli_returns_1_on_dirty(self, tmp_path, capsys):
        """main() exits 1 for a non-pristine repo."""
        repo = _init_repo(tmp_path)
        (repo / "ghost.py").write_text("# ghost")
        rc = main(["--repo-root", str(repo), "--no-sweep"])
        assert rc == 1

    def test_cli_json_output_pristine(self, tmp_path, capsys):
        """--json flag emits a machine-readable JSON object with status=PRISTINE."""
        repo = _init_repo(tmp_path)
        rc = main(["--repo-root", str(repo), "--no-sweep", "--json"])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 0
        assert payload["status"] == "PRISTINE"
        assert len(payload["expected"]) == 64
        assert payload["deviations"] == []

    def test_cli_json_output_not_pristine(self, tmp_path, capsys):
        """--json flag lists deviations when repo is not pristine."""
        repo = _init_repo(tmp_path)
        (repo / "ghost.py").write_text("# ghost")
        rc = main(["--repo-root", str(repo), "--no-sweep", "--json"])
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert rc == 1
        assert payload["status"] == "NOT_PRISTINE"
        assert any("ghost.py" in d for d in payload["deviations"])

    def test_cli_returns_2_on_bad_repo(self, tmp_path):
        """main() exits 2 (fail-closed) when given a non-git directory."""
        non_repo = tmp_path / "not_a_git_repo"
        non_repo.mkdir()
        rc = main(["--repo-root", str(non_repo), "--no-sweep"])
        assert rc == 2
