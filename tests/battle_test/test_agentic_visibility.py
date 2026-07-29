"""The roster showed WHO, never what they worked on, under whom, or where.

`AgentEntry` was flat — id, kind, goal, state, times — and had exactly ONE
producer (`serpent_flow`'s Phase B subagent path). So L3 worktree units,
which are the organism's most parallel work, ran with no operator-facing
trace at all: the isolation L3 *promises* could not be verified by the
person it protects.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.agent_roster import (
    AgentRoster,
    order_by_lineage,
    render_roster,
)


def _peopled():
    r = AgentRoster()
    r.spawn("op-1", kind="op", goal="harden the vision floor")
    r.spawn("unit-a", kind="L3-unit", goal="patch the floor",
            op_id="op-1", parent_id="op-1", worktree="unit-a-wt")
    r.spawn("sub-1", kind="Explore", goal="map callers", parent_id="unit-a")
    r.spawn("unit-b", kind="L3-unit", goal="rebuild fixture",
            op_id="op-1", parent_id="op-1", worktree="unit-b-wt")
    return r


class TestTheGraphIsExpressible:
    def test_an_entry_carries_op_parent_and_worktree(self):
        rows = _peopled().snapshot()["rows"]
        unit = next(r for r in rows if r["id"] == "unit-a")
        assert unit["op_id"] == "op-1"
        assert unit["parent_id"] == "op-1"
        assert unit["worktree"] == "unit-a-wt"

    def test_absent_structure_is_ABSENT_not_empty(self):
        """A present-but-empty parent claims the agent has no parent. A
        false claim about the graph is worse than no claim."""
        r = AgentRoster()
        r.spawn("plain", kind="agent", goal="g")
        row = r.snapshot()["rows"][0]
        assert "parent_id" not in row
        assert "worktree" not in row

    def test_existing_callers_keep_working(self):
        """The structural args are optional on purpose: this roster had
        one producer for a long time, and the way to fix that is to make
        joining cheap, not to demand every caller learn a schema."""
        r = AgentRoster()
        r.spawn("legacy", "Explore", "a goal")      # positional, as before
        assert r.snapshot()["rows"][0]["id"] == "legacy"


class TestLineage:
    def test_children_follow_their_parent(self):
        rows = order_by_lineage(_peopled().snapshot()["rows"])
        order = [r["id"] for r in rows]
        assert order.index("unit-a") < order.index("sub-1") < order.index("unit-b")

    def test_depth_is_stamped(self):
        depths = {r["id"]: r["depth"]
                  for r in order_by_lineage(_peopled().snapshot()["rows"])}
        assert depths == {"op-1": 0, "unit-a": 1, "sub-1": 2, "unit-b": 1}

    def test_an_ORPHAN_still_renders(self):
        """A child whose parent already finished and was reaped must not
        vanish — losing an agent from the view is the failure this fixes."""
        rows = order_by_lineage([{"id": "x", "parent_id": "long-gone"}])
        assert len(rows) == 1 and rows[0]["depth"] == 0

    def test_a_CYCLE_cannot_hang_the_render(self):
        """Should be impossible, and therefore will happen."""
        rows = order_by_lineage([{"id": "a", "parent_id": "b"},
                                 {"id": "b", "parent_id": "a"}])
        assert len(rows) == 2

    def test_no_agent_is_ever_lost(self):
        rows = [{"id": f"n{i}", "parent_id": "nope"} for i in range(30)]
        assert len(order_by_lineage(rows)) == 30

    @pytest.mark.parametrize("junk", [None, [], [{}], [{"id": None}], "x"])
    def test_junk_degrades(self, junk):
        assert isinstance(order_by_lineage(junk), list)  # type: ignore[arg-type]


class TestItRenders:
    def test_the_indent_shows_the_graph(self):
        out = render_roster(_peopled().snapshot(), width=100)
        body = "\n".join(out)
        assert "  L3-unit" in body       # depth 1
        assert "    Explore" in body     # depth 2

    def test_the_worktree_is_VISIBLE(self):
        """L3 promises isolation. An operator cannot verify a promise they
        cannot see."""
        body = "\n".join(render_roster(_peopled().snapshot(), width=100))
        assert "unit-a-wt" in body and "unit-b-wt" in body

    def test_indent_eats_the_goal_column_not_the_width(self):
        """An indent that pushed the line wider would wrap on exactly the
        deep rows it exists to clarify."""
        for w in (60, 80, 120):
            out = render_roster(_peopled().snapshot(), width=w)
            assert all(len(line) <= w for line in out), w


class TestTheProducersActuallyJOIN:
    def test_L3_units_enter_the_roster(self):
        """THE gap: L3 was the organism's most parallel work and had no
        roster presence at all."""
        import inspect

        from backend.core.ouroboros.governance.autonomy import (
            subagent_scheduler,
        )
        src = inspect.getsource(subagent_scheduler)
        assert "get_agent_roster" in src
        assert "L3-unit" in src

    def test_L3_units_also_LEAVE_it(self):
        """An agent stuck at "running" forever is worse than one never
        shown: it reports work that is not happening."""
        import inspect

        from backend.core.ouroboros.governance.autonomy import (
            subagent_scheduler,
        )
        assert hasattr(subagent_scheduler, "_retire_from_roster")
        src = inspect.getsource(subagent_scheduler)
        # Retired at the SAME seam telemetry already fires on, so the two
        # cannot disagree about whether a unit finished.
        assert "_retire_from_roster(_result" in src

    def test_a_roster_fault_never_stops_a_work_unit(self):
        import inspect

        from backend.core.ouroboros.governance.autonomy import (
            subagent_scheduler,
        )
        src = inspect.getsource(subagent_scheduler._retire_from_roster)
        assert "except Exception" in src
