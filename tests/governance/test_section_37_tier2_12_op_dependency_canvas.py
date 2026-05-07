"""§37 Tier 2 #12 — Op dependency graph + parallel fan-out canvas.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (~38 tests):
  Slice 1 — OpBlock fan-out fields + register_parent
    * OpBlock has 4 fan-out fields (parent_op_id /
      candidate_index / subagent_kind / child_op_ids)
    * Defaults preserve backward compat (existing constructors
      yield neutral values)
    * is_root / fan_out_size derived properties
    * to_dict includes fan-out fields
    * Master flag default-FALSE per §33.1
    * register_parent no-op when master off
    * register_parent atomically updates child + parent
    * register_parent rejects empty / self-parent
    * register_parent idempotent on re-call
    * register_parent rejects unknown ops
    * get_parent_op_id / get_child_op_ids accessors
    * find_root_ops returns ops with no parent
    * walk_subtree BFS order + depth clamp + cycle defense
    * 2 AST pins clean + each fires on synthetic regression

  Slice 2 — /canvas REPL verb
    * Auto-discovery (matches=False on unrelated lines)
    * /canvas help / bare overview / tree / op / json / dot /
      fanout / unknown subcommand
    * Disabled message when master flag off
    * /canvas op rejects missing op-id
    * Tree rendering: ASCII branch glyphs (`├─` `└─` `│`)
    * Tree rendering: handles evicted children gracefully
    * JSON output includes schema_version + ops list
    * DOT output: digraph header + node defs + edge defs
    * Fanout subcommand filters to ops with ≥2 children
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/battle_test/"
        "op_block_buffer.py"
    )


@pytest.fixture(autouse=True)
def _reset_buffer():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        reset_default_buffer_for_tests,
    )
    reset_default_buffer_for_tests()
    yield
    reset_default_buffer_for_tests()


# ---------------------------------------------------------------------------
# Slice 1 — OpBlock dataclass extensions
# ---------------------------------------------------------------------------


def test_op_block_has_fan_out_fields():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlock,
    )
    fields = {
        f.name
        for f in OpBlock.__dataclass_fields__.values()
    }
    assert {
        "parent_op_id", "candidate_index",
        "subagent_kind", "child_op_ids",
    }.issubset(fields)


def test_op_block_fan_out_defaults_neutral():
    """Backward compat: existing constructors that don't pass
    fan-out fields still yield neutral defaults."""
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlock,
    )
    b = OpBlock(ref="o-1", op_id="op-1")
    assert b.parent_op_id == ""
    assert b.candidate_index == 0
    assert b.subagent_kind == ""
    assert b.child_op_ids == ()


def test_op_block_is_root_and_fan_out_size():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlock,
    )
    root = OpBlock(ref="o-1", op_id="op-1")
    assert root.is_root is True
    assert root.fan_out_size == 0
    leaf = OpBlock(
        ref="o-2", op_id="op-2", parent_op_id="op-1",
    )
    assert leaf.is_root is False
    parent = OpBlock(
        ref="o-3", op_id="op-3",
        child_op_ids=("op-2", "op-4"),
    )
    assert parent.fan_out_size == 2


def test_op_block_to_dict_includes_fan_out_fields():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlock,
    )
    b = OpBlock(
        ref="o-1", op_id="op-1",
        parent_op_id="op-0",
        candidate_index=2,
        subagent_kind="explore",
        child_op_ids=("op-A", "op-B"),
    )
    d = b.to_dict()
    assert d["parent_op_id"] == "op-0"
    assert d["candidate_index"] == 2
    assert d["subagent_kind"] == "explore"
    assert d["child_op_ids"] == ["op-A", "op-B"]
    assert d["is_root"] is False
    assert d["fan_out_size"] == 2


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        op_dependency_graph_enabled,
    )
    assert op_dependency_graph_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        op_dependency_graph_enabled,
    )
    for v in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv(
            "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", v,
        )
        assert op_dependency_graph_enabled() is True


# ---------------------------------------------------------------------------
# register_parent
# ---------------------------------------------------------------------------


def test_register_parent_noop_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("parent-1")
    buf.start_op("child-1")
    ok = buf.register_parent(
        child_op_id="child-1", parent_op_id="parent-1",
    )
    assert ok is False
    # Child still root.
    assert buf.get_parent_op_id("child-1") == ""


def test_register_parent_atomic_update(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("parent-1")
    buf.start_op("child-1")
    ok = buf.register_parent(
        child_op_id="child-1", parent_op_id="parent-1",
        candidate_index=0, subagent_kind="explore",
    )
    assert ok is True
    assert buf.get_parent_op_id("child-1") == "parent-1"
    assert buf.get_child_op_ids("parent-1") == ("child-1",)


def test_register_parent_rejects_self_parent(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("op-1")
    ok = buf.register_parent(
        child_op_id="op-1", parent_op_id="op-1",
    )
    assert ok is False


def test_register_parent_rejects_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("op-1")
    ok = buf.register_parent(child_op_id="", parent_op_id="op-1")
    assert ok is False
    ok2 = buf.register_parent(child_op_id="op-1", parent_op_id="")
    assert ok2 is False


def test_register_parent_rejects_unknown_op(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("known")
    ok = buf.register_parent(
        child_op_id="known", parent_op_id="missing",
    )
    assert ok is False
    ok2 = buf.register_parent(
        child_op_id="missing", parent_op_id="known",
    )
    assert ok2 is False


def test_register_parent_idempotent(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("parent-1")
    buf.start_op("child-1")
    buf.register_parent(
        child_op_id="child-1", parent_op_id="parent-1",
    )
    buf.register_parent(
        child_op_id="child-1", parent_op_id="parent-1",
    )
    buf.register_parent(
        child_op_id="child-1", parent_op_id="parent-1",
    )
    # Three calls — child still appears once.
    assert buf.get_child_op_ids("parent-1") == ("child-1",)


def test_register_parent_kway_fanout(monkeypatch):
    """Move 6 K-way pattern: one parent, K candidate
    children with distinct candidate_index."""
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("parent-K")
    for i in range(5):
        buf.start_op(f"cand-{i}")
        buf.register_parent(
            child_op_id=f"cand-{i}", parent_op_id="parent-K",
            candidate_index=i, subagent_kind="general",
        )
    parent_block = buf._find_block_by_op_id("parent-K")
    assert parent_block.fan_out_size == 5
    # Candidate indexes preserved.
    for i in range(5):
        c = buf._find_block_by_op_id(f"cand-{i}")
        assert c.candidate_index == i


# ---------------------------------------------------------------------------
# Accessors + walk_subtree
# ---------------------------------------------------------------------------


def test_find_root_ops(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("root-A")
    buf.start_op("root-B")
    buf.start_op("child-1")
    buf.register_parent(
        child_op_id="child-1", parent_op_id="root-A",
    )
    roots = buf.find_root_ops()
    root_ids = {r.op_id for r in roots}
    assert "root-A" in root_ids
    assert "root-B" in root_ids
    assert "child-1" not in root_ids


def test_walk_subtree_bfs_order(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    # Build:
    #   root → A (depth 1) → A.1, A.2 (depth 2)
    #        → B (depth 1)
    for op_id in (
        "root", "A", "B", "A1", "A2",
    ):
        buf.start_op(op_id)
    buf.register_parent(child_op_id="A", parent_op_id="root")
    buf.register_parent(child_op_id="B", parent_op_id="root")
    buf.register_parent(child_op_id="A1", parent_op_id="A")
    buf.register_parent(child_op_id="A2", parent_op_id="A")
    walked = [op.op_id for op in buf.walk_subtree("root")]
    # BFS: root, then A B (depth 1), then A1 A2 (depth 2).
    assert walked[0] == "root"
    assert set(walked[1:3]) == {"A", "B"}
    assert set(walked[3:5]) == {"A1", "A2"}


def test_walk_subtree_depth_clamp(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    # Build a deep chain of 8 ops.
    for i in range(8):
        buf.start_op(f"op-{i}")
        if i > 0:
            buf.register_parent(
                child_op_id=f"op-{i}",
                parent_op_id=f"op-{i-1}",
            )
    walked = [
        op.op_id for op in buf.walk_subtree(
            "op-0", max_depth=3,
        )
    ]
    # Root + 3 levels of descent = 4 ops max.
    assert len(walked) <= 4


def test_walk_subtree_cycle_defense(monkeypatch):
    """If a malformed register_parent ever produced a cycle,
    walk_subtree must terminate (visited-set defense)."""
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    buf.start_op("A")
    buf.start_op("B")
    # Naturally A→B. But mutate B's child_op_ids directly to
    # synthesize a cycle (real code would never do this).
    buf.register_parent(child_op_id="B", parent_op_id="A")
    # Force-mutate B's child_op_ids to include A (cycle).
    block_b = buf._find_block_by_op_id("B")
    from dataclasses import replace
    cycled = replace(block_b, child_op_ids=("A",))
    buf._items[block_b.ref] = cycled
    # Walk MUST terminate (visited-set guard).
    walked = buf.walk_subtree("A")
    assert len(walked) >= 1  # at minimum, root visited


def test_get_parent_get_children_unknown_op_returns_empty():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        OpBlockBuffer,
    )
    buf = OpBlockBuffer()
    assert buf.get_parent_op_id("unknown") == ""
    assert buf.get_child_op_ids("unknown") == ()


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "op_block_fan_out_fields_present",
        "op_dependency_master_flag_default_false",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_fan_out_pin_fires_when_field_removed():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class OpBlock:
    parent_op_id: str = ""
    candidate_index: int = 0
    # subagent_kind missing
    child_op_ids: tuple = ()
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "op_block_fan_out_fields_present"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("subagent_kind" in v for v in violations)


def test_master_flag_pin_fires_on_default_true():
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
def op_dependency_graph_enabled() -> bool:
    raw = os.environ.get("JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "").strip()
    if raw == "":
        return True
    return raw in ("1",)
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "op_dependency_master_flag_default_false"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# Slice 2 — /canvas REPL
# ---------------------------------------------------------------------------


def test_repl_unmatched_line():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/something_else")
    assert out.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas help")
    assert out.ok is True
    assert "/canvas tree" in out.text
    assert "/canvas fanout" in out.text


def test_repl_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas")
    assert out.ok is True
    assert "disabled" in out.text


def test_repl_overview_no_root_ops(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas")
    assert out.ok is True
    assert "no root ops" in out.text


def _build_demo_tree(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        get_default_buffer,
    )
    buf = get_default_buffer()
    for op_id in ("root", "child-A", "child-B", "child-C"):
        buf.start_op(op_id)
    buf.register_parent(
        child_op_id="child-A", parent_op_id="root",
        candidate_index=0, subagent_kind="explore",
    )
    buf.register_parent(
        child_op_id="child-B", parent_op_id="root",
        candidate_index=1, subagent_kind="review",
    )
    buf.register_parent(
        child_op_id="child-C", parent_op_id="root",
        candidate_index=2, subagent_kind="general",
    )
    return buf


def test_repl_overview_with_tree(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas")
    assert out.ok is True
    assert "root" in out.text
    assert "3 child" in out.text


def test_repl_tree_renders_branch_glyphs(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas tree")
    assert out.ok is True
    # ASCII branch glyph(s) present.
    assert "├" in out.text or "└" in out.text


def test_repl_op_focused_subtree(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas op root")
    assert out.ok is True
    assert "child-A" in out.text
    assert "child-B" in out.text


def test_repl_op_missing_id():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas op")
    assert out.ok is False
    assert "missing op-id" in out.text


def test_repl_op_unknown_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas op nonexistent")
    assert out.ok is False
    assert "no op found" in out.text


def test_repl_json_output(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas json")
    assert out.ok is True
    payload = json.loads(out.text)
    assert payload["schema_version"] == "canvas_repl.1"
    assert len(payload["ops"]) >= 4
    op_ids = {op["op_id"] for op in payload["ops"]}
    assert {"root", "child-A", "child-B", "child-C"} <= op_ids


def test_repl_json_focused(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas json child-A")
    assert out.ok is True
    payload = json.loads(out.text)
    op_ids = {op["op_id"] for op in payload["ops"]}
    # Subtree of leaf = just itself.
    assert op_ids == {"child-A"}


def test_repl_dot_output(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas dot")
    assert out.ok is True
    assert "digraph" in out.text
    assert "rankdir=LR" in out.text
    # Edge from root to children.
    assert "->" in out.text


def test_repl_fanout_filters_correctly(monkeypatch):
    _build_demo_tree(monkeypatch)
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas fanout")
    assert out.ok is True
    # root has 3 children → appears.
    assert "root" in out.text
    # child-A is leaf → does not appear.
    # (defensive — may appear in label text but not in fanout
    # listing; this asserts the count logic finds the parent)
    assert "1 fan-out ops" in out.text


def test_repl_fanout_no_qualifying_ops(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true",
    )
    from backend.core.ouroboros.battle_test.op_block_buffer import (  # noqa: E501
        get_default_buffer,
    )
    buf = get_default_buffer()
    buf.start_op("solo")
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas fanout")
    assert out.ok is True
    assert "no fan-out ops" in out.text


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.canvas_repl import (
        dispatch_canvas_command,
    )
    out = dispatch_canvas_command("/canvas bogus")
    assert out.ok is False
    assert "unknown subcommand" in out.text


def test_naming_collision_avoided():
    """Slice 2 ships /canvas (NOT /graph — existing Path D.1
    REPL surfaces L3 execution graphs, different scope)."""
    from backend.core.ouroboros.governance import (
        canvas_repl, graph_repl,
    )
    # Both modules ship dispatchers; verb names are distinct.
    assert hasattr(canvas_repl, "dispatch_canvas_command")
    assert hasattr(graph_repl, "dispatch_graph_command")
