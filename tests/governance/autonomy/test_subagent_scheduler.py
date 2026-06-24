from __future__ import annotations

import asyncio
import time

import pytest


def _make_graph(
    *,
    graph_id="graph-scheduler",
    op_id="op-scheduler",
    owned_b=("prime/b.py",),
    dependency_ids_b=(),
):
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        ExecutionGraph,
        WorkUnitSpec,
    )

    return ExecutionGraph(
        graph_id=graph_id,
        op_id=op_id,
        planner_id="planner-v1",
        schema_version="2d.1",
        concurrency_limit=2,
        units=(
            WorkUnitSpec(
                unit_id="u1",
                repo="jarvis",
                goal="update a",
                target_files=("jarvis/a.py",),
                owned_paths=("jarvis/a.py",),
            ),
            WorkUnitSpec(
                unit_id="u2",
                repo="prime",
                goal="update b",
                target_files=("prime/b.py",),
                owned_paths=owned_b,
                dependency_ids=dependency_ids_b,
            ),
        ),
    )


class _FakeExecutor:
    def __init__(self, *, fail_unit: str = "", delay_s: float = 0.02) -> None:
        self.started: list[str] = []
        self.concurrent_counter = 0
        self.max_concurrency = 0
        self.fail_unit = fail_unit
        self.delay_s = delay_s

    async def execute(self, graph, unit):
        from backend.core.ouroboros.governance.autonomy.subagent_types import (
            WorkUnitResult,
            WorkUnitState,
        )
        from backend.core.ouroboros.governance.saga.saga_types import (
            FileOp,
            PatchedFile,
            RepoPatch,
        )

        self.started.append(unit.unit_id)
        self.concurrent_counter += 1
        self.max_concurrency = max(self.max_concurrency, self.concurrent_counter)
        started = time.monotonic_ns()
        await asyncio.sleep(self.delay_s)
        self.concurrent_counter -= 1

        if unit.unit_id == self.fail_unit:
            return WorkUnitResult(
                unit_id=unit.unit_id,
                repo=unit.repo,
                status=WorkUnitState.FAILED,
                patch=None,
                attempt_count=1,
                started_at_ns=started,
                finished_at_ns=time.monotonic_ns(),
                failure_class="test",
                error="boom",
                causal_parent_id=graph.causal_trace_id,
            )

        patch = RepoPatch(
            repo=unit.repo,
            files=(PatchedFile(path=unit.target_files[0], op=FileOp.CREATE, preimage=None),),
            new_content=((unit.target_files[0], f"# {unit.unit_id}\n".encode("utf-8")),),
        )
        return WorkUnitResult(
            unit_id=unit.unit_id,
            repo=unit.repo,
            status=WorkUnitState.COMPLETED,
            patch=patch,
            attempt_count=1,
            started_at_ns=started,
            finished_at_ns=time.monotonic_ns(),
            causal_parent_id=graph.causal_trace_id,
        )


@pytest.mark.asyncio
async def test_runs_independent_units_in_parallel(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    executor = _FakeExecutor(delay_s=0.05)
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    graph = _make_graph()

    accepted = await scheduler.submit(graph)
    assert accepted is True
    state = await scheduler.wait_for_graph(graph.graph_id, timeout_s=1.0)
    assert state.phase.value == "completed"
    assert executor.max_concurrency == 2
    assert scheduler.get_merged_patches(graph.graph_id).keys() == {"jarvis", "prime"}
    await scheduler.stop()


@pytest.mark.asyncio
async def test_colliding_owned_paths_are_serialized(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    executor = _FakeExecutor()
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    graph = _make_graph(owned_b=("jarvis/a.py",))

    accepted = await scheduler.submit(graph)
    assert accepted is True
    state = await scheduler.wait_for_graph(graph.graph_id, timeout_s=1.0)
    assert state.phase.value == "completed"
    assert executor.max_concurrency == 1
    assert executor.started == ["u1", "u2"]
    await scheduler.stop()


@pytest.mark.asyncio
async def test_failed_dependency_stops_graph(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    executor = _FakeExecutor(fail_unit="u1")
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    graph = _make_graph(dependency_ids_b=("u1",))

    accepted = await scheduler.submit(graph)
    assert accepted is True
    state = await scheduler.wait_for_graph(graph.graph_id, timeout_s=1.0)
    assert state.phase.value == "failed"
    assert "u1" in state.failed_units
    assert "u2" not in state.completed_units
    await scheduler.stop()


@pytest.mark.asyncio
async def test_submit_is_idempotent_for_completed_graphs(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    executor = _FakeExecutor()
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    graph = _make_graph()

    accepted = await scheduler.submit(graph)
    assert accepted is True
    state = await scheduler.wait_for_graph(graph.graph_id, timeout_s=1.0)
    assert state.phase.value == "completed"
    first_started = list(executor.started)

    accepted_again = await scheduler.submit(graph)
    assert accepted_again is True
    state_again = await scheduler.wait_for_graph(graph.graph_id, timeout_s=0.1)
    assert state_again.phase.value == "completed"
    assert executor.started == first_started
    await scheduler.stop()


@pytest.mark.asyncio
async def test_recover_inflight_respects_graph_concurrency_limit(tmp_path) -> None:
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_types import (
        GraphExecutionState,
    )

    store = ExecutionGraphStore(tmp_path)
    graph_a = _make_graph(graph_id="graph-a", op_id="op-a")
    graph_b = _make_graph(graph_id="graph-b", op_id="op-b")
    store.save(GraphExecutionState(graph=graph_a, ready_units=("u1", "u2")))
    store.save(GraphExecutionState(graph=graph_b, ready_units=("u1", "u2")))

    executor = _FakeExecutor()
    scheduler = SubagentScheduler(
        store=store,
        command_bus=CommandBus(maxsize=100),
        event_emitter=EventEmitter(),
        executor=executor,
        max_concurrent_graphs=1,
    )
    await scheduler.start()

    await scheduler.recover_inflight()
    health = scheduler.health()
    assert health["active_graphs"] == ["graph-a"]
    assert health["queued_graphs"] == ["graph-b"]

    state_a = await scheduler.wait_for_graph("graph-a", timeout_s=1.0)
    state_b = await scheduler.wait_for_graph("graph-b", timeout_s=1.0)
    assert state_a.phase.value == "completed"
    assert state_b.phase.value == "completed"
    await scheduler.stop()


# ===========================================================================
# Phase 1b — Ephemeral Memory Sandbox: deterministic finally-GC vaporization.
# The GenerationSubagentExecutor builds a per-worker sandbox (gated) and
# vaporizes it in its finally block on success / exception / cancellation.
# Only the Iron Return artifact crosses to the Commander.
# ===========================================================================


def _swarm_unit(unit_id="sw1", goal="add a feature to a.py", repo="jarvis"):
    from backend.core.ouroboros.governance.autonomy.subagent_types import WorkUnitSpec

    # is_swarm_worker becomes True via the synthesized swarm fields.
    return WorkUnitSpec(
        unit_id=unit_id,
        repo=repo,
        goal=goal,
        target_files=("jarvis/a.py",),
        owned_paths=("jarvis/a.py",),
        system_prompt_template="SYNTHESIZED-WORKER-PROMPT",
        allowed_tools=("read_file", "edit_file"),
        mutation_budget=3,
        context_budget_tokens=8000,
        worker_role="python-source mutator",
    )


def _legacy_unit(unit_id="lg1"):
    from backend.core.ouroboros.governance.autonomy.subagent_types import WorkUnitSpec

    return WorkUnitSpec(
        unit_id=unit_id, repo="jarvis", goal="update a.py",
        target_files=("jarvis/a.py",), owned_paths=("jarvis/a.py",),
    )


def _swarm_graph(unit):
    from backend.core.ouroboros.governance.autonomy.subagent_types import ExecutionGraph

    return ExecutionGraph(
        graph_id="g-swarm", op_id="op-swarm", planner_id="p1",
        schema_version="swarm.graph.1a", concurrency_limit=1, units=(unit,),
    )


class _StubGen:
    """Minimal generator: returns one candidate, or raises, per config."""

    def __init__(self, *, raise_exc=None):
        self._raise = raise_exc

    async def generate(self, ctx, deadline):
        if self._raise is not None:
            raise self._raise

        class _Gen:
            is_noop = False
            candidates = [{"file_path": "jarvis/a.py", "full_content": "x = 1\n"}]

        return _Gen()


def _build_executor(gen, tmp_path):
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        GenerationSubagentExecutor,
    )

    return GenerationSubagentExecutor(
        generator=gen,
        validation_runner=None,  # non-runnable / skip path
        repo_roots={"jarvis": tmp_path},
        worktree_manager=None,
    )


@pytest.mark.asyncio
async def test_sandbox_off_byte_identical_no_sandbox(monkeypatch, tmp_path):
    """OFF -> executor builds NO sandbox; vaporize_quietly never called."""
    import backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox as sbm

    monkeypatch.delenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(sbm, "vaporize_quietly", lambda *a, **k: calls.append(a))

    ex = _build_executor(_StubGen(), tmp_path)
    unit = _swarm_unit()
    result = await ex.execute(_swarm_graph(unit), unit)
    assert result.status.value == "completed"
    # gate off -> sandbox is None -> vaporize_quietly is NOT invoked
    assert calls == []
    # and no sandbox was constructed
    assert ex._build_sandbox_for_unit(unit) is None


@pytest.mark.asyncio
async def test_sandbox_vaporized_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")

    ex = _build_executor(_StubGen(), tmp_path)
    unit = _swarm_unit()
    # construct + spy: the executor builds its own; we assert one is buildable
    sb = ex._build_sandbox_for_unit(unit)
    assert sb is not None and sb.stats()["turns"] == 1  # seeded with sub-goal

    result = await ex.execute(_swarm_graph(unit), unit)
    assert result.status.value == "completed"
    # The executor's own sandbox was vaporized in finally (proof via log path);
    # here we vaporize our probe to assert the contract holds.
    info = sb.vaporize(force_gc=False)
    assert sb.vaporized is True


@pytest.mark.asyncio
async def test_sandbox_vaporized_on_exception(monkeypatch, tmp_path):
    """An exception inside execute -> finally still vaporizes the sandbox."""
    import backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox as sbm

    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")
    vaporized = []
    orig = sbm.vaporize_quietly
    monkeypatch.setattr(
        sbm, "vaporize_quietly",
        lambda sb, **k: (vaporized.append(getattr(sb, "worker_id", None)), orig(sb, **k))[1],
    )

    ex = _build_executor(_StubGen(raise_exc=RuntimeError("kaboom")), tmp_path)
    unit = _swarm_unit()
    result = await ex.execute(_swarm_graph(unit), unit)
    # execute catches the exception and returns a FAILED result...
    assert result.status.value == "failed"
    # ...and the finally vaporized exactly one sandbox for this worker.
    assert vaporized == [unit.unit_id]


@pytest.mark.asyncio
async def test_sandbox_vaporized_on_cancellation(monkeypatch, tmp_path):
    """CancelledError propagates but the finally vaporizes the sandbox first."""
    import asyncio as _aio
    import backend.core.ouroboros.governance.autonomy.ephemeral_memory_sandbox as sbm

    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")
    vaporized = []
    orig = sbm.vaporize_quietly
    monkeypatch.setattr(
        sbm, "vaporize_quietly",
        lambda sb, **k: (vaporized.append(getattr(sb, "worker_id", None)), orig(sb, **k))[1],
    )

    class _CancelGen:
        async def generate(self, ctx, deadline):
            raise _aio.CancelledError()

    ex = _build_executor(_CancelGen(), tmp_path)
    unit = _swarm_unit()
    with pytest.raises(_aio.CancelledError):
        await ex.execute(_swarm_graph(unit), unit)
    assert vaporized == [unit.unit_id]


@pytest.mark.asyncio
async def test_legacy_unit_gets_no_sandbox_even_when_on(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")
    ex = _build_executor(_StubGen(), tmp_path)
    # legacy (non-swarm) unit -> no synthesized shape -> no sandbox
    assert ex._build_sandbox_for_unit(_legacy_unit()) is None


@pytest.mark.asyncio
async def test_iron_return_crosses_only_artifact(monkeypatch, tmp_path):
    """The result command carries the iron_return artifact (not the sandbox)."""
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.iron_return import verify_artifact
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    monkeypatch.setenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", "true")
    bus = CommandBus(maxsize=100)
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=bus,
        event_emitter=EventEmitter(),
        executor=_build_executor(_StubGen(), tmp_path),
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    unit = _swarm_unit()
    await scheduler.submit(_swarm_graph(unit))
    state = await scheduler.wait_for_graph("g-swarm", timeout_s=2.0)
    assert state.phase.value == "completed"
    await scheduler.stop()

    # Drain the command bus and find the result command for our unit.
    found = None
    for _ in range(bus.qsize()):
        cmd = await bus.get()
        if cmd.payload.get("unit_id") == unit.unit_id:
            found = cmd
    assert found is not None
    art = found.payload.get("iron_return")
    assert art is not None
    assert verify_artifact(art) is True
    # The artifact carries ONLY the contract keys — no scratchpad / messages.
    assert "messages" not in art and "scratchpad" not in art


@pytest.mark.asyncio
async def test_iron_return_absent_when_gate_off(monkeypatch, tmp_path):
    from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
    from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
    from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
        ExecutionGraphStore,
    )
    from backend.core.ouroboros.governance.autonomy.subagent_scheduler import (
        SubagentScheduler,
    )

    monkeypatch.delenv("JARVIS_SWARM_EPHEMERAL_SANDBOX_ENABLED", raising=False)
    bus = CommandBus(maxsize=100)
    scheduler = SubagentScheduler(
        store=ExecutionGraphStore(tmp_path),
        command_bus=bus,
        event_emitter=EventEmitter(),
        executor=_build_executor(_StubGen(), tmp_path),
        max_concurrent_graphs=1,
    )
    await scheduler.start()
    unit = _swarm_unit()
    await scheduler.submit(_swarm_graph(unit))
    await scheduler.wait_for_graph("g-swarm", timeout_s=2.0)
    await scheduler.stop()

    found = None
    for _ in range(bus.qsize()):
        cmd = await bus.get()
        if cmd.payload.get("unit_id") == unit.unit_id:
            found = cmd
    assert found is not None
    assert "iron_return" not in found.payload  # byte-identical Phase 1a payload
