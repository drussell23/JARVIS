"""GitignoreGuard Slice 1 -- regression spine.

Pins the pure-stdlib subprocess primitive that asks ``git
check-ignore`` whether a given path matches a ``.gitignore``
rule independently of tracked status -- the load-bearing
property closing the AutoCommitter sovereignty gap surfaced in
soak v4.

Coverage:
  * Master flag asymmetric env semantics (default false until
    Slice 3)
  * Timeout env knob clamping (floor/ceiling/garbage)
  * Closed-5 GitignoreGuardOutcome taxonomy + value set
  * Frozen GitignoreViolation dataclass + to_dict round-trip
  * is_path_ignored: master-off / empty-input / non-string /
    untracked-ignored / tracked-ignored / not-ignored
  * find_ignored_targets: empty / single-batch / dedup of
    duplicates / non-string entries skipped / fail-open on
    subprocess failure
  * find_tracked_but_ignored: master-off / empty-tracked /
    no-ignored / batched correctly / fail-open
  * classify_path: every closed-5 outcome reachable
  * NEVER raises -- defensive across every IO boundary
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.gitignore_guard import (
    GITIGNORE_GUARD_SCHEMA_VERSION,
    GitignoreGuardOutcome,
    GitignoreViolation,
    classify_path,
    find_ignored_targets,
    find_tracked_but_ignored,
    gitignore_check_timeout_s,
    gitignore_guard_enabled,
    is_path_ignored,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
        "JARVIS_GITIGNORE_CHECK_TIMEOUT_S",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Build a real (tiny) git repo with a .gitignore + tracked +
    untracked + tracked-but-ignored files. Skips when git is not
    available on PATH."""
    if not shutil.which("git"):
        pytest.skip("git binary not available")
    monkeypatch.setenv(
        "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
    )

    def _run(*args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=str(tmp_path),
            capture_output=True, text=True,
            check=check,
        )
        return result

    _run("init", "-q", "-b", "main")
    _run("config", "user.email", "test@example.com")
    _run("config", "user.name", "test")

    # .gitignore: ignore *.pyc + secrets/
    (tmp_path / ".gitignore").write_text(
        "*.pyc\nsecrets/\nbuild/\n"
    )

    # Files
    (tmp_path / "src.py").write_text("x = 1\n")
    (tmp_path / "ignored_new.pyc").write_text("garbage")  # untracked + ignored
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key").write_text("secret")  # untracked + ignored

    # Tracked-but-ignored: stage a .pyc with --force, then commit.
    (tmp_path / "tracked_legacy.pyc").write_text("legacy bytecode")
    _run("add", ".gitignore", "src.py")
    _run("add", "-f", "tracked_legacy.pyc")
    _run("commit", "-q", "-m", "initial")
    return tmp_path


# ---------------------------------------------------------------------------
# Constants + flags
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    def test_constant(self):
        assert GITIGNORE_GUARD_SCHEMA_VERSION == "gitignore_guard.v1"


class TestMasterFlag:
    def test_default_true_post_graduation(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            raising=False,
        )
        assert gitignore_guard_enabled() is True

    def test_empty_is_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "",
        )
        assert gitignore_guard_enabled() is True

    def test_whitespace_is_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "   ",
        )
        assert gitignore_guard_enabled() is True

    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "On"])
    def test_truthy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", raw,
        )
        assert gitignore_guard_enabled() is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "garbage"])
    def test_falsy(self, monkeypatch, raw):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", raw,
        )
        assert gitignore_guard_enabled() is False


class TestTimeoutKnob:
    def test_default(self):
        assert gitignore_check_timeout_s() == 5.0

    def test_floor(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GITIGNORE_CHECK_TIMEOUT_S", "0.0")
        assert gitignore_check_timeout_s() == 1.0

    def test_ceiling(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GITIGNORE_CHECK_TIMEOUT_S", "100")
        assert gitignore_check_timeout_s() == 30.0

    def test_garbage(self, monkeypatch):
        monkeypatch.setenv("JARVIS_GITIGNORE_CHECK_TIMEOUT_S", "abc")
        assert gitignore_check_timeout_s() == 5.0


# ---------------------------------------------------------------------------
# Closed-5 enum + frozen dataclass
# ---------------------------------------------------------------------------


class TestClosedTaxonomy:
    def test_exactly_five_values(self):
        assert len(list(GitignoreGuardOutcome)) == 5

    def test_value_set_exact(self):
        expected = {
            "clean", "skipped_ignored", "blocked_tracked_ignored",
            "disabled", "failed",
        }
        actual = {v.value for v in GitignoreGuardOutcome}
        assert actual == expected


class TestViolationDataclass:
    def test_default(self):
        v = GitignoreViolation(
            file_path="x.pyc",
            outcome=GitignoreGuardOutcome.SKIPPED_IGNORED,
        )
        assert v.file_path == "x.pyc"
        assert v.outcome is GitignoreGuardOutcome.SKIPPED_IGNORED
        assert v.source == ""
        assert v.schema_version == GITIGNORE_GUARD_SCHEMA_VERSION

    def test_frozen(self):
        v = GitignoreViolation(
            file_path="x.pyc",
            outcome=GitignoreGuardOutcome.SKIPPED_IGNORED,
        )
        with pytest.raises(FrozenInstanceError):
            v.file_path = "y.pyc"  # type: ignore[misc]

    def test_to_dict(self):
        v = GitignoreViolation(
            file_path="src/x.pyc",
            outcome=GitignoreGuardOutcome.BLOCKED_TRACKED_IGNORED,
            source=".gitignore:5",
        )
        d = v.to_dict()
        assert d["file_path"] == "src/x.pyc"
        assert d["outcome"] == "blocked_tracked_ignored"
        assert d["source"] == ".gitignore:5"


# ---------------------------------------------------------------------------
# Master-off short-circuits
# ---------------------------------------------------------------------------


class TestMasterOffShortCircuits:
    def test_is_path_ignored_master_off_returns_false(
        self, tmp_path, monkeypatch,
    ):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        # Even a clearly-ignored .pyc returns False when master off.
        assert is_path_ignored(tmp_path, "x.pyc") is False

    def test_find_ignored_targets_master_off_returns_empty(
        self, tmp_path, monkeypatch,
    ):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        assert find_ignored_targets(
            tmp_path, ["x.pyc", "y.pyc"],
        ) == ()

    def test_find_tracked_but_ignored_master_off_returns_empty(
        self, tmp_path, monkeypatch,
    ):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        assert find_tracked_but_ignored(tmp_path) == ()

    def test_classify_master_off_returns_disabled(
        self, tmp_path, monkeypatch,
    ):
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        out = classify_path(tmp_path, "x.pyc")
        assert out is GitignoreGuardOutcome.DISABLED

    def test_master_off_no_subprocess(self, tmp_path, monkeypatch):
        """Ensure master-off path does NOT launch any subprocess."""
        # Post-graduation default is true; explicit "false" is the
        # operator escape hatch.
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED",
            "false",
        )
        with mock.patch(
            "subprocess.run",
            side_effect=AssertionError("subprocess.run should not run"),
        ):
            is_path_ignored(tmp_path, "x.pyc")
            find_ignored_targets(tmp_path, ["a.pyc"])
            find_tracked_but_ignored(tmp_path)
            classify_path(tmp_path, "x.pyc")


# ---------------------------------------------------------------------------
# is_path_ignored — input validation
# ---------------------------------------------------------------------------


class TestIsPathIgnoredInputs:
    def test_empty_string_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
        assert is_path_ignored(tmp_path, "") is False
        assert is_path_ignored(tmp_path, "   ") is False

    def test_non_string_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
        assert is_path_ignored(tmp_path, None) is False  # type: ignore[arg-type]
        assert is_path_ignored(tmp_path, 42) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Real git repo integration tests
# ---------------------------------------------------------------------------


class TestRealGitRepo:
    def test_untracked_ignored_path_returns_true(self, git_repo):
        assert is_path_ignored(git_repo, "ignored_new.pyc") is True

    def test_clean_tracked_path_returns_false(self, git_repo):
        assert is_path_ignored(git_repo, "src.py") is False

    def test_tracked_but_ignored_returns_true(self, git_repo):
        # The load-bearing property: even though tracked_legacy.pyc
        # is in the index, .gitignore matches it -> True.
        assert is_path_ignored(git_repo, "tracked_legacy.pyc") is True

    def test_directory_ignored_returns_true(self, git_repo):
        # secrets/ matches the secrets/ rule.
        assert is_path_ignored(git_repo, "secrets/key") is True

    def test_nonexistent_clean_path_returns_false(self, git_repo):
        # Path doesn't exist + not ignored -> False
        assert is_path_ignored(git_repo, "nonexistent.py") is False

    def test_find_ignored_targets_partitions_correctly(self, git_repo):
        out = find_ignored_targets(git_repo, [
            "src.py",            # clean
            "ignored_new.pyc",   # ignored + untracked
            "tracked_legacy.pyc", # ignored + tracked
            "secrets/key",       # ignored (dir match)
            "nonexistent.py",    # not ignored
        ])
        assert "src.py" not in out
        assert "ignored_new.pyc" in out
        assert "tracked_legacy.pyc" in out
        assert "secrets/key" in out
        assert "nonexistent.py" not in out

    def test_find_ignored_targets_empty_input(self, git_repo):
        assert find_ignored_targets(git_repo, []) == ()

    def test_find_ignored_targets_all_clean(self, git_repo):
        assert find_ignored_targets(git_repo, ["src.py"]) == ()

    def test_find_ignored_targets_dedup(self, git_repo):
        out = find_ignored_targets(git_repo, [
            "ignored_new.pyc",
            "ignored_new.pyc",  # duplicate
            "ignored_new.pyc",
        ])
        # Single batch call dedupes input -> one output entry.
        assert len(out) == 1
        assert out[0] == "ignored_new.pyc"

    def test_find_ignored_targets_skips_garbage(self, git_repo):
        out = find_ignored_targets(git_repo, [
            "ignored_new.pyc",
            None,  # type: ignore[list-item]
            "",
            "   ",
            42,  # type: ignore[list-item]
        ])
        assert out == ("ignored_new.pyc",)

    def test_find_tracked_but_ignored_finds_legacy(self, git_repo):
        out = find_tracked_but_ignored(git_repo)
        # tracked_legacy.pyc was force-added despite *.pyc rule.
        assert "tracked_legacy.pyc" in out
        # src.py is tracked but NOT ignored -> excluded.
        assert "src.py" not in out
        # ignored_new.pyc is ignored but NOT tracked -> excluded.
        assert "ignored_new.pyc" not in out

    def test_find_tracked_but_ignored_clean_repo_empty(
        self, tmp_path, monkeypatch,
    ):
        if not shutil.which("git"):
            pytest.skip("git binary not available")
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
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
        (tmp_path / "src.py").write_text("x")
        subprocess.run(
            ["git", "add", "."],
            cwd=str(tmp_path), check=True,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "init"],
            cwd=str(tmp_path), check=True,
        )
        assert find_tracked_but_ignored(tmp_path) == ()

    def test_classify_clean(self, git_repo):
        assert classify_path(git_repo, "src.py") is (
            GitignoreGuardOutcome.CLEAN
        )

    def test_classify_skipped_ignored(self, git_repo):
        # ignored + untracked
        assert classify_path(git_repo, "ignored_new.pyc") is (
            GitignoreGuardOutcome.SKIPPED_IGNORED
        )

    def test_classify_blocked_tracked_ignored(self, git_repo):
        # ignored + tracked -- the load-bearing case
        assert classify_path(
            git_repo, "tracked_legacy.pyc",
        ) is GitignoreGuardOutcome.BLOCKED_TRACKED_IGNORED

    def test_classify_failed_on_empty_input(self, git_repo):
        assert classify_path(git_repo, "") is (
            GitignoreGuardOutcome.FAILED
        )


# ---------------------------------------------------------------------------
# Fail-open contract: subprocess failures degrade silently
# ---------------------------------------------------------------------------


class TestFailOpen:
    @pytest.fixture(autouse=True)
    def _enable(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )

    def test_git_missing_returns_false(self, tmp_path):
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError,
        ):
            assert is_path_ignored(tmp_path, "x.pyc") is False
            assert find_ignored_targets(tmp_path, ["a.pyc"]) == ()
            assert find_tracked_but_ignored(tmp_path) == ()

    def test_timeout_returns_false(self, tmp_path):
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(
                cmd="git", timeout=5,
            ),
        ):
            assert is_path_ignored(tmp_path, "x.pyc") is False
            assert find_ignored_targets(tmp_path, ["a.pyc"]) == ()
            assert find_tracked_but_ignored(tmp_path) == ()

    def test_oserror_returns_false(self, tmp_path):
        with mock.patch(
            "subprocess.run", side_effect=OSError("disk gone"),
        ):
            assert is_path_ignored(tmp_path, "x.pyc") is False

    def test_unexpected_exception_returns_false(self, tmp_path):
        with mock.patch(
            "subprocess.run",
            side_effect=RuntimeError("unexpected"),
        ):
            assert is_path_ignored(tmp_path, "x.pyc") is False
            assert find_ignored_targets(tmp_path, ["a.pyc"]) == ()

    def test_unexpected_returncode_returns_false(self, tmp_path):
        # returncode 128 = git error (e.g., not a repo)
        result = subprocess.CompletedProcess(
            args=[], returncode=128, stdout="", stderr="fatal",
        )
        with mock.patch(
            "subprocess.run", return_value=result,
        ):
            assert is_path_ignored(tmp_path, "x.pyc") is False
            assert find_ignored_targets(tmp_path, ["a.pyc"]) == ()

    def test_classify_fails_when_subprocess_fails(self, tmp_path):
        # is_path_ignored returns False (fail-open) -> classify
        # returns CLEAN. That's the documented behavior:
        # subprocess failure is indistinguishable from
        # not-ignored at the primitive level. Slice 2's post-
        # staging validator is the second layer that catches
        # any breaches that slip past.
        with mock.patch(
            "subprocess.run", side_effect=FileNotFoundError,
        ):
            assert classify_path(tmp_path, "x.pyc") is (
                GitignoreGuardOutcome.CLEAN
            )


# ---------------------------------------------------------------------------
# Batched find_tracked_but_ignored
# ---------------------------------------------------------------------------


class TestBatching:
    def test_batch_size_clamped_floor(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
        # batch_size=0 should be clamped to 1 internally.
        # We verify by mocking ls-files to return many paths and
        # counting check-ignore calls.
        ls_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="\n".join(f"f{i}.txt" for i in range(50)),
            stderr="",
        )
        check_ignore_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        )
        call_count = {"n": 0}

        def _stub(args, **kwargs):
            if "ls-files" in args:
                return ls_result
            call_count["n"] += 1
            return check_ignore_result

        with mock.patch("subprocess.run", side_effect=_stub):
            find_tracked_but_ignored(
                tmp_path, batch_size=0,
            )
        # 50 paths / batch=1 (clamped) = 50 check-ignore calls
        assert call_count["n"] == 50

    def test_batch_size_clamped_ceiling(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
        # batch_size=99999 should be clamped to 2000.
        ls_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="\n".join(f"f{i}.txt" for i in range(2500)),
            stderr="",
        )
        check_ignore_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        )
        call_count = {"n": 0}

        def _stub(args, **kwargs):
            if "ls-files" in args:
                return ls_result
            call_count["n"] += 1
            return check_ignore_result

        with mock.patch("subprocess.run", side_effect=_stub):
            find_tracked_but_ignored(
                tmp_path, batch_size=99999,
            )
        # 2500 paths / batch=2000 (clamped) = 2 calls
        assert call_count["n"] == 2

    def test_default_batch_size(self, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_AUTO_COMMITTER_GITIGNORE_GUARD_ENABLED", "true",
        )
        # 1500 paths / default batch=500 = 3 calls
        ls_result = subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="\n".join(f"f{i}.txt" for i in range(1500)),
            stderr="",
        )
        check_ignore_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        )
        call_count = {"n": 0}

        def _stub(args, **kwargs):
            if "ls-files" in args:
                return ls_result
            call_count["n"] += 1
            return check_ignore_result

        with mock.patch("subprocess.run", side_effect=_stub):
            find_tracked_but_ignored(tmp_path)
        assert call_count["n"] == 3
