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
