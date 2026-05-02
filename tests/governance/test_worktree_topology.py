"""Gap #3 Slice 1 — worktree_topology substrate regression suite.

Covers:

  §1   master flag + default-off
  §2   closed-taxonomy enums (Outcome + EdgeKind)
  §3   scheduler validation (missing _graphs / wrong shape)
  §4   empty scheduler → EMPTY outcome
  §5   single-graph projection — nodes + edges + state derivation
  §6   multi-graph projection + summary aggregates
  §7   worktree-path correspondence + orphan detection
  §8   barrier_id grouping inserts implicit edges
  §9   per-unit attempt_count read from results
  §10  defensive: never-raises contract on garbage input
  §11  to_dict round-trip + JSON serialization
  §12  AST authority pins
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.verification.worktree_topology import (
    WORKTREE_TOPOLOGY_SCHEMA_VERSION,
    EdgeKind,
    GraphTopology,
    TopologyEdge,
    TopologyOutcome,
    TopologySummary,
    WorktreeNode,
    WorktreeTopology,
    compute_worktree_topology,
    worktree_topology_enabled,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "verification"
    / "worktree_topology.py"
)


def _spec(
    unit_id: str, *, repo: str = "primary",
    deps: tuple = (), barrier: str = "",
    target_files: tuple = ("file.py",),
) -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id, repo=repo,
        goal=f"goal-for-{unit_id}",
        target_files=target_files,
        dependency_ids=deps,
        barrier_id=barrier,
    )


def _make_graph(
    units: tuple, *, graph_id: str = "g1", op_id: str = "op-1",
) -> ExecutionGraph:
    return ExecutionGraph(
        graph_id=graph_id, op_id=op_id,
        planner_id="test-planner",
        schema_version="1.0",
        units=units,
        concurrency_limit=4,
    )


def _make_state(
    graph: ExecutionGraph,
    *,
    phase: GraphExecutionPhase = GraphExecutionPhase.RUNNING,
    running: tuple = (),
    completed: tuple = (),
    failed: tuple = (),
    cancelled: tuple = (),
    results: dict = None,
    last_error: str = "",
) -> GraphExecutionState:
    return GraphExecutionState(
        graph=graph, phase=phase,
        running_units=running,
        completed_units=completed,
        failed_units=failed,
        cancelled_units=cancelled,
        results=results or {},
        last_error=last_error,
    )


class _StubScheduler:
    def __init__(self, graphs: dict):
        self._graphs = graphs


# ============================================================================
# §1 — Master flag + default-off
# ============================================================================


class TestMasterFlag:
    def test_default_post_graduation_is_true(self, monkeypatch):
        """Slice 5 graduation (2026-05-02): substrate is read-only,
        structurally safe to enable by default."""
        monkeypatch.delenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", raising=False,
        )
        assert worktree_topology_enabled() is True

    def test_explicit_true(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "true",
        )
        assert worktree_topology_enabled() is True

    def test_explicit_false_hot_revert(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "false",
        )
        assert worktree_topology_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "maybe",
        )
        assert worktree_topology_enabled() is False

    def test_disabled_short_circuit_outcome(self, monkeypatch):
        """When operator hot-reverts, compute_worktree_topology
        returns DISABLED regardless of inputs."""
        monkeypatch.setenv(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "false",
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({}),
        )
        assert out.outcome is TopologyOutcome.DISABLED
        assert out.detail == "master_flag_off"
        assert out.graphs == ()


# ============================================================================
# §2 — Closed-taxonomy enums
# ============================================================================


class TestClosedTaxonomy:
    def test_outcome_has_five_values(self):
        assert {o.value for o in TopologyOutcome} == {
            "ok", "empty", "disabled",
            "scheduler_invalid", "failed",
        }

    def test_edge_kind_has_two_values(self):
        assert {e.value for e in EdgeKind} == {
            "dependency", "barrier",
        }


# ============================================================================
# §3 — Scheduler validation
# ============================================================================


class TestSchedulerValidation:
    def test_missing_graphs_attribute(self):
        class NoGraphs:
            pass
        out = compute_worktree_topology(
            scheduler=NoGraphs(), enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.SCHEDULER_INVALID
        assert "missing _graphs" in out.detail

    def test_graphs_not_mapping(self):
        class WrongType:
            _graphs = ['not', 'a', 'dict']
        out = compute_worktree_topology(
            scheduler=WrongType(), enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.SCHEDULER_INVALID

    def test_none_scheduler(self):
        out = compute_worktree_topology(
            scheduler=None, enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.SCHEDULER_INVALID


# ============================================================================
# §4 — Empty scheduler → EMPTY
# ============================================================================


class TestEmptyScheduler:
    def test_empty_graphs_dict(self):
        out = compute_worktree_topology(
            scheduler=_StubScheduler({}),
            enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.EMPTY
        assert out.graphs == ()
        assert out.summary.total_graphs == 0


# ============================================================================
# §5 — Single-graph projection
# ============================================================================


class TestSingleGraphProjection:
    def test_three_unit_graph_projects_correctly(self):
        units = (
            _spec("a"),
            _spec("b", deps=("a",)),
            _spec("c", deps=("a", "b")),
        )
        graph = _make_graph(units)
        state = _make_state(
            graph,
            running=("b",),
            completed=("a",),
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.OK
        assert len(out.graphs) == 1
        g = out.graphs[0]
        assert g.graph_id == "g1"
        assert g.op_id == "op-1"
        assert len(g.nodes) == 3
        # State derivation
        node_state = {n.unit_id: n.state for n in g.nodes}
        assert node_state["a"] is WorkUnitState.COMPLETED
        assert node_state["b"] is WorkUnitState.RUNNING
        assert node_state["c"] is WorkUnitState.PENDING
        # Edges: a→b, a→c, b→c
        edge_pairs = {
            (e.from_unit_id, e.to_unit_id) for e in g.edges
            if e.edge_kind is EdgeKind.DEPENDENCY
        }
        assert edge_pairs == {("a", "b"), ("a", "c"), ("b", "c")}

    def test_failed_state_propagates(self):
        units = (_spec("a"),)
        graph = _make_graph(units)
        state = _make_state(
            graph,
            phase=GraphExecutionPhase.FAILED,
            failed=("a",),
            last_error="generation_failure",
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert out.graphs[0].phase is GraphExecutionPhase.FAILED
        assert out.graphs[0].nodes[0].state is WorkUnitState.FAILED
        assert out.graphs[0].last_error == "generation_failure"

    def test_cancelled_state_propagates(self):
        units = (_spec("a"),)
        graph = _make_graph(units)
        state = _make_state(
            graph,
            phase=GraphExecutionPhase.CANCELLED,
            cancelled=("a",),
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert out.graphs[0].nodes[0].state is WorkUnitState.CANCELLED


# ============================================================================
# §6 — Multi-graph projection + summary
# ============================================================================


class TestMultiGraphSummary:
    def test_summary_aggregates_across_graphs(self):
        # Graph 1: 2 units, both completed
        g1 = _make_graph(
            (_spec("a"), _spec("b", deps=("a",))),
            graph_id="g1", op_id="op-1",
        )
        s1 = _make_state(
            g1, phase=GraphExecutionPhase.COMPLETED,
            completed=("a", "b"),
        )
        # Graph 2: 1 unit, running
        g2 = _make_graph(
            (_spec("c"),), graph_id="g2", op_id="op-2",
        )
        s2 = _make_state(
            g2, phase=GraphExecutionPhase.RUNNING,
            running=("c",),
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": s1, "g2": s2}),
            enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.OK
        assert out.summary.total_graphs == 2
        assert out.summary.total_units == 3
        assert out.summary.units_by_state.get("completed") == 2
        assert out.summary.units_by_state.get("running") == 1
        assert out.summary.graphs_by_phase.get("completed") == 1
        assert out.summary.graphs_by_phase.get("running") == 1

    def test_graphs_returned_in_sorted_order(self):
        # Submit out-of-order; verify projection sorts by graph_id
        out = compute_worktree_topology(
            scheduler=_StubScheduler({
                "g3": _make_state(
                    _make_graph((_spec("c"),), graph_id="g3"),
                ),
                "g1": _make_state(
                    _make_graph((_spec("a"),), graph_id="g1"),
                ),
                "g2": _make_state(
                    _make_graph((_spec("b"),), graph_id="g2"),
                ),
            }),
            enabled_override=True,
        )
        assert [g.graph_id for g in out.graphs] == ["g1", "g2", "g3"]


# ============================================================================
# §7 — Worktree-path correspondence + orphan detection
# ============================================================================


class TestWorktreeCorrespondence:
    def test_unit_with_matching_worktree(self):
        units = (_spec("alpha"),)
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            git_worktree_paths=[
                "/some/path/.worktrees/unit-alpha",
            ],
            enabled_override=True,
        )
        node = out.graphs[0].nodes[0]
        assert node.has_worktree is True
        assert node.worktree_path.endswith("/unit-alpha")
        assert out.summary.units_with_worktree == 1

    def test_unit_without_worktree(self):
        units = (_spec("beta"),)
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            git_worktree_paths=[],
            enabled_override=True,
        )
        node = out.graphs[0].nodes[0]
        assert node.has_worktree is False
        assert node.worktree_path == ""
        assert out.summary.units_with_worktree == 0

    def test_orphan_worktree_detected(self):
        # Unit "alpha" exists in scheduler; orphan worktree
        # "ghost" exists on disk but no unit references it.
        units = (_spec("alpha"),)
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            git_worktree_paths=[
                "/some/path/.worktrees/unit-alpha",
                "/some/path/.worktrees/unit-ghost",
            ],
            enabled_override=True,
        )
        assert out.summary.orphan_worktree_count == 1
        assert any(
            "unit-ghost" in p
            for p in out.summary.orphan_worktree_paths
        )

    def test_non_unit_worktree_path_ignored(self):
        # A worktree without the canonical "unit-" prefix is
        # silently ignored (operator may have other worktrees).
        units = (_spec("alpha"),)
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            git_worktree_paths=[
                "/some/path/main",  # not a unit-* path
                "/some/path/.worktrees/unit-alpha",
            ],
            enabled_override=True,
        )
        assert out.summary.orphan_worktree_count == 0
        assert out.graphs[0].nodes[0].has_worktree is True


# ============================================================================
# §8 — Barrier edges
# ============================================================================


class TestBarrierEdges:
    def test_units_sharing_barrier_get_implicit_edges(self):
        # Three units in barrier "B1" — sorted alphabetically the
        # implicit edges are a→b, b→c (chain through sorted order).
        units = (
            _spec("a", barrier="B1"),
            _spec("b", barrier="B1"),
            _spec("c", barrier="B1"),
        )
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        barrier_edges = [
            e for e in out.graphs[0].edges
            if e.edge_kind is EdgeKind.BARRIER
        ]
        assert len(barrier_edges) == 2
        edge_pairs = {(e.from_unit_id, e.to_unit_id) for e in barrier_edges}
        assert edge_pairs == {("a", "b"), ("b", "c")}

    def test_single_unit_in_barrier_no_implicit_edge(self):
        units = (_spec("a", barrier="B1"),)
        graph = _make_graph(units)
        state = _make_state(graph)
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert all(
            e.edge_kind is not EdgeKind.BARRIER
            for e in out.graphs[0].edges
        )


# ============================================================================
# §9 — attempt_count from WorkUnitResult
# ============================================================================


class TestAttemptCount:
    def test_attempt_count_read_from_results(self):
        units = (_spec("a"),)
        graph = _make_graph(units)
        result = WorkUnitResult(
            unit_id="a", repo="primary",
            status=WorkUnitState.FAILED,
            patch=None, attempt_count=3,
            started_at_ns=1, finished_at_ns=2,
            failure_class="timeout",
        )
        state = _make_state(
            graph, failed=("a",),
            results={"a": result},
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert out.graphs[0].nodes[0].attempt_count == 3

    def test_attempt_count_zero_when_no_result(self):
        units = (_spec("a"),)
        graph = _make_graph(units)
        state = _make_state(graph, running=("a",))
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            enabled_override=True,
        )
        assert out.graphs[0].nodes[0].attempt_count == 0


# ============================================================================
# §10 — Defensive: never-raises
# ============================================================================


class TestDefensive:
    def test_garbage_graph_value_skipped(self):
        # _graphs contains a non-GraphExecutionState value
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"bad": "not a state"}),
            enabled_override=True,
        )
        # Should be EMPTY since bad value is silently filtered out
        assert out.outcome is TopologyOutcome.EMPTY

    def test_mixed_valid_and_garbage(self):
        units = (_spec("a"),)
        valid = _make_state(_make_graph(units))
        out = compute_worktree_topology(
            scheduler=_StubScheduler({
                "g1": valid,
                "garbage": 42,
            }),
            enabled_override=True,
        )
        assert out.outcome is TopologyOutcome.OK
        assert len(out.graphs) == 1
        assert out.graphs[0].graph_id == "g1"

    def test_no_raise_on_bizarre_input(self):
        # Pure smoke: arbitrary types must not blow up
        out = compute_worktree_topology(
            scheduler="not even an object",
            enabled_override=True,
        )
        # Strings have no _graphs → SCHEDULER_INVALID
        assert out.outcome is TopologyOutcome.SCHEDULER_INVALID


# ============================================================================
# §11 — to_dict + JSON round-trip
# ============================================================================


class TestSerialization:
    def test_topology_to_dict_serializes_to_json(self):
        units = (_spec("a"), _spec("b", deps=("a",)))
        graph = _make_graph(units)
        state = _make_state(
            graph, completed=("a",), running=("b",),
        )
        out = compute_worktree_topology(
            scheduler=_StubScheduler({"g1": state}),
            git_worktree_paths=[
                "/x/.worktrees/unit-a",
            ],
            enabled_override=True,
        )
        # Round-trip through JSON — every field must be serializable
        s = json.dumps(out.to_dict())
        assert "schema_version" in s
        assert "worktree_topology.1" in s
        assert "raise_floor" not in s  # no leakage from other arc
        # Confirm key shape preserved
        loaded = json.loads(s)
        assert loaded["outcome"] == "ok"
        assert loaded["summary"]["total_units"] == 2
        assert loaded["summary"]["units_with_worktree"] == 1

    def test_node_to_dict_round_trip(self):
        n = WorktreeNode(
            unit_id="a", repo="r", goal="g",
            target_files=("f.py",),
            owned_paths=("f.py",),
            dependency_ids=(),
            state=WorkUnitState.RUNNING,
            barrier_id="",
            has_worktree=True,
            worktree_path="/x/unit-a",
            attempt_count=2,
        )
        d = n.to_dict()
        assert d["state"] == "running"
        assert d["target_files"] == ["f.py"]
        json.dumps(d)  # must serialize

    def test_edge_to_dict_round_trip(self):
        e = TopologyEdge(
            from_unit_id="a", to_unit_id="b",
            edge_kind=EdgeKind.DEPENDENCY,
        )
        d = e.to_dict()
        assert d["edge_kind"] == "dependency"

    def test_summary_to_dict_round_trip(self):
        s = TopologySummary(
            total_graphs=2, total_units=5,
            units_by_state={"running": 2, "completed": 3},
            graphs_by_phase={"running": 2},
            units_with_worktree=2,
            orphan_worktree_count=1,
            orphan_worktree_paths=("/orphan",),
        )
        d = s.to_dict()
        assert d["total_units"] == 5
        json.dumps(d)


# ============================================================================
# §12 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "worktree_manager",  # one-way: substrate consumes git output
                          # as caller-supplied strings, never invokes
                          # WorktreeManager directly
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.", "Path(", "pathlib",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_authority_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: "
                        f"{module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Slice 1 may import:
          * autonomy.subagent_types (frozen DAG types)
          * meta.shipped_code_invariants (Slice 5 cage close —
            lazy-imported inside register_shipped_invariants)"""
        allowed = {
            "backend.core.ouroboros.governance.autonomy.subagent_types",
            "backend.core.ouroboros.governance.meta.shipped_code_invariants",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: "
                        f"{module}"
                    )

    def test_no_filesystem_io(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_eval_exec_compile(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(
                node.func, ast.Name,
            ):
                assert node.func.id not in (
                    "eval", "exec", "compile",
                ), f"forbidden bare call: {node.func.id}"

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_schema_version_canonical(self):
        assert WORKTREE_TOPOLOGY_SCHEMA_VERSION == "worktree_topology.1"
