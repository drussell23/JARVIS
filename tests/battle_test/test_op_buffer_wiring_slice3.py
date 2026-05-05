"""Tests for Gap #3 Slice 3 — buffer wiring + /expand verb regression checks.

Most of this is AST-grep regression because the wiring lives inside
SerpentFlow which has heavyweight construction (Console, prompt_toolkit,
etc.). The substrate (OpBlockBuffer) was tested exhaustively in Slice 2;
here we verify the call sites are present.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def _load_serpent_flow() -> str:
    return _SERPENT_FLOW.read_text()


# ===========================================================================
# Master flag helper
# ===========================================================================


def test_op_collapse_enabled_default_off(monkeypatch: pytest.MonkeyPatch):
    """Slice 3 ships master flag default-off; Slice 5 graduates to true."""
    monkeypatch.delenv("JARVIS_OP_COLLAPSE_ENABLED", raising=False)
    src = _load_serpent_flow()
    # Source-level smoke: the helper exists
    assert "_op_collapse_enabled" in src
    assert "JARVIS_OP_COLLAPSE_ENABLED" in src


# ===========================================================================
# Hook-presence regression pins (will be promoted to AST invariants
# in Slice 5)
# ===========================================================================


def test_op_started_invokes_buffer_start():
    src = _load_serpent_flow()
    # The hook must appear inside op_started. Locate the method via
    # AST + verify at least one call to _maybe_buffer_op_start in body.
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) or isinstance(node, ast.FunctionDef):
            if node.name == "op_started":
                body_src = ast.unparse(node)
                assert "_maybe_buffer_op_start" in body_src, (
                    "op_started must call _maybe_buffer_op_start — Gap #3 hook missing"
                )
                return
    pytest.fail("op_started method not found in SerpentFlow")


def test_op_line_invokes_buffer_append():
    src = _load_serpent_flow()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_op_line":
                body_src = ast.unparse(node)
                assert "_maybe_buffer_op_line" in body_src, (
                    "_op_line must call _maybe_buffer_op_line — "
                    "buffered block recovery is broken without it"
                )
                return
    pytest.fail("_op_line method not found in SerpentFlow")


def test_op_completed_invokes_buffer_commit():
    src = _load_serpent_flow()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_completed":
                body_src = ast.unparse(node)
                assert "_maybe_buffer_op_commit" in body_src, (
                    "op_completed must call _maybe_buffer_op_commit — "
                    "blocks would never transition to COMMITTED"
                )
                return
    pytest.fail("op_completed method not found in SerpentFlow")


def test_op_failed_invokes_buffer_commit():
    src = _load_serpent_flow()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_failed":
                body_src = ast.unparse(node)
                assert "_maybe_buffer_op_commit" in body_src, (
                    "op_failed must call _maybe_buffer_op_commit — "
                    "failed-op blocks would never commit"
                )
                return
    pytest.fail("op_failed method not found in SerpentFlow")


# ===========================================================================
# /expand verb dispatch regression
# ===========================================================================


def test_repl_dispatch_routes_expand():
    src = _load_serpent_flow()
    assert 'line.startswith("/expand")' in src
    assert "self._handle_expand(line)" in src


def test_handle_expand_method_defined():
    src = _load_serpent_flow()
    tree = ast.parse(src)
    seen = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "_handle_expand",
        "_print_expand_summary",
        "_expand_tool_body",
        "_expand_diff",
        "_expand_op_block",
        "_expand_op_block_by_op_id",
    }
    missing = required - seen
    assert not missing, f"Missing Slice 3 expand handlers: {sorted(missing)}"


def test_handle_expand_dispatches_all_three_prefixes():
    """The dispatcher must route t-N / d-N / o-N — losing any branch
    silently breaks expansion for that artifact kind."""
    src = _load_serpent_flow()
    tree = ast.parse(src)
    handler_src = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_handle_expand":
                handler_src = ast.unparse(node)
                break
    assert handler_src, "_handle_expand not found"
    # ast.unparse() may use single quotes; check both forms
    for prefix in ("t-", "d-", "o-"):
        assert (
            f"startswith('{prefix}')" in handler_src
            or f'startswith("{prefix}")' in handler_src
        ), f"_handle_expand missing dispatch for {prefix!r} prefix"


# ===========================================================================
# End-to-end via the buffer substrate (substrate-level integration)
# ===========================================================================


def test_buffer_lifecycle_maps_to_serpent_flow_pattern():
    """Simulate the SerpentFlow lifecycle pattern: start_op → append →
    commit. The buffer should produce a COMMITTED block that /expand
    can retrieve."""
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        OpBlockBuffer,
        OpBlockState,
        reset_default_buffer_for_tests,
    )
    reset_default_buffer_for_tests()
    try:
        buf = OpBlockBuffer(capacity=10)
        buf.start_op("op-end-to-end")
        buf.append("op-end-to-end", "🔬 sensed     test goal")
        buf.append("op-end-to-end", "✏️ Update(foo.py)")
        buf.append("op-end-to-end", "✨ evolved")
        committed = buf.commit("op-end-to-end", "⏺ 1 file evolved · ⏱ 1.2s")
        assert committed is not None
        assert committed.state is OpBlockState.COMMITTED
        assert committed.line_count == 3
        # Lookup by ref retrieves the full block
        retrieved = buf.lookup(committed.ref)
        assert retrieved.lines == committed.lines
        assert "evolved" in retrieved.summary_line
    finally:
        reset_default_buffer_for_tests()
