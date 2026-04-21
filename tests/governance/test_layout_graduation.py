"""Graduation pins — SerpentFlow Opt-in Split Layout arc.

Pins guard bit-rot of the 4 new modules (+ the feedback design
memory). These pins must all pass for the arc to land and remain
additive/opt-in.
"""
from __future__ import annotations

import io
import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# §1 Authority — new layout modules don't import gate/execution code
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/battle_test/layout_controller.py",
    "backend/core/ouroboros/battle_test/split_layout.py",
    "backend/core/ouroboros/battle_test/layout_repl.py",
    "backend/core/ouroboros/battle_test/serpent_flow_app.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_layout_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# No model-callable surface — layout is operator-driven only
# ===========================================================================


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_layout_modules_do_not_import_tool_frameworks(rel_path: str):
    """Neither Venom tool_executor nor MCP tool client; layout is
    never a model tool."""
    src = Path(rel_path).read_text()
    for forbidden in ("tool_executor", "mcp_tool_client", "tool_registry"):
        assert not re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(forbidden)}",
            src, re.MULTILINE,
        ), f"{rel_path} imports model-callable surface {forbidden!r}"


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.battle_test.layout_controller import (
        LAYOUT_CONTROLLER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.battle_test.split_layout import (
        SPLIT_LAYOUT_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SERPENT_FLOW_APP_SCHEMA_VERSION,
    )
    assert LAYOUT_CONTROLLER_SCHEMA_VERSION == "serpent_layout.v1"
    assert SPLIT_LAYOUT_SCHEMA_VERSION == "serpent_split_layout.v1"
    assert SERPENT_FLOW_APP_SCHEMA_VERSION == "serpent_flow_app.v1"


# ===========================================================================
# Default mode is flow — honors feedback_tui_design.md
# ===========================================================================


def test_env_default_mode_is_flow(monkeypatch):
    """With no env override, boot must land on flow — matches the
    'avoid pinned dashboards' posture from feedback_tui_design.md."""
    from backend.core.ouroboros.battle_test.layout_controller import (
        layout_default_from_env,
    )
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    assert layout_default_from_env() == "flow"


def test_app_default_is_flow(monkeypatch):
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SerpentFlowApp,
    )
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp.from_argv([], output_stream=io.StringIO())
    assert app.controller.is_flow


# ===========================================================================
# Flow-mode behavioral equivalence — emits route to the stream writer
# exactly like the legacy flowing SerpentFlow path would
# ===========================================================================


def test_flow_mode_emits_go_through_stream_writer_verbatim():
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController, MODE_FLOW,
    )
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SerpentFlowApp,
    )
    captured = []
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    # Every emit channel lands on the same flow writer (existing
    # SerpentFlow will have printed these to the scrolling console).
    app.emit_stream("stream line")
    app.emit_dashboard("dashboard line")
    app.emit_diff("diff line")
    assert captured == ["stream line", "dashboard line", "diff line"]


def test_flow_mode_does_not_populate_split_buffers():
    """Regression guard: flow mode must never fill region buffers —
    otherwise a future leak into Rich Live would change default UX."""
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController, MODE_FLOW,
    )
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SerpentFlowApp,
    )
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    for _ in range(10):
        app.emit_stream("x")
        app.emit_dashboard("y")
        app.emit_diff("z")
    snap = app.split_layout.snapshot()
    # Every region stays empty in flow mode.
    for region, lines in snap.items():
        assert lines == (), (
            f"flow mode leaked into split region {region!r}"
        )


# ===========================================================================
# TTY fallback — non-TTY runs never activate Rich Live
# ===========================================================================


def test_split_renderer_never_activates_without_tty():
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController, MODE_SPLIT,
    )
    from backend.core.ouroboros.battle_test.split_layout import SplitLayout
    s = SplitLayout(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        output_stream=io.StringIO(),  # not a TTY
    )
    assert s.start() is False
    assert s.active is False


# ===========================================================================
# REPL verb surface — every documented verb reachable
# ===========================================================================


@pytest.mark.parametrize("verb", ["flow", "split", "focus", "help"])
def test_repl_verb_registered(verb: str):
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController, MODE_FLOW,
    )
    from backend.core.ouroboros.battle_test.layout_repl import (
        dispatch_layout_command,
    )
    c = LayoutController(initial_mode=MODE_FLOW)
    if verb == "focus":
        line = f"/layout {verb} stream"
    else:
        line = f"/layout {verb}"
    res = dispatch_layout_command(line, controller=c)
    assert res.matched is True
    assert res.ok is True, f"{verb} unexpectedly failed: {res.text}"


def test_repl_help_covers_every_verb():
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController,
    )
    from backend.core.ouroboros.battle_test.layout_repl import (
        dispatch_layout_command,
    )
    c = LayoutController()
    res = dispatch_layout_command("/layout help", controller=c)
    assert res.ok
    for verb in ("flow", "split", "focus"):
        assert verb in res.text


# ===========================================================================
# Single-keystroke escape — /layout flow from every mode
# ===========================================================================


@pytest.mark.parametrize("starting", [
    "split", "focus:stream", "focus:dashboard", "focus:diff",
])
def test_flow_verb_escapes_from_every_mode(starting: str):
    from backend.core.ouroboros.battle_test.layout_controller import (
        LayoutController,
    )
    from backend.core.ouroboros.battle_test.layout_repl import (
        dispatch_layout_command,
    )
    c = LayoutController(initial_mode=starting)
    res = dispatch_layout_command("/layout flow", controller=c)
    assert res.ok
    assert c.mode == "flow"


# ===========================================================================
# CLI flag — --split activates at boot
# ===========================================================================


def test_cli_split_flag_activates_split_at_boot(monkeypatch):
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SerpentFlowApp,
    )
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp.from_argv(
        ["--split"], output_stream=io.StringIO(),
    )
    assert app.controller.is_split


def test_cli_flag_overrides_env(monkeypatch):
    from backend.core.ouroboros.battle_test.serpent_flow_app import (
        SerpentFlowApp,
    )
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    app = SerpentFlowApp.from_argv(
        ["--flow"], output_stream=io.StringIO(),
    )
    assert app.controller.is_flow


# ===========================================================================
# Docstring bit-rot
# ===========================================================================


def test_layout_controller_docstring_mentions_operator_authority():
    import backend.core.ouroboros.battle_test.layout_controller as m
    doc = (m.__doc__ or "").lower()
    assert "operator" in doc


def test_split_layout_docstring_mentions_lazy_rich():
    import backend.core.ouroboros.battle_test.split_layout as m
    doc = (m.__doc__ or "").lower()
    assert "lazy" in doc or "rich" in doc
    assert "tty" in doc or "headless" in doc


def test_serpent_flow_app_docstring_mentions_zero_change_default():
    import backend.core.ouroboros.battle_test.serpent_flow_app as m
    doc = (m.__doc__ or "").lower()
    # "zero-change" / "flowing default" — captures the invariant that
    # the legacy path is unaltered when the operator doesn't opt in.
    assert "zero-change" in doc or "flowing default" in doc


# ===========================================================================
# Region vocabulary stable
# ===========================================================================


def test_region_constants_frozen():
    from backend.core.ouroboros.battle_test.layout_controller import (
        REGION_DASHBOARD, REGION_DIFF, REGION_STREAM, valid_regions,
    )
    assert REGION_STREAM == "stream"
    assert REGION_DASHBOARD == "dashboard"
    assert REGION_DIFF == "diff"
    # Order stable so tests that index valid_regions() stay meaningful
    assert valid_regions() == ("stream", "dashboard", "diff")
