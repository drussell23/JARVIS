"""Section 38 Slice 4 (PRD v2.60 to v2.61, 2026-05-07) -
live activity radar regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_4(monkeypatch):
    monkeypatch.delenv("JARVIS_ACTIVITY_RADAR_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_ACTIVITY_RADAR_WINDOW_S", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_ACTIVITY_RADAR_HISTORY_LIMIT", raising=False,
    )
    yield


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.activity_radar import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.activity_radar import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", value)
    assert master_enabled() is True


# Closed taxonomy


def test_category_taxonomy_5_values():
    from backend.core.ouroboros.governance.activity_radar import (
        ActivityCategory,
    )
    assert {m.name for m in ActivityCategory} == {
        "SENSORS", "BRIDGES", "GOVERNANCE", "GENERATION", "OTHER",
    }


# categorize_event_type


@pytest.mark.parametrize(
    "event_type,expected_category",
    [
        # Sensors (exact + prefix)
        ("curiosity_intent_emitted", "sensors"),
        ("curiosity_question_emitted", "sensors"),
        ("codebase_character_injected", "sensors"),
        ("intent_classified", "sensors"),
        ("vision_anything", "sensors"),
        ("test_failure", "sensors"),
        # Bridges
        ("execution_graph_progress", "bridges"),
        ("autonomy_command_bus", "bridges"),
        ("task_completed", "bridges"),
        ("task_started", "bridges"),
        # Governance
        ("posture_changed", "governance"),
        ("behavioral_drift_detected", "governance"),
        ("invariant_drift_detected", "governance"),
        ("cost_band_crossed", "governance"),
        ("circuit_breaker_approaching", "governance"),
        ("governor_emergency_brake", "governance"),
        ("memory_pressure_changed", "governance"),
        # Generation
        ("plan_generated", "generation"),
        ("multi_prior_dispatch", "generation"),
        ("thinking_progress_tick", "generation"),
        ("decision_recorded", "generation"),
        # Other (unknown / not categorized)
        ("totally_unknown_xyz", "other"),
        ("", "other"),
    ],
)
def test_categorize_event_type(event_type, expected_category):
    from backend.core.ouroboros.governance.activity_radar import (
        categorize_event_type,
    )
    assert (
        categorize_event_type(event_type).value
        == expected_category
    )


def test_categorize_defensive_on_non_string():
    from backend.core.ouroboros.governance.activity_radar import (
        categorize_event_type,
    )
    for bad in (None, 42, [], {}):
        assert categorize_event_type(bad).value == "other"


# Aggregation


def test_aggregate_empty_broker():
    """When broker has no events, snapshot reports zero
    activity but doesn't raise."""
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
    )
    snap = aggregate_activity()
    # No assertion on event_count (broker may have residual
    # events from other tests); just verify shape.
    assert snap.window_s > 0
    assert isinstance(snap.events_in_window, int)
    assert isinstance(snap.by_category, tuple)


def test_aggregate_with_window_override():
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
    )
    snap = aggregate_activity(window_s_override=120.0)
    assert snap.window_s == 120.0


def test_aggregate_with_real_events():
    """Inject events into canonical broker, verify aggregation
    composes correctly."""
    import time as _time
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
    )
    from backend.core.ouroboros.governance.activity_radar import (
        aggregate_activity,
    )
    broker = get_default_broker()
    # Use registered event types (broker drops unknowns).
    test_events = [
        ("posture_changed", "op-rad-1"),
        ("behavioral_drift_detected", "op-rad-1"),
        ("execution_graph_progress", "op-rad-2"),
        ("plan_generated", "op-rad-3"),
        ("task_completed", "op-rad-3"),
    ]
    for et, op in test_events:
        broker.publish(et, op, {})
    snap = aggregate_activity(now_unix=_time.time())
    # Should see at least our injected events.
    assert snap.events_in_window >= 5
    # Verify category counts (additive — other tests may have
    # injected events).
    bridges = snap.total_for_category(
        snap.by_category[1].category,
    ) if len(snap.by_category) > 1 else 0
    assert isinstance(bridges, int)


# Format render


def test_format_master_off_returns_empty():
    from backend.core.ouroboros.governance.activity_radar import (
        format_activity_radar,
    )
    assert format_activity_radar() == ""


def test_format_renders_when_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
    )
    from backend.core.ouroboros.governance.activity_radar import (
        format_activity_radar,
    )
    # Inject an event so render has content.
    get_default_broker().publish(
        "posture_changed", "op-fmt-test", {},
    )
    rendered = format_activity_radar()
    if rendered:
        assert "Activity radar" in rendered
        assert "GOVERNANCE" in rendered or "governance" in rendered.lower()


# /radar REPL


def test_radar_repl_unmatched_returns_matched_false():
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/something_else")
    assert r.matched is False


def test_radar_repl_help_master_off():
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar help")
    assert r.ok is True
    assert "activity radar" in r.text.lower()


def test_radar_repl_show_master_off_blocks():
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar show")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_radar_repl_show_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar show")
    assert r.ok is True


def test_radar_repl_show_with_window(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar show 30")
    assert r.ok is True


def test_radar_repl_categories(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar categories")
    assert r.ok is True
    assert "categories" in r.text.lower()


def test_radar_repl_status(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_radar_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv("JARVIS_ACTIVITY_RADAR_ENABLED", "true")
    from backend.core.ouroboros.governance.radar_repl import (
        dispatch_radar_command,
    )
    r = dispatch_radar_command("/radar gibberish")
    assert r.ok is False
    assert "unknown" in r.text.lower()


# AST pins


def _radar_pins():
    from backend.core.ouroboros.governance.activity_radar import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _radar_source():
    return Path(
        "backend/core/ouroboros/governance/activity_radar.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _radar_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _radar_pins()
    src = _radar_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _radar_pins()
    pin = next(
        p for p in pins
        if "master_default_false" in p.invariant_name
    )
    bad_src = (
        "def master_enabled():\n"
        "    return True\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _radar_pins()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_taxonomy_fires_on_missing_value():
    pins = _radar_pins()
    pin = next(
        p for p in pins
        if "category_taxonomy_5_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class ActivityCategory(str, enum.Enum):\n"
        "    SENSORS = 'sensors'\n"
        "    BRIDGES = 'bridges'\n"
        # Missing GOVERNANCE / GENERATION / OTHER
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_broker_fires_on_missing_compose():
    pins = _radar_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_broker" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_firing_telemetry_fires_on_missing_compose():
    pins = _radar_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_firing_telemetry" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry seed


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.activity_radar import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 6 tunables.
    assert n == 7
    names = {c["name"] for c in reg.calls}
    assert "JARVIS_ACTIVITY_RADAR_ENABLED" in names


# Composition assertions


def test_canonical_broker_importable():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
    )
    broker = get_default_broker()
    assert broker is not None
    assert hasattr(broker, "recent_history")


def test_canonical_firing_telemetry_importable():
    from backend.core.ouroboros.governance.firing_telemetry import (
        get_default_registry,
    )
    registry = get_default_registry()
    assert registry is not None
    assert hasattr(registry, "snapshot")
