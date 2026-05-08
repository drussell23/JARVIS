"""Section 38 Slice 5 (PRD v2.61 to v2.62, 2026-05-07) -
op fan-out tree regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_slice_5(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OP_FANOUT_TREE_ENABLED", raising=False,
    )
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test import (
        op_block_buffer as obb,
    )
    obb.reset_default_buffer_for_tests()
    yield
    obb.reset_default_buffer_for_tests()


# Master flag


def test_master_flag_default_false():
    from backend.core.ouroboros.governance.op_fanout_tree import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_flag_truthy(monkeypatch, value):
    from backend.core.ouroboros.governance.op_fanout_tree import (
        master_enabled,
    )
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", value)
    assert master_enabled() is True


# Aggregation


def test_aggregate_empty_returns_empty():
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    rows = aggregate_fanout_rows()
    assert rows == ()


def test_aggregate_single_op_no_children():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    buf.start_op("op-solo")
    rows = aggregate_fanout_rows()
    assert len(rows) == 1
    assert rows[0].depth == 0
    assert rows[0].op_id == "op-solo"


def test_aggregate_parent_with_children():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    buf.start_op("op-p")
    buf.start_op("op-c1")
    buf.register_parent(
        child_op_id="op-c1",
        parent_op_id="op-p",
        candidate_index=0,
        subagent_kind="explore",
    )
    buf.start_op("op-c2")
    buf.register_parent(
        child_op_id="op-c2",
        parent_op_id="op-p",
        candidate_index=1,
        subagent_kind="review",
    )
    rows = aggregate_fanout_rows()
    assert len(rows) == 3
    # Root first.
    assert rows[0].depth == 0
    assert rows[0].op_id == "op-p"
    # Children at depth 1.
    child_rows = [r for r in rows if r.depth == 1]
    assert len(child_rows) == 2
    assert {r.op_id for r in child_rows} == {"op-c1", "op-c2"}


def test_aggregate_grandchild_depth():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    buf.start_op("op-p")
    buf.start_op("op-c")
    buf.register_parent(
        child_op_id="op-c",
        parent_op_id="op-p",
    )
    buf.start_op("op-g")
    buf.register_parent(
        child_op_id="op-g",
        parent_op_id="op-c",
    )
    rows = aggregate_fanout_rows()
    assert len(rows) == 3
    by_op = {r.op_id: r for r in rows}
    assert by_op["op-p"].depth == 0
    assert by_op["op-c"].depth == 1
    assert by_op["op-g"].depth == 2


def test_aggregate_max_depth_override(monkeypatch):
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    # Build 5-deep chain.
    buf.start_op("op-d0")
    for i in range(1, 5):
        buf.start_op(f"op-d{i}")
        buf.register_parent(
            child_op_id=f"op-d{i}",
            parent_op_id=f"op-d{i-1}",
        )
    rows = aggregate_fanout_rows(max_depth_override=2)
    # Walk subtree limits to 2 levels (depth 0, 1, 2).
    depths = {r.depth for r in rows}
    assert max(depths) <= 2


def test_aggregate_max_total_lines_override():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    buf.start_op("op-p")
    for i in range(10):
        buf.start_op(f"op-c{i}")
        buf.register_parent(
            child_op_id=f"op-c{i}",
            parent_op_id="op-p",
        )
    rows = aggregate_fanout_rows(max_total_lines_override=4)
    assert len(rows) <= 4


def test_aggregate_root_filter():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        aggregate_fanout_rows,
    )
    buf = get_default_buffer()
    buf.start_op("op-r1")
    buf.start_op("op-r2")
    buf.start_op("op-r3")
    rows = aggregate_fanout_rows(
        root_op_ids_filter=("op-r2",),
    )
    assert len(rows) == 1
    assert rows[0].op_id == "op-r2"


# Format render


def test_format_master_off_returns_empty():
    from backend.core.ouroboros.governance.op_fanout_tree import (
        format_fanout_tree,
    )
    assert format_fanout_tree() == ""


def test_format_no_fanout_returns_empty(monkeypatch):
    """When all ops are roots with no children, no fan-out
    structure exists — render returns empty (caller falls back
    to flat panel)."""
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        format_fanout_tree,
    )
    buf = get_default_buffer()
    buf.start_op("op-flat-1")
    buf.start_op("op-flat-2")
    rendered = format_fanout_tree()
    assert rendered == ""


def test_format_single_parent_two_children(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        format_fanout_tree,
    )
    buf = get_default_buffer()
    buf.start_op("op-019dparent")
    buf.append(
        op_id="op-019dparent",
        line="Update(file.py)",
    )
    buf.start_op("op-019dchild1")
    buf.register_parent(
        child_op_id="op-019dchild1",
        parent_op_id="op-019dparent",
        subagent_kind="explore",
    )
    buf.start_op("op-019dchild2")
    buf.register_parent(
        child_op_id="op-019dchild2",
        parent_op_id="op-019dparent",
        subagent_kind="review",
    )
    rendered = format_fanout_tree()
    assert rendered  # non-empty
    # Root marker + 2 branch glyphs (├─ and └─ for last child).
    assert "● " in rendered
    assert "├─" in rendered
    assert "└─" in rendered
    assert "explore" in rendered
    assert "review" in rendered


def test_format_three_level_tree(monkeypatch):
    """3-level tree renders with vertical-bar continuation."""
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    from backend.core.ouroboros.governance.op_fanout_tree import (
        format_fanout_tree,
    )
    buf = get_default_buffer()
    buf.start_op("op-r")
    buf.start_op("op-c")
    buf.register_parent(
        child_op_id="op-c", parent_op_id="op-r",
    )
    buf.start_op("op-g")
    buf.register_parent(
        child_op_id="op-g", parent_op_id="op-c",
    )
    rendered = format_fanout_tree()
    assert rendered
    lines = rendered.split("\n")
    assert len(lines) == 3
    # Root, child (depth 1), grandchild (depth 2).
    assert "● " in lines[0]
    assert "└─" in lines[1] or "├─" in lines[1]
    # Grandchild has indented branch glyph.
    assert "└─" in lines[2] or "├─" in lines[2]


# /fanout REPL


def test_fanout_repl_unmatched_returns_matched_false():
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/something_else")
    assert r.matched is False


def test_fanout_repl_help_master_off():
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout help")
    assert r.ok is True
    assert "fan-out" in r.text.lower()


def test_fanout_repl_show_master_off_blocks():
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout show")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_fanout_repl_show_master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout show")
    assert r.ok is True


def test_fanout_repl_status(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_fanout_repl_depth_with_arg(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout depth 4")
    assert r.ok is True


def test_fanout_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    from backend.core.ouroboros.governance.fanout_repl import (
        dispatch_fanout_command,
    )
    r = dispatch_fanout_command("/fanout gibberish")
    assert r.ok is False


# AST pins


def _fanout_pins():
    from backend.core.ouroboros.governance.op_fanout_tree import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _fanout_source():
    return Path(
        "backend/core/ouroboros/governance/op_fanout_tree.py"
    ).read_text()


def test_pins_register_exactly_4():
    pins = _fanout_pins()
    assert len(pins) == 4


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_pin_passes_on_canonical_source(idx):
    pins = _fanout_pins()
    src = _fanout_source()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_default_false_fires_on_premature_flip():
    pins = _fanout_pins()
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
    pins = _fanout_pins()
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


def test_pin_composes_op_block_buffer_fires_on_missing():
    pins = _fanout_pins()
    pin = next(
        p for p in pins
        if "composes_canonical_op_block_buffer" in p.invariant_name
    )
    bad_src = "x = 1\n"
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


def test_pin_no_hardcoded_glyphs_fires_on_missing_accessor():
    pins = _fanout_pins()
    pin = next(
        p for p in pins
        if "no_hardcoded_glyphs" in p.invariant_name
    )
    bad_src = "x = 1\n"  # no accessor functions
    bad_tree = ast.parse(bad_src)
    violations = pin.validate(bad_tree, bad_src)
    assert violations


# FlagRegistry seed


def test_register_flags_returns_count():
    from backend.core.ouroboros.governance.op_fanout_tree import (
        register_flags,
    )

    class _MockRegistry:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _MockRegistry()
    n = register_flags(reg)
    assert n == 5


# Composition


def test_canonical_op_block_buffer_importable():
    from backend.core.ouroboros.battle_test.op_block_buffer import (
        get_default_buffer,
    )
    buf = get_default_buffer()
    assert buf is not None
    assert hasattr(buf, "find_root_ops")
    assert hasattr(buf, "walk_subtree")
    assert hasattr(buf, "get_child_op_ids")
