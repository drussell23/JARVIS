"""The causality tree must draw the parentage it recorded.

The op-DAG view was complete: a contextvar, an acyclic-guarded
`register_parent`, a 777-line renderer and a verb. Two master flags gate it
(`JARVIS_OP_DEPENDENCY_GRAPH_ENABLED` records the edges,
`JARVIS_OP_FANOUT_TREE_ENABLED` renders them), both default false.

Turned on, it drew the wrong tree. `walk_subtree` promises BREADTH-first and
other callers rely on that; `format_fanout_tree` indents by `depth` IN ROW
ORDER, which needs DEPTH-first. Fed BFS, a grandchild emitted after its uncle
renders nested under the uncle.

Not cosmetic. The tree's entire claim is "this op caused that one", and drawn
from BFS rows it asserts a parentage the graph does not record — a surface
confidently showing the wrong answer, which is worse than showing none.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("_flags")


@pytest.fixture
def _flags(monkeypatch):
    monkeypatch.setenv("JARVIS_OP_DEPENDENCY_GRAPH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_OP_FANOUT_TREE_ENABLED", "true")
    yield


@pytest.fixture
def graph():
    """A > (B > D), C — the shape that exposes the ordering."""
    from backend.core.ouroboros.battle_test import op_block_buffer as OB

    OB.reset_default_buffer_for_tests() if hasattr(
        OB, "reset_default_buffer_for_tests") else None
    buf = OB.get_default_buffer()
    buf.start_op("A")
    with OB.executing("A"):
        buf.start_op("B")
        buf.start_op("C")
        with OB.executing("B"):
            buf.start_op("D")
    return buf


class TestTheGraphIsRecorded:
    def test_edges_register_through_the_contextvar(self, graph):
        """`executing()` sets the parent; `start_op` links it. Nobody has to
        thread `parent_op_id` through a signature."""
        from backend.core.ouroboros.governance import op_fanout_tree as F

        by_op = {r.op_id: r for r in F.aggregate_fanout_rows()}
        assert by_op["B"].parent_op_id == "A"
        assert by_op["C"].parent_op_id == "A"
        assert by_op["D"].parent_op_id == "B"

    def test_depths_are_computed_from_the_chain(self, graph):
        from backend.core.ouroboros.governance import op_fanout_tree as F

        by_op = {r.op_id: r for r in F.aggregate_fanout_rows()}
        assert (by_op["A"].depth, by_op["B"].depth, by_op["D"].depth) == (0, 1, 2)


class TestRowOrder:
    def test_rows_are_depth_first(self, graph):
        """THE fix. BFS gives A B C D; rendering that depth-indented puts D
        under C, which is not D's parent."""
        from backend.core.ouroboros.governance import op_fanout_tree as F

        assert [r.op_id for r in F.aggregate_fanout_rows()] == ["A", "B", "D", "C"]

    def test_sibling_order_is_preserved(self, graph):
        """BFS order is SPAWN order, and that is meaningful — the first
        subagent dispatched should read first."""
        from backend.core.ouroboros.governance import op_fanout_tree as F

        order = [r.op_id for r in F.aggregate_fanout_rows()]
        assert order.index("B") < order.index("C")

    def test_the_child_follows_its_own_parent(self, graph):
        from backend.core.ouroboros.governance import op_fanout_tree as F

        order = [r.op_id for r in F.aggregate_fanout_rows()]
        assert order[order.index("B") + 1] == "D"


class TestRendering:
    def test_the_grandchild_nests_under_its_parent(self, graph):
        from backend.core.ouroboros.governance import op_fanout_tree as F

        lines = F.format_fanout_tree(F.aggregate_fanout_rows()).splitlines()
        assert lines[0].startswith("●") and " A" in lines[0]
        assert lines[1].lstrip().startswith("├─") and " B" in lines[1]
        assert " D" in lines[2], f"D did not follow B: {lines}"
        assert lines[3].lstrip().startswith("└─") and " C" in lines[3]

    def test_the_continuation_glyph_marks_an_open_branch(self, graph):
        """`│` under B says C is still coming. Without it the grandchild
        reads as a sibling of the branch it hangs from."""
        from backend.core.ouroboros.governance import op_fanout_tree as F

        lines = F.format_fanout_tree(F.aggregate_fanout_rows()).splitlines()
        assert "│" in lines[2], lines

    def test_a_single_op_with_no_fanout_renders_nothing(self):
        """Documented: fan-out is the load-bearing signal. A lone op is not
        a tree and does not need a badge saying so."""
        from backend.core.ouroboros.battle_test import op_block_buffer as OB
        from backend.core.ouroboros.governance import op_fanout_tree as F

        buf = OB.get_default_buffer()
        buf.start_op("lonely")
        rows = tuple(r for r in F.aggregate_fanout_rows()
                     if r.op_id == "lonely")
        assert F.format_fanout_tree(rows) == ""


class TestDepthFirstHelper:
    def _blocks(self, pairs):
        import types
        return [types.SimpleNamespace(op_id=o, parent_op_id=p)
                for o, p in pairs]

    def test_an_orphan_is_kept_visible(self):
        """An edge lost to eviction must not make an op VANISH from the
        tree. A visible orphan is a far better failure than a silent one."""
        from backend.core.ouroboros.governance.op_fanout_tree import _depth_first

        blocks = self._blocks([("A", ""), ("B", "A"), ("Z", "GONE")])
        got = [b.op_id for b in _depth_first(blocks, "A")]
        assert got[:2] == ["A", "B"] and "Z" in got

    def test_a_cycle_cannot_spin_the_walk(self):
        """`register_parent` refuses cycles, but this renders on the
        operator's frame and must not depend on that being airtight."""
        from backend.core.ouroboros.governance.op_fanout_tree import _depth_first

        blocks = self._blocks([("A", "B"), ("B", "A")])
        got = [b.op_id for b in _depth_first(blocks, "A")]
        assert sorted(got) == ["A", "B"]

    def test_empty_and_garbage_are_survivable(self):
        from backend.core.ouroboros.governance.op_fanout_tree import _depth_first

        assert _depth_first([], "A") == []
        assert _depth_first(None, "A") == []
