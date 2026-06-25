"""Tests for swarm_invoker -- the SwarmOrchestrator -> SubagentScheduler seam.

Confirms:
  (a) swarm-OFF -> legacy GenerationSubagentExecutor path, byte-identical
      (no define_worker / SubagentFactory.build call);
  (b) swarm-ON + multi-node parallelizable DAG -> per-unit dynamic worker
      synthesis (worker_synthesizer) + SubagentFactory.build cage +
      caged execute in the unit's worktree + sandbox;
  (c) swarm-ON + single-node / non-parallelizable DAG -> legacy executor,
      no swarm (the swarm only engages a genuinely-parallelizable DAG);
  (d) a deadlocked pair -> EpistemicDeadlockBreaker.observe_turn kills +
      dissolves -> the unit returns FAILED (not hung);
  (e) synthesis / cage construction failure -> fail-CLOSED FAILED unit
      (NEVER an uncaged execution);
  (f) the seam reuses the EXISTING synthesizer / factory / breaker /
      executor -- the Golden Rule preserved (shape synthesized, no static
      role enum).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.saga.saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _swarm_unit(unit_id: str, target: str, *, deps=()) -> WorkUnitSpec:
    """A swarm-synthesized unit (carries swarm fields -> is_swarm_worker)."""
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="JARVIS",
        goal="refactor and fix " + target,
        target_files=(target,),
        owned_paths=(target,),
        dependency_ids=deps,
        # Swarm-synthesized fields -> is_swarm_worker True.
        system_prompt_template="You are a synthesized worker for " + target,
        allowed_tools=("read_file", "edit_file"),
        mutation_budget=3,
        context_budget_tokens=8000,
        worker_role="python-source mutator",
    )


def _legacy_unit(unit_id: str, target: str, *, deps=()) -> WorkUnitSpec:
    """A legacy fixed-type unit (no swarm fields -> is_swarm_worker False)."""
    return WorkUnitSpec(
        unit_id=unit_id,
        repo="JARVIS",
        goal="update " + target,
        target_files=(target,),
        owned_paths=(target,),
        dependency_ids=deps,
    )


def _multi_node_graph(units) -> ExecutionGraph:
    return ExecutionGraph(
        graph_id="g-multi",
        op_id="op-multi",
        planner_id="swarm_orchestrator",
        schema_version="swarm.graph.1a",
        concurrency_limit=2,
        units=tuple(units),
    )


def _single_node_graph(unit) -> ExecutionGraph:
    return ExecutionGraph(
        graph_id="g-single",
        op_id="op-single",
        planner_id="swarm_orchestrator",
        schema_version="swarm.graph.1a",
        concurrency_limit=1,
        units=(unit,),
    )


class _RecordingExecutor:
    """A stand-in legacy GenerationSubagentExecutor that records calls."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def execute(self, graph, unit) -> WorkUnitResult:
        self.calls.append(unit.unit_id)
        now = time.monotonic_ns()
        patch = RepoPatch(
            repo=unit.repo,
            files=(PatchedFile(path=unit.target_files[0], op=FileOp.CREATE, preimage=None),),
            new_content=((unit.target_files[0], b"# ok\n"),),
        )
        return WorkUnitResult(
            unit_id=unit.unit_id,
            repo=unit.repo,
            status=WorkUnitState.COMPLETED,
            patch=patch,
            attempt_count=1,
            started_at_ns=now,
            finished_at_ns=now,
            causal_parent_id=graph.causal_trace_id,
        )


def _set_swarm(monkeypatch, value: str) -> None:
    monkeypatch.setenv("JARVIS_SWARM_ORCHESTRATOR_ENABLED", value)


# ---------------------------------------------------------------------------
# (a) swarm-OFF -> legacy executor, no synthesis (byte-identical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_off_uses_legacy_executor_no_synthesis(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "false")
    inner = _RecordingExecutor()

    define_calls: list = []
    build_calls: list = []

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: define_calls.append(sg),
        build_worker=lambda *a, **k: build_calls.append((a, k)),
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    assert result.status is WorkUnitState.COMPLETED
    assert inner.calls == ["u1"]
    # OFF -> no synthesis, no cage construction.
    assert define_calls == []
    assert build_calls == []


# ---------------------------------------------------------------------------
# (b) swarm-ON + multi-node DAG -> per-unit synthesis + cage + caged execute
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_on_multi_node_routes_through_synthesis_and_cage(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()

    synth_calls: list = []
    cage_units: list = []

    class _FakeShape:
        is_mutating = True
        read_only = False
        allowed_tools = ("read_file", "edit_file")
        mutation_budget = 3
        role = "python-source mutator"

    class _FakeBuilt:
        def __init__(self, unit_id):
            self.worker_id = unit_id
            self.backend = object()  # the ScopedToolBackend cage (stand-in)
            self.shape = _FakeShape()

    def _define(sg):
        synth_calls.append(getattr(sg, "unit_id", sg))
        return _FakeShape()

    def _build(shape, *, worker_id, goal, scope_paths, **kw):
        cage_units.append(worker_id)
        return _FakeBuilt(worker_id)

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=_define,
        build_worker=_build,
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    r1 = await invoker.execute(graph, graph.unit_map["u1"])
    r2 = await invoker.execute(graph, graph.unit_map["u2"])

    assert r1.status is WorkUnitState.COMPLETED
    assert r2.status is WorkUnitState.COMPLETED
    # Per-unit dynamic synthesis happened for each swarm unit.
    assert synth_calls == ["u1", "u2"]
    assert cage_units == ["u1", "u2"]
    # The caged unit still executed through the existing executor (worktree +
    # sandbox live in that executor) -- reuse, no net-new executor.
    assert inner.calls == ["u1", "u2"]


# ---------------------------------------------------------------------------
# (c) swarm-ON + single-node DAG -> legacy executor, no swarm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_on_single_node_uses_legacy_no_swarm(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()
    define_calls: list = []

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: define_calls.append(sg),
        build_worker=lambda *a, **k: None,
    )

    graph = _single_node_graph(_swarm_unit("only", "a.py"))
    result = await invoker.execute(graph, graph.unit_map["only"])

    assert result.status is WorkUnitState.COMPLETED
    assert inner.calls == ["only"]
    # Single-node DAG is not parallelizable -> swarm does not engage.
    assert define_calls == []


@pytest.mark.asyncio
async def test_swarm_on_legacy_unit_in_multi_graph_uses_legacy(monkeypatch):
    """A non-swarm unit (no synthesized fields) -> legacy even in a multi DAG."""
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()
    define_calls: list = []

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: define_calls.append(sg),
        build_worker=lambda *a, **k: None,
    )

    graph = _multi_node_graph([_legacy_unit("u1", "a.py"), _legacy_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    assert result.status is WorkUnitState.COMPLETED
    assert inner.calls == ["u1"]
    assert define_calls == []


# ---------------------------------------------------------------------------
# (e) synthesis / cage failure -> fail-CLOSED FAILED unit (never uncaged)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesis_failure_fails_closed(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()

    def _boom_define(sg):
        raise RuntimeError("synthesis exploded")

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=_boom_define,
        build_worker=lambda *a, **k: None,
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    assert result.status is WorkUnitState.FAILED
    assert "swarm" in result.failure_class
    # Fail-CLOSED: the unit NEVER reached the (uncaged) executor.
    assert inner.calls == []


@pytest.mark.asyncio
async def test_cage_failure_fails_closed(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()

    def _boom_build(shape, **kw):
        raise RuntimeError("cannot cage")

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: object(),
        build_worker=_boom_build,
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    assert result.status is WorkUnitState.FAILED
    assert "swarm" in result.failure_class
    # NEVER an uncaged execution.
    assert inner.calls == []


@pytest.mark.asyncio
async def test_cage_returns_none_fails_closed(monkeypatch):
    """A build that returns a built worker with no backend -> fail-CLOSED."""
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()

    class _NoCage:
        backend = None
        worker_id = "u1"

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: object(),
        build_worker=lambda *a, **k: _NoCage(),
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    assert result.status is WorkUnitState.FAILED
    assert inner.calls == []


# ---------------------------------------------------------------------------
# (d) deadlock -> observe_turn kills + dissolves -> unit FAILED (not hung)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deadlock_breaker_wired_into_bus_round_trip(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si
    from backend.core.ouroboros.governance.autonomy.deadlock_breaker import (
        DeadlockInterruptedException,
    )

    _set_swarm(monkeypatch, "true")
    inner = _RecordingExecutor()

    class _Built:
        def __init__(self, uid):
            self.worker_id = uid
            self.backend = object()

    # An executor whose generation deadlocks: the round-trip observer raises.
    async def _deadlocking_execute(graph, unit):
        # The invoker hands the round-trip observer to the executor through a
        # turn callback; simulate the worker pair exceeding the turn budget.
        raise DeadlockInterruptedException(
            correlation_id="c",
            worker_a="u1",
            worker_b="u2",
            trigger="max_turn_budget",
            transcript="...",
            dissolved_units=("u1", "u2"),
        )

    inner.execute = _deadlocking_execute  # type: ignore[assignment]

    invoker = si.SwarmUnitInvoker(
        legacy_executor=inner,
        define_worker=lambda sg: object(),
        build_worker=lambda *a, **k: _Built(getattr(k.get("worker_id"), "__str__", lambda: "u1")()),
    )

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    result = await invoker.execute(graph, graph.unit_map["u1"])

    # A shattered deadlock -> FAILED unit (DAGComposer treats this as
    # ComposeFailure -> legacy serial), never a hang.
    assert result.status is WorkUnitState.FAILED
    assert "deadlock" in result.failure_class


def test_observe_turn_kills_and_dissolves():
    """Direct: a pair over the turn budget -> shatter -> FAILED-shaped yield."""
    from backend.core.ouroboros.governance.autonomy.deadlock_breaker import (
        DeadlockInterruptedException,
        EpistemicDeadlockBreaker,
        max_turn_budget,
    )

    killed: list = []
    breaker = EpistemicDeadlockBreaker(
        correlation_id="c",
        worker_a="u1",
        worker_b="u2",
        kill_unit=lambda wid: killed.append(wid),
    )
    with pytest.raises(DeadlockInterruptedException) as exc:
        for _ in range(max_turn_budget() + 2):
            breaker.observe_turn("same text", verified_artifact=False)
    assert set(killed) == {"u1", "u2"}
    assert set(exc.value.dissolved_units) == {"u1", "u2"}


# ---------------------------------------------------------------------------
# (f) parallelizability decision
# ---------------------------------------------------------------------------


def test_is_parallelizable_true_for_independent_multi_node(monkeypatch):
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    graph = _multi_node_graph([_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")])
    assert si.is_graph_parallelizable(graph) is True


def test_is_parallelizable_false_for_single_node():
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    graph = _single_node_graph(_swarm_unit("only", "a.py"))
    assert si.is_graph_parallelizable(graph) is False


def test_is_parallelizable_false_when_fully_serial_chain():
    """concurrency_limit>1 but every unit depends on the previous -> serial."""
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    graph = ExecutionGraph(
        graph_id="g-chain",
        op_id="op-chain",
        planner_id="swarm_orchestrator",
        schema_version="swarm.graph.1a",
        concurrency_limit=2,
        units=(
            _swarm_unit("u1", "a.py"),
            _swarm_unit("u2", "b.py", deps=("u1",)),
        ),
    )
    assert si.is_graph_parallelizable(graph) is False


def test_is_parallelizable_false_when_concurrency_limit_one():
    from backend.core.ouroboros.governance.autonomy import swarm_invoker as si

    graph = ExecutionGraph(
        graph_id="g-cl1",
        op_id="op-cl1",
        planner_id="swarm_orchestrator",
        schema_version="swarm.graph.1a",
        concurrency_limit=1,
        units=(_swarm_unit("u1", "a.py"), _swarm_unit("u2", "b.py")),
    )
    assert si.is_graph_parallelizable(graph) is False
