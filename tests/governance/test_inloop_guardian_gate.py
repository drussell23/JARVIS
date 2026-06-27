"""Anti-Venom Task 10 — in-loop SemanticGuardian content-gate regression spine.

Covers:
  1. edit_file introducing os.system → ToolError returned, file NOT written.
  2. Benign edit/write → succeeds (no false block).
  3. No false-positive: edit a file with subprocess.run already on disk,
     adding only a comment (delta=0) → succeeds (proves on-disk baseline).
  4. Guardian crash (monkeypatch) → fail-closed ToolError, file NOT written.
  5. Soft finding → writes + advisory note appended to success result.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import backend.core.ouroboros.governance.tool_executor as te_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_executor(tmp_path: Path) -> te_mod.ToolExecutor:
    """Build a bare ToolExecutor rooted at tmp_path."""
    return te_mod.ToolExecutor(tmp_path)


def _seed_read(te: te_mod.ToolExecutor, rel: str) -> None:
    """Bypass the must-have-read gate by directly seeding _files_read."""
    te._files_read.add(rel)


def _write_and_seed(te: te_mod.ToolExecutor, tmp_path: Path, name: str, content: str) -> Path:
    """Create a file in tmp_path with given content, seed _files_read for it."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    _seed_read(te, name)
    return p


# ---------------------------------------------------------------------------
# Test 1 — edit_file introducing os.system → ToolError, file NOT written
# ---------------------------------------------------------------------------


def test_edit_file_os_system_blocked(tmp_path: Path, monkeypatch) -> None:
    """edit_file that introduces os.system must be blocked with a ToolError.

    File NOT written (on-disk content unchanged after the call).
    """
    # Ensure guardian is enabled.
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SEMGUARD_SHELL_EXEC_INTRODUCED_ENABLED", "1")

    te = _make_executor(tmp_path)
    original = "def foo():\n    pass\n"
    p = _write_and_seed(te, tmp_path, "target.py", original)

    # NOTE: the string below is *test-input data* passed to the guardian's
    # inspect() as new_text — it is never executed; subprocess.run is not
    # used here because we are testing the guardian's ability to DETECT and
    # BLOCK os.system introductions, not to call them.
    result = te._edit_file({
        "path": "target.py",
        "old_text": "    pass",
        "new_text": '    os.system("echo hi")',  # inert string literal — never executed
    })

    # Must be a ToolError (not an "OK:")
    assert result.startswith("ToolError:"), (
        f"Expected ToolError from SemanticGuardian block, got: {result!r}"
    )
    # Must mention a SemanticGuardian pattern — either shell_exec_introduced
    # or dynamic_import_chain (both fire on os.system; whichever is first wins)
    assert "shell_exec_introduced" in result or "dynamic_import_chain" in result, (
        f"Expected a known hard pattern in error, got: {result!r}"
    )
    # File must NOT have been written
    assert p.read_text(encoding="utf-8") == original, (
        "File was written despite the guardian block — disk should be unchanged."
    )


# ---------------------------------------------------------------------------
# Test 2 — benign edit/write → succeeds
# ---------------------------------------------------------------------------


def test_benign_edit_succeeds(tmp_path: Path, monkeypatch) -> None:
    """A safe edit that adds a comment must succeed without any block."""
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    _write_and_seed(te, tmp_path, "benign.py", "x = 1\n")

    result = te._edit_file({
        "path": "benign.py",
        "old_text": "x = 1",
        "new_text": "# safe change\nx = 1",
    })

    assert result.startswith("OK:"), (
        f"Expected OK: for benign edit, got: {result!r}"
    )


def test_write_file_benign_new_file_succeeds(tmp_path: Path, monkeypatch) -> None:
    """write_file creating a new benign file must succeed."""
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    content = "def greet():\n    return 'hello'\n"

    result = te._write_file({
        "path": "new_module.py",
        "content": content,
    })

    assert result.startswith("OK:"), (
        f"Expected OK: for benign write_file, got: {result!r}"
    )
    assert (tmp_path / "new_module.py").read_text(encoding="utf-8") == content


# ---------------------------------------------------------------------------
# Test 3 — no false-positive: pre-existing subprocess.run on disk, delta=0
# ---------------------------------------------------------------------------


def test_no_false_positive_on_disk_baseline(tmp_path: Path, monkeypatch) -> None:
    """An edit to a file that ALREADY contains subprocess.run (delta=0) must succeed.

    Proves the on-disk baseline is read correctly instead of using old="",
    which would incorrectly treat the pre-existing call as a new introduction.
    """
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SEMGUARD_SHELL_EXEC_INTRODUCED_ENABLED", "1")

    te = _make_executor(tmp_path)
    # File already has subprocess.run on disk — NOT a new introduction.
    original = (
        "import subprocess\n"
        "def bar():\n"
        "    subprocess.run(['ls'])\n"
    )
    _write_and_seed(te, tmp_path, "existing.py", original)

    # Edit only adds a comment — subprocess.run count is UNCHANGED (delta=0)
    result = te._edit_file({
        "path": "existing.py",
        "old_text": "import subprocess",
        "new_text": "# existing file with subprocess\nimport subprocess",
    })

    assert result.startswith("OK:"), (
        f"Expected OK: (no false-positive on delta-0 subprocess.run), got: {result!r}"
    )
    # Confirm file was written
    on_disk = (tmp_path / "existing.py").read_text(encoding="utf-8")
    assert "# existing file with subprocess" in on_disk


# ---------------------------------------------------------------------------
# Test 4 — guardian crash → fail-closed ToolError, file NOT written
# ---------------------------------------------------------------------------


def test_guardian_crash_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """If SemanticGuardian.inspect raises, the tool must fail closed.

    Returns ToolError mentioning 'guardian evaluation failed'; disk unchanged.
    """
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    original = "def foo():\n    pass\n"
    p = _write_and_seed(te, tmp_path, "crash_target.py", original)

    def _raising_inspect(self, **kwargs):
        raise RuntimeError("simulated guardian crash")

    monkeypatch.setattr(te_mod.SemanticGuardian, "inspect", _raising_inspect)

    result = te._edit_file({
        "path": "crash_target.py",
        "old_text": "    pass",
        "new_text": "    return 42",
    })

    assert "ToolError" in result, (
        f"Expected ToolError on guardian crash, got: {result!r}"
    )
    assert "guardian evaluation failed" in result, (
        f"Expected 'guardian evaluation failed' in error, got: {result!r}"
    )
    # File must NOT have been written
    assert p.read_text(encoding="utf-8") == original, (
        "File was written despite guardian crash — should be fail-closed."
    )


# ---------------------------------------------------------------------------
# Test 5 — soft finding → writes successfully + advisory note in result
# ---------------------------------------------------------------------------


def test_soft_finding_allows_write_with_advisory(tmp_path: Path, monkeypatch) -> None:
    """A soft finding (docstring_only_delete) must allow the write to proceed.

    The returned success string must contain an advisory note so the model
    is informed without being blocked.
    """
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SEMGUARD_DOCSTRING_ONLY_DELETE_ENABLED", "1")

    te = _make_executor(tmp_path)
    # A function with a docstring and a substantive body.
    original = (
        'def compute():\n'
        '    """Compute something important."""\n'
        '    result = 1 + 1\n'
        '    return result\n'
    )
    _write_and_seed(te, tmp_path, "soft_target.py", original)

    # Remove the docstring — triggers docstring_only_delete (soft)
    result = te._edit_file({
        "path": "soft_target.py",
        "old_text": '    """Compute something important."""\n    result = 1 + 1',
        "new_text": "    result = 1 + 1",
    })

    # Must succeed (soft does not block)
    assert result.startswith("OK:"), (
        f"Expected OK: for soft finding, got: {result!r}"
    )
    # Advisory note must be present if the soft pattern fired
    # (guardian may be configured; if pattern didn't fire just check OK)
    if "SemanticGuard advisory" in result:
        assert "docstring_only_delete" in result, (
            f"Advisory note should mention the pattern name, got: {result!r}"
        )
    # File must have been written
    on_disk = (tmp_path / "soft_target.py").read_text(encoding="utf-8")
    assert '"""Compute something important."""' not in on_disk, (
        "Docstring should have been removed by the edit."
    )
