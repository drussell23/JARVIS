"""Tests for split_layout (Slice 2)."""
from __future__ import annotations

import io
import sys

import pytest

from backend.core.ouroboros.battle_test.layout_controller import (
    LayoutController,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
)
from backend.core.ouroboros.battle_test.split_layout import (
    RegionBuffer,
    SPLIT_LAYOUT_SCHEMA_VERSION,
    SplitLayout,
    reset_default_split_layout,
    get_default_split_layout,
)


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_pinned():
    assert SPLIT_LAYOUT_SCHEMA_VERSION == "serpent_split_layout.v1"


# ===========================================================================
# RegionBuffer bounded deque
# ===========================================================================


def test_region_buffer_push_appends():
    b = RegionBuffer(name="stream", maxlen=4)
    b.push("a")
    b.push("b")
    assert b.snapshot() == ("a", "b")


def test_region_buffer_bounded_drops_oldest():
    b = RegionBuffer(name="stream", maxlen=3)
    for line in "abcdef":
        b.push(line)
    # Only last 3 survive
    assert b.snapshot() == ("d", "e", "f")


def test_region_buffer_counts_total_pushes():
    b = RegionBuffer(name="stream", maxlen=2)
    for line in "abcd":
        b.push(line)
    # Actual stored lines capped but push_count is total
    assert b.push_count == 4
    assert b.line_count == 2


def test_region_buffer_clear():
    b = RegionBuffer(name="stream", maxlen=4)
    b.push("a")
    b.clear()
    assert b.snapshot() == ()
    # push_count is preserved (historical counter)
    assert b.push_count == 1


def test_region_buffer_as_text_joins_with_newline():
    b = RegionBuffer(name="stream", maxlen=4)
    b.push("a")
    b.push("b")
    assert b.as_text() == "a\nb"


# ===========================================================================
# SplitLayout — headless (no Rich, no TTY)
# ===========================================================================


def _split(monkeypatch=None) -> SplitLayout:
    if monkeypatch is not None:
        monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    # Headless-safe: an in-memory stream is not a TTY, so start()
    # returns False and buffers work purely.
    return SplitLayout(
        controller=LayoutController(initial_mode=MODE_FLOW),
        output_stream=io.StringIO(),
    )


def test_split_layout_push_writes_to_region_buffer():
    s = _split()
    assert s.push(REGION_STREAM, "hello") is True
    snap = s.snapshot()
    assert snap[REGION_STREAM] == ("hello",)
    assert snap[REGION_DASHBOARD] == ()


def test_split_layout_push_rejects_unknown_region():
    s = _split()
    assert s.push("made_up", "x") is False


def test_split_layout_visible_regions_in_flow():
    c = LayoutController(initial_mode=MODE_FLOW)
    s = SplitLayout(controller=c, output_stream=io.StringIO())
    # Flow mode → no regions visible
    assert s.visible_regions() == []


def test_split_layout_visible_regions_in_split():
    c = LayoutController(initial_mode=MODE_SPLIT)
    s = SplitLayout(controller=c, output_stream=io.StringIO())
    assert set(s.visible_regions()) == {
        REGION_STREAM, REGION_DASHBOARD, REGION_DIFF,
    }


def test_split_layout_visible_regions_in_focus():
    c = LayoutController(initial_mode="focus:diff")
    s = SplitLayout(controller=c, output_stream=io.StringIO())
    assert s.visible_regions() == [REGION_DIFF]


def test_split_layout_visible_regions_respond_to_mode_change():
    c = LayoutController(initial_mode=MODE_FLOW)
    s = SplitLayout(controller=c, output_stream=io.StringIO())
    assert s.visible_regions() == []
    c.to_split()
    assert set(s.visible_regions()) == {
        REGION_STREAM, REGION_DASHBOARD, REGION_DIFF,
    }
    c.to_focus(REGION_STREAM)
    assert s.visible_regions() == [REGION_STREAM]


def test_split_layout_start_returns_false_on_nontty():
    s = _split()
    assert s.start() is False
    assert s.active is False


def test_split_layout_clear_single_region():
    s = _split()
    s.push(REGION_STREAM, "a")
    s.push(REGION_DASHBOARD, "b")
    s.clear(REGION_STREAM)
    snap = s.snapshot()
    assert snap[REGION_STREAM] == ()
    assert snap[REGION_DASHBOARD] == ("b",)


def test_split_layout_clear_all_regions():
    s = _split()
    s.push(REGION_STREAM, "a")
    s.push(REGION_DASHBOARD, "b")
    s.clear()
    snap = s.snapshot()
    for region in (REGION_STREAM, REGION_DASHBOARD, REGION_DIFF):
        assert snap[region] == ()


def test_split_layout_stats_reports_counts():
    s = _split()
    s.push(REGION_STREAM, "a")
    s.push(REGION_STREAM, "b")
    s.push(REGION_DIFF, "d")
    stats = s.stats()
    assert stats[REGION_STREAM]["push_count"] == 2
    assert stats[REGION_STREAM]["line_count"] == 2
    assert stats[REGION_DIFF]["push_count"] == 1


def test_split_layout_respects_max_lines_env(monkeypatch):
    # 15 is above the 10-line floor but below default — so we
    # exercise the env-configured cap without bumping into the floor.
    monkeypatch.setenv("JARVIS_SPLIT_LAYOUT_MAX_LINES", "15")
    s = SplitLayout(
        controller=LayoutController(initial_mode=MODE_FLOW),
        output_stream=io.StringIO(),
    )
    for i in range(30):
        s.push(REGION_STREAM, f"line-{i}")
    snap = s.snapshot()[REGION_STREAM]
    # Capped to 15 — drop oldest first
    assert len(snap) == 15
    assert snap[-1] == "line-29"


def test_split_layout_max_lines_floor_is_10(monkeypatch):
    """Operators can't set a pathologically tiny buffer."""
    monkeypatch.setenv("JARVIS_SPLIT_LAYOUT_MAX_LINES", "1")
    s = SplitLayout(
        controller=LayoutController(initial_mode=MODE_FLOW),
        output_stream=io.StringIO(),
    )
    for i in range(15):
        s.push(REGION_STREAM, f"line-{i}")
    assert len(s.snapshot()[REGION_STREAM]) == 10


def test_split_layout_start_idempotent_when_already_inactive():
    s = _split()
    assert s.start() is False
    # A second call is also False + stays inert.
    assert s.start() is False


def test_split_layout_stop_before_start_is_safe():
    s = _split()
    # stop() without a prior successful start() must not raise.
    s.stop()
    assert s.active is False


# ===========================================================================
# Visibility: no-controller case defaults to everything
# ===========================================================================


def test_split_layout_no_controller_defaults_to_all_visible():
    s = SplitLayout(controller=None, output_stream=io.StringIO())
    assert set(s.visible_regions()) == {
        REGION_STREAM, REGION_DASHBOARD, REGION_DIFF,
    }


# ===========================================================================
# Singleton
# ===========================================================================


def test_singleton_returns_same_instance(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    reset_default_split_layout()
    try:
        a = get_default_split_layout()
        b = get_default_split_layout()
        assert a is b
    finally:
        reset_default_split_layout()


def test_singleton_reset_spawns_fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_SERPENT_LAYOUT_DEFAULT", raising=False)
    reset_default_split_layout()
    try:
        a = get_default_split_layout()
        reset_default_split_layout()
        b = get_default_split_layout()
        assert a is not b
    finally:
        reset_default_split_layout()


# ===========================================================================
# Defensive: push never raises, even in weird concurrent patterns
# ===========================================================================


def test_push_is_safe_during_mode_transitions():
    c = LayoutController(initial_mode=MODE_FLOW)
    s = SplitLayout(controller=c, output_stream=io.StringIO())
    s.push(REGION_STREAM, "before-split")
    c.to_split()
    s.push(REGION_DASHBOARD, "during-split")
    c.to_focus(REGION_DIFF)
    s.push(REGION_DIFF, "during-focus")
    c.to_flow()
    s.push(REGION_STREAM, "after-flow")
    snap = s.snapshot()
    assert "before-split" in snap[REGION_STREAM]
    assert "during-split" in snap[REGION_DASHBOARD]
    assert "during-focus" in snap[REGION_DIFF]
    assert "after-flow" in snap[REGION_STREAM]
