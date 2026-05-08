"""Phase 2 (PRD §37 v2.54→v2.55, 2026-05-07) — active-thinking
progress aggregator regression spine.

Covers:

  * EffortBand closed 4-value taxonomy
  * compute_effort_band pure-function determinism (boundary
    cases, strictest-axis-wins, defensive on bad inputs)
  * derive_verb_phrase heuristic (gerund pattern, fallback,
    edge cases)
  * format_thinking_line render shape
  * ThinkingProgressObserver chatter-suppression structural
    (verb / band crossings → sse_eligible True; identical
    re-update → False)
  * NarrativeChannel.frames_by_op_kind + active_thinking_frame
    canonical accessors
  * publish_thinking_progress_event composes canonical broker
  * status_line._format_thinking_token integration
  * 5 AST pins (master_default_false / taxonomy_4_values /
    authority_asymmetry / composes_canonical_narrative /
    composes_canonical_stream_renderer)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_phase2(monkeypatch):
    """Clean observer + channel state for each test."""
    monkeypatch.delenv(
        "JARVIS_THINKING_PROGRESS_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance import (
        thinking_progress_aggregator as tpa,
    )
    from backend.core.ouroboros.battle_test import (
        narrative_channel as nc,
    )
    tpa.reset_observer_for_tests()
    nc.reset_default_channel_for_tests()
    yield
    tpa.reset_observer_for_tests()
    nc.reset_default_channel_for_tests()


# ---------------------------------------------------------------------------
# Master flag default-FALSE
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "on"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        master_enabled,
    )
    monkeypatch.setenv(
        "JARVIS_THINKING_PROGRESS_ENABLED", value,
    )
    assert master_enabled() is True


# ---------------------------------------------------------------------------
# EffortBand closed taxonomy
# ---------------------------------------------------------------------------


def test_effort_band_4_values():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        EffortBand,
    )
    members = {m.name for m in EffortBand}
    assert members == {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"}


# ---------------------------------------------------------------------------
# compute_effort_band — pure function
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed,tokens,expected",
    [
        # LOW: short + small
        (0.0, 0, "low"),
        (10.0, 1000, "low"),
        (29.9, 4999, "low"),
        # MEDIUM: low_elapsed crossed OR low_tokens crossed
        (30.0, 0, "medium"),
        (0.0, 5000, "medium"),
        (60.0, 8000, "medium"),
        # HIGH: medium thresholds crossed
        (120.0, 0, "high"),
        (0.0, 15_000, "high"),
        (200.0, 20_000, "high"),
        # VERY_HIGH: high thresholds crossed
        (300.0, 0, "very_high"),
        (0.0, 30_000, "very_high"),
        (500.0, 50_000, "very_high"),
    ],
)
def test_compute_effort_band_thresholds(
    elapsed, tokens, expected,
):
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        compute_effort_band,
    )
    band = compute_effort_band(
        elapsed_s=elapsed, tokens_total=tokens,
    )
    assert band.value == expected


def test_compute_effort_band_strictest_axis_wins():
    """Strictest threshold wins (whichever axis pushes higher)."""
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        compute_effort_band,
    )
    # Tokens push to HIGH; elapsed only LOW.
    assert compute_effort_band(
        elapsed_s=10.0, tokens_total=20_000,
    ).value == "high"
    # Elapsed pushes to VERY_HIGH; tokens only LOW.
    assert compute_effort_band(
        elapsed_s=400.0, tokens_total=100,
    ).value == "very_high"


def test_compute_effort_band_defensive_on_bad_inputs():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        compute_effort_band,
    )
    # NaN / negative / non-numeric all coerce to 0.
    assert compute_effort_band(
        elapsed_s=float("nan"), tokens_total=0,
    ).value == "low"
    assert compute_effort_band(
        elapsed_s=-100.0, tokens_total=-50,
    ).value == "low"


def test_compute_effort_band_thresholds_env_overridable(
    monkeypatch,
):
    """Operator binding "no hardcoding" — thresholds are
    env-overridable."""
    monkeypatch.setenv(
        "JARVIS_THINKING_PROGRESS_LOW_ELAPSED_S", "5.0",
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        compute_effort_band,
    )
    # With low_elapsed=5s, 10s elapsed → MEDIUM (was LOW with default 30s).
    assert compute_effort_band(
        elapsed_s=10.0, tokens_total=0,
    ).value == "medium"


# ---------------------------------------------------------------------------
# derive_verb_phrase
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prose,expected",
    [
        ("Investigating root cause", "Investigating"),
        ("Considering all options", "Considering"),
        ("Reviewing the diff carefully", "Reviewing"),
        ("Analyzing the failure", "Analyzing"),
    ],
)
def test_derive_verb_phrase_gerund_pattern(prose, expected):
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        derive_verb_phrase,
    )
    assert derive_verb_phrase(prose) == expected


def test_derive_verb_phrase_fallback_on_empty():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        derive_verb_phrase,
    )
    assert derive_verb_phrase("") == "Thinking"
    assert derive_verb_phrase("   ") == "Thinking"


def test_derive_verb_phrase_defensive_on_non_string():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        derive_verb_phrase,
    )
    assert derive_verb_phrase(None) == "Thinking"
    assert derive_verb_phrase(42) == "Thinking"
    assert derive_verb_phrase([]) == "Thinking"


def test_derive_verb_phrase_handles_multiline():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        derive_verb_phrase,
    )
    # Takes first non-empty line.
    multiline = "\n\nInvestigating something\nThen later"
    assert derive_verb_phrase(multiline) == "Investigating"


# ---------------------------------------------------------------------------
# format_thinking_line
# ---------------------------------------------------------------------------


def test_format_thinking_line_active_renders():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        ThinkingProgressSnapshot,
        EffortBand,
        format_thinking_line,
    )
    snap = ThinkingProgressSnapshot(
        op_id="op-x",
        verb_phrase="Investigating",
        elapsed_s=412.0,  # 6m 52s
        tokens_input=0,
        tokens_output=24_000,
        effort_band=EffortBand.HIGH,
        is_active=True,
    )
    line = format_thinking_line(snap)
    assert line.startswith("* ")
    assert "Investigating" in line
    assert "6m 52s" in line
    assert "24k tokens" in line
    assert "high effort" in line


def test_format_thinking_line_inactive_returns_empty():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        ThinkingProgressSnapshot,
        format_thinking_line,
    )
    snap = ThinkingProgressSnapshot(is_active=False)
    assert format_thinking_line(snap) == ""


def test_format_elapsed_short():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        ThinkingProgressSnapshot,
        EffortBand,
        format_thinking_line,
    )
    snap = ThinkingProgressSnapshot(
        op_id="op", verb_phrase="X",
        elapsed_s=5.0, tokens_output=100,
        effort_band=EffortBand.LOW,
        is_active=True,
    )
    line = format_thinking_line(snap)
    assert "5s" in line
    assert "100 tokens" in line


def test_format_thinking_line_under_1k_tokens():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        ThinkingProgressSnapshot,
        EffortBand,
        format_thinking_line,
    )
    snap = ThinkingProgressSnapshot(
        op_id="op", verb_phrase="X",
        elapsed_s=1, tokens_output=42,
        effort_band=EffortBand.LOW,
        is_active=True,
    )
    line = format_thinking_line(snap)
    # Under 1000: literal count.
    assert "42 tokens" in line


# ---------------------------------------------------------------------------
# NarrativeChannel canonical accessors (Phase 2 read-API extension)
# ---------------------------------------------------------------------------


def test_active_thinking_frame_returns_buffering():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    channel = get_default_channel()
    frame = channel.start_frame(
        op_id="op-z",
        phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    active = channel.active_thinking_frame(op_id="op-z")
    assert active is not None
    assert active.ref == frame.ref


def test_active_thinking_frame_returns_none_for_unknown_op():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
    )
    channel = get_default_channel()
    assert channel.active_thinking_frame(op_id="nonexistent") is None


def test_frames_by_op_kind_filter():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    channel = get_default_channel()
    channel.start_frame(
        op_id="op-A",
        phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.start_frame(
        op_id="op-A",
        phase="PLAN",
        kind=NarrativeKind.PLAN_PROSE,
    )
    thinking = channel.frames_by_op_kind(
        op_id="op-A",
        kind=NarrativeKind.THINKING,
    )
    assert len(thinking) == 1
    plan = channel.frames_by_op_kind(
        op_id="op-A",
        kind=NarrativeKind.PLAN_PROSE,
    )
    assert len(plan) == 1


def test_frames_by_op_kind_defensive_on_bad_inputs():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    channel = get_default_channel()
    # Empty op_id → empty.
    assert channel.frames_by_op_kind(
        op_id="",
        kind=NarrativeKind.THINKING,
    ) == ()


# ---------------------------------------------------------------------------
# ThinkingProgressObserver — chatter-suppression structural
# ---------------------------------------------------------------------------


def test_observer_first_update_is_eligible():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        get_default_observer,
    )
    channel = get_default_channel()
    channel.start_frame(
        op_id="op-1", phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.append_token(
        op_id="op-1", phase="GENERATE",
        kind=NarrativeKind.THINKING,
        token="Investigating root cause",
    )
    observer = get_default_observer()
    snap, eligible = observer.update(op_id="op-1")
    # First update — verb + band changed from None
    assert eligible is True
    assert snap is not None
    assert snap.verb_phrase == "Investigating"


def test_observer_second_update_no_change_silent():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        get_default_observer,
    )
    channel = get_default_channel()
    channel.start_frame(
        op_id="op-2", phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.append_token(
        op_id="op-2", phase="GENERATE",
        kind=NarrativeKind.THINKING,
        token="Considering options",
    )
    observer = get_default_observer()
    observer.update(op_id="op-2")
    # Second identical update — silent.
    _, eligible = observer.update(op_id="op-2")
    assert eligible is False


def test_observer_returns_none_on_empty_op_id():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        get_default_observer,
    )
    observer = get_default_observer()
    snap, eligible = observer.update(op_id="")
    assert snap is None
    assert eligible is False


def test_observer_get_returns_stored_snapshot():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        get_default_observer,
    )
    channel = get_default_channel()
    channel.start_frame(
        op_id="op-3", phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.append_token(
        op_id="op-3", phase="GENERATE",
        kind=NarrativeKind.THINKING,
        token="Reviewing diff",
    )
    observer = get_default_observer()
    observer.update(op_id="op-3")
    snap = observer.get("op-3")
    assert snap is not None
    assert snap.verb_phrase == "Reviewing"


def test_observer_all_active_filters_inactive():
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        get_default_observer,
    )
    channel = get_default_channel()
    # Active op
    channel.start_frame(
        op_id="op-act", phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.append_token(
        op_id="op-act", phase="GENERATE",
        kind=NarrativeKind.THINKING,
        token="Active",
    )
    observer = get_default_observer()
    observer.update(op_id="op-act")
    observer.update(op_id="op-inactive")  # never had frame
    active = observer.all_active()
    op_ids = {s.op_id for s in active}
    assert "op-act" in op_ids
    assert "op-inactive" not in op_ids


# ---------------------------------------------------------------------------
# §33.5 versioned-artifact projection
# ---------------------------------------------------------------------------


def test_snapshot_to_dict_has_schema_version():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        ThinkingProgressSnapshot,
        EffortBand,
        THINKING_PROGRESS_SCHEMA_VERSION,
    )
    snap = ThinkingProgressSnapshot(
        op_id="op", verb_phrase="X", elapsed_s=10.0,
        tokens_input=5, tokens_output=10,
        effort_band=EffortBand.LOW, is_active=True,
    )
    d = snap.to_dict()
    assert d["schema_version"] == THINKING_PROGRESS_SCHEMA_VERSION
    assert d["tokens_total"] == 15
    assert d["effort_band"] == "low"


# ---------------------------------------------------------------------------
# status_line._format_thinking_token integration
# ---------------------------------------------------------------------------


def test_status_line_thinking_token_master_off():
    from backend.core.ouroboros.battle_test.status_line import (
        _format_thinking_token,
    )
    # Master off — empty.
    assert _format_thinking_token(op_id="op-x") == ""


def test_status_line_thinking_token_empty_op_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_THINKING_PROGRESS_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_thinking_token,
    )
    assert _format_thinking_token(op_id="") == ""


def test_status_line_thinking_token_renders_when_active(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_THINKING_PROGRESS_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (  # noqa: E501
        get_default_channel,
        NarrativeKind,
    )
    channel = get_default_channel()
    channel.start_frame(
        op_id="op-render", phase="GENERATE",
        kind=NarrativeKind.THINKING,
    )
    channel.append_token(
        op_id="op-render", phase="GENERATE",
        kind=NarrativeKind.THINKING,
        token="Investigating",
    )
    from backend.core.ouroboros.battle_test.status_line import (
        _format_thinking_token,
    )
    token = _format_thinking_token(op_id="op-render")
    assert "*" in token
    assert "Investigating" in token


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def _aggregator_pins():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _aggregator_source():
    return Path(
        "backend/core/ouroboros/governance/"
        "thinking_progress_aggregator.py"
    ).read_text()


def test_pins_register_exactly_5():
    pins = _aggregator_pins()
    assert len(pins) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _aggregator_pins()
    src = _aggregator_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _aggregator_pins()
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


def test_pin_taxonomy_fires_on_missing_value():
    pins = _aggregator_pins()
    pin = next(
        p for p in pins
        if "effort_band_taxonomy_4_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class EffortBand(str, enum.Enum):\n"
        "    LOW = 'low'\n"
        "    MEDIUM = 'medium'\n"
        # Missing HIGH + VERY_HIGH
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_authority_asymmetry_fires_on_orchestrator_import():
    pins = _aggregator_pins()
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


def test_pin_composes_narrative_fires_on_missing_compose():
    pins = _aggregator_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_narrative" in p.invariant_name
    )
    bad_src = "x = 1\n"  # missing narrative_channel
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("narrative_channel" in v for v in violations)


def test_pin_composes_stream_renderer_fires_on_missing_compose():
    pins = _aggregator_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_stream_renderer" in p.invariant_name
    )
    bad_src = "x = 1\n"  # missing stream_renderer
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("stream_renderer" in v for v in violations)


# ---------------------------------------------------------------------------
# Event-type registration
# ---------------------------------------------------------------------------


def test_thinking_progress_event_registered_in_broker():
    """The new SSE event type MUST be in the canonical
    _VALID_EVENT_TYPES frozenset."""
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        _VALID_EVENT_TYPES,
        EVENT_TYPE_THINKING_PROGRESS_TICK,
    )
    assert EVENT_TYPE_THINKING_PROGRESS_TICK in _VALID_EVENT_TYPES
    assert (
        EVENT_TYPE_THINKING_PROGRESS_TICK == "thinking_progress_tick"
    )


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.thinking_progress_aggregator import (  # noqa: E501
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 6 thresholds.
    assert n == 7
    names = {c["name"] for c in reg.calls}
    assert "JARVIS_THINKING_PROGRESS_ENABLED" in names
