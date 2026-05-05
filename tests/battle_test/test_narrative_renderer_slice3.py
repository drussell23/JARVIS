"""Tests for narrative_renderer (Gap #6 Slice 3)."""
from __future__ import annotations

from unittest import mock

import pytest
from rich.console import Console

from backend.core.ouroboros.battle_test.narrative_channel import (
    FrameState,
    NarrativeFrame,
    NarrativeKind,
)
from backend.core.ouroboros.battle_test.narrative_renderer import (
    FrameStyle,
    NARRATIVE_RENDERER_SCHEMA_VERSION,
    RenderedFrame,
    compose,
    get_style,
    render_to_console,
)


def _make_frame(
    *,
    op_id: str = "op-x",
    phase: str = "PLAN",
    kind: NarrativeKind = NarrativeKind.PLAN_PROSE,
    prose: str = "I'll start by reading the auth module",
    state: FrameState = FrameState.COMMITTED,
    provider: str = "claude",
) -> NarrativeFrame:
    return NarrativeFrame(
        ref="n-1", op_id=op_id, phase=phase, kind=kind,
        provider=provider, prose=prose, state=state,
        started_at=0.0, terminal_at=1.0,
    )


# ===========================================================================
# Schema
# ===========================================================================


def test_schema_version_pinned():
    assert NARRATIVE_RENDERER_SCHEMA_VERSION == "narrative_renderer.v1"


# ===========================================================================
# get_style — closed dispatch covers every NarrativeKind
# ===========================================================================


@pytest.mark.parametrize("kind", list(NarrativeKind))
def test_every_kind_has_explicit_style(kind):
    """Constraint 1 — visual hierarchy. Every kind must have an
    EXPLICIT style entry (not the fallback)."""
    style = get_style(kind)
    assert isinstance(style, FrameStyle)
    assert style.glyph
    assert style.tint
    assert style.italic is True


def test_intent_and_plan_share_thinking_glyph():
    """💭 is the model voice — used for INTENT and PLAN_PROSE both."""
    assert get_style(NarrativeKind.INTENT).glyph == "💭"
    assert get_style(NarrativeKind.PLAN_PROSE).glyph == "💭"


def test_tool_preamble_has_speaker_glyph():
    """🗣 = speaking — distinguishes per-tool narration from planning."""
    assert get_style(NarrativeKind.TOOL_PREAMBLE).glyph == "🗣"


def test_thinking_uses_thinker_glyph():
    """🤔 = extended-thinking REASONING_TOKEN content."""
    assert get_style(NarrativeKind.THINKING).glyph == "🤔"


def test_l2_repair_uses_wrench_yellow():
    """Repair narrative gets a wrench in yellow — operator must
    distinguish repair iteration from normal flow."""
    style = get_style(NarrativeKind.L2_REPAIR_PROSE)
    assert style.glyph == "🔧"
    assert "yellow" in style.tint


def test_postmortem_uses_skull_red():
    """💀 + red — failed-op narrative is visually loud."""
    style = get_style(NarrativeKind.POSTMORTEM_PROSE)
    assert style.glyph == "💀"
    assert "red" in style.tint


def test_unknown_kind_falls_back():
    """Forward-compat: pass non-enum input → fallback style, no crash."""
    style = get_style("unknown")
    assert style.glyph == "💭"


# ===========================================================================
# compose — produces Rich markup
# ===========================================================================


def test_compose_returns_rendered_frame():
    rendered = compose(_make_frame())
    assert isinstance(rendered, RenderedFrame)
    assert rendered.schema_version == NARRATIVE_RENDERER_SCHEMA_VERSION


def test_compose_includes_glyph_in_markup():
    rendered = compose(_make_frame(kind=NarrativeKind.INTENT))
    assert "💭" in rendered.markup


def test_compose_includes_prose():
    rendered = compose(
        _make_frame(prose="I need to verify JWT issuer"),
    )
    assert "verify JWT issuer" in rendered.markup


def test_compose_includes_italic_style():
    """Constraint 1 — italic is the structural marker for model voice."""
    rendered = compose(_make_frame())
    assert "italic" in rendered.markup


def test_compose_includes_tint():
    """Verify the tint from the dispatch table reaches the markup."""
    rendered = compose(_make_frame(kind=NarrativeKind.PLAN_PROSE))
    assert "bright_blue" in rendered.markup


def test_compose_op_active_uses_side_rail_indent():
    """Constraint 3 — flowing under the op-block ┃ rail."""
    rendered = compose(_make_frame(), op_active=True)
    # The ``  │  `` indent is the structural marker
    assert rendered.markup.startswith("  │  ")


def test_compose_op_inactive_uses_flush_indent():
    rendered = compose(_make_frame(), op_active=False)
    assert rendered.markup.startswith("  ")
    assert not rendered.markup.startswith("  │")


def test_compose_empty_prose_returns_empty_markup():
    """DISCARDED / empty frames render nothing — Constraint 3."""
    rendered = compose(_make_frame(prose=""))
    assert rendered.markup == ""


def test_compose_whitespace_only_prose_returns_empty():
    rendered = compose(_make_frame(prose="   \n  "))
    assert rendered.markup == ""


def test_compose_escapes_brackets():
    """Rich treats ``[`` as markup; raw bracket in prose must escape."""
    rendered = compose(_make_frame(prose="see [here] for context"))
    # Escaped form must appear; raw `[here]` would be interpreted as a
    # Rich style.
    assert "\\[here]" in rendered.markup


def test_compose_handles_non_frame_input():
    rendered = compose("not a frame")  # type: ignore[arg-type]
    assert rendered.markup == ""


# ===========================================================================
# Line wrapping (Constraint 3 — no clutter)
# ===========================================================================


def test_compose_no_wrap_when_max_chars_zero():
    long_prose = "word " * 50  # 250 chars
    rendered = compose(
        _make_frame(prose=long_prose), max_chars_per_line=0,
    )
    # Single line (no \n inside the prose section beyond markup tags)
    # The rendered.markup may contain inner control chars but no
    # multi-line wrap.
    line_count = rendered.markup.count("\n") + 1
    assert line_count == 1


def test_compose_wraps_at_max_chars():
    prose = " ".join(["word"] * 30)  # 30 words × 5 chars = ~150 chars
    rendered = compose(
        _make_frame(prose=prose), max_chars_per_line=40,
    )
    lines = rendered.markup.split("\n")
    assert len(lines) > 1


def test_compose_wrapped_continuation_indent_aligns():
    """Continuation lines must align under the prose, not under the
    glyph — Constraint 3."""
    prose = "first second third fourth fifth sixth seventh eighth"
    rendered = compose(
        _make_frame(prose=prose), max_chars_per_line=20, op_active=True,
    )
    lines = rendered.markup.split("\n")
    if len(lines) > 1:
        # Continuation has more indent than the leading line
        assert lines[1].startswith("  │     ")


# ===========================================================================
# render_to_console — end-to-end via Console(record=True)
# ===========================================================================


def test_render_to_console_emits_markup():
    """End-to-end: Rich Console captures the styled output."""
    console = Console(record=True, force_terminal=True, color_system="truecolor")
    ok = render_to_console(
        _make_frame(prose="JWT validation needs an issuer check"),
        console,
    )
    assert ok is True
    text = console.export_text()
    assert "💭" in text
    assert "JWT validation" in text


def test_render_to_console_returns_false_for_empty_frame():
    console = Console(record=True, force_terminal=True)
    ok = render_to_console(_make_frame(prose=""), console)
    assert ok is False


def test_render_to_console_handles_console_exception():
    """Console.print raising must NOT propagate."""
    bad_console = mock.Mock()
    bad_console.print.side_effect = RuntimeError("console blew up")
    ok = render_to_console(_make_frame(), bad_console)
    assert ok is False  # error swallowed


def test_render_to_console_handles_non_console_input():
    """Non-Console object (no .print attr) → False, not crash."""
    ok = render_to_console(_make_frame(), object())
    assert ok is False


# ===========================================================================
# Visual hierarchy — distinct from system actions (Constraint 1)
# ===========================================================================


def test_intent_tint_distinct_from_system_cyan():
    """The model-voice tint must NOT collide with the system 'neural'
    cyan used by serpent_flow's _C palette. Operators must
    structurally tell them apart."""
    style = get_style(NarrativeKind.INTENT)
    assert style.tint != "cyan"
    # bright_blue is distinct from cyan — operators perceive a hue shift
    assert "blue" in style.tint or "black" in style.tint


def test_tool_preamble_tint_quieter_than_intent():
    """🗣 (tool preamble) should be quieter than 💭 (intent / plan):
    bright_black gray vs bright_blue. Operators see intent first."""
    intent = get_style(NarrativeKind.INTENT)
    preamble = get_style(NarrativeKind.TOOL_PREAMBLE)
    assert intent.tint != preamble.tint
