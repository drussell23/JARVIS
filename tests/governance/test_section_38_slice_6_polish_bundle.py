"""Section 38 Slice 6 (PRD v2.62 to v2.63, 2026-05-07) -
polish bundle regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_6(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_POLISH_BUNDLE_ENABLED", raising=False,
    )
    for sub in (
        "JARVIS_POLISH_HEARTBEAT_ENABLED",
        "JARVIS_POLISH_MOOD_ENABLED",
        "JARVIS_POLISH_PREDICTIVE_TIMER_ENABLED",
        "JARVIS_POLISH_SPARKLINES_ENABLED",
        "JARVIS_POLISH_SPINNER_ENABLED",
        "JARVIS_POLISH_TRUNCATION_AFFORDANCES_ENABLED",
        "JARVIS_POLISH_SMART_PATH_TRUNCATE_ENABLED",
        "JARVIS_POLISH_EFFORT_PHRASES_ENABLED",
        "JARVIS_POLISH_SPARKLINE_WIDTH",
    ):
        monkeypatch.delenv(sub, raising=False)
    yield


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.polish_bundle import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.polish_bundle import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", value)
    assert master_enabled() is True


# (1) Heartbeat


def test_heartbeat_master_off_returns_empty():
    from backend.core.ouroboros.governance.polish_bundle import (
        format_heartbeat,
    )
    assert format_heartbeat(ops_per_min=5.0, tick_index=0) == ""


def test_heartbeat_master_on_alternates(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_heartbeat,
    )
    h0 = format_heartbeat(ops_per_min=5.0, tick_index=0)
    h1 = format_heartbeat(ops_per_min=5.0, tick_index=1)
    assert h0 != h1
    assert h0 == "♥"
    assert h1 == "♡"


def test_heartbeat_resting_slow_alternation(monkeypatch):
    """At ops_per_min < 0.1, alternation slows to every 4 ticks."""
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_heartbeat,
    )
    # tick 0, 1, 2, 3 → all same; tick 4 → flip.
    h0 = format_heartbeat(ops_per_min=0.0, tick_index=0)
    h1 = format_heartbeat(ops_per_min=0.0, tick_index=1)
    h4 = format_heartbeat(ops_per_min=0.0, tick_index=4)
    assert h0 == h1
    assert h0 != h4


# (2) Mood


def test_mood_taxonomy_4_values():
    from backend.core.ouroboros.governance.polish_bundle import (
        MoodGlyph,
    )
    assert {m.name for m in MoodGlyph} == {
        "CONFIDENT", "NEUTRAL", "STRUGGLING", "EMERGENCY",
    }


@pytest.mark.parametrize(
    "kwargs,expected_mood",
    [
        # CONFIDENT — high convergence, low error, low cost
        (dict(convergence_score=0.9, error_rate=0.05,
              cost_burn_pct=0.2), "confident"),
        # STRUGGLING — low convergence
        (dict(convergence_score=0.2, error_rate=0.05,
              cost_burn_pct=0.2), "struggling"),
        # STRUGGLING — high errors
        (dict(convergence_score=0.6, error_rate=0.25,
              cost_burn_pct=0.4), "struggling"),
        # STRUGGLING — high cost
        (dict(convergence_score=0.6, error_rate=0.05,
              cost_burn_pct=0.85), "struggling"),
        # EMERGENCY — very high error
        (dict(convergence_score=0.6, error_rate=0.50,
              cost_burn_pct=0.4), "emergency"),
        # EMERGENCY — very high cost
        (dict(convergence_score=0.6, error_rate=0.05,
              cost_burn_pct=0.97), "emergency"),
        # EMERGENCY — governor brake (overrides everything)
        (dict(governor_emergency=True), "emergency"),
        # NEUTRAL — middle state
        (dict(convergence_score=0.5, error_rate=0.10,
              cost_burn_pct=0.4), "neutral"),
    ],
)
def test_compute_mood(kwargs, expected_mood):
    from backend.core.ouroboros.governance.polish_bundle import (
        compute_mood,
    )
    assert compute_mood(**kwargs).value == expected_mood


def test_format_mood_master_off():
    from backend.core.ouroboros.governance.polish_bundle import (
        compute_mood, format_mood_indicator,
    )
    m = compute_mood(convergence_score=0.9)
    assert format_mood_indicator(m) == ""


def test_format_mood_emoji_glyphs(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        MoodGlyph, format_mood_indicator,
    )
    assert format_mood_indicator(MoodGlyph.CONFIDENT) == "😎"
    assert format_mood_indicator(MoodGlyph.NEUTRAL) == "😐"
    assert format_mood_indicator(MoodGlyph.STRUGGLING) == "😰"
    assert format_mood_indicator(MoodGlyph.EMERGENCY) == "🆘"


def test_compute_mood_defensive_on_extreme_inputs():
    from backend.core.ouroboros.governance.polish_bundle import (
        compute_mood,
    )
    # Out-of-range inputs clamped; NEVER raises.
    m = compute_mood(
        convergence_score=2.0,
        error_rate=-1.0,
        cost_burn_pct=99.9,
    )
    # cost clamped to 1.0 → triggers EMERGENCY rule.
    assert m.value == "emergency"


# (3) Predictive timer


def test_predictive_timer_master_off():
    from backend.core.ouroboros.governance.polish_bundle import (
        format_predictive_graduation_timer,
    )
    assert format_predictive_graduation_timer() == ""


# (4) Sparklines


def test_sparkline_master_off():
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    assert format_sparkline([1, 2, 3]) == ""


def test_sparkline_renders_canonical_blocks(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    out = format_sparkline([1, 2, 3, 4, 5, 6, 7, 8])
    assert out == "▁▂▃▄▅▆▇█"


def test_sparkline_flat_renders_mid_block(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    out = format_sparkline([5, 5, 5])
    # All-equal → mid-block (▅).
    assert "▅" in out


def test_sparkline_resamples_long_series(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    out = format_sparkline(
        list(range(100)), width=10,
    )
    assert len(out) == 10


def test_sparkline_pads_short_series(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    out = format_sparkline([5], width=5)
    assert len(out) == 5


def test_sparkline_empty_returns_empty(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    assert format_sparkline([]) == ""


def test_sparkline_defensive_on_bad_inputs(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_sparkline,
    )
    # Mixed types — non-numeric coerces to 0.
    out = format_sparkline([1, "bad", 3, None, 5])
    assert out  # non-empty


# (5) Braille spinner


def test_spinner_master_off_returns_empty():
    from backend.core.ouroboros.governance.polish_bundle import (
        BrailleSpinner,
    )
    s = BrailleSpinner()
    assert s.advance() == ""


def test_spinner_advances_through_canonical_cycle(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        BrailleSpinner,
    )
    s = BrailleSpinner()
    frames = [s.advance() for _ in range(10)]
    expected = [
        "⠋", "⠙", "⠹", "⠸", "⠼",
        "⠴", "⠦", "⠧", "⠇", "⠏",
    ]
    assert frames == expected


def test_spinner_cycle_wraps(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        BrailleSpinner,
    )
    s = BrailleSpinner()
    f0 = s.advance()
    for _ in range(9):
        s.advance()
    f10 = s.advance()
    # After 10 frames, cycle returns to first.
    assert f0 == f10


def test_spinner_current_does_not_advance(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        BrailleSpinner,
    )
    s = BrailleSpinner()
    a = s.current()
    b = s.current()
    assert a == b


def test_spinner_reset(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        BrailleSpinner,
    )
    s = BrailleSpinner()
    s.advance()
    s.advance()
    s.reset()
    assert s.advance() == "⠋"


# (6) Truncation affordances


def test_truncation_affordance_master_off_falls_back():
    from backend.core.ouroboros.governance.polish_bundle import (
        format_truncation_affordance,
    )
    out = format_truncation_affordance(
        truncated_count=12, ref="t-3",
    )
    # Fallback shape — count without affordance.
    assert "12" in out
    assert "/expand" not in out


def test_truncation_affordance_master_on_includes_expand(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_truncation_affordance,
    )
    out = format_truncation_affordance(
        truncated_count=12, ref="t-3",
    )
    assert "+12 lines" in out
    assert "(/expand t-3)" in out


def test_truncation_affordance_zero_count_empty(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        format_truncation_affordance,
    )
    assert format_truncation_affordance(
        truncated_count=0, ref="t-3",
    ) == ""


# (7) Smart path truncation


def test_smart_path_short_returns_unchanged(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        smart_path_truncate,
    )
    assert smart_path_truncate("short.py", max_chars=60) == "short.py"


def test_smart_path_long_elides_middle(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        smart_path_truncate,
    )
    out = smart_path_truncate(
        "very/long/path/with/many/segments/that/needs/leaf.py",
        max_chars=30,
    )
    assert "..." in out
    assert "leaf.py" in out


def test_smart_path_master_off_char_truncates():
    from backend.core.ouroboros.governance.polish_bundle import (
        smart_path_truncate,
    )
    out = smart_path_truncate(
        "very/long/path/that/does/not/get/smart/truncate.py",
        max_chars=20,
    )
    assert len(out) <= 20


# (8) Effort phrases


def test_effort_phrase_master_off_canonical_label():
    """Master off → falls back to canonical EffortBand label."""
    from backend.core.ouroboros.governance.polish_bundle import (
        effort_phrase_for_band,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (
        EffortBand,
    )
    out = effort_phrase_for_band(EffortBand.HIGH)
    # Canonical label is "high effort".
    assert out == "high effort"


def test_effort_phrase_master_on_predictive(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        effort_phrase_for_band,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (
        EffortBand,
    )
    assert effort_phrase_for_band(EffortBand.LOW) == "just started"
    assert effort_phrase_for_band(EffortBand.MEDIUM) == "working through it"
    assert effort_phrase_for_band(EffortBand.HIGH) == "deep in analysis"
    assert effort_phrase_for_band(EffortBand.VERY_HIGH) == "nearly done thinking"


def test_effort_phrase_string_input(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        effort_phrase_for_band,
    )
    assert effort_phrase_for_band("low") == "just started"
    assert effort_phrase_for_band("very_high") == "nearly done thinking"


def test_effort_phrase_unknown_returns_empty(monkeypatch):
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    from backend.core.ouroboros.governance.polish_bundle import (
        effort_phrase_for_band,
    )
    assert effort_phrase_for_band("xyz_unknown") == ""


# Sub-flag granularity


def test_sub_flag_off_disables_specific_feature(monkeypatch):
    """Bundle on + sub-flag explicitly off → feature disabled."""
    monkeypatch.setenv("JARVIS_POLISH_BUNDLE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_POLISH_MOOD_ENABLED", "false")
    from backend.core.ouroboros.governance.polish_bundle import (
        compute_mood, format_mood_indicator,
    )
    m = compute_mood(convergence_score=0.9)
    assert format_mood_indicator(m) == ""


# AST pins


def _polish_pins():
    from backend.core.ouroboros.governance.polish_bundle import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _polish_source():
    return Path(
        "backend/core/ouroboros/governance/polish_bundle.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _polish_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _polish_pins()
    src = _polish_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires():
    pins = _polish_pins()
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


def test_pin_authority_asymmetry_fires():
    pins = _polish_pins()
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


def test_pin_mood_taxonomy_fires_on_missing():
    pins = _polish_pins()
    pin = next(
        p for p in pins
        if "mood_taxonomy_4_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class MoodGlyph(str, enum.Enum):\n"
        "    NEUTRAL = 'neutral'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_effort_band_fires_on_missing():
    pins = _polish_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_effort_band" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_sparkline_blocks_canonical_fires_on_drift():
    pins = _polish_pins()
    pin = next(
        p for p in pins
        if "sparkline_blocks_canonical" in p.invariant_name
    )
    bad_src = (
        "_SPARKLINE_BLOCKS = ('a', 'b', 'c')\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.polish_bundle import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 8 sub-flags + sparkline_width = 10.
    assert n == 10


# Composition assertions


def test_canonical_effort_band_importable():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (
        EffortBand,
    )
    assert {m.name for m in EffortBand} == {
        "LOW", "MEDIUM", "HIGH", "VERY_HIGH",
    }


def test_canonical_eta_projection_importable():
    from backend.core.ouroboros.governance.phase9_substrate_health import (
        EtaProjection,
    )
    assert EtaProjection is not None
