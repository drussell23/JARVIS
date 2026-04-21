"""Tests for serpent_flow_app (Slice 4)."""
from __future__ import annotations

import io

import pytest

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
)
from backend.core.ouroboros.battle_test.serpent_flow_app import (
    SERPENT_FLOW_APP_SCHEMA_VERSION,
    SerpentFlowApp,
    resolve_initial_mode,
)
from backend.core.ouroboros.battle_test.split_layout import SplitLayout


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_pinned():
    assert SERPENT_FLOW_APP_SCHEMA_VERSION == "serpent_flow_app.v1"


# ===========================================================================
# resolve_initial_mode — precedence: CLI > env > flow
# ===========================================================================


def test_resolve_defaults_to_flow(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    assert resolve_initial_mode([]) == "flow"


def test_resolve_cli_beats_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "flow")
    assert resolve_initial_mode(["--split"]) == "split"


def test_resolve_env_when_no_cli(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    assert resolve_initial_mode([]) == "split"


def test_resolve_layout_equals_focus_stream(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    assert resolve_initial_mode(["--layout=focus:stream"]) == "focus:stream"


def test_resolve_invalid_cli_falls_through_to_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    # --layout=evil is invalid → ignored → env wins
    assert resolve_initial_mode(["--layout=evil"]) == "split"


# ===========================================================================
# Default construction
# ===========================================================================


def test_app_default_constructs_in_flow_mode(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp(output_stream=io.StringIO())
    assert app.controller.is_flow


def test_app_from_argv_split(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp.from_argv(
        ["--split"], output_stream=io.StringIO(),
    )
    assert app.controller.is_split


def test_app_from_argv_focus(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp.from_argv(
        ["--layout=focus:diff"], output_stream=io.StringIO(),
    )
    assert app.controller.mode == "focus:diff"


def test_app_snapshot_shape(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        output_stream=io.StringIO(),
    )
    snap = app.snapshot()
    assert snap["schema_version"] == "serpent_flow_app.v1"
    assert snap["mode"] == "split"
    assert snap["is_split"] is True
    assert snap["is_flow"] is False
    assert "split_active" in snap


# ===========================================================================
# Flow mode — writes go through the flow writer, NOT split buffers
# ===========================================================================


def test_flow_mode_emit_stream_invokes_stream_writer():
    captured = []
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    app.emit_stream("hello stream")
    app.emit_dashboard("dash status")
    app.emit_diff("unified diff")
    assert captured == ["hello stream", "dash status", "unified diff"]


def test_flow_mode_split_buffers_stay_empty():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    app.emit_stream("x")
    app.emit_dashboard("y")
    app.emit_diff("z")
    snap = app.split_layout.snapshot()
    for region in (REGION_STREAM, REGION_DASHBOARD, REGION_DIFF):
        assert snap[region] == (), (
            f"flow-mode emit leaked into split buffer {region}"
        )


def test_flow_mode_falls_back_to_print_when_writer_missing(capsys):
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
    )
    app.emit_stream("hello")
    captured = capsys.readouterr()
    assert "hello" in captured.out


# ===========================================================================
# Split mode — writes flow into region buffers
# ===========================================================================


def test_split_mode_routes_emit_to_region_buffers():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    app.emit_stream("op-abc phase=CLASSIFY")
    app.emit_dashboard("cost=$0.03")
    app.emit_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@")
    snap = app.split_layout.snapshot()
    assert snap[REGION_STREAM] == ("op-abc phase=CLASSIFY",)
    assert snap[REGION_DASHBOARD] == ("cost=$0.03",)
    assert snap[REGION_DIFF] == ("--- a/x\n+++ b/x\n@@ -1 +1 @@",)


def test_split_mode_does_not_invoke_flow_writer():
    captured = []
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    app.emit_stream("split content")
    assert captured == []  # flow writer never called in split


# ===========================================================================
# Focus mode — every region still buffers (so return-to-split
# preserves history), flow writer still silent
# ===========================================================================


def test_focus_mode_buffers_every_region():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode="focus:diff"),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    app.emit_stream("a")
    app.emit_dashboard("b")
    app.emit_diff("c")
    snap = app.split_layout.snapshot()
    assert snap[REGION_STREAM] == ("a",)
    assert snap[REGION_DASHBOARD] == ("b",)
    assert snap[REGION_DIFF] == ("c",)


# ===========================================================================
# Mode transitions via controller — app follows
# ===========================================================================


def test_app_follows_controller_flow_to_split():
    c = LayoutController(initial_mode=MODE_FLOW)
    captured = []
    app = SerpentFlowApp(
        controller=c,
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    # First emit: flow mode
    app.emit_stream("a-flow")
    # Switch to split
    c.to_split()
    app.emit_stream("b-split")
    # The post-transition emit lands in the split buffer
    snap = app.split_layout.snapshot()
    assert snap[REGION_STREAM] == ("b-split",)
    # The pre-transition emit was captured by the flow writer
    assert captured == ["a-flow"]


def test_app_follows_controller_split_to_flow():
    c = LayoutController(initial_mode=MODE_SPLIT)
    captured = []
    app = SerpentFlowApp(
        controller=c,
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    app.emit_stream("before-flow-switch")
    c.to_flow()
    app.emit_stream("after-flow-switch")
    assert captured == ["after-flow-switch"]
    snap = app.split_layout.snapshot()
    assert snap[REGION_STREAM] == ("before-flow-switch",)


# ===========================================================================
# Lifecycle: start / stop — headless safe
# ===========================================================================


def test_start_in_flow_returns_false_headless():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        output_stream=io.StringIO(),
    )
    # Flow mode + headless → renderer never activates
    assert app.start() is False


def test_start_in_split_returns_false_headless():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        output_stream=io.StringIO(),
    )
    # Split mode + non-TTY → still False (no Rich Live)
    assert app.start() is False
    assert app.split_layout.active is False


def test_stop_is_idempotent():
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        output_stream=io.StringIO(),
    )
    app.stop()
    app.stop()
    app.start()
    app.stop()
    app.stop()


# ===========================================================================
# Writer exception doesn't escape emit
# ===========================================================================


def test_writer_exception_is_swallowed():
    def _boom(_):
        raise RuntimeError("write failed")
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=_boom,
        output_stream=io.StringIO(),
    )
    # Must not raise
    app.emit_stream("hi")
