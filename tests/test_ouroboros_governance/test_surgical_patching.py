"""
Tests for Task 4: Surgical Patching — unified-diff output for single-file tasks.

Covers:
1. _apply_unified_diff() applies a simple hunk correctly.
2. _apply_unified_diff() applies multi-hunk diffs.
3. _apply_unified_diff() raises ValueError on context mismatch.
4. _apply_unified_diff() handles diff with no trailing newline.
5. _build_codegen_prompt() emits 2b.1-diff schema for single-file tasks.
6. _build_codegen_prompt() emits 2b.1 schema for multi-file tasks.
7. _build_codegen_prompt() emits 2c.1 schema for cross-repo tasks.
8. _parse_generation_response() reconstructs full_content from unified diff.
9. _parse_generation_response() skips candidates where diff apply fails.
10. _parse_generation_response() raises when ALL diff candidates fail.
"""
from __future__ import annotations

import json
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backend.core.ouroboros.governance.providers import (
    _apply_unified_diff,
    _build_codegen_prompt,
    _parse_generation_response,
    _SCHEMA_VERSION,
    _SCHEMA_VERSION_DIFF,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    target_files=("src/foo.py",),
    description="fix bug",
    op_id="op-test-001",
    cross_repo=False,
    repo_scope=(),
    expanded_context_files=(),
):
    ctx = MagicMock()
    ctx.target_files = target_files
    ctx.description = description
    ctx.op_id = op_id
    ctx.cross_repo = cross_repo
    ctx.repo_scope = repo_scope
    ctx.expanded_context_files = expanded_context_files
    ctx.telemetry = None
    ctx.system_context = None
    return ctx


ORIGINAL = textwrap.dedent("""\
    def greet(name):
        return "Hello, " + name


    def farewell(name):
        return "Goodbye, " + name
""")


# ---------------------------------------------------------------------------
# Test 1 — _apply_unified_diff: simple hunk
# ---------------------------------------------------------------------------


def test_apply_unified_diff_simple():
    diff = textwrap.dedent("""\
        @@ -1,2 +1,2 @@
         def greet(name):
        -    return "Hello, " + name
        +    return f"Hello, {name}!"
    """)
    result = _apply_unified_diff(ORIGINAL, diff)
    assert 'return f"Hello, {name}!"' in result
    assert 'return "Hello, " + name' not in result


# ---------------------------------------------------------------------------
# Test 2 — _apply_unified_diff: multiple hunks
# ---------------------------------------------------------------------------


def test_apply_unified_diff_multi_hunk():
    diff = textwrap.dedent("""\
        @@ -1,2 +1,2 @@
         def greet(name):
        -    return "Hello, " + name
        +    return f"Hello, {name}!"
        @@ -5,2 +5,2 @@
         def farewell(name):
        -    return "Goodbye, " + name
        +    return f"Goodbye, {name}!"
    """)
    result = _apply_unified_diff(ORIGINAL, diff)
    assert 'return f"Hello, {name}!"' in result
    assert 'return f"Goodbye, {name}!"' in result


# ---------------------------------------------------------------------------
# Test 3 — _apply_unified_diff: context mismatch raises ValueError
# ---------------------------------------------------------------------------


def test_apply_unified_diff_context_mismatch():
    diff = textwrap.dedent("""\
        @@ -1,2 +1,2 @@
         def nonexistent_function(x):
        -    pass
        +    return x
    """)
    with pytest.raises(ValueError, match="does not match source"):
        _apply_unified_diff(ORIGINAL, diff)


# ---------------------------------------------------------------------------
# Test 4 — _apply_unified_diff: file without trailing newline
# ---------------------------------------------------------------------------


def test_apply_unified_diff_no_trailing_newline():
    original = "x = 1\ny = 2"  # no trailing \n
    diff = "@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n+y = 3\n"
    result = _apply_unified_diff(original, diff)
    assert "y = 3" in result
    assert "y = 2" not in result


# ---------------------------------------------------------------------------
# Test 5 — _build_codegen_prompt: single-file → 2b.1-diff schema
# ---------------------------------------------------------------------------


def test_build_codegen_prompt_single_file_emits_diff_schema(tmp_path):
    src = tmp_path / "src" / "foo.py"
    src.parent.mkdir(parents=True)
    src.write_text("def foo(): pass\n")

    ctx = _make_ctx(target_files=("src/foo.py",), cross_repo=False)
    with patch(
        "backend.core.ouroboros.governance.providers._find_context_files",
        return_value=([], []),
    ):
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)

    assert _SCHEMA_VERSION_DIFF in prompt
    assert "unified_diff" in prompt
    assert "full_content" not in prompt


# ---------------------------------------------------------------------------
# Test 6 — _build_codegen_prompt: multi-file → 2b.1 schema (full_content)
# ---------------------------------------------------------------------------


def test_build_codegen_prompt_multi_file_emits_full_content_schema(tmp_path):
    for name in ("a.py", "b.py"):
        f = tmp_path / name
        f.write_text("pass\n")

    ctx = _make_ctx(target_files=("a.py", "b.py"), cross_repo=False)
    with patch(
        "backend.core.ouroboros.governance.providers._find_context_files",
        return_value=([], []),
    ):
        prompt = _build_codegen_prompt(ctx, repo_root=tmp_path)

    assert _SCHEMA_VERSION in prompt
    assert "full_content" in prompt
    assert _SCHEMA_VERSION_DIFF not in prompt


# ---------------------------------------------------------------------------
# Test 7 — _build_codegen_prompt: cross-repo → 2c.1 schema
# ---------------------------------------------------------------------------


def test_build_codegen_prompt_cross_repo_emits_2c1_schema(tmp_path):
    jarvis_root = tmp_path / "jarvis"
    jarvis_root.mkdir()
    prime_root = tmp_path / "prime"
    prime_root.mkdir()
    (jarvis_root / "a.py").write_text("pass\n")
    (prime_root / "b.py").write_text("pass\n")

    ctx = _make_ctx(
        target_files=(str(jarvis_root / "a.py"), str(prime_root / "b.py")),
        cross_repo=True,
        repo_scope=("jarvis", "prime"),
    )
    repo_roots = {"jarvis": jarvis_root, "prime": prime_root}
    with patch(
        "backend.core.ouroboros.governance.providers._find_context_files",
        return_value=([], []),
    ):
        prompt = _build_codegen_prompt(ctx, repo_root=jarvis_root, repo_roots=repo_roots)

    assert "2c.1" in prompt


# ---------------------------------------------------------------------------
# Test 8 — _parse_generation_response: reconstructs full_content from diff
# ---------------------------------------------------------------------------


def test_parse_generation_response_applies_diff(tmp_path):
    src = tmp_path / "src" / "foo.py"
    src.parent.mkdir(parents=True)
    src.write_text(ORIGINAL)

    diff = (
        "@@ -1,2 +1,2 @@\n"
        " def greet(name):\n"
        '-    return "Hello, " + name\n'
        '+    return f"Hello, {name}!"\n'
    )
    raw = json.dumps({
        "schema_version": _SCHEMA_VERSION_DIFF,
        "candidates": [{
            "candidate_id": "c1",
            "file_path": "src/foo.py",
            "unified_diff": diff,
            "rationale": "Use f-string",
        }],
        "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
    })

    ctx = _make_ctx(target_files=("src/foo.py",))
    result = _parse_generation_response(
        raw,
        provider_name="test",
        duration_s=0.1,
        ctx=ctx,
        source_hash="abc123",
        source_path=str(src),
    )

    assert len(result.candidates) == 1
    assert 'return f"Hello, {name}!"' in result.candidates[0]["full_content"]


# ---------------------------------------------------------------------------
# Test 9 — _parse_generation_response: skips bad diff, keeps good candidate
# ---------------------------------------------------------------------------


def test_parse_generation_response_skips_bad_diff(tmp_path):
    src = tmp_path / "foo.py"
    src.write_text(ORIGINAL)

    good_diff = (
        "@@ -1,2 +1,2 @@\n"
        " def greet(name):\n"
        '-    return "Hello, " + name\n'
        '+    return f"Hello, {name}!"\n'
    )
    bad_diff = "@@ -99,1 +99,1 @@\n-nonexistent\n+replacement\n"

    raw = json.dumps({
        "schema_version": _SCHEMA_VERSION_DIFF,
        "candidates": [
            {
                "candidate_id": "c1",
                "file_path": "foo.py",
                "unified_diff": bad_diff,
                "rationale": "bad hunk",
            },
            {
                "candidate_id": "c2",
                "file_path": "foo.py",
                "unified_diff": good_diff,
                "rationale": "good one",
            },
        ],
        "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
    })

    ctx = _make_ctx(target_files=("foo.py",))
    result = _parse_generation_response(
        raw,
        provider_name="test",
        duration_s=0.1,
        ctx=ctx,
        source_hash="abc123",
        source_path=str(src),
    )

    # Only the good candidate survives
    assert len(result.candidates) == 1
    assert 'return f"Hello, {name}!"' in result.candidates[0]["full_content"]


# ---------------------------------------------------------------------------
# Test 10 — _parse_generation_response: all diffs fail → RuntimeError
# ---------------------------------------------------------------------------


def test_parse_generation_response_all_diffs_fail_raises(tmp_path):
    src = tmp_path / "foo.py"
    src.write_text(ORIGINAL)

    bad_diff = "@@ -99,1 +99,1 @@\n-nonexistent\n+replacement\n"
    raw = json.dumps({
        "schema_version": _SCHEMA_VERSION_DIFF,
        "candidates": [{
            "candidate_id": "c1",
            "file_path": "foo.py",
            "unified_diff": bad_diff,
            "rationale": "all bad",
        }],
        "provider_metadata": {"model_id": "test-model", "reasoning_summary": "ok"},
    })

    ctx = _make_ctx(target_files=("foo.py",))
    with pytest.raises(RuntimeError, match="diff_apply_failed_all_candidates"):
        _parse_generation_response(
            raw,
            provider_name="test",
            duration_s=0.1,
            ctx=ctx,
            source_hash="abc123",
            source_path=str(src),
        )
