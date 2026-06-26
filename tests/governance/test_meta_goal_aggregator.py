"""Tests for the Adaptive Meta-Goal Aggregator (swarm run-#3 single_file_op fix).

The aggregator bundles N *disjoint* single-file ops pooled in the dispatch
queue into ONE fan-outable Meta-Goal whose ExecutionGraph fans out to
parallel swarm workers -- instead of each single-file op hitting the
``SINGLE_FILE_OP`` reject in ``is_fanout_eligible`` and queuing serially.

Coverage:
- 3 disjoint single-file ops in the window -> ONE Meta-Goal,
  ``is_fanout_eligible(... force=...)`` returns ``allowed=True n=3``
  (the run-#3 fix).
- collision-COUPLED ops are NOT bundled together.
- 50 ops -> CHUNKED into capacity-sized Meta-Goals (no single DAG >
  capacity; batch size adapts to a mocked MemoryPressureGate / worker cap).
- partial recomposition (Poison-Pill): 2 succeed + 1 fail -> composed
  candidate has the 2, the failed unit routed to the Cryo-DLQ
  (``append_dlq`` called), ONE PR not scrapped.
- a node failover mid-flight -> the unit resumes via the override handoff,
  siblings unaffected, DAG completes.
- telemetry tags meta_goal_id + unit op-id.
- master OFF -> single-file dispatch byte-identical (no Meta-Goal).
"""
from __future__ import annotations

import os
from typing import Optional, Tuple

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)

import backend.core.ouroboros.governance.meta_goal_aggregator as mga
from backend.core.ouroboros.governance.meta_goal_aggregator import (
    MetaGoalAggregator,
    PooledOp,
    MetaGoalBundle,
    META_GOAL_FLAG,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Default-ON aggregator + parallel master for the bundling tests.

    Individual tests that need master-OFF byte-identical behaviour flip the
    flags back off explicitly.
    """
    monkeypatch.setenv(META_GOAL_FLAG, "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_META_GOAL_MIN_OPS", "2")
    # Make memory + posture deterministic / permissive by default.
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "5")
    yield


def _gate_allowing(n: int) -> MemoryPressureGate:
    """A MemoryPressureGate whose can_fanout clamps to <= n (OK level)."""

    class _G(MemoryPressureGate):
        def can_fanout(self, n_requested: int) -> FanoutDecision:  # type: ignore[override]
            allowed = min(int(n_requested), n)
            return FanoutDecision(
                allowed=allowed >= 1,
                n_requested=int(n_requested),
                n_allowed=allowed,
                level=PressureLevel.OK,
                free_pct=90.0,
                reason_code="test.capped",
                source="test",
            )

    return _G()


def _posture_neutral():
    return (None, None)


class _DisjointOracle:
    """Oracle stub: every file is indexed with NO coupling -> provably disjoint.

    This models the common case the aggregator targets: isolated single-file
    chaos failures on distinct, import-isolated files. The zero-trust
    collision matrix needs *positive* Oracle data to PROVE disjointness; an
    absent Oracle would (correctly) treat unknown coupling as a collision.
    """

    class _Node:
        def __init__(self, fp):
            self.file_path = fp

    def find_nodes_in_file(self, file_path):
        return [self._Node(file_path)]

    def get_dependencies(self, node):
        return []

    def get_dependents(self, node):
        return []


def _op(op_id: str, file_path: str) -> PooledOp:
    return PooledOp(op_id=op_id, file_path=file_path, full_content=f"# {file_path}\n", rationale=f"fix {file_path}")


def _completed_result(unit_id: str, file_path: str) -> WorkUnitResult:
    patch = RepoPatch(
        repo="jarvis",
        files=(PatchedFile(path=file_path, op=FileOp.CREATE, preimage=None),),
        new_content=((file_path, f"# patched {file_path}\n".encode("utf-8")),),
    )
    return WorkUnitResult(
        unit_id=unit_id,
        repo="jarvis",
        status=WorkUnitState.COMPLETED,
        patch=patch,
        attempt_count=1,
        started_at_ns=1,
        finished_at_ns=2,
    )


def _failed_result(unit_id: str) -> WorkUnitResult:
    return WorkUnitResult(
        unit_id=unit_id,
        repo="jarvis",
        status=WorkUnitState.FAILED,
        patch=None,
        attempt_count=1,
        started_at_ns=1,
        finished_at_ns=2,
        failure_class="infra",
        error="dw collapse + jprime failover exhausted",
    )


# ---------------------------------------------------------------------------
# 1. The run-#3 fix: 3 disjoint single-file ops -> ONE Meta-Goal, allowed=True n=3
# ---------------------------------------------------------------------------


def test_three_disjoint_single_file_ops_form_one_meta_goal():
    agg = MetaGoalAggregator(
        gate=_gate_allowing(5),
        posture_fn=_posture_neutral,
        oracle=_DisjointOracle(),  # None oracle => zero-trust; single-file unique paths still disjoint via direct-overlap check only
    )
    ops = [
        _op("op-aaa", "backend/a.py"),
        _op("op-bbb", "backend/b.py"),
        _op("op-ccc", "backend/c.py"),
    ]
    for o in ops:
        agg.offer(o)

    bundles = agg.drain_ready_bundles()
    assert len(bundles) == 1, "3 disjoint single-file ops should form exactly one Meta-Goal"
    bundle = bundles[0]
    assert isinstance(bundle, MetaGoalBundle)
    # The run-#3 fix: eligibility allows fan-out of 3, NOT a SINGLE_FILE_OP reject.
    assert bundle.eligibility.allowed is True
    assert bundle.eligibility.n_allowed == 3
    assert len(bundle.graph.units) == 3
    # target_files = union of the 3 single-file ops
    union = {f for u in bundle.graph.units for f in u.target_files}
    assert union == {"backend/a.py", "backend/b.py", "backend/c.py"}
    # meta_goal_id present + each unit traceable back to its origin op
    assert bundle.meta_goal_id
    assert set(bundle.unit_to_op.values()) == {"op-aaa", "op-bbb", "op-ccc"}


def test_below_min_ops_does_not_bundle(monkeypatch):
    monkeypatch.setenv("JARVIS_META_GOAL_MIN_OPS", "3")
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    agg.offer(_op("op-a", "backend/a.py"))
    agg.offer(_op("op-b", "backend/b.py"))
    # Only 2 < min 3 -> nothing ready.
    assert agg.drain_ready_bundles() == []


# ---------------------------------------------------------------------------
# 2. Collision-coupled ops are NOT bundled together
# ---------------------------------------------------------------------------


class _CouplingOracle:
    """Tiny Oracle stub: a.py and b.py are import-coupled; c.py is isolated."""

    class _Node:
        def __init__(self, fp):
            self.file_path = fp

    _COUPLING = {
        "backend/a.py": {"backend/b.py"},
        "backend/b.py": {"backend/a.py"},
        "backend/c.py": set(),
        "backend/d.py": set(),
    }

    def find_nodes_in_file(self, file_path):
        if file_path in self._COUPLING:
            return [self._Node(file_path)]
        return []

    def get_dependencies(self, node):
        return [self._Node(f) for f in self._COUPLING.get(node.file_path, set())]

    def get_dependents(self, node):
        return []


def test_collision_coupled_ops_are_not_bundled_together():
    oracle = _CouplingOracle()
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=oracle)
    # a + b are coupled; c + d are isolated. The largest disjoint group must
    # NOT contain both a and b.
    for o in [
        _op("op-a", "backend/a.py"),
        _op("op-b", "backend/b.py"),
        _op("op-c", "backend/c.py"),
        _op("op-d", "backend/d.py"),
    ]:
        agg.offer(o)

    bundles = agg.drain_ready_bundles()
    # At least one bundle formed; no bundle co-groups a.py and b.py.
    assert bundles, "isolated c+d (plus one of a/b) should still bundle"
    for bundle in bundles:
        files = {f for u in bundle.graph.units for f in u.target_files}
        assert not ({"backend/a.py", "backend/b.py"} <= files), (
            "coupled a.py + b.py must never be in the same Meta-Goal"
        )


# ---------------------------------------------------------------------------
# 3. 50 ops -> CHUNKED by live capacity (no mega-DAG / OOM)
# ---------------------------------------------------------------------------


def test_fifty_ops_chunk_by_capacity_no_mega_dag():
    capacity = 4
    agg = MetaGoalAggregator(
        gate=_gate_allowing(capacity),
        posture_fn=_posture_neutral,
        oracle=_DisjointOracle(),
        max_concurrent_workers=capacity,
    )
    for i in range(50):
        agg.offer(_op(f"op-{i:03d}", f"backend/mod_{i:03d}.py"))

    bundles = agg.drain_ready_bundles()
    assert len(bundles) >= 1
    # No single Meta-Goal DAG may exceed the live capacity (no OOM mega-DAG).
    for b in bundles:
        assert len(b.graph.units) <= capacity, (
            f"Meta-Goal exceeded capacity {capacity}: {len(b.graph.units)} units"
        )
        assert b.graph.concurrency_limit <= capacity
    # All 50 ops accounted for across bundles (none lost).
    total_units = sum(len(b.graph.units) for b in bundles)
    assert total_units == 50


def test_chunk_size_adapts_to_lower_capacity():
    """Tighter memory gate -> smaller Meta-Goal batches."""
    agg = MetaGoalAggregator(
        gate=_gate_allowing(2),  # gate clamps to 2
        posture_fn=_posture_neutral,
        oracle=_DisjointOracle(),
        max_concurrent_workers=10,  # workers permit more, but gate wins (strictest)
    )
    for i in range(6):
        agg.offer(_op(f"op-{i}", f"backend/f{i}.py"))
    bundles = agg.drain_ready_bundles()
    for b in bundles:
        assert len(b.graph.units) <= 2


# ---------------------------------------------------------------------------
# 4. Partial recomposition (Poison-Pill): 2 succeed + 1 fail
# ---------------------------------------------------------------------------


def test_partial_recompose_commits_successes_and_dlqs_failure(monkeypatch):
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    for o in [
        _op("op-a", "backend/a.py"),
        _op("op-b", "backend/b.py"),
        _op("op-c", "backend/c.py"),
    ]:
        agg.offer(o)
    bundle = agg.drain_ready_bundles()[0]

    units = list(bundle.graph.units)
    # 2 succeed, 1 fails (even after failover).
    results = {
        units[0].unit_id: _completed_result(units[0].unit_id, units[0].target_files[0]),
        units[1].unit_id: _completed_result(units[1].unit_id, units[1].target_files[0]),
        units[2].unit_id: _failed_result(units[2].unit_id),
    }
    state = GraphExecutionState(
        graph=bundle.graph,
        phase=GraphExecutionPhase.FAILED,
        completed_units=(units[0].unit_id, units[1].unit_id),
        failed_units=(units[2].unit_id,),
        results=results,
    )

    dlq_calls = []
    monkeypatch.setattr(
        mga, "append_dlq",
        lambda envelope, *, reason, path=None: dlq_calls.append((envelope, reason)),
    )

    recomp = agg.partial_recompose(bundle, state)

    # The composed candidate keeps the 2 successes -> ONE PR, not scrapped.
    assert recomp.composed is not None
    assert recomp.composed.is_failure is False
    composed_files = set(recomp.composed.file_paths)
    assert units[0].target_files[0] in composed_files
    assert units[1].target_files[0] in composed_files
    assert units[2].target_files[0] not in composed_files
    # The failed sibling was routed to the Cryo-DLQ for standalone retry.
    assert len(dlq_calls) == 1
    env, reason = dlq_calls[0]
    assert "op-c" in str(env) or env.get("op_id") == "op-c"
    assert "meta_goal" in reason or "poison" in reason or "partial" in reason


def test_all_success_composes_full_union_no_dlq(monkeypatch):
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    for o in [_op("op-a", "backend/a.py"), _op("op-b", "backend/b.py")]:
        agg.offer(o)
    bundle = agg.drain_ready_bundles()[0]
    units = list(bundle.graph.units)
    results = {
        u.unit_id: _completed_result(u.unit_id, u.target_files[0]) for u in units
    }
    state = GraphExecutionState(
        graph=bundle.graph,
        phase=GraphExecutionPhase.COMPLETED,
        completed_units=tuple(u.unit_id for u in units),
        results=results,
    )
    dlq_calls = []
    monkeypatch.setattr(
        mga, "append_dlq",
        lambda envelope, *, reason, path=None: dlq_calls.append((envelope, reason)),
    )
    recomp = agg.partial_recompose(bundle, state)
    assert recomp.composed is not None and recomp.composed.is_failure is False
    assert len(recomp.composed.file_paths) == 2
    assert dlq_calls == []  # nothing failed -> nothing DLQ'd


# ---------------------------------------------------------------------------
# 5. Failover-aware per-node resume (siblings unaffected)
# ---------------------------------------------------------------------------


def test_failover_resumed_unit_is_treated_as_success_for_recompose(monkeypatch):
    """A node that DW-collapsed then resumed via override returns COMPLETED;
    partial-recomp still includes its patch (not scrapped)."""
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    for o in [_op("op-a", "backend/a.py"), _op("op-b", "backend/b.py")]:
        agg.offer(o)
    bundle = agg.drain_ready_bundles()[0]
    units = list(bundle.graph.units)

    # Build the per-unit failover override (the existing handoff vehicle).
    override = agg.build_failover_override(bundle, units[0].unit_id)
    assert override["provider_override"] == "gcp-jprime"
    assert override["unit_id"] == units[0].unit_id
    assert override["meta_goal_id"] == bundle.meta_goal_id

    # Both units complete (unit 0 resumed via jprime; sibling unaffected).
    results = {u.unit_id: _completed_result(u.unit_id, u.target_files[0]) for u in units}
    state = GraphExecutionState(
        graph=bundle.graph,
        phase=GraphExecutionPhase.COMPLETED,
        completed_units=tuple(u.unit_id for u in units),
        results=results,
    )
    dlq_calls = []
    monkeypatch.setattr(
        mga, "append_dlq",
        lambda envelope, *, reason, path=None: dlq_calls.append((envelope, reason)),
    )
    recomp = agg.partial_recompose(bundle, state)
    assert recomp.composed is not None and recomp.composed.is_failure is False
    assert len(recomp.composed.file_paths) == 2  # sibling unaffected, both present
    assert dlq_calls == []


# ---------------------------------------------------------------------------
# 6. Telemetry tags meta_goal_id + unit op-id
# ---------------------------------------------------------------------------


def test_telemetry_tags_meta_and_unit(caplog):
    import logging
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    for o in [
        _op("op-a", "backend/a.py"),
        _op("op-b", "backend/b.py"),
    ]:
        agg.offer(o)
    with caplog.at_level(logging.INFO):
        bundle = agg.drain_ready_bundles()[0]
        lines = agg.telemetry_lines(bundle)
    # Every per-unit telemetry line carries BOTH meta-goal id AND the origin op id.
    assert lines, "expected per-unit telemetry lines"
    for line in lines:
        assert "[MetaGoal]" in line
        assert f"meta={bundle.meta_goal_id}" in line
        assert "unit=" in line
        assert "file=" in line
    # The origin op-ids appear across the lines.
    joined = "\n".join(lines)
    assert "op-a" in joined and "op-b" in joined


# ---------------------------------------------------------------------------
# 7. Master OFF -> single-file dispatch byte-identical (no Meta-Goal)
# ---------------------------------------------------------------------------


def test_master_off_is_byte_identical_no_bundle(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    agg = MetaGoalAggregator(gate=_gate_allowing(5), posture_fn=_posture_neutral, oracle=_DisjointOracle())
    for o in [
        _op("op-a", "backend/a.py"),
        _op("op-b", "backend/b.py"),
        _op("op-c", "backend/c.py"),
    ]:
        agg.offer(o)
    # OFF -> no bundles ever; ops stay single-file (pooled ops returned as-is).
    assert agg.drain_ready_bundles() == []
    # The pooled ops are still retrievable for the legacy single-file path.
    pending = agg.pending_ops()
    assert {p.op_id for p in pending} == {"op-a", "op-b", "op-c"}


def test_aggregator_enabled_helper(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    assert mga.meta_goal_aggregator_enabled() is False
    monkeypatch.setenv(META_GOAL_FLAG, "true")
    assert mga.meta_goal_aggregator_enabled() is True
