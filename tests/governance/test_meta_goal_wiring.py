"""Tests for the MetaGoalAggregator LIVE wiring (the built-but-no-caller fix).

The aggregator (``meta_goal_aggregator.py``) was fully built + tested but had
NO live caller -- nothing fed it the pooled ops nor dispatched the Meta-Goals
it produced, so 3 disjoint single-file chaos ops still dispatched serially
(``single_file_op``, no fan-out). This module covers ONLY the wiring seam
(``meta_goal_wiring.py`` + the GovernedLoopService drain loop) that makes the
aggregator RUN and routes its output into the EXISTING fan-out path.

Coverage:
- flag ON: 3 disjoint single-file ops fed -> aggregator bundles -> ONE
  Meta-Goal reaches ``enforce_evaluate_fanout`` (force=True) with a
  multi-unit graph (the run-#3 end-to-end fix -- swarm fan-out invoked).
- a genuinely single op -> NO bundle -> falls through to the legacy
  single-file ``_bg_pool.submit`` path (no fan-out).
- an aggregator exception -> the op falls through fail-soft to legacy
  dispatch (op never lost, drain loop never crashes).
- master OFF -> NO aggregator in the path; every op dispatches single-file
  (byte-identical; aggregator never offered).
- the drain loop starts at boot when enabled + cancels cleanly on shutdown.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance.memory_pressure_gate import (
    FanoutDecision,
    MemoryPressureGate,
    PressureLevel,
)
from backend.core.ouroboros.governance.meta_goal_aggregator import (
    META_GOAL_FLAG,
    MetaGoalAggregator,
    PooledOp,
)
import backend.core.ouroboros.governance.meta_goal_wiring as wiring
from backend.core.ouroboros.governance.meta_goal_wiring import (
    dispatch_ready_bundles,
    offer_ctx,
    pooled_op_from_ctx,
    synthetic_generation_for_bundle,
)
from backend.core.ouroboros.governance.parallel_dispatch import (
    FanoutOutcome,
    extract_candidate_files,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "true")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_DISPATCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_META_GOAL_MIN_OPS", "2")
    monkeypatch.setenv("JARVIS_WAVE3_PARALLEL_MAX_UNITS", "8")
    yield


def _gate_allowing(n: int) -> MemoryPressureGate:
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


class _DisjointOracle:
    """Oracle stub: every file indexed with NO coupling -> provably disjoint.

    The zero-trust collision matrix needs *positive* Oracle data to PROVE
    disjointness; an absent Oracle correctly treats unknown coupling as a
    collision. Models the aggregator's target case: isolated single-file ops
    on import-isolated files (production wires the real TheOracle here).
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


def _posture_maintain():
    from backend.core.ouroboros.governance.posture import Posture

    def _fn() -> Tuple[Optional["Posture"], Optional[float]]:
        return Posture.MAINTAIN, 0.9

    return _fn


@dataclass
class _FakeCtx:
    """Minimal OperationContext stand-in for the feed path."""

    op_id: str
    target_files: Tuple[str, ...] = ()
    goal: str = ""
    repo: str = "jarvis"


class _RecordingScheduler:
    """Captures the graph(s) submitted to the swarm fan-out path."""

    def __init__(self) -> None:
        self.submitted_graphs: List[Any] = []

    async def submit(self, graph: Any) -> bool:
        self.submitted_graphs.append(graph)
        return True

    async def wait_for_graph(self, graph_id: str, timeout_s: float) -> Any:
        # Return a terminal-ish state object the fan-out path can read.
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            GraphExecutionPhase,
            GraphExecutionState,
        )

        graph = next(
            (g for g in self.submitted_graphs if g.graph_id == graph_id), None
        )
        units = graph.units if graph is not None else ()
        return GraphExecutionState(
            graph=graph,
            phase=GraphExecutionPhase.COMPLETED,
            completed_units=tuple(u.unit_id for u in units),
        )


# ---------------------------------------------------------------------------
# pure-helper tests
# ---------------------------------------------------------------------------


def test_pooled_op_from_ctx_single_file():
    ctx = _FakeCtx(op_id="op-a", target_files=("pkg/a.py",), goal="fix a")
    op = pooled_op_from_ctx(ctx)
    assert op is not None
    assert op.op_id == "op-a"
    assert op.file_path == "pkg/a.py"
    assert op.rationale == "fix a"


def test_pooled_op_from_ctx_rejects_multi_and_empty():
    # Multi-file is a genuine coupled op -> NOT poolable as a single-file op.
    assert pooled_op_from_ctx(_FakeCtx(op_id="m", target_files=("a.py", "b.py"))) is None
    # No target file -> nothing to bundle.
    assert pooled_op_from_ctx(_FakeCtx(op_id="z", target_files=())) is None
    # Missing op_id -> nothing to bundle.
    assert pooled_op_from_ctx(_FakeCtx(op_id="", target_files=("a.py",))) is None


def test_synthetic_generation_round_trips_through_extract():
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=_DisjointOracle())
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py", rationale=f"r{i}"))
    bundles = agg.drain_ready_bundles()
    assert len(bundles) == 1
    bundle = bundles[0]
    gen = synthetic_generation_for_bundle(bundle)
    files = extract_candidate_files(gen)
    assert files is not None
    assert len(files) == 3
    assert {f.file_path for f in files} == {"pkg/m_0.py", "pkg/m_1.py", "pkg/m_2.py"}


# ---------------------------------------------------------------------------
# the run-#3 end-to-end fix: bundle -> ONE Meta-Goal -> swarm fan-out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_three_disjoint_ops_bundle_into_one_fanout(monkeypatch):
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=_DisjointOracle())
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py", rationale=f"r{i}"))

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg,
        scheduler=sched,
        gate=_gate_allowing(8),
        posture_fn=_posture_maintain(),
    )

    # ONE Meta-Goal dispatched into the EXISTING fan-out path.
    assert len(results) == 1
    assert results[0].outcome == FanoutOutcome.COMPLETED
    # The swarm scheduler received ONE multi-unit graph (the run-#3 fix).
    assert len(sched.submitted_graphs) == 1
    assert len(sched.submitted_graphs[0].units) == 3
    # The pooled ops were drained (consumed by the bundle).
    assert agg.pending_ops() == ()


@pytest.mark.asyncio
async def test_single_op_falls_through_no_bundle(monkeypatch):
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain())
    agg.offer(PooledOp(op_id="solo", file_path="pkg/solo.py"))

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )

    # Below MIN_OPS -> no bundle, no fan-out; the op stays pooled for the
    # legacy single-file flush.
    assert results == []
    assert sched.submitted_graphs == []
    assert {o.op_id for o in agg.pending_ops()} == {"solo"}


@pytest.mark.asyncio
async def test_aggregator_error_fails_soft(monkeypatch):
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain())
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    def _boom() -> list:
        raise RuntimeError("aggregator exploded")

    monkeypatch.setattr(agg, "drain_ready_bundles", _boom)

    sched = _RecordingScheduler()
    # Must NOT raise -- fail-soft. No bundles dispatched; ops are NOT lost
    # (they stay pooled for the legacy single-file flush path).
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )
    assert results == []
    assert sched.submitted_graphs == []


@pytest.mark.asyncio
async def test_dispatch_error_per_bundle_fails_soft(monkeypatch):
    """A fan-out dispatch error on one bundle does not crash the drain or
    lose the other bundles."""
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain())
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    async def _boom(*a, **k):
        raise RuntimeError("submit exploded")

    monkeypatch.setattr(wiring, "enforce_evaluate_fanout", _boom)

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )
    # Dispatch failed-soft -> no results, no crash.
    assert results == []


# ---------------------------------------------------------------------------
# OFF byte-identical: no aggregator in the path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_flag_no_bundle(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    agg = MetaGoalAggregator(gate=_gate_allowing(8), posture_fn=_posture_maintain())
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )
    # Master OFF -> drain_ready_bundles returns [] -> no fan-out; ops stay
    # pooled (the legacy path consumes them).
    assert results == []
    assert sched.submitted_graphs == []
    assert len(agg.pending_ops()) == 3


# ---------------------------------------------------------------------------
# boot: drain loop starts when enabled + cancels on shutdown
# ---------------------------------------------------------------------------


class _StubGLS:
    """Minimal host exposing only the surface the drain-loop wiring touches."""

    def __init__(self) -> None:
        self._meta_goal_aggregator = None
        self._meta_goal_drain_task: Optional[asyncio.Task] = None
        self._subagent_scheduler = _RecordingScheduler()
        self._bg_pool = None

    # Borrow the real loop-lifecycle helpers off the wiring module.
    _start_meta_goal_drain_loop = wiring.start_meta_goal_drain_loop
    _stop_meta_goal_drain_loop = wiring.stop_meta_goal_drain_loop


@pytest.mark.asyncio
async def test_drain_loop_starts_and_cancels(monkeypatch):
    monkeypatch.setenv("JARVIS_META_GOAL_TICK_INTERVAL_S", "0.02")
    host = _StubGLS()
    wiring.start_meta_goal_drain_loop(host)
    assert host._meta_goal_aggregator is not None
    assert host._meta_goal_drain_task is not None
    assert not host._meta_goal_drain_task.done()
    # Let it tick at least once.
    await asyncio.sleep(0.05)
    await wiring.stop_meta_goal_drain_loop(host)
    assert host._meta_goal_drain_task is None


@pytest.mark.asyncio
async def test_drain_loop_not_started_when_off(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    host = _StubGLS()
    wiring.start_meta_goal_drain_loop(host)
    # OFF byte-identical: no task, no aggregator wired in.
    assert host._meta_goal_drain_task is None
    assert host._meta_goal_aggregator is None


# ---------------------------------------------------------------------------
# feed seam: offer_ctx routes single-file ops to the aggregator
# ---------------------------------------------------------------------------


class _RecordingPool:
    def __init__(self) -> None:
        self.submitted: List[Any] = []

    async def submit(self, ctx: Any) -> str:
        self.submitted.append(ctx)
        return f"bgop-{getattr(ctx, 'op_id', '?')}"


def _feed_host():
    host = _StubGLS()
    host._meta_goal_aggregator = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=_DisjointOracle(),
    )
    host._bg_pool = _RecordingPool()
    return host


def test_offer_ctx_pools_single_file_op():
    host = _feed_host()
    pooled = offer_ctx(host, _FakeCtx(op_id="op-x", target_files=("pkg/x.py",)))
    assert pooled is True
    assert {o.op_id for o in host._meta_goal_aggregator.pending_ops()} == {"op-x"}
    # ctx retained for a possible aged-out legacy flush.
    assert "op-x" in host._meta_goal_pending_ctx


def test_offer_ctx_rejects_multi_file_op():
    host = _feed_host()
    pooled = offer_ctx(host, _FakeCtx(op_id="op-m", target_files=("a.py", "b.py")))
    # Coupled/multi-file -> caller keeps legacy dispatch (offer_ctx False).
    assert pooled is False
    assert host._meta_goal_aggregator.pending_ops() == ()


def test_offer_ctx_off_flag_byte_identical(monkeypatch):
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    host = _feed_host()
    assert offer_ctx(host, _FakeCtx(op_id="op-y", target_files=("pkg/y.py",))) is False
    assert host._meta_goal_aggregator.pending_ops() == ()


def test_offer_ctx_no_aggregator_is_false():
    host = _StubGLS()  # no _meta_goal_aggregator wired
    assert offer_ctx(host, _FakeCtx(op_id="op-z", target_files=("pkg/z.py",))) is False


@pytest.mark.asyncio
async def test_aged_single_op_flushes_to_legacy_pool(monkeypatch):
    """A genuinely-single op that ages past the coalescing window is flushed
    to the legacy _bg_pool.submit (never stranded), and dropped from the
    aggregator pool (no double-submit on the next tick)."""
    monkeypatch.setenv("JARVIS_META_GOAL_COALESCE_WINDOW_S", "0.01")
    host = _feed_host()
    assert offer_ctx(host, _FakeCtx(op_id="solo", target_files=("pkg/solo.py",)))
    # Let it age past the (tiny) window.
    await asyncio.sleep(0.03)
    await wiring._flush_aged_ops(host, host._meta_goal_aggregator)
    # Flushed to legacy pool exactly once and dropped from the aggregator pool.
    assert [getattr(c, "op_id", None) for c in host._bg_pool.submitted] == ["solo"]
    assert host._meta_goal_aggregator.pending_ops() == ()
    # A second flush is a no-op (no double-submit).
    await wiring._flush_aged_ops(host, host._meta_goal_aggregator)
    assert len(host._bg_pool.submitted) == 1


# ---------------------------------------------------------------------------
# Self-Warming Oracle JIT wire: the v5 'JIT never fired' fix
#
# The aggregator's SYNC ``drain_ready_bundles`` partitions over a COLD Oracle
# on a fresh node -> every cross-file pair INDETERMINATE -> COLLIDE -> nothing
# bundles ('aged out ... no disjoint sibling found'). The fix: the live bundle
# path (``dispatch_ready_bundles``) AWAITs ``aggregator.prewarm_window()`` ->
# ``prewarm_collision_files(oracle, op-files)`` BEFORE the partition, gated by
# JARVIS_ORACLE_SELF_WARMING_ENABLED. Warmed Oracle -> _coupled_files finds the
# files indexed -> DISJOINT -> the disjoint set BUNDLES (allowed=true n=3).
# ---------------------------------------------------------------------------


class _ColdOracle:
    """Oracle stub that starts COLD (no file indexed) and warms ONLY via the
    async ``ensure_file_indexed`` JIT -- models the real fresh-node TheOracle.

    Before a file is warmed, ``find_nodes_in_file`` returns ``[]`` (the genuine
    'no data' miss the zero-trust matrix reads as INDETERMINATE -> COLLIDE).
    After ``ensure_file_indexed`` is awaited for it, the file resolves to a
    single node with NO coupling -> provably DISJOINT.
    """

    class _Node:
        def __init__(self, fp):
            self.file_path = fp

    def __init__(self) -> None:
        self._indexed: set = set()
        self.warmed_calls: List[str] = []

    def find_nodes_in_file(self, file_path):
        if file_path in self._indexed:
            return [self._Node(file_path)]
        return []  # COLD miss -> indeterminate -> COLLIDE

    async def ensure_file_indexed(self, file_path, **_kw):
        self.warmed_calls.append(file_path)
        self._indexed.add(file_path)
        return True

    def get_dependencies(self, node):
        return []

    def get_dependents(self, node):
        return []


@pytest.mark.asyncio
async def test_cold_oracle_prewarm_bundles_three_disjoint_ops(monkeypatch):
    """Flag ON + COLD oracle + 3 disjoint-file ops -> prewarm AWAITED with
    those files BEFORE the partition -> oracle warmed -> the 3 ops BUNDLE into
    ONE Meta-Goal (allowed=true n=3). This is the v5 node's failing path now
    succeeding on the LIVE seam."""
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "1")
    oracle = _ColdOracle()
    agg = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=oracle,
    )
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py", rationale=f"r{i}"))

    # Sanity: a COLD drain WITHOUT a prewarm bundles NOTHING (the v5 bug).
    assert oracle.warmed_calls == []

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )

    # The prewarm AWAITED ensure_file_indexed for the exact op files BEFORE the
    # (sync) partition warmed the cold oracle.
    assert set(oracle.warmed_calls) == {"pkg/m_0.py", "pkg/m_1.py", "pkg/m_2.py"}
    # Warmed -> disjoint proven -> ONE multi-unit Meta-Goal fanned out (n=3).
    assert len(results) == 1
    assert results[0].outcome == FanoutOutcome.COMPLETED
    assert len(sched.submitted_graphs) == 1
    assert len(sched.submitted_graphs[0].units) == 3
    assert agg.pending_ops() == ()


@pytest.mark.asyncio
async def test_prewarm_window_emits_observable_telemetry(monkeypatch, caplog):
    """The pre-warm fires an observable one-line telemetry the soak debug.log
    can grep (v5 diagnostic was 'JIT log lines: 0')."""
    import logging as _logging

    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "1")
    oracle = _ColdOracle()
    agg = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=oracle,
    )
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    with caplog.at_level(_logging.INFO, logger="Ouroboros.MetaGoalAggregator"):
        warmed = await agg.prewarm_window()

    assert warmed == 3
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "pre-warming oracle" in joined
    assert "before disjointness check" in joined


@pytest.mark.asyncio
async def test_prewarm_off_byte_identical_no_bundle_on_cold(monkeypatch):
    """Flag OFF -> NO pre-warm call, ops do NOT bundle on a cold oracle
    (byte-identical to today). The cold partition reads indeterminate ->
    COLLIDE -> nothing fans out; ops stay pooled."""
    monkeypatch.delenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", raising=False)
    oracle = _ColdOracle()
    agg = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=oracle,
    )
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    sched = _RecordingScheduler()
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )

    # OFF -> no warm, no bundle (cold oracle stays indeterminate -> COLLIDE).
    assert oracle.warmed_calls == []
    assert results == []
    assert sched.submitted_graphs == []
    # Ops are NOT lost -- they stay pooled for the legacy single-file flush.
    assert len(agg.pending_ops()) == 3


@pytest.mark.asyncio
async def test_prewarm_error_fails_soft(monkeypatch):
    """A pre-warm raise is swallowed -> the drain still runs (fail-soft) on the
    cold oracle -> nothing bundles, ops fall through, NO crash."""
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "1")

    class _BoomOracle(_ColdOracle):
        async def ensure_file_indexed(self, file_path, **_kw):
            raise RuntimeError("oracle warm exploded")

    oracle = _BoomOracle()
    agg = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=oracle,
    )
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    sched = _RecordingScheduler()
    # Must NOT raise -- fail-soft. prewarm_collision_files swallows per-file.
    results = await dispatch_ready_bundles(
        agg, scheduler=sched, gate=_gate_allowing(8), posture_fn=_posture_maintain(),
    )
    # Warm failed -> cold partition -> no bundle, ops not lost, no crash.
    assert results == []
    assert sched.submitted_graphs == []
    assert len(agg.pending_ops()) == 3


@pytest.mark.asyncio
async def test_prewarm_window_no_op_when_master_off(monkeypatch):
    """``prewarm_window`` is a no-op when the Meta-Goal master flag is OFF
    (no aggregation -> nothing to warm), even if self-warming is ON."""
    monkeypatch.setenv(META_GOAL_FLAG, "false")
    monkeypatch.setenv("JARVIS_ORACLE_SELF_WARMING_ENABLED", "1")
    oracle = _ColdOracle()
    agg = MetaGoalAggregator(
        gate=_gate_allowing(8), posture_fn=_posture_maintain(), oracle=oracle,
    )
    for i in range(3):
        agg.offer(PooledOp(op_id=f"op-{i}", file_path=f"pkg/m_{i}.py"))

    warmed = await agg.prewarm_window()
    assert warmed == 0
    assert oracle.warmed_calls == []
