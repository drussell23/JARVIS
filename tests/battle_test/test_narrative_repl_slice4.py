"""Tests for Gap #6 Slice 4 — REPL /narrate verb + /expand n-N
integration regression checks.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")
_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "JARVIS_NARRATIVE_DENSITY",
        "JARVIS_NARRATIVE_INTENT_ENABLED",
        "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED",
        "JARVIS_NARRATIVE_THINKING_VERBOSE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def _src() -> str:
    return _SERPENT_FLOW.read_text()


# ===========================================================================
# REPL dispatch — /narrate routes
# ===========================================================================


def test_repl_dispatch_routes_narrate():
    src = _src()
    assert 'line.startswith("/narrate")' in src
    assert "self._handle_narrate(line)" in src


def test_handle_narrate_method_defined():
    src = _src()
    tree = ast.parse(src)
    seen = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_handle_narrate" in seen


def test_handle_narrate_supports_four_densities():
    src = _src()
    tree = ast.parse(src)
    handler_src = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_handle_narrate":
                handler_src = ast.unparse(node)
                break
    assert handler_src
    for density in ("off", "preambles", "on", "verbose"):
        assert (
            f"'{density}'" in handler_src
            or f'"{density}"' in handler_src
        ), f"_handle_narrate missing density {density!r}"


# ===========================================================================
# /expand dispatcher extended with n- prefix
# ===========================================================================


def test_expand_dispatcher_routes_n_prefix():
    src = _src()
    tree = ast.parse(src)
    handler_src = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_handle_expand":
                handler_src = ast.unparse(node)
                break
    assert handler_src
    assert (
        "startswith('n-')" in handler_src
        or 'startswith("n-")' in handler_src
    ), "_handle_expand missing n- prefix dispatch"


def test_expand_narrative_frame_method_defined():
    src = _src()
    tree = ast.parse(src)
    seen = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_expand_narrative_frame" in seen


# ===========================================================================
# Tool transparency — preamble synthesizer wired into op_tool_start
# ===========================================================================


def test_op_tool_start_imports_synthesizer():
    """Constraint 2 — Tool Transparency. The synthesizer call MUST
    appear inside op_tool_start so when the model omits a preamble
    a deterministic fallback fires."""
    src = _src()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_tool_start":
                body = ast.unparse(node)
                if "synthesize_preamble" in body:
                    found = True
                    break
    assert found, (
        "op_tool_start must call synthesize_preamble — Tool Transparency "
        "constraint requires every tool call to have a 🗣 line"
    )


def test_op_tool_start_master_flag_gated():
    """Synthesizer fallback gated by JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED."""
    src = _src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_tool_start":
                body = ast.unparse(node)
                assert "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED" in body
                return
    pytest.fail("op_tool_start not found")


# ===========================================================================
# Intent prompt fire-and-forget at op_started
# ===========================================================================


def test_op_started_fires_intent_prompt():
    src = _src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "op_started":
                body = ast.unparse(node)
                assert "_maybe_fire_intent_prompt" in body
                return
    pytest.fail("op_started not found")


def test_intent_prompt_helper_defined():
    src = _src()
    tree = ast.parse(src)
    seen = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_maybe_fire_intent_prompt" in seen


def test_intent_prompt_uses_create_task():
    """Fire-and-forget pattern via asyncio.create_task. NEVER blocks
    op_started — Constraint 3 (No Clutter)."""
    src = _src()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_maybe_fire_intent_prompt":
                body = ast.unparse(node)
                assert "create_task" in body
                return
    pytest.fail("_maybe_fire_intent_prompt not found")


# ===========================================================================
# /narrate density control — env var side-effects
# ===========================================================================


def test_narrate_off_disables_intent_and_preambles():
    src = _src()
    tree = ast.parse(src)
    handler = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_handle_narrate":
                handler = ast.unparse(node)
                break
    assert handler
    # density=off must explicitly turn off both flags
    assert "JARVIS_NARRATIVE_INTENT_ENABLED" in handler
    assert "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED" in handler


def test_narrate_verbose_enables_thinking_surfacing():
    src = _src()
    tree = ast.parse(src)
    handler = ""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "_handle_narrate":
                handler = ast.unparse(node)
                break
    assert handler
    assert "JARVIS_NARRATIVE_THINKING_VERBOSE" in handler


# ===========================================================================
# Visual hierarchy still preserved — Constraint 1 regression check
# ===========================================================================


def test_renderer_still_uses_italic_marker():
    """Visual hierarchy regression: model voice remains italic."""
    renderer_src = (
        _REPO / "backend/core/ouroboros/battle_test/narrative_renderer.py"
    ).read_text()
    assert "italic" in renderer_src


def test_renderer_uses_bright_blue_for_intent():
    """Constraint 1 — gray-blue tint for INTENT/PLAN_PROSE so it's
    structurally distinct from cyan system actions."""
    renderer_src = (
        _REPO / "backend/core/ouroboros/battle_test/narrative_renderer.py"
    ).read_text()
    assert "bright_blue" in renderer_src
    assert "bright_black" in renderer_src
