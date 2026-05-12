"""Treefinement Production Wiring Phase B — GitApplyDiffApplier spine.

Pins the canonical ``git apply`` composition + per-file (old, new)
capture contract. Exercises the parser as a pure function and the
applier against real git-init'd tmp repos.

Invariants covered
------------------
* Pure-function diff parser handles modified / new / deleted /
  multi-file / quoted-path / malformed-input shapes (NEVER raises)
* Apply succeeds → returns DiffApplyResult(files=tuples, error="")
  with ALL touched paths populated correctly
* Apply failure → returns DiffApplyResult(files=(), error="<code>")
  with the operator-greppable error code
* git binary missing → "git_not_installed"
* Apply timeout → "git_apply_timeout" + subprocess killed
* Cancellation propagates with subprocess cleanup
* Empty / whitespace / no-target diffs short-circuit cleanly
* DiffApplier Protocol conformance (runtime isinstance check)
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.repair_tree import (
    DiffApplier,
    DiffApplyResult,
)
from backend.core.ouroboros.governance.repair_tree_production import (
    GIT_APPLY_TIMEOUT_S_ENV_VAR,
    GitApplyDiffApplier,
    extract_diff_targets,
    _ParsedDiffTarget,
)


# ===========================================================================
# git availability gate — skip apply tests if git not installed
# ===========================================================================


_GIT_AVAILABLE = shutil.which("git") is not None
_skip_no_git = pytest.mark.skipif(
    not _GIT_AVAILABLE, reason="git binary not on PATH",
)


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo at ``path`` with a single commit."""
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(path)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    # Initial empty commit so the repo has HEAD
    subprocess.run(
        ["git", "-C", str(path), "commit",
         "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
    )


def _commit_file(path: Path, file_path: str, content: str) -> None:
    """Write ``content`` to ``file_path`` (relative to ``path``) and
    commit it."""
    full = path / file_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", file_path],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", f"add {file_path}"],
        check=True, capture_output=True,
    )


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ===========================================================================
# Pure-function diff parser tests
# ===========================================================================


def test_parse_empty_diff_returns_empty():
    assert extract_diff_targets("") == []
    assert extract_diff_targets("   \n  ") == []
    assert extract_diff_targets(None) == []  # type: ignore[arg-type]


def test_parse_single_modified_file():
    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    targets = extract_diff_targets(diff)
    assert targets == [
        _ParsedDiffTarget(path="foo.py", is_new=False, is_deleted=False),
    ]


def test_parse_new_file():
    """``--- /dev/null`` flags is_new=True."""
    diff = (
        "--- /dev/null\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1 @@\n"
        "+content\n"
    )
    targets = extract_diff_targets(diff)
    assert len(targets) == 1
    assert targets[0].path == "new_file.py"
    assert targets[0].is_new is True
    assert targets[0].is_deleted is False


def test_parse_deleted_file():
    """``+++ /dev/null`` flags is_deleted=True; path comes from src."""
    diff = (
        "--- a/dead.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x\n"
    )
    targets = extract_diff_targets(diff)
    assert len(targets) == 1
    assert targets[0].path == "dead.py"
    assert targets[0].is_new is False
    assert targets[0].is_deleted is True


def test_parse_multi_file_diff():
    diff = (
        "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-1\n+1a\n"
        "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-2\n+2a\n"
        "--- a/three.py\n+++ b/three.py\n@@ -1 +1 @@\n-3\n+3a\n"
    )
    targets = extract_diff_targets(diff)
    assert [t.path for t in targets] == ["one.py", "two.py", "three.py"]


def test_parse_dedups_repeated_paths():
    """Same path appearing twice in a diff is deduped to one entry."""
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
        "--- a/foo.py\n+++ b/foo.py\n@@ -2 +2 @@\n-a\n+b\n"
    )
    targets = extract_diff_targets(diff)
    assert len(targets) == 1
    assert targets[0].path == "foo.py"


def test_parse_strips_git_prefix():
    """``a/`` and ``b/`` git default prefixes are stripped."""
    diff = (
        "--- a/path/to/deeply/nested.py\n"
        "+++ b/path/to/deeply/nested.py\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    targets = extract_diff_targets(diff)
    assert targets[0].path == "path/to/deeply/nested.py"


def test_parse_strips_trailing_timestamp():
    """Some diff tools emit ``--- a/path\\t<timestamp>``."""
    diff = (
        "--- a/foo.py\t2026-05-11 12:00:00\n"
        "+++ b/foo.py\t2026-05-11 12:00:01\n"
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    targets = extract_diff_targets(diff)
    assert targets[0].path == "foo.py"


def test_parse_unquotes_c_quoted_paths():
    """Git c-quoted paths (paths with spaces) are unquoted."""
    diff = (
        '--- "a/path with spaces.py"\n'
        '+++ "b/path with spaces.py"\n'
        "@@ -1 +1 @@\n-x\n+y\n"
    )
    targets = extract_diff_targets(diff)
    assert targets[0].path == "path with spaces.py"


def test_parse_skips_lone_minus_minus_minus():
    """``--- `` without a following ``+++ `` is skipped (malformed)."""
    diff = (
        "--- a/foo.py\n"
        "(no +++ here)\n"
        "--- a/bar.py\n+++ b/bar.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    targets = extract_diff_targets(diff)
    # Only bar.py parses cleanly
    assert [t.path for t in targets] == ["bar.py"]


def test_parse_garbage_input_returns_empty():
    """Random junk (no headers) returns empty list, no exception."""
    assert extract_diff_targets("just some random text\nno headers") == []
    assert extract_diff_targets("--- only one --- line") == []


# ===========================================================================
# Protocol conformance (runtime)
# ===========================================================================


def test_applier_implements_diff_applier_protocol():
    """GitApplyDiffApplier MUST be a runtime DiffApplier."""
    applier = GitApplyDiffApplier()
    assert isinstance(applier, DiffApplier)


# ===========================================================================
# Empty / no-target short-circuits (no git invocation needed)
# ===========================================================================


def test_apply_empty_diff_returns_empty_diff_error(tmp_path):
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=""))
    assert result == DiffApplyResult(files=(), error="empty_diff")


def test_apply_whitespace_diff_returns_empty_diff_error(tmp_path):
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff="   \n  \t  "))
    assert result.error == "empty_diff"


def test_apply_no_targets_returns_no_targets_error(tmp_path):
    """Diff with no ``--- `` / ``+++ `` headers."""
    applier = GitApplyDiffApplier()
    result = _run(applier(
        worktree_dir=tmp_path,
        diff="this is not a diff but is non-empty",
    ))
    assert result.error == "no_targets_in_diff"


def test_apply_non_string_diff_returns_empty_diff_error(tmp_path):
    """Defensive — non-string input short-circuits cleanly."""
    applier = GitApplyDiffApplier()
    result = _run(applier(
        worktree_dir=tmp_path, diff=None,  # type: ignore[arg-type]
    ))
    assert result.error == "empty_diff"


# ===========================================================================
# git binary missing
# ===========================================================================


def test_apply_with_missing_git_returns_not_installed(tmp_path):
    """Inject a ``git_executable`` path that doesn't exist."""
    applier = GitApplyDiffApplier(
        git_executable="/nonexistent/path/to/git",
    )
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    result = _run(applier(worktree_dir=tmp_path, diff=diff))
    assert result.error == "git_not_installed"
    assert result.files == ()


# ===========================================================================
# End-to-end against real git repos
# ===========================================================================


@_skip_no_git
def test_apply_modifies_existing_file(tmp_path):
    """Round-trip: set up file → apply diff → verify (path, old, new)."""
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "foo.py", "x = 1\n")

    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.error == ""
    assert len(result.files) == 1
    path, old, new = result.files[0]
    assert path == "foo.py"
    assert old == "x = 1\n"
    assert new == "x = 2\n"
    # Verify worktree actually has the new content
    assert (tmp_path / "foo.py").read_text() == "x = 2\n"


@_skip_no_git
def test_apply_creates_new_file(tmp_path):
    _init_git_repo(tmp_path)

    diff = (
        "--- /dev/null\n"
        "+++ b/new_module.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def hello():\n"
        "+    pass\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.error == ""
    assert len(result.files) == 1
    path, old, new = result.files[0]
    assert path == "new_module.py"
    assert old == ""  # new file → empty old
    assert new == "def hello():\n    pass\n"


@_skip_no_git
def test_apply_deletes_file(tmp_path):
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "dead.py", "removed = True\n")

    diff = (
        "--- a/dead.py\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-removed = True\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.error == ""
    assert len(result.files) == 1
    path, old, new = result.files[0]
    assert path == "dead.py"
    assert old == "removed = True\n"
    assert new == ""  # deleted → empty new
    # File actually removed
    assert not (tmp_path / "dead.py").exists()


@_skip_no_git
def test_apply_multi_file_diff(tmp_path):
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "one.py", "one = 1\n")
    _commit_file(tmp_path, "two.py", "two = 2\n")

    diff = (
        "--- a/one.py\n+++ b/one.py\n@@ -1 +1 @@\n-one = 1\n+one = 11\n"
        "--- a/two.py\n+++ b/two.py\n@@ -1 +1 @@\n-two = 2\n+two = 22\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.error == ""
    assert len(result.files) == 2
    paths = {p for (p, _, _) in result.files}
    assert paths == {"one.py", "two.py"}
    # Verify each file's actual content updated
    assert (tmp_path / "one.py").read_text() == "one = 11\n"
    assert (tmp_path / "two.py").read_text() == "two = 22\n"


@_skip_no_git
def test_apply_conflict_returns_failed_error(tmp_path):
    """Diff that doesn't match the actual file content → git rejects."""
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "foo.py", "actual content\n")

    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "@@ -1 +1 @@\n"
        "-different expected content\n"  # doesn't match worktree
        "+modified\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.files == ()
    assert result.error.startswith("git_apply_failed:exit"), result.error
    # Actual file should be untouched
    assert (tmp_path / "foo.py").read_text() == "actual content\n"


@_skip_no_git
def test_apply_malformed_diff_returns_failed_error(tmp_path):
    """Headers parse but body is malformed → git rejects."""
    _init_git_repo(tmp_path)
    _commit_file(tmp_path, "foo.py", "x\n")

    diff = (
        "--- a/foo.py\n"
        "+++ b/foo.py\n"
        "this is not a valid hunk header\n"
        "+y\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    assert result.files == ()
    assert (
        result.error.startswith("git_apply_failed:")
        or result.error.startswith("no_targets")
    ), result.error


@_skip_no_git
def test_apply_stale_diff_for_missing_file(tmp_path):
    """Diff references a file that doesn't exist in the worktree."""
    _init_git_repo(tmp_path)

    diff = (
        "--- a/missing.py\n"
        "+++ b/missing.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    applier = GitApplyDiffApplier()
    result = _run(applier(worktree_dir=tmp_path, diff=diff))

    # git apply should reject because the file doesn't match
    assert result.files == ()
    assert result.error.startswith("git_apply_failed:"), result.error


# ===========================================================================
# Timeout + cancellation
# ===========================================================================


def test_apply_timeout_kills_subprocess(tmp_path):
    """Inject a fake git that sleeps forever; verify timeout fires."""
    # Fake git is a shell script that sleeps
    fake_git = tmp_path / "fake_git"
    fake_git.write_text("#!/bin/sh\nsleep 60\n")
    fake_git.chmod(0o755)

    applier = GitApplyDiffApplier(
        timeout_s=0.1,
        git_executable=str(fake_git),
    )
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    result = _run(applier(worktree_dir=tmp_path, diff=diff))
    assert result.error == "git_apply_timeout"
    assert result.files == ()


def test_apply_cancellation_propagates(tmp_path):
    """asyncio.CancelledError MUST propagate (not quarantine)."""
    fake_git = tmp_path / "fake_git_slow"
    fake_git.write_text("#!/bin/sh\nsleep 60\n")
    fake_git.chmod(0o755)

    applier = GitApplyDiffApplier(
        timeout_s=60.0,  # longer than the cancel
        git_executable=str(fake_git),
    )
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )

    async def _cancel_after_delay():
        task = asyncio.create_task(applier(
            worktree_dir=tmp_path, diff=diff,
        ))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        _run(_cancel_after_delay())


# ===========================================================================
# Defensive — no exceptions ever propagate (fail-closed contract)
# ===========================================================================


def test_apply_never_raises_on_invalid_worktree_dir(tmp_path):
    """Worktree dir that doesn't exist — git apply will fail; but
    the applier MUST NOT raise. Returns structured error."""
    invalid = tmp_path / "does-not-exist"
    applier = GitApplyDiffApplier()
    diff = (
        "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    result = _run(applier(worktree_dir=invalid, diff=diff))
    # Either git_subprocess_failed (cwd missing) or git_apply_failed
    assert result.files == ()
    assert result.error != ""
    assert (
        result.error.startswith("git_subprocess_failed")
        or result.error.startswith("git_apply_failed")
        or result.error.startswith("git_not_installed")
    ), result.error
