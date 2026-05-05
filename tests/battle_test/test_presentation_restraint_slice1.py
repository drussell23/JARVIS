"""Tests for presentation_restraint (Gap #7 Slice 1)."""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from rich.console import Console

from backend.core.ouroboros.battle_test.presentation_restraint import (
    MASTER_FLAG_ENV_VAR,
    MinimalWelcomePayload,
    PRESENTATION_RESTRAINT_SCHEMA_VERSION,
    clear_captured_layers_for_tests,
    get_captured_layers,
    is_restraint_enabled,
    render_minimal_welcome,
    render_organism,
    render_preflight,
    restore_diagnostic_logs_for_tests,
    set_captured_layers,
    suppress_diagnostic_logs,
)


_REPO = Path("/Users/djrussell23/Documents/repos/JARVIS-AI-Agent")


@pytest.fixture(autouse=True)
def clean(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    clear_captured_layers_for_tests()
    restore_diagnostic_logs_for_tests()
    yield
    clear_captured_layers_for_tests()
    restore_diagnostic_logs_for_tests()


# ===========================================================================
# Schema + master flag
# ===========================================================================


def test_schema_version_pinned():
    assert PRESENTATION_RESTRAINT_SCHEMA_VERSION == "presentation_restraint.v1"


def test_master_flag_default_off():
    """Slice 1 ships default-off. Slice 5 graduates to true."""
    assert is_restraint_enabled() is False


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("", False), ("garbage", False),
])
def test_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_restraint_enabled() is expected


# ===========================================================================
# MinimalWelcomePayload — frozen + projection
# ===========================================================================


def test_minimal_welcome_payload_frozen():
    p = MinimalWelcomePayload(
        title="🐍 OUROBOROS + VENOM",
        subtitle="autonomous coding organism",
        verb_hints=(("/help", "commands"),),
        cwd_str="~/repo",
        branch="main",
        cost_cap_str="$0.50",
        idle_timeout_str="600s",
        mode_str="Governed",
    )
    with pytest.raises(Exception):
        p.title = "tampered"  # type: ignore[misc]


def test_payload_schema_version():
    p = MinimalWelcomePayload(
        title="x", subtitle="y", verb_hints=(),
        cwd_str="", branch="", cost_cap_str="",
        idle_timeout_str="", mode_str="",
    )
    assert p.schema_version == PRESENTATION_RESTRAINT_SCHEMA_VERSION


# ===========================================================================
# render_minimal_welcome — emits panel + context line
# ===========================================================================


def test_render_minimal_welcome_emits_title_emoji_kept():
    """Operator decision: emojis are kept inside the panel — restraint
    is about density, not identity erasure."""
    console = Console(record=True, force_terminal=True, color_system="truecolor")
    ok = render_minimal_welcome(
        console,
        session_id="bt-test",
        branch="main",
        cost_cap=0.50,
        idle_timeout_s=600,
        mode_str="Governed",
        cwd_str="~/repo",
    )
    assert ok is True
    text = console.export_text()
    assert "🐍" in text
    assert "OUROBOROS + VENOM" in text


def test_render_minimal_welcome_includes_verb_hints():
    """Default hints surface /help, /preflight, /organism, /expand."""
    console = Console(record=True, force_terminal=True)
    render_minimal_welcome(console, branch="main", cost_cap=0.50)
    text = console.export_text()
    assert "/help" in text
    assert "/preflight" in text
    assert "/organism" in text


def test_render_minimal_welcome_includes_context_line():
    console = Console(record=True, force_terminal=True)
    render_minimal_welcome(
        console,
        cwd_str="~/repos/JARVIS-AI-Agent",
        branch="main",
        cost_cap=0.50,
        idle_timeout_s=600,
        mode_str="Governed",
    )
    text = console.export_text()
    # Context line below the panel — operators see cwd / branch / budget
    assert "~/repos/JARVIS-AI-Agent" in text
    assert "main" in text
    assert "$0.50" in text


def test_render_minimal_welcome_omits_session_id():
    """Session id is captured for /status retrieval but NOT shown at
    boot — CC restraint."""
    console = Console(record=True, force_terminal=True)
    render_minimal_welcome(
        console, session_id="bt-2026-05-05-123456",
        branch="main", cost_cap=0.50,
    )
    text = console.export_text()
    assert "bt-2026-05-05-123456" not in text


def test_render_minimal_welcome_handles_non_console_input():
    """No `.print` attr → False, never raises."""
    assert render_minimal_welcome(object()) is False  # type: ignore[arg-type]


def test_render_minimal_welcome_custom_verb_hints():
    console = Console(record=True, force_terminal=True)
    render_minimal_welcome(
        console,
        verb_hints=(
            ("/foo", "first"),
            ("/bar", "second"),
        ),
        branch="main",
        cost_cap=0.50,
    )
    text = console.export_text()
    assert "/foo" in text
    assert "/bar" in text
    # Defaults must NOT appear when caller supplies overrides
    assert "/preflight" not in text


# ===========================================================================
# Captured layers — backs /organism
# ===========================================================================


def test_set_and_get_captured_layers_round_trip():
    layers = [
        ("🧭", "Strategic Direction", True, "7 Manifesto principles"),
        ("🧠", "Consciousness", True, "Memory + Prophecy + Health"),
    ]
    set_captured_layers(layers)
    captured = get_captured_layers()
    assert captured is not None
    assert len(captured) == 2
    assert captured[0][1] == "Strategic Direction"


def test_set_captured_layers_with_none_clears():
    set_captured_layers([("a", "b", True, "c")])
    set_captured_layers(None)
    assert get_captured_layers() is None


def test_set_captured_layers_filters_malformed_entries():
    """Bad entries silently dropped — never raises."""
    layers = [
        ("ok", "real_layer", True, "detail"),
        ("partial", "missing_fields"),  # only 2 fields
        ("ok2", "another_real", False, "detail2"),
    ]
    set_captured_layers(layers)
    captured = get_captured_layers()
    assert captured is not None
    assert len(captured) == 2  # malformed entry dropped


def test_set_captured_layers_handles_non_iterable():
    """Garbage input → captured stays None, never raises."""
    set_captured_layers(42)  # type: ignore[arg-type]
    assert get_captured_layers() is None


# ===========================================================================
# render_organism — uses captured layers when none passed
# ===========================================================================


def test_render_organism_uses_captured_when_none_provided():
    set_captured_layers([
        ("🧠", "Consciousness", True, "Memory + Prophecy + Health"),
    ])
    console = Console(record=True, force_terminal=True)
    ok = render_organism(console)
    assert ok is True
    text = console.export_text()
    assert "Consciousness" in text
    assert "Memory + Prophecy + Health" in text


def test_render_organism_explicit_layers_override():
    set_captured_layers([("X", "captured", True, "detail")])
    console = Console(record=True, force_terminal=True)
    render_organism(console, layers=[("Y", "explicit", False, "other")])
    text = console.export_text()
    assert "explicit" in text
    assert "captured" not in text


def test_render_organism_no_layers_prints_hint():
    """Pre-boot REPL state: no layers captured → graceful hint."""
    console = Console(record=True, force_terminal=True)
    ok = render_organism(console)
    assert ok is True
    text = console.export_text()
    assert "No organism state" in text or "harness boot" in text


def test_render_organism_emits_emojis_kept():
    set_captured_layers([
        ("🧭", "Strategic Direction", True, "7 principles"),
    ])
    console = Console(record=True, force_terminal=True)
    render_organism(console)
    text = console.export_text()
    assert "🧭" in text


def test_render_organism_handles_non_console_input():
    assert render_organism(object()) is False  # type: ignore[arg-type]


# ===========================================================================
# render_preflight — env-driven, fresh snapshot per call
# ===========================================================================


def test_render_preflight_emits_checklist_header():
    console = Console(record=True, force_terminal=True)
    ok = render_preflight(console)
    assert ok is True
    text = console.export_text()
    assert "Preflight Checklist" in text


def test_render_preflight_shows_provider_status(monkeypatch):
    monkeypatch.setenv("DOUBLEWORD_API_KEY", "test-key")
    console = Console(record=True, force_terminal=True)
    render_preflight(console)
    text = console.export_text()
    assert "DoubleWord" in text


def test_render_preflight_warns_on_missing_keys(monkeypatch):
    monkeypatch.delenv("DOUBLEWORD_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    console = Console(record=True, force_terminal=True)
    render_preflight(console)
    text = console.export_text()
    assert "ERROR" in text or "No API keys" in text


def test_render_preflight_explicit_checks_override_default():
    """Caller-supplied checks override the default detection list."""
    console = Console(record=True, force_terminal=True)
    render_preflight(console, checks=[
        {"label": "Custom Check", "env_key": "FAKE_VAR", "detail": "custom"},
    ])
    text = console.export_text()
    assert "Custom Check" in text
    assert "DoubleWord" not in text


def test_render_preflight_handles_non_console():
    assert render_preflight(object()) is False  # type: ignore[arg-type]


# ===========================================================================
# suppress_diagnostic_logs — root-leak fix
# ===========================================================================


def test_suppress_diagnostic_logs_disables_propagation():
    diag = logging.getLogger("jarvis.shutdown.diagnostics")
    # Restore baseline — propagation may already have been mutated
    diag.propagate = True
    suppress_diagnostic_logs()
    assert diag.propagate is False


def test_suppress_diagnostic_logs_idempotent():
    suppress_diagnostic_logs()
    suppress_diagnostic_logs()  # second call: no-op
    diag = logging.getLogger("jarvis.shutdown.diagnostics")
    assert diag.propagate is False


def test_restore_diagnostic_logs_for_tests():
    diag = logging.getLogger("jarvis.shutdown.diagnostics")
    diag.propagate = True
    suppress_diagnostic_logs()
    assert diag.propagate is False
    restore_diagnostic_logs_for_tests()
    assert diag.propagate is True


def test_suppress_returns_true_on_success():
    assert suppress_diagnostic_logs() is True


# ===========================================================================
# Boot integration regression — serpent_flow.boot_banner short-circuits
# ===========================================================================


_SERPENT_FLOW = _REPO / "backend/core/ouroboros/battle_test/serpent_flow.py"


def test_boot_banner_imports_presentation_restraint():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "boot_banner":
                body = ast.unparse(node)
                assert "presentation_restraint" in body
                assert "render_minimal_welcome" in body
                assert "set_captured_layers" in body
                return
    pytest.fail("boot_banner method not found")


def test_boot_banner_master_flag_short_circuit():
    """boot_banner must check is_restraint_enabled() and return early
    when the flag is on (legacy path runs only when flag is off)."""
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "boot_banner":
                body = ast.unparse(node)
                assert "is_restraint_enabled" in body
                assert "Legacy multi-section dashboard" in body or "_on = " in body
                return
    pytest.fail("boot_banner method not found")


# ===========================================================================
# REPL dispatch regression — /preflight + /organism wired
# ===========================================================================


def test_repl_dispatches_preflight():
    src = _SERPENT_FLOW.read_text()
    assert '"/preflight"' in src
    assert "self._handle_preflight()" in src


def test_repl_dispatches_organism():
    src = _SERPENT_FLOW.read_text()
    assert '"/organism"' in src
    assert "self._handle_organism()" in src


def test_handle_preflight_method_defined():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    seen = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_handle_preflight" in seen


def test_handle_organism_method_defined():
    src = _SERPENT_FLOW.read_text()
    tree = ast.parse(src)
    seen = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_handle_organism" in seen


# ===========================================================================
# Script-level integration — _print_preflight short-circuits
# ===========================================================================


def test_script_print_preflight_short_circuits_under_restraint():
    """The boot script's _print_preflight must short-circuit when the
    master flag is on — operators only see the verbose checklist when
    they explicitly run /preflight."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_print_preflight":
            body = ast.unparse(node)
            assert "is_restraint_enabled" in body
            assert "presentation_restraint" in body
            return
    pytest.fail("_print_preflight not found in battle-test script")


def test_script_still_enforces_api_key_fail_fast():
    """The hard-fail check (no providers configured) MUST run even
    under restraint — that's not chrome, it's a structural error."""
    src = (_REPO / "scripts/ouroboros_battle_test.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_print_preflight":
            body = ast.unparse(node)
            # The restraint branch must still call sys.exit on missing keys
            assert "sys.exit(1)" in body
            assert "No API keys" in body
            return
    pytest.fail("_print_preflight not found")
