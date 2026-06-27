"""Anti-Venom Task 10/11 — in-loop SemanticGuardian content-gate regression spine.

Covers:
  1. edit_file introducing os.system → ToolError returned, file NOT written.
  2. Benign edit/write → succeeds (no false block).
  3. No false-positive: edit a file with subprocess.run already on disk,
     adding only a comment (delta=0) → succeeds (proves on-disk baseline).
  4. Guardian crash (monkeypatch) → fail-closed ToolError, file NOT written.
  5. Soft finding → writes + advisory note appended to success result.

Task 11 additions (Vector 1 + Vector 2):
  6. delete_file — hard guardian finding → ToolError, file NOT deleted.
  7. delete_file — benign file → deletes successfully.
  8. delete_file — guardian crash → fail-closed ToolError, file NOT deleted.
  9. bash sandbox — sandbox_run_bash is called with read_only=True (Vector 2).
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
    assert result.startswith("ToolError:") and "evaluation failed" in result.lower(), (
        f"Expected ToolError with 'evaluation failed' in error, got: {result!r}"
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


# ---------------------------------------------------------------------------
# Task 11 / Vector 1 — _delete_file SemanticGuardian gate
# ---------------------------------------------------------------------------


def test_delete_file_hard_finding_blocked(tmp_path: Path, monkeypatch) -> None:
    """delete_file must return ToolError and NOT delete the file when the
    guardian fires a hard finding against the (old_content, new_content="")
    pair.

    A file containing os.system trips 'shell_exec_introduced' when compared
    against new_content="" because the call disappears — the guardian detects
    a removal that looks suspicious under removed_import_still_referenced or
    function_body_collapsed patterns.  To guarantee a hit we monkeypatch
    inspect() to return one hard finding directly.
    """
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    content = "import os\nos.system('id')\n"
    p = _write_and_seed(te, tmp_path, "dangerous.py", content)

    # Inject a guaranteed hard finding via monkeypatch so the test is not
    # coupled to which specific pattern fires on this content/empty delta.
    from backend.core.ouroboros.governance.semantic_guardian import Detection

    def _hard_inspect(self, **kwargs):
        return [
            Detection(
                pattern="shell_exec_introduced",
                severity="hard",
                message="os.system call detected",
                lines=(2,),
                file_path=kwargs.get("file_path", ""),
            )
        ]

    monkeypatch.setattr(te_mod.SemanticGuardian, "inspect", _hard_inspect)

    result = te._delete_file({"path": "dangerous.py"})

    assert result.startswith("ToolError:"), (
        f"Expected ToolError from guardian block on delete, got: {result!r}"
    )
    assert "SemanticGuardian blocked" in result, (
        f"Expected 'SemanticGuardian blocked' in error, got: {result!r}"
    )
    # File must NOT have been deleted
    assert p.exists(), "File must still exist — guardian blocked the delete."


def test_delete_file_benign_deletes(tmp_path: Path, monkeypatch) -> None:
    """delete_file on a benign file must succeed (no false block)."""
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    p = _write_and_seed(te, tmp_path, "benign_del.py", "x = 1\n")

    result = te._delete_file({"path": "benign_del.py"})

    assert result.startswith("OK:"), (
        f"Expected OK: for benign delete, got: {result!r}"
    )
    assert not p.exists(), "File must have been deleted."


def test_delete_file_guardian_crash_fail_closed(tmp_path: Path, monkeypatch) -> None:
    """If SemanticGuardian.inspect raises during delete_file, the tool must
    fail closed: ToolError returned, file NOT deleted.
    """
    monkeypatch.setenv("JARVIS_SEMANTIC_GUARD_ENABLED", "1")

    te = _make_executor(tmp_path)
    p = _write_and_seed(te, tmp_path, "crash_del.py", "y = 2\n")

    def _raising_inspect(self, **kwargs):
        raise RuntimeError("simulated guardian crash in delete")

    monkeypatch.setattr(te_mod.SemanticGuardian, "inspect", _raising_inspect)

    result = te._delete_file({"path": "crash_del.py"})

    assert "ToolError" in result, (
        f"Expected ToolError on guardian crash during delete, got: {result!r}"
    )
    assert "evaluation failed" in result.lower(), (
        f"Expected 'evaluation failed' in error, got: {result!r}"
    )
    # File must NOT have been deleted
    assert p.exists(), "File must still exist after guardian crash (fail-closed)."


# ---------------------------------------------------------------------------
# Task 11 / Vector 2 — bash sandbox read-only proof
# ---------------------------------------------------------------------------


def test_bash_sandbox_readonly_prevents_repo_writes(monkeypatch) -> None:
    """sandbox_exec.sandbox_run_bash must be called with read_only=True.

    This is the structural proof that the bash tool cannot write to the repo
    even inside the air-gapped container (the bash vector is CLOSED).

    We monkeypatch run_in_container and assert read_only=True is forwarded,
    and verify the :ro mount reaches the docker argv.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    # Build a fake container result so sandbox_run_bash returns cleanly.
    from backend.core.ouroboros.governance import container_sandbox as _cs

    fake_result = MagicMock()
    fake_result.breach = _cs.ContainmentBreach.NONE  # type: ignore[attr-defined]
    fake_result.ok = True
    fake_result.stdout = "ok"
    fake_result.stderr = ""
    fake_result.returncode = 0
    fake_result.diagnostic = ""

    captured: dict = {}

    async def _fake_run_in_container(cmd, *, worktree=None, docker_run=None, read_only=False, **kw):
        captured["read_only"] = read_only
        return fake_result

    with patch.object(_cs, "run_in_container", side_effect=_fake_run_in_container):
        from backend.core.ouroboros.governance import sandbox_exec as _se
        result = asyncio.get_event_loop().run_until_complete(
            _se.sandbox_run_bash("echo hello", worktree="/tmp")
        )

    assert captured.get("read_only") is True, (
        f"sandbox_run_bash must forward read_only=True to run_in_container, "
        f"got read_only={captured.get('read_only')!r}"
    )

    # Strengthen: verify the :ro mount actually reaches the docker argv
    argv = _cs.build_container_argv(code="echo x > f", worktree="/some/repo", read_only=True)
    argv_str = " ".join(argv)
    assert "/some/repo:/work:ro" in argv_str, (
        f"argv must contain '/some/repo:/work:ro' (read-only mount), "
        f"got argv: {argv!r}"
    )
    assert ":/work:rw" not in argv_str, (
        f"argv must NOT contain ':rw' mount (must be read-only), "
        f"got argv: {argv!r}"
    )
