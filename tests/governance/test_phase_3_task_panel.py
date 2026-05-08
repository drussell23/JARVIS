"""Phase 3 (PRD §37 v2.55→v2.56, 2026-05-07) — persistent
task-panel aggregator + bottom_toolbar integration regression
spine.

Covers:

  * TaskPanelGlyph closed 3-value taxonomy
  * glyph_char canonical mapping
  * derive_label heuristic (summary/lines/fallback priority)
  * short_op_id env-overridable
  * aggregate_panel_entries composes OpBlockBuffer canonical
    sources (active_blocks + recently_committed)
  * format_task_panel multi-line render
  * bottom_toolbar callable merges raw + panel + status
  * 5 AST pins (master_default_false / glyph_taxonomy_3_values
    / authority_asymmetry / composes_canonical_op_block_buffer
    / no_hardcoded_glyphs)
  * OpBlockBuffer.blocks_by_state + recently_committed canonical
    accessors (Phase 3 read-API extension)
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Test isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_phase3(monkeypatch):
    monkeypatch.delenv("JARVIS_TASK_PANEL_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_TASK_PANEL_MAX_LINES", raising=False,
    )
    monkeypatch.delenv(
        "JARVIS_TASK_PANEL_RECENT_COMMIT_FADE_S", raising=False,
    )
    from backend.core.ouroboros.battle_test import (
        op_block_buffer as obb,
    )
    obb.reset_default_buffer_for_tests()
    yield
    obb.reset_default_buffer_for_tests()


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "TRUE", "yes", "on"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_TASK_PANEL_ENABLED", value)
    assert master_enabled() is True


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_glyph_taxonomy_3_values():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        TaskPanelGlyph,
    )
    members = {m.name for m in TaskPanelGlyph}
    assert members == {"IN_PROGRESS", "PENDING", "COMPLETED"}


def test_glyph_char_canonical_mapping():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        TaskPanelGlyph,
        glyph_char,
    )
    assert glyph_char(TaskPanelGlyph.IN_PROGRESS) == "■"
    assert glyph_char(TaskPanelGlyph.PENDING) == "□"
    assert glyph_char(TaskPanelGlyph.COMPLETED) == "✓"


# ---------------------------------------------------------------------------
# derive_label
# ---------------------------------------------------------------------------


def test_derive_label_prefers_summary():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    label = derive_label(
        block_lines=("⏺ Update(foo.py)",),
        summary_line="⏺ Update(foo.py)  ⎿ +12/-3 in 2 files  [42s]",
    )
    assert label.startswith("⏺ Update(foo.py)")
    assert "+12/-3" in label


def test_derive_label_falls_back_to_first_line():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    label = derive_label(
        block_lines=("⏺ Update(foo.py)", "  some detail"),
        summary_line="",
    )
    assert "⏺ Update(foo.py)" in label


def test_derive_label_strips_rich_markup():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    label = derive_label(
        block_lines=(
            "[bold green]⏺ Update[/bold green](foo.py)",
        ),
    )
    # Rich markup gone.
    assert "[bold" not in label
    assert "[/bold" not in label
    assert "⏺ Update(foo.py)" in label


def test_derive_label_strips_ansi_escapes():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    label = derive_label(
        block_lines=("\x1b[32m⏺ Update\x1b[0m(foo.py)",),
    )
    assert "\x1b" not in label
    assert "⏺ Update(foo.py)" in label


def test_derive_label_fallback_when_empty():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    label = derive_label(
        block_lines=(),
        summary_line="",
    )
    assert label == "in progress"


def test_derive_label_truncates_long_text(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_PANEL_LABEL_MAX_CHARS", "30")
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        derive_label,
    )
    long = "x" * 200
    label = derive_label(summary_line=long)
    assert len(label) == 30


# ---------------------------------------------------------------------------
# short_op_id
# ---------------------------------------------------------------------------


def test_short_op_id_truncates_to_default():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        short_op_id,
    )
    assert short_op_id("op-019d1234abcd") == "1234abcd"[-6:]


def test_short_op_id_handles_short_inputs():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        short_op_id,
    )
    # Shorter than default cap → returns as-is.
    assert short_op_id("ab") == "ab"
    assert short_op_id("") == ""


def test_short_op_id_env_overridable(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_PANEL_OP_ID_SHORT_LEN", "10")
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        short_op_id,
    )
    assert short_op_id("op-019d1234abcdef") == "1234abcdef"


# ---------------------------------------------------------------------------
# OpBlockBuffer canonical accessors (Phase 3 read-API extension)
# ---------------------------------------------------------------------------


def test_blocks_by_state_filters_correctly():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
        OpBlockState,
    )
    buf = get_default_buffer()
    buf.start_op("op-A")  # BUFFERING
    buf.start_op("op-B")
    buf.commit(op_id="op-B", summary_line="committed B")
    active = buf.blocks_by_state((OpBlockState.BUFFERING,))
    committed = buf.blocks_by_state((OpBlockState.COMMITTED,))
    assert len(active) == 1
    assert len(committed) == 1


def test_active_blocks_returns_buffering_only():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("op-A")
    buf.start_op("op-B")
    buf.commit(op_id="op-B", summary_line="done")
    active = buf.active_blocks()
    assert len(active) == 1
    assert active[0].op_id == "op-A"


def test_recently_committed_filters_by_window():
    import time as _time
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("op-old")
    buf.commit(op_id="op-old", summary_line="old")
    # Advance now beyond window.
    now_in_future = _time.monotonic() + 100.0
    recent = buf.recently_committed(
        within_seconds=30.0,
        now_monotonic=now_in_future,
    )
    assert len(recent) == 0


def test_recently_committed_includes_within_window():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("op-recent")
    buf.commit(op_id="op-recent", summary_line="recent")
    recent = buf.recently_committed(within_seconds=30.0)
    assert len(recent) == 1


def test_blocks_by_state_defensive_on_bad_inputs():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    # Empty tuple → empty result.
    assert buf.blocks_by_state(()) == ()


# ---------------------------------------------------------------------------
# aggregate_panel_entries
# ---------------------------------------------------------------------------


def test_aggregate_returns_active_first():
    """Panel order: BUFFERING first, then COMMITTED."""
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        aggregate_panel_entries,
        TaskPanelGlyph,
    )
    buf = get_default_buffer()
    buf.start_op("op-active1")
    buf.start_op("op-completed1")
    buf.commit(op_id="op-completed1", summary_line="✓ done")
    buf.start_op("op-active2")
    entries = aggregate_panel_entries()
    # Active first.
    assert entries[0].glyph == TaskPanelGlyph.IN_PROGRESS
    assert entries[1].glyph == TaskPanelGlyph.IN_PROGRESS
    # Committed second.
    assert entries[2].glyph == TaskPanelGlyph.COMPLETED


def test_aggregate_caps_at_max_lines():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        aggregate_panel_entries,
    )
    buf = get_default_buffer()
    for i in range(10):
        buf.start_op(f"op-{i}")
    entries = aggregate_panel_entries(max_lines_override=3)
    assert len(entries) == 3


def test_aggregate_empty_buffer_returns_empty():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        aggregate_panel_entries,
    )
    assert aggregate_panel_entries() == ()


# ---------------------------------------------------------------------------
# format_task_panel
# ---------------------------------------------------------------------------


def test_format_master_off_returns_empty():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        format_task_panel,
    )
    buf = get_default_buffer()
    buf.start_op("op-x")
    # Master off — empty.
    assert format_task_panel() == ""


def test_format_master_on_renders_lines(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_PANEL_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        format_task_panel,
    )
    buf = get_default_buffer()
    buf.start_op("op-019d1234abcd")
    buf.append(
        op_id="op-019d1234abcd",
        line="⏺ Update(foo.py)",
    )
    rendered = format_task_panel()
    assert "■" in rendered
    assert "⏺ Update(foo.py)" in rendered


def test_format_completed_uses_check_glyph(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_PANEL_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        format_task_panel,
    )
    buf = get_default_buffer()
    buf.start_op("op-done")
    buf.commit(op_id="op-done", summary_line="✓ committed")
    rendered = format_task_panel()
    assert "✓" in rendered


# ---------------------------------------------------------------------------
# bottom_toolbar integration
# ---------------------------------------------------------------------------


def test_bottom_toolbar_no_segments_passes_through(monkeypatch):
    """Master flags off — pass through raw unchanged
    (byte-identical pre-Phase-3 behavior)."""
    monkeypatch.delenv("JARVIS_TASK_PANEL_ENABLED", raising=False)
    monkeypatch.delenv(
        "JARVIS_OUROBOROS_STATUS_LINE_ENABLED", raising=False,
    )
    from backend.core.ouroboros.battle_test.live_status_line import (  # noqa: E501
        make_bottom_toolbar_callable,
    )
    callable_fn = make_bottom_toolbar_callable(
        lambda: "raw-output",
    )
    result = callable_fn()
    # Pass through (raw is the only segment).
    assert "raw-output" in str(getattr(result, "value", result))


def test_bottom_toolbar_merges_panel_segment(monkeypatch):
    monkeypatch.setenv("JARVIS_TASK_PANEL_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.battle_test.live_status_line import (  # noqa: E501
        make_bottom_toolbar_callable,
    )
    buf = get_default_buffer()
    buf.start_op("op-test123456")
    buf.append(op_id="op-test123456", line="⏺ Test op")
    callable_fn = make_bottom_toolbar_callable(
        lambda: "raw-line",
    )
    result = callable_fn()
    text = str(getattr(result, "value", result))
    assert "raw-line" in text
    assert "■" in text
    assert "⏺ Test op" in text


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def _aggregator_pins():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _aggregator_source():
    return Path(
        "backend/core/ouroboros/governance/"
        "task_panel_aggregator.py"
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


def test_pin_taxonomy_fires_on_missing_glyph():
    pins = _aggregator_pins()
    pin = next(
        p for p in pins
        if "glyph_taxonomy_3_values" in p.invariant_name
    )
    bad_src = (
        "import enum\n"
        "class TaskPanelGlyph(str, enum.Enum):\n"
        "    IN_PROGRESS = 'in_progress'\n"
        # Missing PENDING + COMPLETED
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


def test_pin_composes_op_block_buffer_fires_on_missing_compose():
    pins = _aggregator_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_op_block_buffer" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations
    assert any("op_block_buffer" in v for v in violations)


# ---------------------------------------------------------------------------
# FlagRegistry seed
# ---------------------------------------------------------------------------


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.task_panel_aggregator import (  # noqa: E501
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    # Master + 4 tunables.
    assert n == 5
    names = {c["name"] for c in reg.calls}
    assert "JARVIS_TASK_PANEL_ENABLED" in names
