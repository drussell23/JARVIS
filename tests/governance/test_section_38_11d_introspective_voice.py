"""Section 38.11-D (PRD v2.67 to v2.68, 2026-05-08) -
introspective voice regression spine.

Three structural commitments verified:

  (1) Canonical NarrativeKind extended 6→7 (DREAM added).
  (2) Renderer style table covers DREAM glyph/tint.
  (3) New introspective_voice module composes canonical
      NarrativeChannel; producer-bridge emit_dream_prose
      writes DREAM frames; aggregator surfaces 4 axes.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_38_11d(monkeypatch):
    for var in (
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED",
        "JARVIS_INTROSPECTIVE_DREAM_BRIDGE_ENABLED",
        "JARVIS_INTROSPECTIVE_PANEL_ENABLED",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.battle_test import (
        narrative_channel as nc,
    )
    nc.reset_default_channel_for_tests()
    yield
    nc.reset_default_channel_for_tests()


# ============================================================ Canonical extension


def test_canonical_narrative_kind_extended_to_7():
    """The §38.11-D canonical extension MUST add DREAM
    without removing any of the original 6 values."""
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind,
    )
    values = {m.value for m in NarrativeKind}
    assert values == {
        "intent", "plan_prose", "tool_preamble",
        "thinking", "l2_repair_prose", "postmortem_prose",
        "dream",
    }


def test_dream_kind_value():
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind,
    )
    assert NarrativeKind.DREAM.value == "dream"


def test_renderer_style_table_covers_dream():
    """The renderer's _KIND_STYLES table MUST include DREAM."""
    src = Path(
        "backend/core/ouroboros/battle_test/"
        "narrative_renderer.py"
    ).read_text()
    assert "NarrativeKind.DREAM" in src
    # The style row is right above the closing brace; assert
    # both glyph + tint appear nearby.
    assert "🌙" in src
    assert "bright_magenta" in src


def test_existing_narrative_kind_pin_passes_after_extension():
    """The pre-existing AST pin in narrative_channel.py
    enforces the (now 7-value) closed taxonomy. After our
    extension it MUST still pass — extension was additive."""
    from backend.core.ouroboros.battle_test.narrative_channel import (
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    target = next(
        p for p in pins
        if p.invariant_name == "narrative_kind_taxonomy_frozen"
    )
    src = Path(target.target_file).read_text()
    tree = ast.parse(src)
    violations = target.validate(tree, src)
    assert not violations, (
        f"existing pin fired after additive extension: {violations}"
    )


# ----------------------------------------------------------- Master flag


def test_master_default_false():
    from backend.core.ouroboros.governance.introspective_voice import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", value,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        master_enabled,
    )
    assert master_enabled() is True


def test_subflags_master_off_force_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_DREAM_BRIDGE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_PANEL_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        dream_bridge_enabled, panel_enabled,
    )
    assert dream_bridge_enabled() is False
    assert panel_enabled() is False


def test_subflags_default_on_when_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        dream_bridge_enabled, panel_enabled,
    )
    assert dream_bridge_enabled() is True
    assert panel_enabled() is True


# --------------------------------------------------- Closed axis taxonomy


def test_axis_taxonomy_4_values():
    from backend.core.ouroboros.governance.introspective_voice import (
        IntrospectionAxis,
    )
    assert {m.name for m in IntrospectionAxis} == {
        "INTENT", "THINKING", "SELF_CORRECTION", "DREAM",
    }


def test_axis_coerce_lenient():
    from backend.core.ouroboros.governance.introspective_voice import (
        IntrospectionAxis,
    )
    assert (
        IntrospectionAxis.coerce("dream")
        is IntrospectionAxis.DREAM
    )
    assert IntrospectionAxis.coerce("nonsense") is IntrospectionAxis.THINKING
    assert IntrospectionAxis.coerce(None) is IntrospectionAxis.THINKING


# ---------------------------------------------------- Producer-bridge


def test_emit_dream_prose_master_off():
    from backend.core.ouroboros.governance.introspective_voice import (
        emit_dream_prose,
    )
    assert emit_dream_prose(op_id="op-1", prose="x") is False


def test_emit_dream_prose_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        FrameState, NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        emit_dream_prose,
    )
    ok = emit_dream_prose(
        op_id="op-1",
        prose="speculative blueprint: precompute coherence",
    )
    assert ok is True
    # Verify the canonical channel has a COMMITTED DREAM frame
    ch = get_default_channel()
    frames = ch.frames_by_op_kind(
        op_id="op-1",
        kind=NarrativeKind.DREAM,
        states=(FrameState.COMMITTED,),
    )
    assert len(frames) == 1
    assert "speculative" in frames[0].prose


def test_emit_dream_prose_subflag_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_DREAM_BRIDGE_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        emit_dream_prose,
    )
    assert emit_dream_prose(op_id="op-1", prose="x") is False


# ------------------------------------------------------------ Aggregator


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.introspective_voice import (
        aggregate_introspection_frames,
    )
    assert aggregate_introspection_frames() == ()


def test_aggregate_no_frames_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        aggregate_introspection_frames,
    )
    assert aggregate_introspection_frames() == ()


def test_aggregate_4_axes(monkeypatch):
    """Plant frames across all 4 axes; aggregator returns
    one IntrospectionFrame per axis with correct mapping."""
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        IntrospectionAxis, aggregate_introspection_frames,
        emit_dream_prose,
    )

    ch = get_default_channel()

    def plant(op_id, kind, prose):
        ch.start_frame(
            op_id=op_id, phase="TEST", kind=kind,
            provider="test",
        )
        ch.append_token(
            op_id=op_id, phase="TEST", kind=kind, token=prose,
        )
        ch.commit(op_id=op_id, phase="TEST", kind=kind)

    plant("op-i", NarrativeKind.INTENT, "intent prose")
    plant("op-t", NarrativeKind.THINKING, "thinking prose")
    plant("op-r", NarrativeKind.L2_REPAIR_PROSE, "repair prose")
    emit_dream_prose(op_id="op-d", prose="dream prose")

    frames = aggregate_introspection_frames(limit_per_axis=3)
    axes_seen = {f.axis for f in frames}
    assert axes_seen == {
        IntrospectionAxis.INTENT,
        IntrospectionAxis.THINKING,
        IntrospectionAxis.SELF_CORRECTION,
        IntrospectionAxis.DREAM,
    }


def test_aggregate_excludes_buffering(monkeypatch):
    """Only COMMITTED frames are aggregated. A BUFFERING
    DREAM frame (started but not committed) MUST NOT
    surface."""
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        aggregate_introspection_frames,
    )
    ch = get_default_channel()
    ch.start_frame(
        op_id="op-buf", phase="DREAM",
        kind=NarrativeKind.DREAM, provider="test",
    )
    # NOT committed — BUFFERING.
    frames = aggregate_introspection_frames()
    assert frames == ()


def test_aggregate_filtered_by_op_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        aggregate_introspection_frames,
    )
    ch = get_default_channel()

    def plant(op_id, kind, prose):
        ch.start_frame(
            op_id=op_id, phase="TEST", kind=kind, provider="test",
        )
        ch.append_token(
            op_id=op_id, phase="TEST", kind=kind, token=prose,
        )
        ch.commit(op_id=op_id, phase="TEST", kind=kind)

    plant("op-A", NarrativeKind.INTENT, "A intent")
    plant("op-B", NarrativeKind.INTENT, "B intent")
    frames = aggregate_introspection_frames(
        op_id="op-A",
    )
    op_ids = {f.op_id for f in frames}
    assert op_ids == {"op-A"}


# --------------------------------------------------------- Renderer


def test_format_panel_master_off():
    from backend.core.ouroboros.governance.introspective_voice import (
        format_introspective_voice_panel,
    )
    assert format_introspective_voice_panel() == ""


def test_format_panel_no_frames(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        format_introspective_voice_panel,
    )
    assert format_introspective_voice_panel() == ""


def test_format_panel_renders_4_axes(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspective_voice import (
        emit_dream_prose, format_introspective_voice_panel,
    )
    ch = get_default_channel()

    def plant(op_id, kind, prose):
        ch.start_frame(
            op_id=op_id, phase="TEST", kind=kind, provider="test",
        )
        ch.append_token(
            op_id=op_id, phase="TEST", kind=kind, token=prose,
        )
        ch.commit(op_id=op_id, phase="TEST", kind=kind)

    plant("op-i", NarrativeKind.INTENT, "intent here")
    plant("op-t", NarrativeKind.THINKING, "thinking here")
    plant("op-r", NarrativeKind.L2_REPAIR_PROSE, "repair here")
    emit_dream_prose(op_id="op-d", prose="dream here")

    out = format_introspective_voice_panel()
    assert "Introspective voice" in out
    # All 4 glyphs present
    assert "💭" in out
    assert "🤔" in out
    assert "🔧" in out
    assert "🌙" in out
    # All 4 prose strings present
    assert "intent here" in out
    assert "thinking here" in out
    assert "repair here" in out
    assert "dream here" in out


# --------------------------------------------- /introspect REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/something")
    assert r.matched is False


def test_repl_help_master_off():
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect help")
    assert r.ok is True
    assert "introspective" in r.text.lower()


def test_repl_panel_master_off_blocks():
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect panel")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_panel_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect panel")
    assert r.ok is True


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_dream_emits_frame(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.narrative_channel import (
        FrameState, NarrativeKind, get_default_channel,
    )
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command(
        "/introspect dream test prose",
    )
    assert r.ok is True
    ch = get_default_channel()
    frames = ch.frames_by_op_kind(
        op_id="test-dream",
        kind=NarrativeKind.DREAM,
        states=(FrameState.COMMITTED,),
    )
    assert len(frames) == 1


def test_repl_dream_requires_text(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect dream")
    assert r.ok is False


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_INTROSPECTIVE_VOICE_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.introspect_repl import (
        dispatch_introspect_command,
    )
    r = dispatch_introspect_command("/introspect gibberish")
    assert r.ok is False


# -------------------------------------------------------- AST pins


def _pins():
    from backend.core.ouroboros.governance.introspective_voice import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src(rel: str) -> str:
    return Path(rel).read_text()


def test_pins_register_5():
    assert len(_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_on_canonical_source(idx):
    pins = _pins()
    pin = pins[idx]
    src = _src(pin.target_file)
    tree = ast.parse(src)
    violations = pin.validate(tree, src)
    assert not violations, (
        f"{pin.invariant_name} fired on canonical source: "
        f"{violations}"
    )


def test_pin_master_default_false_fires():
    pin = next(
        p for p in _pins()
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
    pin = next(
        p for p in _pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad_src = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_axis_taxonomy_fires_on_missing():
    pin = next(
        p for p in _pins()
        if "axis_taxonomy" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class IntrospectionAxis(str, enum.Enum):\n"
        "    INTENT = 'intent'\n"
    )
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_composes_narrative_channel_fires():
    pin = next(
        p for p in _pins()
        if "composes_canonical_narrative_channel"
        in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# --------------------------------------------------- FlagRegistry


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.introspective_voice import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 3  # master + 2 sub-flags


# --------------------------------------- Canonical-source smokes


def test_canonical_event_type_dream_emitted_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_DREAM_EMITTED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_DREAM_EMITTED == "dream_emitted"
    assert EVENT_TYPE_DREAM_EMITTED in _VALID_EVENT_TYPES


def test_canonical_dream_kind_present_at_runtime():
    """Bytes-pin: even after refactoring, NarrativeKind.DREAM
    MUST exist — emit_dream_prose depends on it."""
    from backend.core.ouroboros.battle_test.narrative_channel import (
        NarrativeKind,
    )
    assert hasattr(NarrativeKind, "DREAM")
    assert NarrativeKind.DREAM.value == "dream"
