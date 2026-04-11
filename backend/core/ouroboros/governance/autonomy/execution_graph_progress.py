"""backend/core/ouroboros/governance/autonomy/execution_graph_progress.py

ExecutionGraph Progress Tracker — Phase 3b operational visibility for the
multi-op L3 execution graphs.

The SubagentScheduler already emits coarse pub-sub events via EventEmitter
(``EXECUTION_GRAPH_STATE_CHANGED`` / ``WORK_UNIT_STATE_CHANGED``). Those are
excellent for coordination between L1/L2/L3/L4 but are too raw for CLI
rendering or operator dashboards: they don't carry per-unit wall-clock,
completion ratio, critical-path hints, or bounded retention.

This module provides a **stateful aggregator** that:

* Subscribes to the scheduler's existing EventEmitter — **no scheduler
  changes required**. The tracker is a pure observer.
* Translates raw autonomy events into richer ``GraphEvent`` records
  (enum-tagged, ns-timestamped, plus computed fields like elapsed_ms).
* Maintains a live ``GraphProgress`` snapshot per graph_id with
  per-unit status, start/finish timestamps, attempt counts, failure
  reason, and patch file summaries.
* Computes the current critical path (longest weighted chain through
  the unit DAG) on demand — used by the SerpentFlow renderer to
  highlight the stragglers blocking graph completion.
* Offers an async fan-out for multiple subscribers (SerpentFlow,
  dashboards, tests) via bounded ``asyncio.Queue`` channels. Slow
  consumers are dropped rather than blocking the scheduler.
* Bounds retention (``JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED``,
  default 50) so long-running sessions don't leak memory.

Env gates:

* ``JARVIS_EXEC_GRAPH_PROGRESS_ENABLED`` — master switch (default
  ``true``). When ``false`` the tracker is a no-op shell.
* ``JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED`` — LRU cap on completed
  graph snapshots (default ``50``).
* ``JARVIS_EXEC_GRAPH_PROGRESS_QUEUE_SIZE`` — subscriber channel
  capacity before drop (default ``128``).

The tracker is designed to be consumed by:

1. ``SerpentFlow`` — the flowing CLI renders graph progress as a live
   multi-lane view (Task #204).
2. ``GovernedLoopService`` — exposes the default tracker via its
   ``execution_graph_tracker`` attribute so external observers can
   subscribe (Task #205).
3. Tests — direct construction with a fresh EventEmitter and explicit
   event injection (Task #206).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    Any,
    AsyncIterator,
    Dict,
    List,
    Mapping,
    Optional,
    Set,
    Tuple,
)

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.event_emitter import EventEmitter
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    WorkUnitSpec,
    WorkUnitState,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() not in ("false", "0", "no", "off", "")


def _env_int(key: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.debug("ExecutionGraphProgress: env %s=%r not int, using %d", key, raw, default)
        return default
    return max(minimum, value)


_DEFAULT_MAX_RETAINED = 50
_DEFAULT_QUEUE_SIZE = 128
_DEFAULT_ENABLED = True


# ---------------------------------------------------------------------------
# Event taxonomy
# ---------------------------------------------------------------------------


class GraphEventKind(str, Enum):
    """Progress-tracker event taxonomy.

    These are richer than the raw ``EXECUTION_GRAPH_STATE_CHANGED`` /
    ``WORK_UNIT_STATE_CHANGED`` events emitted by the scheduler: the
    tracker synthesizes lifecycle edges (SUBMITTED vs STARTED, UNIT_READY
    vs UNIT_STARTED) by diffing successive raw snapshots.
    """

    # Graph-level lifecycle
    GRAPH_SUBMITTED = "graph.submitted"
    GRAPH_STARTED = "graph.started"
    GRAPH_COMPLETED = "graph.completed"
    GRAPH_FAILED = "graph.failed"
    GRAPH_CANCELLED = "graph.cancelled"

    # Unit-level lifecycle
    UNIT_READY = "unit.ready"
    UNIT_STARTED = "unit.started"
    UNIT_COMPLETED = "unit.completed"
    UNIT_FAILED = "unit.failed"
    UNIT_CANCELLED = "unit.cancelled"

    # Coordination
    MERGE_DECIDED = "merge.decided"


_GRAPH_PHASE_TO_KIND: Dict[GraphExecutionPhase, GraphEventKind] = {
    GraphExecutionPhase.CREATED: GraphEventKind.GRAPH_SUBMITTED,
    GraphExecutionPhase.RUNNING: GraphEventKind.GRAPH_STARTED,
    GraphExecutionPhase.COMPLETED: GraphEventKind.GRAPH_COMPLETED,
    GraphExecutionPhase.FAILED: GraphEventKind.GRAPH_FAILED,
    GraphExecutionPhase.CANCELLED: GraphEventKind.GRAPH_CANCELLED,
}

_UNIT_STATE_TO_KIND: Dict[WorkUnitState, GraphEventKind] = {
    WorkUnitState.PENDING: GraphEventKind.UNIT_READY,  # synthesized when a unit becomes ready
    WorkUnitState.RUNNING: GraphEventKind.UNIT_STARTED,
    WorkUnitState.COMPLETED: GraphEventKind.UNIT_COMPLETED,
    WorkUnitState.FAILED: GraphEventKind.UNIT_FAILED,
    WorkUnitState.CANCELLED: GraphEventKind.UNIT_CANCELLED,
}

_TERMINAL_UNIT_STATES: frozenset = frozenset(
    {WorkUnitState.COMPLETED, WorkUnitState.FAILED, WorkUnitState.CANCELLED}
)


# ---------------------------------------------------------------------------
# Event / state records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraphEvent:
    """Immutable progress event record.

    Emitted to subscribers in monotonic order per graph. ``payload``
    carries kind-specific extras (failure_class, runtime_ms, merged
    unit ids, etc.). Callers should treat ``payload`` as read-only.
    """

    kind: GraphEventKind
    graph_id: str
    op_id: str
    ts_ns: int
    unit_id: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "graph_id": self.graph_id,
            "op_id": self.op_id,
            "unit_id": self.unit_id,
            "ts_ns": self.ts_ns,
            "payload": dict(self.payload),
        }


@dataclass
class UnitProgress:
    """Mutable per-unit progress view.

    Unlike ``WorkUnitSpec`` (frozen, planning-time) this tracks
    runtime state as the scheduler advances the unit. Instances are
    owned by a ``GraphProgress`` and should only be mutated through
    the tracker under its async lock.
    """

    unit_id: str
    repo: str
    goal: str
    target_files: Tuple[str, ...]
    dependency_ids: Tuple[str, ...]
    owned_paths: Tuple[str, ...]
    barrier_id: str
    timeout_s: float
    state: WorkUnitState = WorkUnitState.PENDING
    ready_at_ns: Optional[int] = None
    started_at_ns: Optional[int] = None
    finished_at_ns: Optional[int] = None
    attempt_count: int = 0
    failure_class: str = ""
    error: str = ""
    runtime_ms: float = 0.0
    patch_file_count: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL_UNIT_STATES

    @property
    def elapsed_ms(self) -> float:
        """Ms elapsed so far — wall clock while running, total once finished."""
        if self.started_at_ns is None:
            return 0.0
        end_ns = self.finished_at_ns or time.monotonic_ns()
        return round((end_ns - self.started_at_ns) / 1_000_000, 3)

    @classmethod
    def from_spec(cls, spec: WorkUnitSpec) -> "UnitProgress":
        return cls(
            unit_id=spec.unit_id,
            repo=spec.repo,
            goal=spec.goal,
            target_files=tuple(spec.target_files),
            dependency_ids=tuple(spec.dependency_ids),
            owned_paths=tuple(spec.effective_owned_paths),
            barrier_id=spec.barrier_id,
            timeout_s=spec.timeout_s,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "repo": self.repo,
            "goal": self.goal,
            "target_files": list(self.target_files),
            "dependency_ids": list(self.dependency_ids),
            "owned_paths": list(self.owned_paths),
            "barrier_id": self.barrier_id,
            "timeout_s": self.timeout_s,
            "state": self.state.value,
            "ready_at_ns": self.ready_at_ns,
            "started_at_ns": self.started_at_ns,
            "finished_at_ns": self.finished_at_ns,
            "attempt_count": self.attempt_count,
            "failure_class": self.failure_class,
            "error": self.error,
            "runtime_ms": self.runtime_ms,
            "elapsed_ms": self.elapsed_ms,
            "patch_file_count": self.patch_file_count,
            "is_terminal": self.is_terminal,
        }


@dataclass
class GraphProgress:
    """Aggregated view of a single execution graph's progress.

    Holds the authoritative ``UnitProgress`` entries plus graph-level
    phase, timing, and a short event history for late subscribers.
    """

    graph_id: str
    op_id: str
    planner_id: str
    schema_version: str
    concurrency_limit: int
    plan_digest: str
    units: "OrderedDict[str, UnitProgress]"
    phase: GraphExecutionPhase = GraphExecutionPhase.CREATED
    submitted_at_ns: int = field(default_factory=time.monotonic_ns)
    started_at_ns: Optional[int] = None
    finished_at_ns: Optional[int] = None
    last_error: str = ""
    merge_decisions: List[Dict[str, Any]] = field(default_factory=list)
    event_history: List[GraphEvent] = field(default_factory=list)
    _max_history: int = 256

    # --- construction ------------------------------------------------

    @classmethod
    def from_graph(cls, graph: ExecutionGraph) -> "GraphProgress":
        units: "OrderedDict[str, UnitProgress]" = OrderedDict()
        for spec in graph.units:
            units[spec.unit_id] = UnitProgress.from_spec(spec)
        return cls(
            graph_id=graph.graph_id,
            op_id=graph.op_id,
            planner_id=graph.planner_id,
            schema_version=graph.schema_version,
            concurrency_limit=graph.concurrency_limit,
            plan_digest=graph.plan_digest,
            units=units,
        )

    # --- aggregation -------------------------------------------------

    def units_by_status(self) -> Dict[WorkUnitState, List[UnitProgress]]:
        buckets: Dict[WorkUnitState, List[UnitProgress]] = {
            state: [] for state in WorkUnitState
        }
        for unit in self.units.values():
            buckets[unit.state].append(unit)
        return buckets

    def completion_pct(self) -> float:
        if not self.units:
            return 0.0
        finished = sum(1 for u in self.units.values() if u.is_terminal)
        return round(finished / len(self.units), 4)

    @property
    def is_terminal(self) -> bool:
        return self.phase in (
            GraphExecutionPhase.COMPLETED,
            GraphExecutionPhase.FAILED,
            GraphExecutionPhase.CANCELLED,
        )

    @property
    def runtime_ms(self) -> float:
        if self.started_at_ns is None:
            return 0.0
        end_ns = self.finished_at_ns or time.monotonic_ns()
        return round((end_ns - self.started_at_ns) / 1_000_000, 3)

    # --- critical path ----------------------------------------------

    def critical_path(self) -> List[str]:
        """Return the longest weighted chain through the unit DAG.

        Weight = ``runtime_ms`` for terminal units, ``elapsed_ms`` for
        running units, ``timeout_s * 1000`` as an upper-bound estimate
        for units that haven't started yet. This gives a meaningful
        "what is blocking completion" answer at any point in the
        lifecycle, not just post-mortem.

        Uses dynamic programming on a topological order — O(V+E).
        """
        if not self.units:
            return []

        # Build adjacency (parent -> children) and compute indegrees.
        children: Dict[str, List[str]] = {uid: [] for uid in self.units}
        indegree: Dict[str, int] = {uid: 0 for uid in self.units}
        for uid, unit in self.units.items():
            for dep in unit.dependency_ids:
                if dep in self.units:
                    children[dep].append(uid)
                    indegree[uid] += 1

        # Topological sort (Kahn's).
        order: List[str] = []
        queue: List[str] = sorted(uid for uid, deg in indegree.items() if deg == 0)
        indeg = dict(indegree)
        while queue:
            node = queue.pop(0)
            order.append(node)
            for child in sorted(children[node]):
                indeg[child] -= 1
                if indeg[child] == 0:
                    queue.append(child)

        if len(order) != len(self.units):
            # Cycle — shouldn't happen (graph validated at construction)
            return []

        # Longest path DP.
        best: Dict[str, float] = {}
        parent: Dict[str, Optional[str]] = {}
        for uid in order:
            unit = self.units[uid]
            weight = self._unit_weight_ms(unit)
            candidates: List[Tuple[float, Optional[str]]] = []
            for dep in unit.dependency_ids:
                if dep in best:
                    candidates.append((best[dep], dep))
            if candidates:
                base_cost, base_parent = max(candidates, key=lambda p: p[0])
                best[uid] = base_cost + weight
                parent[uid] = base_parent
            else:
                best[uid] = weight
                parent[uid] = None

        if not best:
            return []

        # Walk back from the heaviest terminal.
        terminal = max(best.items(), key=lambda p: p[1])[0]
        chain: List[str] = []
        cursor: Optional[str] = terminal
        while cursor is not None:
            chain.append(cursor)
            cursor = parent.get(cursor)
        chain.reverse()
        return chain

    @staticmethod
    def _unit_weight_ms(unit: UnitProgress) -> float:
        if unit.state == WorkUnitState.COMPLETED or unit.state == WorkUnitState.FAILED:
            return max(unit.runtime_ms, 1.0)
        if unit.state == WorkUnitState.RUNNING:
            return max(unit.elapsed_ms, 1.0)
        if unit.state == WorkUnitState.CANCELLED:
            return max(unit.runtime_ms, 1.0)
        # PENDING — fall back to declared timeout as upper bound.
        return max(unit.timeout_s * 1000.0, 1.0)

    # --- history -----------------------------------------------------

    def record_event(self, event: GraphEvent) -> None:
        self.event_history.append(event)
        if len(self.event_history) > self._max_history:
            # Drop oldest to stay bounded.
            self.event_history = self.event_history[-self._max_history :]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "op_id": self.op_id,
            "planner_id": self.planner_id,
            "schema_version": self.schema_version,
            "concurrency_limit": self.concurrency_limit,
            "plan_digest": self.plan_digest,
            "phase": self.phase.value,
            "submitted_at_ns": self.submitted_at_ns,
            "started_at_ns": self.started_at_ns,
            "finished_at_ns": self.finished_at_ns,
            "runtime_ms": self.runtime_ms,
            "completion_pct": self.completion_pct(),
            "last_error": self.last_error,
            "units": [u.as_dict() for u in self.units.values()],
            "merge_decisions": list(self.merge_decisions),
            "critical_path": self.critical_path(),
        }


# ---------------------------------------------------------------------------
# Subscriber channel
# ---------------------------------------------------------------------------


@dataclass
class _Subscriber:
    """Internal subscriber state — an async queue with a drop counter."""

    queue: "asyncio.Queue[GraphEvent]"
    dropped: int = 0
    name: str = ""


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


class ExecutionGraphProgressTracker:
    """Stateful aggregator for L3 execution graph progress.

    Subscribes to the scheduler's existing EventEmitter and translates
    raw autonomy events into a richer, per-graph progress view. Does
    not mutate scheduler state — purely observational.

    Usage::

        tracker = ExecutionGraphProgressTracker(emitter)
        await tracker.bind()  # register handlers

        # From a consumer:
        async for event in tracker.subscribe():
            render(event, tracker.snapshot(event.graph_id))

    The tracker supports multiple concurrent subscribers. Slow
    consumers drop events (tracked via ``dropped_events``) rather
    than blocking the emitter, preserving scheduler throughput.
    """

    def __init__(
        self,
        emitter: Optional[EventEmitter],
        *,
        enabled: Optional[bool] = None,
        max_retained: Optional[int] = None,
        queue_size: Optional[int] = None,
    ) -> None:
        self._emitter = emitter
        self._enabled = (
            _env_bool("JARVIS_EXEC_GRAPH_PROGRESS_ENABLED", _DEFAULT_ENABLED)
            if enabled is None
            else bool(enabled)
        )
        self._max_retained = (
            _env_int(
                "JARVIS_EXEC_GRAPH_PROGRESS_MAX_RETAINED",
                _DEFAULT_MAX_RETAINED,
                minimum=1,
            )
            if max_retained is None
            else max(1, int(max_retained))
        )
        self._queue_size = (
            _env_int(
                "JARVIS_EXEC_GRAPH_PROGRESS_QUEUE_SIZE",
                _DEFAULT_QUEUE_SIZE,
                minimum=1,
            )
            if queue_size is None
            else max(1, int(queue_size))
        )

        # graph_id -> GraphProgress. OrderedDict for LRU eviction of
        # terminal graphs once _max_retained is exceeded.
        self._graphs: "OrderedDict[str, GraphProgress]" = OrderedDict()

        # Subscriber fan-out.
        self._subscribers: List[_Subscriber] = []

        # Concurrency.
        self._lock = asyncio.Lock()
        self._bound = False

        # Stats.
        self._events_emitted = 0
        self._events_dropped = 0

        # Auto-bind at construction so callers don't need to drive an
        # explicit async lifecycle. Safe because subscribe is sync.
        self.bind()

    # ------------------------------------------------------------------
    # Binding / lifecycle
    # ------------------------------------------------------------------

    def bind(self) -> None:
        """Register scheduler event handlers. Idempotent and safe when
        ``self._emitter`` is ``None`` (the tracker becomes a no-op
        shell usable in tests or when autonomy is disabled)."""
        if self._bound or self._emitter is None or not self._enabled:
            return
        # EventEmitter.subscribe's type signature says handler returns
        # None, but the emitter actually awaits coroutine handlers via
        # inspect.iscoroutinefunction(). The type: ignore is safe.
        self._emitter.subscribe(
            EventType.EXECUTION_GRAPH_STATE_CHANGED,
            self._on_graph_event,  # type: ignore[arg-type]
        )
        self._emitter.subscribe(
            EventType.WORK_UNIT_STATE_CHANGED,
            self._on_unit_event,  # type: ignore[arg-type]
        )
        self._emitter.subscribe(
            EventType.MERGE_DECISION_RECORDED,
            self._on_merge_event,  # type: ignore[arg-type]
        )
        self._bound = True
        logger.debug(
            "ExecutionGraphProgressTracker bound (max_retained=%d, queue_size=%d)",
            self._max_retained,
            self._queue_size,
        )

    def register_graph(self, graph: ExecutionGraph) -> GraphProgress:
        """Create a progress record for *graph* before the first raw
        event arrives. Called by the scheduler adapter at submit time
        so the first observer snapshot already has unit specs loaded.

        Idempotent — a second call returns the existing record.
        """
        existing = self._graphs.get(graph.graph_id)
        if existing is not None:
            return existing
        progress = GraphProgress.from_graph(graph)
        self._graphs[graph.graph_id] = progress
        self._evict_if_needed()
        return progress

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------

    def subscribe(self, *, name: str = "") -> AsyncIterator[GraphEvent]:
        """Return an async iterator that yields events in emission order.

        Subscribers get events emitted **after** they subscribe; historic
        events must be fetched from ``snapshot(graph_id).event_history``.
        The iterator terminates when ``close()`` is called or the
        tracker is garbage-collected.
        """
        sub = _Subscriber(
            queue=asyncio.Queue(maxsize=self._queue_size),
            name=name or f"sub-{len(self._subscribers)}",
        )
        self._subscribers.append(sub)
        return self._drain_subscriber(sub)

    async def _drain_subscriber(self, sub: _Subscriber) -> AsyncIterator[GraphEvent]:
        try:
            while True:
                event = await sub.queue.get()
                yield event
        finally:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def unsubscribe_all(self) -> None:
        """Drop all subscribers. Used on shutdown to release queue refs."""
        self._subscribers.clear()

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self, graph_id: str) -> Optional[GraphProgress]:
        """Return the current ``GraphProgress`` for *graph_id*, or
        ``None`` if no such graph has been tracked."""
        return self._graphs.get(graph_id)

    def all_active(self) -> List[GraphProgress]:
        """Return progress records for graphs that have not terminated."""
        return [gp for gp in self._graphs.values() if not gp.is_terminal]

    def all_tracked(self) -> List[GraphProgress]:
        """Return all currently retained graph progress records."""
        return list(self._graphs.values())

    def stats(self) -> Dict[str, Any]:
        return {
            "enabled": self._enabled,
            "bound": self._bound,
            "tracked_graphs": len(self._graphs),
            "active_graphs": sum(1 for gp in self._graphs.values() if not gp.is_terminal),
            "subscribers": len(self._subscribers),
            "events_emitted": self._events_emitted,
            "events_dropped": self._events_dropped,
            "max_retained": self._max_retained,
            "queue_size": self._queue_size,
        }

    # ------------------------------------------------------------------
    # Handlers (scheduler -> tracker)
    # ------------------------------------------------------------------

    async def _on_graph_event(self, envelope: EventEnvelope) -> None:
        """Translate ``EXECUTION_GRAPH_STATE_CHANGED`` into a
        ``GraphEvent`` and update the per-graph progress record."""
        if not self._enabled:
            return
        payload = envelope.payload or {}
        graph_id = str(payload.get("graph_id") or "")
        if not graph_id:
            return
        phase_raw = payload.get("phase") or GraphExecutionPhase.CREATED.value
        try:
            phase = GraphExecutionPhase(str(phase_raw))
        except ValueError:
            logger.debug("ExecutionGraphProgress: unknown phase=%r", phase_raw)
            return

        async with self._lock:
            progress = self._graphs.get(graph_id)
            if progress is None:
                # Scheduler didn't pre-register — synthesize a stub so
                # we don't drop the event entirely. This happens in
                # tests or when the tracker is attached after submit.
                progress = self._synthesize_stub(envelope.op_id or "", graph_id)
                self._graphs[graph_id] = progress

            old_phase = progress.phase
            progress.phase = phase
            progress.last_error = str(payload.get("last_error") or progress.last_error)

            if phase == GraphExecutionPhase.RUNNING and progress.started_at_ns is None:
                progress.started_at_ns = envelope.emitted_at_ns
            if phase in (
                GraphExecutionPhase.COMPLETED,
                GraphExecutionPhase.FAILED,
                GraphExecutionPhase.CANCELLED,
            ):
                progress.finished_at_ns = envelope.emitted_at_ns
                # Flush any still-running units to cancelled so the
                # snapshot reflects true terminal state.
                for unit in progress.units.values():
                    if not unit.is_terminal:
                        unit.state = WorkUnitState.CANCELLED
                        unit.finished_at_ns = envelope.emitted_at_ns
                        if unit.started_at_ns is not None:
                            unit.runtime_ms = unit.elapsed_ms

            # Mark freshly-ready units that just crossed into the
            # ready set (they haven't started yet but are unblocked).
            ready_set: Set[str] = set(payload.get("ready_units") or [])
            for uid in ready_set:
                unit = progress.units.get(uid)
                if unit is not None and unit.ready_at_ns is None:
                    unit.ready_at_ns = envelope.emitted_at_ns
                    await self._fan_out(
                        GraphEvent(
                            kind=GraphEventKind.UNIT_READY,
                            graph_id=progress.graph_id,
                            op_id=progress.op_id,
                            ts_ns=envelope.emitted_at_ns,
                            unit_id=uid,
                            payload={"barrier_id": unit.barrier_id},
                        ),
                        progress,
                    )

            # Emit the graph-level event if the phase actually changed
            # (or it's the first event for this graph_id).
            if phase != old_phase or progress.event_history == []:
                kind = _GRAPH_PHASE_TO_KIND.get(phase, GraphEventKind.GRAPH_SUBMITTED)
                await self._fan_out(
                    GraphEvent(
                        kind=kind,
                        graph_id=progress.graph_id,
                        op_id=progress.op_id,
                        ts_ns=envelope.emitted_at_ns,
                        payload={
                            "phase": phase.value,
                            "completion_pct": progress.completion_pct(),
                            "runtime_ms": progress.runtime_ms,
                            "last_error": progress.last_error,
                        },
                    ),
                    progress,
                )

            if progress.is_terminal:
                self._evict_if_needed()

    async def _on_unit_event(self, envelope: EventEnvelope) -> None:
        """Translate ``WORK_UNIT_STATE_CHANGED`` into a tracker event."""
        if not self._enabled:
            return
        payload = envelope.payload or {}
        graph_id = str(payload.get("graph_id") or "")
        unit_id = str(payload.get("unit_id") or "")
        if not graph_id or not unit_id:
            return
        status_raw = payload.get("status") or WorkUnitState.PENDING.value
        try:
            status = WorkUnitState(str(status_raw))
        except ValueError:
            logger.debug("ExecutionGraphProgress: unknown unit status=%r", status_raw)
            return

        async with self._lock:
            progress = self._graphs.get(graph_id)
            if progress is None:
                progress = self._synthesize_stub(envelope.op_id or "", graph_id)
                self._graphs[graph_id] = progress

            unit = progress.units.get(unit_id)
            if unit is None:
                # Unknown unit — synthesize a minimal placeholder so
                # we don't silently drop the event.
                unit = UnitProgress(
                    unit_id=unit_id,
                    repo=str(payload.get("repo") or ""),
                    goal="",
                    target_files=(),
                    dependency_ids=(),
                    owned_paths=tuple(payload.get("owned_paths") or ()),
                    barrier_id=str(payload.get("barrier_id") or ""),
                    timeout_s=0.0,
                )
                progress.units[unit_id] = unit

            old_state = unit.state
            unit.state = status
            if status == WorkUnitState.RUNNING and unit.started_at_ns is None:
                unit.started_at_ns = envelope.emitted_at_ns
            if status in _TERMINAL_UNIT_STATES and unit.finished_at_ns is None:
                unit.finished_at_ns = envelope.emitted_at_ns
                # runtime_ms may have been supplied by scheduler; if
                # not, compute from start/finish.
                runtime_ms = payload.get("runtime_ms")
                if isinstance(runtime_ms, (int, float)):
                    unit.runtime_ms = float(runtime_ms)
                elif unit.started_at_ns is not None:
                    unit.runtime_ms = round(
                        (unit.finished_at_ns - unit.started_at_ns) / 1_000_000, 3
                    )

            unit.failure_class = str(payload.get("failure_class") or unit.failure_class)
            unit.error = str(payload.get("error") or unit.error)
            patch_count = payload.get("patch_file_count")
            if isinstance(patch_count, int) and patch_count >= 0:
                unit.patch_file_count = patch_count

            if old_state != status:
                kind = _UNIT_STATE_TO_KIND.get(status, GraphEventKind.UNIT_READY)
                await self._fan_out(
                    GraphEvent(
                        kind=kind,
                        graph_id=progress.graph_id,
                        op_id=progress.op_id,
                        ts_ns=envelope.emitted_at_ns,
                        unit_id=unit.unit_id,
                        payload={
                            "state": status.value,
                            "repo": unit.repo,
                            "runtime_ms": unit.runtime_ms,
                            "attempt_count": unit.attempt_count,
                            "failure_class": unit.failure_class,
                            "error": unit.error,
                            "barrier_id": unit.barrier_id,
                            "owned_paths": list(unit.owned_paths),
                        },
                    ),
                    progress,
                )

    async def _on_merge_event(self, envelope: EventEnvelope) -> None:
        """Translate ``MERGE_DECISION_RECORDED`` into a tracker event.

        The scheduler emits one of these per barrier flush; we record
        the decision against the graph progress so SerpentFlow can
        show the merge boundaries as they happen.
        """
        if not self._enabled:
            return
        payload = envelope.payload or {}
        graph_id = str(payload.get("graph_id") or "")
        if not graph_id:
            return
        async with self._lock:
            progress = self._graphs.get(graph_id)
            if progress is None:
                return
            decision = {
                "barrier_id": str(payload.get("barrier_id") or ""),
                "repo": str(payload.get("repo") or ""),
                "merged_unit_ids": list(payload.get("merged_unit_ids") or ()),
                "skipped_unit_ids": list(payload.get("skipped_unit_ids") or ()),
                "conflict_units": list(payload.get("conflict_units") or ()),
                "decision_hash": str(payload.get("decision_hash") or ""),
                "ts_ns": envelope.emitted_at_ns,
            }
            progress.merge_decisions.append(decision)
            await self._fan_out(
                GraphEvent(
                    kind=GraphEventKind.MERGE_DECIDED,
                    graph_id=graph_id,
                    op_id=envelope.op_id or progress.op_id,
                    ts_ns=envelope.emitted_at_ns,
                    payload=decision,
                ),
                progress,
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _fan_out(self, event: GraphEvent, progress: GraphProgress) -> None:
        """Record the event in history and deliver to all subscribers.

        Bounded queues drop oldest-on-full: this keeps the emitter
        path non-blocking when consumers are slow. Dropped events are
        counted per-subscriber and in tracker-wide stats.
        """
        progress.record_event(event)
        self._events_emitted += 1
        for sub in list(self._subscribers):
            try:
                sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                sub.dropped += 1
                self._events_dropped += 1
                # Evict the oldest to make room, then put current.
                try:
                    _ = sub.queue.get_nowait()
                    sub.queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _synthesize_stub(self, op_id: str, graph_id: str) -> GraphProgress:
        """Build a minimal placeholder when an event arrives before
        ``register_graph`` was called. Used in tests and for
        defensive reattachment mid-flight."""
        return GraphProgress(
            graph_id=graph_id,
            op_id=op_id,
            planner_id="",
            schema_version="",
            concurrency_limit=1,
            plan_digest="",
            units=OrderedDict(),
        )

    def _evict_if_needed(self) -> None:
        """Evict oldest terminal graphs past the retention cap."""
        if len(self._graphs) <= self._max_retained:
            return
        # Walk in insertion order, drop terminal entries until we're
        # under the cap. Active entries are never evicted.
        to_drop: List[str] = []
        for gid, gp in self._graphs.items():
            if len(self._graphs) - len(to_drop) <= self._max_retained:
                break
            if gp.is_terminal:
                to_drop.append(gid)
        for gid in to_drop:
            self._graphs.pop(gid, None)


# ---------------------------------------------------------------------------
# Default singleton
# ---------------------------------------------------------------------------


_default_tracker: Optional[ExecutionGraphProgressTracker] = None


def get_default_tracker() -> Optional[ExecutionGraphProgressTracker]:
    """Return the process-wide default tracker, if one was installed.

    The governed loop service calls ``install_default_tracker`` during
    boot with the scheduler's emitter. Consumers (SerpentFlow,
    dashboards) then fetch the shared instance via this accessor.
    Returns ``None`` before the loop service initializes, which is
    expected in unit tests and standalone diagnostics.
    """
    return _default_tracker


def install_default_tracker(
    tracker: ExecutionGraphProgressTracker,
) -> None:
    """Register *tracker* as the process-wide default.

    Idempotent — installing the same instance twice is a no-op. Any
    previously-installed tracker is replaced (its subscribers are
    left intact but will stop receiving events).
    """
    global _default_tracker
    _default_tracker = tracker


def reset_default_tracker() -> None:
    """Clear the process-wide default. For test teardown."""
    global _default_tracker
    _default_tracker = None
