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
