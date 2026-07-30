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
    """Behavioural: each level RESOLVES, rather than merely appearing as a
    literal in the handler's source.

    The previous form asserted `"off" in ast.unparse(handler)` — satisfied
    by a docstring, a comment, or a dead branch. It passed throughout the
    period when `/narrate off` left thirteen Moltbook residents talking.
    """
    from backend.core.ouroboros.ui import narrative_density as nd
    for name in ("off", "preambles", "on", "verbose"):
        assert nd.set_density(name).label == name
        assert nd.current_density().label == name


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


def test_narrate_off_silences_intent_and_preambles(monkeypatch):
    """Behavioural, and checked at the REAL consumer.

    `intent_prompter.is_master_flag_enabled` is the function that decides
    whether the micro-LLM call happens. Asserting a flag name appears in the
    verb's source never established that anything downstream agreed.
    """
    from backend.core.ouroboros.ui import narrative_density as nd
    from backend.core.ouroboros.governance import intent_prompter
    monkeypatch.delenv("JARVIS_NARRATIVE_INTENT_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED", raising=False)
    nd.ensure_discovered()

    nd.set_density("on")
    assert intent_prompter.is_master_flag_enabled() is True
    assert nd.audible("narrative.tool_preamble") is True

    nd.set_density("off")
    assert intent_prompter.is_master_flag_enabled() is False
    assert nd.audible("narrative.tool_preamble") is False


def test_an_explicit_flag_outranks_the_dial(monkeypatch):
    """Operator specificity beats a global dial — and this is why the verb
    had to stop writing those flags. While it did, every read after the
    first `/narrate` saw a flag that looked operator-set, so the dial
    permanently shadowed itself."""
    from backend.core.ouroboros.ui import narrative_density as nd
    from backend.core.ouroboros.governance import intent_prompter
    nd.ensure_discovered()
    nd.set_density("off")
    monkeypatch.setenv("JARVIS_NARRATIVE_INTENT_ENABLED", "true")
    assert intent_prompter.is_master_flag_enabled() is True
    verdict = nd.permits("narrative.intent")
    assert verdict.reason == "explicit:JARVIS_NARRATIVE_INTENT_ENABLED"


def test_narrate_verbose_enables_thinking_surfacing(monkeypatch):
    """`verbose`'s one promise, finally with a consumer.

    The previous form asserted the handler mentioned
    JARVIS_NARRATIVE_THINKING_VERBOSE. That flag had ZERO readers in the
    repository, so the test passed while the feature did not exist.
    """
    from backend.core.ouroboros.ui import narrative_density as nd
    monkeypatch.delenv("JARVIS_NARRATIVE_THINKING_VERBOSE", raising=False)
    nd.ensure_discovered()
    nd.set_density("on")
    assert nd.audible("narrative.thinking") is False
    nd.set_density("verbose")
    assert nd.audible("narrative.thinking") is True


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
