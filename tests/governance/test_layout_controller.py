"""Tests for layout_controller (Slice 1)."""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.layout_controller import (
    LAYOUT_CONTROLLER_SCHEMA_VERSION,
    LayoutController,
    LayoutError,
    LayoutTransition,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
    focus_region,
    is_focus_mode,
    is_valid_mode,
    layout_default_from_env,
    parse_cli_layout_arg,
    reset_default_layout_controller,
    get_default_layout_controller,
    valid_regions,
)


# ===========================================================================
# Schema + vocab
# ===========================================================================


def test_schema_version_pinned():
    assert LAYOUT_CONTROLLER_SCHEMA_VERSION == "serpent_layout.v1"


def test_mode_constants_stable():
    assert MODE_FLOW == "flow"
    assert MODE_SPLIT == "split"


def test_region_constants_stable():
    assert REGION_STREAM == "stream"
    assert REGION_DASHBOARD == "dashboard"
    assert REGION_DIFF == "diff"
    assert valid_regions() == ("stream", "dashboard", "diff")


# ===========================================================================
# is_valid_mode + focus helpers
# ===========================================================================


@pytest.mark.parametrize("mode", [
    "flow", "split",
    "focus:stream", "focus:dashboard", "focus:diff",
])
def test_valid_modes_accepted(mode: str):
    assert is_valid_mode(mode)


@pytest.mark.parametrize("mode", [
    "", "FLOW", "Split",
    "focus:", "focus:unknown",
    "focus:..", "focus:stream.",
    "flowish", "../etc/passwd",
    "\x1b[31mred\x1b[0m",  # ANSI injection
])
def test_invalid_modes_rejected(mode: str):
    assert not is_valid_mode(mode)


def test_focus_region_extracts_region():
    assert focus_region("focus:stream") == "stream"
    assert focus_region("focus:dashboard") == "dashboard"
    assert focus_region("focus:diff") == "diff"


def test_focus_region_returns_none_for_invalid():
    assert focus_region("flow") is None
    assert focus_region("focus:unknown") is None
    assert focus_region("focus:") is None
    assert focus_region("") is None


def test_is_focus_mode_returns_true_only_for_focus():
    assert is_focus_mode("focus:stream")
    assert not is_focus_mode("flow")
    assert not is_focus_mode("split")


# ===========================================================================
# Env defaults
# ===========================================================================


def test_env_default_flow_when_unset(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    assert layout_default_from_env() == "flow"


def test_env_default_respects_valid_value(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    assert layout_default_from_env() == "split"


def test_env_default_respects_focus_value(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "focus:diff")
    assert layout_default_from_env() == "focus:diff"


def test_env_default_lowercases(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "SPLIT")
    assert layout_default_from_env() == "split"


def test_env_default_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SERPENT_LAYOUT_DEFAULT", "not_a_mode_ever",
    )
    assert layout_default_from_env() == "flow"


def test_env_default_falls_back_on_injection_attempt(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SERPENT_LAYOUT_DEFAULT", "../etc/passwd",
    )
    assert layout_default_from_env() == "flow"


# ===========================================================================
# CLI arg parsing
# ===========================================================================


def test_cli_split_flag():
    assert parse_cli_layout_arg(["--split"]) == "split"


def test_cli_flow_flag():
    assert parse_cli_layout_arg(["--flow"]) == "flow"


def test_cli_layout_equals_split():
    assert parse_cli_layout_arg(["--layout=split"]) == "split"


def test_cli_layout_equals_focus_stream():
    assert parse_cli_layout_arg(["--layout=focus:stream"]) == "focus:stream"


def test_cli_layout_space_split():
    assert parse_cli_layout_arg(["--layout", "split"]) == "split"


def test_cli_layout_missing_returns_none():
    assert parse_cli_layout_arg([]) is None
    assert parse_cli_layout_arg(["--other-flag"]) is None


def test_cli_layout_invalid_value_returns_none():
    """Invalid --layout value is ignored (caller will use env / default)."""
    assert parse_cli_layout_arg(["--layout=evil"]) is None
    assert parse_cli_layout_arg(["--layout", "not-a-mode"]) is None


def test_cli_layout_equals_empty_returns_none():
    assert parse_cli_layout_arg(["--layout="]) is None


# ===========================================================================
# LayoutController — state machine
# ===========================================================================


def test_controller_defaults_to_flow(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    c = LayoutController()
    assert c.mode == "flow"
    assert c.is_flow
    assert not c.is_split


def test_controller_respects_env_default(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    c = LayoutController()
    assert c.mode == "split"
    assert c.is_split


def test_controller_explicit_initial_overrides_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SERPENT_LAYOUT_DEFAULT", "split")
    c = LayoutController(initial_mode="flow")
    assert c.mode == "flow"


def test_controller_rejects_invalid_initial(monkeypatch):
    with pytest.raises(LayoutError):
        LayoutController(initial_mode="evil")


def test_controller_to_split_transitions(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    c = LayoutController()
    txn = c.to_split(reason="op")
    assert c.is_split
    assert txn.old_mode == "flow"
    assert txn.new_mode == "split"
    assert txn.reason == "op"


def test_controller_to_focus_transitions():
    c = LayoutController(initial_mode="flow")
    txn = c.to_focus("stream")
    assert c.mode == "focus:stream"
    assert c.focused_region == "stream"
    assert txn.old_mode == "flow"
    assert txn.new_mode == "focus:stream"


def test_controller_to_focus_rejects_bad_region():
    c = LayoutController(initial_mode="flow")
    with pytest.raises(LayoutError):
        c.to_focus("evil")


def test_controller_to_flow_from_split():
    c = LayoutController(initial_mode="split")
    c.to_flow()
    assert c.is_flow


def test_controller_to_flow_from_focus():
    c = LayoutController(initial_mode="focus:diff")
    c.to_flow()
    assert c.is_flow


def test_controller_set_mode_rejects_unknown():
    c = LayoutController(initial_mode="flow")
    with pytest.raises(LayoutError):
        c.set_mode("pwn")


def test_controller_focused_region_is_none_outside_focus():
    c = LayoutController(initial_mode="flow")
    assert c.focused_region is None
    c.to_split()
    assert c.focused_region is None


def test_controller_snapshot_shape():
    c = LayoutController(initial_mode="focus:dashboard")
    snap = c.snapshot()
    assert snap["schema_version"] == "serpent_layout.v1"
    assert snap["mode"] == "focus:dashboard"
    assert snap["is_flow"] is False
    assert snap["is_split"] is False
    assert snap["focused_region"] == "dashboard"
    assert snap["valid_regions"] == ["stream", "dashboard", "diff"]


# ===========================================================================
# LayoutTransition
# ===========================================================================


def test_transition_is_frozen():
    t = LayoutTransition(old_mode="flow", new_mode="split")
    with pytest.raises((AttributeError, TypeError)):
        t.old_mode = "x"  # type: ignore[misc]


def test_transition_project_is_json_safe():
    import json
    t = LayoutTransition(old_mode="flow", new_mode="split", reason="op")
    assert json.loads(json.dumps(t.project())) == t.project()


# ===========================================================================
# Listeners
# ===========================================================================


def test_listener_fires_on_transition():
    c = LayoutController(initial_mode="flow")
    received = []
    c.on_change(received.append)
    c.to_split(reason="test")
    assert len(received) == 1
    assert received[0].old_mode == "flow"
    assert received[0].new_mode == "split"


def test_listener_fires_on_idempotent_nudge():
    """set_mode(current_mode) still emits — useful for re-render
    signaling."""
    c = LayoutController(initial_mode="flow")
    received = []
    c.on_change(received.append)
    c.to_flow()
    assert len(received) == 1
    assert received[0].old_mode == "flow"
    assert received[0].new_mode == "flow"


def test_listener_unsub_stops_firing():
    c = LayoutController(initial_mode="flow")
    received = []
    unsub = c.on_change(received.append)
    c.to_split()
    unsub()
    c.to_flow()
    assert len(received) == 1  # only the pre-unsub transition


def test_listener_exception_is_swallowed():
    c = LayoutController(initial_mode="flow")

    def _explode(_):
        raise RuntimeError("boom")

    c.on_change(_explode)
    # Must not raise
    c.to_split()
    assert c.is_split


def test_listener_receives_reason_field():
    c = LayoutController(initial_mode="flow")
    received = []
    c.on_change(received.append)
    c.to_split(reason="cli_arg_split")
    assert received[0].reason == "cli_arg_split"


# ===========================================================================
# Singleton
# ===========================================================================


def test_singleton_returns_same_instance():
    reset_default_layout_controller()
    try:
        a = get_default_layout_controller()
        b = get_default_layout_controller()
        assert a is b
    finally:
        reset_default_layout_controller()


def test_singleton_reset_spawns_fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    reset_default_layout_controller()
    try:
        a = get_default_layout_controller()
        reset_default_layout_controller()
        b = get_default_layout_controller()
        assert a is not b
    finally:
        reset_default_layout_controller()
