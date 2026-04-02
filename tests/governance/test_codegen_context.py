"""Tests for Ouroboros codegen context injection (Sub-project B)."""
import os
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Task 1: Env-driven truncation + overlap guard
# ---------------------------------------------------------------------------

def test_default_max_target_file_chars():
    """Default should be 65536, not the old hardcoded 12000."""
    from backend.core.ouroboros.governance.providers import _MAX_TARGET_FILE_CHARS
    assert _MAX_TARGET_FILE_CHARS == 65536


def test_no_truncation_for_small_files(tmp_path):
    """Files under max_chars should be returned verbatim."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "small.py"
    content = "x = 1\n" * 100  # ~600 chars
    f.write_text(content)
    result = _read_with_truncation(f)
    assert result == content
    assert "[TRUNCATED" not in result


def test_truncation_for_large_files(tmp_path):
    """Files over max_chars should be truncated with marker."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "large.py"
    content = "x = 1\n" * 20000  # ~120KB
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=10000)
    assert "[TRUNCATED" in result
    assert len(result) < len(content)


def test_truncation_overlap_guard(tmp_path):
    """When max_chars is small, head+tail must not overlap or exceed content."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    f = tmp_path / "medium.py"
    # 25KB file, but max_chars=20000 with default HEAD=52000, TAIL=8000
    content = "".join(f"line {i}\n" for i in range(3000))  # ~25KB
    f.write_text(content)
    result = _read_with_truncation(f, max_chars=20000)
    assert "[TRUNCATED" in result
    # The result should not be longer than the original (no overlap)
    assert len(result) <= len(content)
    # Head and tail should not overlap (no duplicated lines)
    parts = result.split("[TRUNCATED")
    head_part = parts[0]
    tail_part = parts[1].split("]", 1)[1] if len(parts) > 1 else ""
    # Last line of head should not appear in tail
    head_lines = head_part.strip().split("\n")
    tail_lines = tail_part.strip().split("\n")
    if head_lines and tail_lines:
        assert head_lines[-1] != tail_lines[0], "Head and tail overlap!"


def test_truncation_osError_returns_empty(tmp_path):
    """Non-existent file should return empty string, not raise."""
    from backend.core.ouroboros.governance.providers import _read_with_truncation
    result = _read_with_truncation(tmp_path / "nonexistent.py")
    assert result == ""


# ---------------------------------------------------------------------------
# Task 2: Anti-duplication system prompt
# ---------------------------------------------------------------------------

def test_system_prompt_contains_anti_duplication():
    """System prompt must contain anti-duplication instructions."""
    from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT
    assert "ANTI-DUPLICATION" in _CODEGEN_SYSTEM_PROMPT
    assert "do not generate" in _CODEGEN_SYSTEM_PROMPT.lower()
    assert "2b.1-noop" in _CODEGEN_SYSTEM_PROMPT


def test_system_prompt_contains_minimal_edit_guidance():
    """Anti-duplication must include minimal-edit language to avoid over-refusal."""
    from backend.core.ouroboros.governance.providers import _CODEGEN_SYSTEM_PROMPT
    assert "minimal edit" in _CODEGEN_SYSTEM_PROMPT.lower() or "preserve existing" in _CODEGEN_SYSTEM_PROMPT.lower()


# ---------------------------------------------------------------------------
# Task 3: AST function index
# ---------------------------------------------------------------------------

def test_function_index_basic_python():
    """Extracts top-level and class method definitions."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    source = textwrap.dedent('''\
        """Module docstring."""
        import os

        def top_level_func(x: int, y: str) -> bool:
            """Check something."""
            return True

        async def async_func():
            """Async helper."""
            pass

        class MyClass:
            """A class."""

            def method_one(self, data: list) -> None:
                """Process data."""
                pass

            @staticmethod
            def static_helper(n: int) -> int:
                """Helper."""
                return n * 2

            @property
            def name(self) -> str:
                """Get name."""
                return "foo"
    ''')
    result = _build_function_index(source, "example.py")
    assert "top_level_func" in result
    assert "async_func" in result
    assert "MyClass" in result
    assert "method_one" in result
    assert "static_helper" in result
    assert "name" in result  # @property
    assert "DO NOT duplicate" in result


def test_function_index_syntax_error():
    """Unparseable Python returns empty string."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    result = _build_function_index("def broken(:\n  pass", "bad.py")
    assert result == ""


def test_function_index_non_python():
    """Non-.py files return empty string."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    result = _build_function_index("const x = 1;", "file.js")
    assert result == ""


def test_function_index_caps():
    """Index respects 50-entry cap."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    # Generate 60 functions
    lines = []
    for i in range(60):
        lines.append(f"def func_{i}():\n    pass\n")
    source = "\n".join(lines)
    result = _build_function_index(source, "many.py")
    # Should have at most 50 function entries
    func_count = result.count("def func_")
    assert func_count <= 50


def test_function_index_long_signature_truncated():
    """Very long signatures get truncated."""
    from backend.core.ouroboros.governance.providers import _build_function_index
    long_params = ", ".join(f"param_{i}: str" for i in range(20))
    source = f"def long_func({long_params}) -> None:\n    pass\n"
    result = _build_function_index(source, "long.py")
    assert "long_func" in result
    # The signature line should not be excessively long
    for line in result.split("\n"):
        if "long_func" in line:
            assert len(line) <= 120, f"Signature line too long: {len(line)} chars"


# ---------------------------------------------------------------------------
# Task 4: Recent file history
# ---------------------------------------------------------------------------

def test_recent_file_history_with_git(tmp_path):
    """Returns last commits touching the file when .git exists."""
    import subprocess
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    # Set up a real git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_path, capture_output=True)
    f.write_text("x = 2\n")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "update value"], cwd=tmp_path, capture_output=True)

    result = _build_recent_file_history(f, tmp_path)
    assert "Recent Changes" in result
    assert "update value" in result
    assert "initial commit" in result


def test_recent_file_history_no_git(tmp_path):
    """Returns empty string when .git directory missing."""
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    f = tmp_path / "example.py"
    f.write_text("x = 1\n")
    result = _build_recent_file_history(f, tmp_path)
    assert result == ""


def test_recent_file_history_path_outside_repo(tmp_path):
    """Returns empty string when path is not under repo_root."""
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    (tmp_path / ".git").mkdir()  # fake .git dir
    outside = Path("/tmp/not_in_repo.py")
    result = _build_recent_file_history(outside, tmp_path)
    assert result == ""


def test_recent_file_history_capped_length(tmp_path):
    """Output must not exceed 500 chars."""
    import subprocess
    from backend.core.ouroboros.governance.providers import _build_recent_file_history
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    f = tmp_path / "example.py"
    for i in range(10):
        f.write_text(f"x = {i}\n")
        subprocess.run(["git", "add", "example.py"], cwd=tmp_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", f"commit {i} with a {'long ' * 20}message"], cwd=tmp_path, capture_output=True)

    result = _build_recent_file_history(f, tmp_path)
    # Header + content should be bounded
    assert len(result) <= 600  # small buffer for header


# ---------------------------------------------------------------------------
# Task 5: Prompt assembly integration
# ---------------------------------------------------------------------------

def test_prompt_assembly_includes_index_and_history(tmp_path):
    """Full codegen prompt should contain structural index and recent history."""
    import subprocess
    from backend.core.ouroboros.governance.providers import (
        _build_codegen_prompt,
        _CODEGEN_SYSTEM_PROMPT,
    )

    # Set up a git repo with a Python file
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    target = tmp_path / "module.py"
    target.write_text(textwrap.dedent('''\
        """Test module."""

        def existing_function(x: int) -> bool:
            """Already implemented."""
            return x > 0

        class Handler:
            def process(self, data: list) -> None:
                """Process data."""
                pass
    '''))
    subprocess.run(["git", "add", "module.py"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add module"], cwd=tmp_path, capture_output=True)

    # Build a minimal OperationContext mock
    from unittest.mock import MagicMock
    ctx = MagicMock()
    ctx.op_id = "test-op-001"
    ctx.description = "Add a new feature"
    ctx.target_files = ["module.py"]
    ctx.human_instructions = ""
    ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo = False
    ctx.repo_scope = set()
    ctx.telemetry = None

    prompt = _build_codegen_prompt(
        ctx=ctx,
        repo_root=tmp_path,
        repo_roots=None,
    )

    # Structural index should be present
    assert "Structural Index" in prompt
    assert "existing_function" in prompt
    assert "Handler" in prompt
    assert "process" in prompt

    # Recent history should be present
    assert "Recent Changes" in prompt
    assert "add module" in prompt

    # Source snapshot should contain the full file (no truncation for this small file)
    assert "def existing_function" in prompt
    assert "[TRUNCATED" not in prompt

    # Anti-duplication should be in system prompt (separate constant)
    assert "ANTI-DUPLICATION" in _CODEGEN_SYSTEM_PROMPT


def test_prompt_no_truncation_32kb_file(tmp_path):
    """A 32KB file should NOT be truncated with default 64KB budget."""
    from backend.core.ouroboros.governance.providers import _build_codegen_prompt
    from unittest.mock import MagicMock

    target = tmp_path / "big.py"
    # Generate a ~32KB Python file
    lines = [f"def func_{i}():\n    return {i}\n" for i in range(800)]
    target.write_text("\n".join(lines))

    ctx = MagicMock()
    ctx.op_id = "test-op-002"
    ctx.description = "Modify big file"
    ctx.target_files = ["big.py"]
    ctx.human_instructions = ""
    ctx.strategic_memory_prompt = ""
    ctx.expanded_context_files = ()
    ctx.cross_repo = False
    ctx.repo_scope = set()
    ctx.telemetry = None

    prompt = _build_codegen_prompt(
        ctx=ctx,
        repo_root=tmp_path,
        repo_roots=None,
    )
    assert "[TRUNCATED" not in prompt, "32KB file should not be truncated with 64KB budget"
