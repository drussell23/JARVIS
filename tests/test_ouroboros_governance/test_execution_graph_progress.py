"""Tests for the ExecutionGraph progress tracker (Phase 3b).

Covers:

* Event translation — raw ``EXECUTION_GRAPH_STATE_CHANGED`` /
  ``WORK_UNIT_STATE_CHANGED`` / ``MERGE_DECISION_RECORDED`` events become
  richer ``GraphEvent`` records with typed kinds.
* Per-unit state transitions and timing fields.
* Graph-level aggregation: ``completion_pct`` and ``critical_path``.
* Subscriber fan-out with bounded queues and drop-on-full behaviour.
* Retention cap via ``JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED``.
* Env gating via ``JARVIS_EXEC_GRAPH_PROGRESS_ENABLED``.
* Default-tracker install/reset singleton plumbing.

The tracker is a pure observer of the scheduler's existing event
emitter; none of these tests boot the real scheduler — they fake events
directly through an in-process ``EventEmitter``.
"""

from __future__ import annotations

import asyncio
from typing import Iterator, List

import pytest

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (
    ExecutionGraphProgressTracker,
    GraphEvent,
    GraphEventKind,
    GraphProgress,
    UnitProgress,
    get_default_tracker,
    install_default_tracker,
    reset_default_tracker,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    WorkUnitSpec,
    WorkUnitState,
)


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in (
        "JARVIS_EXEC_GRAPH_PROGRESS_ENABLED",
        "JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED",
        "JARVIS_EXEC_GRAPH_PROGRESS_QUEUE_SIZE",
    ):
        monkeypatch.delenv(key, raising=False)
    reset_default_tracker()
    yield
    reset_default_tracker()


def _make_unit(
    unit_id: str,
    *,
    repo: str = "jarvis",
    goal: str = "implement feature",
    target_files: tuple = ("a.py",),
    deps: tuple = (),
    barrier_id: str = "",
    timeout_s: float = 180.0,
) -> WorkUnitSpec:
    return WorkUnitSpec(
        unit_id=unit_id,
        repo=repo,
        goal=goal,
        target_files=target_files,
        dependency_ids=deps,
        barrier_id=barrier_id,
        timeout_s=timeout_s,
    )


def _make_graph(units: tuple, *, graph_id: str = "g1", op_id: str = "op1") -> ExecutionGraph:
    return ExecutionGraph(
        graph_id=graph_id,
        op_id=op_id,
        planner_id="planner-1",
        schema_version="2d.1",
        units=units,
        concurrency_limit=2,
    )


async def _emit_graph(
    emitter: EventEmitter,
    *,
    graph_id: str,
    op_id: str,
    phase: GraphExecutionPhase,
    ready: tuple = (),
    running: tuple = (),
    completed: tuple = (),
    failed: tuple = (),
    cancelled: tuple = (),
    last_error: str = "",
) -> None:
    await emitter.emit(
        EventEnvelope(
            source_layer="L1",
            event_type=EventType.EXECUTION_GRAPH_STATE_CHANGED,
            payload={
                "graph_id": graph_id,
                "phase": phase.value,
                "ready_units": list(ready),
                "running_units": list(running),
                "completed_units": list(completed),
                "failed_units": list(failed),
                "cancelled_units": list(cancelled),
                "last_error": last_error,
            },
            op_id=op_id,
        )
    )


async def _emit_unit(
    emitter: EventEmitter,
    *,
    graph_id: str,
    op_id: str,
    unit_id: str,
    state: WorkUnitState,
    repo: str = "jarvis",
    runtime_ms: float = 0.0,
    error: str = "",
    failure_class: str = "",
) -> None:
    await emitter.emit(
        EventEnvelope(
            source_layer="L1",
            event_type=EventType.WORK_UNIT_STATE_CHANGED,
            payload={
                "graph_id": graph_id,
                "unit_id": unit_id,
                "status": state.value,
                "repo": repo,
                "runtime_ms": runtime_ms,
                "error": error,
                "failure_class": failure_class,
                "owned_paths": [],
                "barrier_id": "",
            },
            op_id=op_id,
        )
    )


# ---------------------------------------------------------------------------
# Construction & binding
# ---------------------------------------------------------------------------


class TestTrackerConstruction:
    def test_auto_binds_on_construction(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        assert tracker._bound is True
        assert emitter.subscriber_count(EventType.EXECUTION_GRAPH_STATE_CHANGED) == 1
        assert emitter.subscriber_count(EventType.WORK_UNIT_STATE_CHANGED) == 1
        assert emitter.subscriber_count(EventType.MERGE_DECISION_RECORDED) == 1

    def test_disabled_via_env_does_not_bind(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_EXEC_GRAPH_PROGRESS_ENABLED", "false")
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        assert tracker._bound is False
        assert emitter.subscriber_count(EventType.EXECUTION_GRAPH_STATE_CHANGED) == 0

    def test_none_emitter_is_safe(self) -> None:
        tracker = ExecutionGraphProgressTracker(None)
        assert tracker._bound is False
        # Snapshot surface still works for consumers.
        assert tracker.snapshot("anything") is None
        assert tracker.all_active() == []

    def test_constructor_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED", "5")
        tracker = ExecutionGraphProgressTracker(
            EventEmitter(), max_retained=12, queue_size=7
        )
        stats = tracker.stats()
        assert stats["max_retained"] == 12
        assert stats["queue_size"] == 7

    def test_bind_is_idempotent(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        tracker.bind()
        tracker.bind()
        # Still exactly one subscriber per event type.
        assert emitter.subscriber_count(EventType.EXECUTION_GRAPH_STATE_CHANGED) == 1


# ---------------------------------------------------------------------------
# Register graph + event translation
# ---------------------------------------------------------------------------


class TestGraphLifecycle:
    @pytest.mark.asyncio
    async def test_register_graph_seeds_unit_specs(self) -> None:
        tracker = ExecutionGraphProgressTracker(EventEmitter())
        graph = _make_graph(
            (
                _make_unit("u1", target_files=("a.py",)),
                _make_unit("u2", target_files=("b.py",), deps=("u1",)),
            )
        )
        tracker.register_graph(graph)
        snap = tracker.snapshot("g1")
        assert snap is not None
        assert set(snap.units.keys()) == {"u1", "u2"}
        assert snap.units["u1"].state == WorkUnitState.PENDING
        assert snap.units["u2"].dependency_ids == ("u1",)

    @pytest.mark.asyncio
    async def test_register_graph_is_idempotent(self) -> None:
        tracker = ExecutionGraphProgressTracker(EventEmitter())
        graph = _make_graph((_make_unit("u1"),))
        first = tracker.register_graph(graph)
        second = tracker.register_graph(graph)
        assert first is second

    @pytest.mark.asyncio
    async def test_phase_transitions_are_recorded(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.CREATED,
            ready=("u1",),
        )
        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.RUNNING,
            running=("u1",),
        )
        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.COMPLETED,
            completed=("u1",),
        )

        snap = tracker.snapshot("g1")
        assert snap is not None
        assert snap.phase == GraphExecutionPhase.COMPLETED
        assert snap.started_at_ns is not None
        assert snap.finished_at_ns is not None
        # All units are terminal after graph completion.
        assert all(u.is_terminal for u in snap.units.values())

    @pytest.mark.asyncio
    async def test_ready_event_synthesized_from_ready_units(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph(
            (_make_unit("u1"), _make_unit("u2", deps=("u1",)))
        )
        tracker.register_graph(graph)
        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.CREATED,
            ready=("u1",),
        )
        snap = tracker.snapshot("g1")
        assert snap is not None
        assert snap.units["u1"].ready_at_ns is not None
        assert snap.units["u2"].ready_at_ns is None


# ---------------------------------------------------------------------------
# Unit-level events
# ---------------------------------------------------------------------------


class TestUnitEvents:
    @pytest.mark.asyncio
    async def test_unit_running_then_completed(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.RUNNING,
        )
        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.COMPLETED,
            runtime_ms=123.0,
        )

        snap = tracker.snapshot("g1")
        assert snap is not None
        unit = snap.units["u1"]
        assert unit.state == WorkUnitState.COMPLETED
        assert unit.started_at_ns is not None
        assert unit.finished_at_ns is not None
        assert unit.runtime_ms == 123.0

    @pytest.mark.asyncio
    async def test_unit_failure_captures_error_fields(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.RUNNING,
        )
        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.FAILED,
            runtime_ms=50.0,
            error="validation failed",
            failure_class="validation",
        )
        snap = tracker.snapshot("g1")
        assert snap is not None
        unit = snap.units["u1"]
        assert unit.state == WorkUnitState.FAILED
        assert unit.error == "validation failed"
        assert unit.failure_class == "validation"

    @pytest.mark.asyncio
    async def test_unknown_unit_synthesizes_placeholder(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        # Never registered — tracker should still absorb the event.
        await _emit_unit(
            emitter,
            graph_id="unknown-graph",
            op_id="op1",
            unit_id="phantom",
            state=WorkUnitState.RUNNING,
        )
        snap = tracker.snapshot("unknown-graph")
        assert snap is not None
        assert "phantom" in snap.units
        assert snap.units["phantom"].state == WorkUnitState.RUNNING


# ---------------------------------------------------------------------------
# Merge events
# ---------------------------------------------------------------------------


class TestMergeEvents:
    @pytest.mark.asyncio
    async def test_merge_decision_recorded(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        await emitter.emit(
            EventEnvelope(
                source_layer="L1",
                event_type=EventType.MERGE_DECISION_RECORDED,
                payload={
                    "graph_id": "g1",
                    "repo": "jarvis",
                    "barrier_id": "bar-1",
                    "decision_hash": "abc123",
                    "merged_unit_ids": ["u1"],
                },
                op_id="op1",
            )
        )
        snap = tracker.snapshot("g1")
        assert snap is not None
        assert len(snap.merge_decisions) == 1
        assert snap.merge_decisions[0]["barrier_id"] == "bar-1"
        assert snap.merge_decisions[0]["merged_unit_ids"] == ["u1"]


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


class TestAggregation:
    def test_completion_pct_counts_terminal_units(self) -> None:
        graph = _make_graph(
            (_make_unit("u1"), _make_unit("u2"), _make_unit("u3"))
        )
        progress = GraphProgress.from_graph(graph)
        progress.units["u1"].state = WorkUnitState.COMPLETED
        progress.units["u2"].state = WorkUnitState.FAILED
        assert progress.completion_pct() == pytest.approx(2 / 3, abs=1e-3)

    def test_completion_pct_empty_graph(self) -> None:
        # Defensive: synthesized stub has no units.
        progress = GraphProgress(
            graph_id="gx",
            op_id="op",
            planner_id="",
            schema_version="",
            concurrency_limit=1,
            plan_digest="",
            units=__import__("collections").OrderedDict(),
        )
        assert progress.completion_pct() == 0.0

    def test_critical_path_diamond(self) -> None:
        # u1 -> (u2, u3) -> u4
        graph = _make_graph(
            (
                _make_unit("u1"),
                _make_unit("u2", deps=("u1",)),
                _make_unit("u3", deps=("u1",)),
                _make_unit("u4", deps=("u2", "u3")),
            )
        )
        progress = GraphProgress.from_graph(graph)
        # All PENDING — weight falls back to timeout_s*1000 = 180000.
        path = progress.critical_path()
        assert len(path) == 3  # one branch through diamond
        assert path[0] == "u1"
        assert path[-1] == "u4"

    def test_critical_path_prefers_heaviest_branch(self) -> None:
        graph = _make_graph(
            (
                _make_unit("u1"),
                _make_unit("u2", deps=("u1",)),
                _make_unit("u3", deps=("u1",)),
                _make_unit("u4", deps=("u2", "u3")),
            )
        )
        progress = GraphProgress.from_graph(graph)
        # u2 finished fast; u3 slow.
        for uid, runtime_ms in (("u1", 100.0), ("u2", 50.0), ("u3", 5000.0), ("u4", 200.0)):
            progress.units[uid].state = WorkUnitState.COMPLETED
            progress.units[uid].runtime_ms = runtime_ms
            progress.units[uid].started_at_ns = 0
            progress.units[uid].finished_at_ns = int(runtime_ms * 1_000_000)
        path = progress.critical_path()
        assert path == ["u1", "u3", "u4"]

    def test_units_by_status_buckets(self) -> None:
        graph = _make_graph(
            (_make_unit("u1"), _make_unit("u2"), _make_unit("u3"))
        )
        progress = GraphProgress.from_graph(graph)
        progress.units["u1"].state = WorkUnitState.COMPLETED
        progress.units["u2"].state = WorkUnitState.RUNNING
        buckets = progress.units_by_status()
        assert len(buckets[WorkUnitState.COMPLETED]) == 1
        assert len(buckets[WorkUnitState.RUNNING]) == 1
        assert len(buckets[WorkUnitState.PENDING]) == 1


# ---------------------------------------------------------------------------
# Subscriber fan-out
# ---------------------------------------------------------------------------


class TestSubscriberFanout:
    @pytest.mark.asyncio
    async def test_subscriber_receives_events(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        received: List[GraphEvent] = []

        async def consume() -> None:
            async for event in tracker.subscribe(name="test"):
                received.append(event)
                if event.kind == GraphEventKind.GRAPH_COMPLETED:
                    break

        task = asyncio.create_task(consume())
        # Give the subscriber a chance to enter the async-for loop.
        await asyncio.sleep(0)

        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.RUNNING,
        )
        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.RUNNING,
        )
        await _emit_unit(
            emitter,
            graph_id="g1",
            op_id="op1",
            unit_id="u1",
            state=WorkUnitState.COMPLETED,
            runtime_ms=1.0,
        )
        await _emit_graph(
            emitter,
            graph_id="g1",
            op_id="op1",
            phase=GraphExecutionPhase.COMPLETED,
        )

        await asyncio.wait_for(task, timeout=2.0)
        kinds = [e.kind for e in received]
        assert GraphEventKind.GRAPH_STARTED in kinds
        assert GraphEventKind.UNIT_STARTED in kinds
        assert GraphEventKind.UNIT_COMPLETED in kinds
        assert GraphEventKind.GRAPH_COMPLETED in kinds

    @pytest.mark.asyncio
    async def test_slow_subscriber_drops_instead_of_blocks(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter, queue_size=2)
        graph = _make_graph((_make_unit("u1"),))
        tracker.register_graph(graph)

        # Subscribe but never consume.
        _iter = tracker.subscribe(name="slow")  # noqa: F841

        # Flood 10 events into a queue with capacity 2.
        for i in range(10):
            await _emit_unit(
                emitter,
                graph_id="g1",
                op_id="op1",
                unit_id="u1",
                state=WorkUnitState.RUNNING if i % 2 == 0 else WorkUnitState.COMPLETED,
            )
        stats = tracker.stats()
        assert stats["events_emitted"] >= 1
        # Scheduler throughput was preserved — no raised exception.


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetention:
    @pytest.mark.asyncio
    async def test_terminal_graphs_evicted_past_cap(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter, max_retained=2)

        for i in range(4):
            gid = f"g{i}"
            graph = _make_graph(
                (_make_unit("u1"),),
                graph_id=gid,
                op_id=f"op{i}",
            )
            tracker.register_graph(graph)
            await _emit_graph(
                emitter,
                graph_id=gid,
                op_id=f"op{i}",
                phase=GraphExecutionPhase.COMPLETED,
            )

        assert len(tracker.all_tracked()) == 2
        # Oldest two evicted.
        assert tracker.snapshot("g0") is None
        assert tracker.snapshot("g1") is None
        assert tracker.snapshot("g2") is not None
        assert tracker.snapshot("g3") is not None

    @pytest.mark.asyncio
    async def test_active_graphs_never_evicted(self) -> None:
        emitter = EventEmitter()
        tracker = ExecutionGraphProgressTracker(emitter, max_retained=2)
        # Graph g0 stays active.
        tracker.register_graph(
            _make_graph((_make_unit("u1"),), graph_id="g0", op_id="op0")
        )
        # Flood terminal graphs.
        for i in range(1, 5):
            gid = f"g{i}"
            tracker.register_graph(
                _make_graph((_make_unit("u1"),), graph_id=gid, op_id=f"op{i}")
            )
            await _emit_graph(
                emitter,
                graph_id=gid,
                op_id=f"op{i}",
                phase=GraphExecutionPhase.COMPLETED,
            )
        assert tracker.snapshot("g0") is not None


# ---------------------------------------------------------------------------
# Unit & dataclass helpers
# ---------------------------------------------------------------------------


class TestDataclasses:
    def test_unit_progress_elapsed_ms_running(self) -> None:
        unit = UnitProgress.from_spec(_make_unit("u1"))
        unit.state = WorkUnitState.RUNNING
        unit.started_at_ns = 0  # t=0
        # elapsed_ms should return non-negative for running unit
        assert unit.elapsed_ms >= 0.0

    def test_unit_progress_as_dict_shape(self) -> None:
        unit = UnitProgress.from_spec(_make_unit("u1", target_files=("x.py",)))
        d = unit.as_dict()
        assert d["unit_id"] == "u1"
        assert d["state"] == "pending"
        assert d["target_files"] == ["x.py"]
        assert d["is_terminal"] is False

    def test_graph_event_as_dict_preserves_payload(self) -> None:
        event = GraphEvent(
            kind=GraphEventKind.UNIT_STARTED,
            graph_id="g1",
            op_id="op1",
            ts_ns=100,
            unit_id="u1",
            payload={"foo": "bar"},
        )
        d = event.as_dict()
        assert d["kind"] == "unit.started"
        assert d["payload"] == {"foo": "bar"}


# ---------------------------------------------------------------------------
# Default tracker singleton
# ---------------------------------------------------------------------------


class TestDefaultTracker:
    def test_default_is_none_until_installed(self) -> None:
        assert get_default_tracker() is None

    def test_install_and_reset(self) -> None:
        tracker = ExecutionGraphProgressTracker(EventEmitter())
        install_default_tracker(tracker)
        assert get_default_tracker() is tracker
        reset_default_tracker()
        assert get_default_tracker() is None
