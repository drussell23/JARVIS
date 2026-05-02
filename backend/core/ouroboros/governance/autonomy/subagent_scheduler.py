"""L3 scheduler for deterministic parallel execution-graph work units."""
from __future__ import annotations

import ast
import asyncio
import logging
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.autonomy_types import (
    CommandEnvelope,
    CommandType,
    EventEnvelope,
    EventType,
)
from backend.core.ouroboros.governance.autonomy.command_bus import CommandBus
from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (
    ExecutionGraphProgressTracker,
)
from backend.core.ouroboros.governance.autonomy.execution_graph_store import (
    ExecutionGraphStore,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.saga.merge_coordinator import MergeCoordinator
from backend.core.ouroboros.governance.saga.saga_types import FileOp, PatchedFile, RepoPatch
from backend.core.ouroboros.governance.test_runner import BlockedPathError

logger = logging.getLogger("Ouroboros.SubagentScheduler")

_TERMINAL_GRAPH_PHASES = {
    GraphExecutionPhase.COMPLETED,
    GraphExecutionPhase.FAILED,
    GraphExecutionPhase.CANCELLED,
}


class GenerationSubagentExecutor:
    """Default work-unit executor backed by the existing governed generator."""

    def __init__(
        self,
        *,
        generator: Any,
        validation_runner: Any,
        repo_roots: Dict[str, Path],
        worktree_manager: Any = None,
    ) -> None:
        self._generator = generator
        self._validation_runner = validation_runner
        self._repo_roots = {name: Path(root) for name, root in repo_roots.items()}
        self._worktree_manager = worktree_manager

    async def execute(self, graph: ExecutionGraph, unit: WorkUnitSpec) -> WorkUnitResult:
        """Generate and validate a single work-unit patch.

        When a WorktreeManager is provided, each unit executes in an isolated
        git worktree — preventing filesystem conflicts between parallel units.
        The worktree is always cleaned up in the finally block.
        """
        started_at_ns = time.monotonic_ns()
        causal_parent_id = graph.causal_trace_id
        _worktree_path: Optional[Path] = None
        try:
            if len(unit.target_files) != 1:
                raise RuntimeError(
                    f"work_unit_multi_file_unsupported:{unit.unit_id}:{len(unit.target_files)}"
                )

            repo_root = self._repo_roots.get(unit.repo)
            if repo_root is None:
                raise RuntimeError(f"unknown_repo_root:{unit.repo}")

            # --- Worktree isolation: create isolated copy for this unit ---
            # Manifesto §1 (Boundary) + §6 (Iron Gate): if we promised a
            # sandbox and cannot obtain it, refuse to execute in the shared
            # tree. A silent fallback lets parallel units collide on the
            # main working copy while the contract still promises isolation.
            if self._worktree_manager is not None:
                _branch = f"unit-{unit.unit_id}-{graph.graph_id}"
                try:
                    _created = await self._worktree_manager.create(_branch)
                    _worktree_path = _created
                    logger.info(
                        "[SubagentExecutor] Worktree created: %s -> %s",
                        _branch, _created,
                    )
                    # Override repo_root to the isolated worktree
                    repo_root = _created
                except Exception as wt_exc:
                    logger.error(
                        "[SubagentExecutor] Worktree creation failed for %s: %s "
                        "— unit fails (isolation promised, not obtained)",
                        _branch, wt_exc,
                    )
                    return WorkUnitResult(
                        unit_id=unit.unit_id,
                        repo=unit.repo,
                        status=WorkUnitState.FAILED,
                        patch=None,
                        attempt_count=1,
                        started_at_ns=started_at_ns,
                        finished_at_ns=time.monotonic_ns(),
                        # Cascading state vector fix (2026-05-01):
                        # worktree isolation failures use a distinct
                        # failure_class so the retry budget and episodic
                        # memory can distinguish them from validation
                        # infra failures. Previously both used "infra",
                        # causing flapping classification.
                        failure_class="worktree_isolation",
                        error=f"worktree_create_failed:{type(wt_exc).__name__}:{wt_exc}",
                        causal_parent_id=causal_parent_id,
                    )

            op_id = f"{graph.op_id}:{unit.unit_id}"
            subctx = OperationContext.create(
                target_files=tuple(unit.target_files),
                description=unit.goal,
                op_id=op_id,
                primary_repo=unit.repo,
                repo_scope=(unit.repo,),
            )
            deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=unit.timeout_s)
            subctx = subctx.with_pipeline_deadline(deadline)

            generation = await self._generator.generate(subctx, deadline)
            if generation.is_noop:
                return WorkUnitResult(
                    unit_id=unit.unit_id,
                    repo=unit.repo,
                    status=WorkUnitState.COMPLETED,
                    patch=RepoPatch(repo=unit.repo, files=()),
                    attempt_count=1,
                    started_at_ns=started_at_ns,
                    finished_at_ns=time.monotonic_ns(),
                    causal_parent_id=causal_parent_id,
                )

            best_candidate: Optional[Dict[str, Any]] = None
            best_failure_class = ""
            best_error = ""
            for candidate in generation.candidates:
                remaining_s = max(
                    0.0,
                    (deadline - datetime.now(tz=timezone.utc)).total_seconds(),
                )
                passed, failure_class, error = await self._validate_candidate(
                    subctx,
                    candidate,
                    remaining_s,
                )
                if passed:
                    best_candidate = candidate
                    break
                best_failure_class = failure_class
                best_error = error

            if best_candidate is None:
                return WorkUnitResult(
                    unit_id=unit.unit_id,
                    repo=unit.repo,
                    status=WorkUnitState.FAILED,
                    patch=None,
                    attempt_count=1,
                    started_at_ns=started_at_ns,
                    finished_at_ns=time.monotonic_ns(),
                    failure_class=best_failure_class or "validation",
                    error=best_error or "no_valid_candidate",
                    causal_parent_id=causal_parent_id,
                )

            patch = self._candidate_to_patch(unit.repo, repo_root, best_candidate)
            return WorkUnitResult(
                unit_id=unit.unit_id,
                repo=unit.repo,
                status=WorkUnitState.COMPLETED,
                patch=patch,
                attempt_count=1,
                started_at_ns=started_at_ns,
                finished_at_ns=time.monotonic_ns(),
                causal_parent_id=causal_parent_id,
            )
        except Exception as exc:
            return WorkUnitResult(
                unit_id=unit.unit_id,
                repo=unit.repo,
                status=WorkUnitState.FAILED,
                patch=None,
                attempt_count=1,
                started_at_ns=started_at_ns,
                finished_at_ns=time.monotonic_ns(),
                failure_class="infra",
                error=str(exc),
                causal_parent_id=causal_parent_id,
            )
        finally:
            # --- Worktree cleanup: always runs, even on exception/cancellation ---
            if _worktree_path is not None and self._worktree_manager is not None:
                try:
                    await self._worktree_manager.cleanup(_worktree_path)
                    logger.info("[SubagentExecutor] Worktree cleaned: %s", _worktree_path)
                except Exception as cleanup_exc:
                    logger.warning(
                        "[SubagentExecutor] Worktree cleanup failed: %s", cleanup_exc
                    )

    async def _validate_candidate(
        self,
        ctx: OperationContext,
        candidate: Dict[str, Any],
        remaining_s: float,
    ) -> Tuple[bool, str, str]:
        """Validate a work-unit candidate using the existing validation runner."""
        target_file = str(candidate.get("file_path", ctx.target_files[0]))
        content = str(candidate.get("full_content", ""))
        if target_file.endswith(".py"):
            try:
                ast.parse(content)
            except SyntaxError as exc:
                return False, "syntax", f"SyntaxError: {exc}"

        runnable_exts = {".py", ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp"}
        if Path(target_file).suffix not in runnable_exts:
            return True, "", ""

        if self._validation_runner is None:
            return True, "", ""

        if remaining_s <= 0.0:
            return False, "budget", "pipeline budget exhausted"

        with tempfile.TemporaryDirectory(prefix="ouroboros_l3_validate_") as sandbox_dir:
            sandbox = Path(sandbox_dir)
            sandbox_file = sandbox / Path(target_file).name
            sandbox_file.write_text(content, encoding="utf-8")
            try:
                result = await self._validation_runner.run(
                    changed_files=(sandbox_file,),
                    sandbox_dir=sandbox,
                    timeout_budget_s=remaining_s,
                    op_id=ctx.op_id,
                )
            except BlockedPathError as exc:
                return False, "security", str(exc)
            except Exception as exc:  # noqa: BLE001
                return False, "infra", str(exc)

        if result.passed:
            return True, "", ""
        return False, str(result.failure_class or "test"), "validation failed"

    @staticmethod
    def _candidate_to_patch(
        repo: str,
        repo_root: Path,
        candidate: Dict[str, Any],
    ) -> RepoPatch:
        """Convert a single-file candidate into a RepoPatch."""
        file_path = str(candidate.get("file_path", ""))
        if not file_path:
            raise RuntimeError("candidate_missing_file_path")

        if "patches" in candidate and isinstance(candidate["patches"], dict):
            patch = candidate["patches"].get(repo)
            if isinstance(patch, RepoPatch):
                return patch

        content = str(candidate.get("full_content", ""))
        disk_path = repo_root / file_path
        if disk_path.exists():
            preimage = disk_path.read_bytes()
            op = FileOp.MODIFY
        else:
            preimage = None
            op = FileOp.CREATE

        return RepoPatch(
            repo=repo,
            files=(PatchedFile(path=file_path, op=op, preimage=preimage),),
            new_content=((file_path, content.encode("utf-8")),),
        )


class SubagentScheduler:
    """Deterministic scheduler for parallel execution graphs."""

    def __init__(
        self,
        *,
        store: ExecutionGraphStore,
        command_bus: CommandBus,
        event_emitter: Any,
        executor: Any,
        merge_coordinator: Optional[MergeCoordinator] = None,
        max_concurrent_graphs: int = 2,
        progress_tracker: Optional[ExecutionGraphProgressTracker] = None,
    ) -> None:
        self._store = store
        self._command_bus = command_bus
        self._event_emitter = event_emitter
        self._executor = executor
        self._merge_coordinator = merge_coordinator or MergeCoordinator()
        self._max_concurrent_graphs = max_concurrent_graphs
        self._progress_tracker = progress_tracker
        self._graphs: Dict[str, GraphExecutionState] = {}
        self._graph_tasks: Dict[str, asyncio.Task] = {}
        self._graph_futures: Dict[str, asyncio.Future] = {}
        self._merged_patches: Dict[str, Dict[str, RepoPatch]] = {}
        self._recovery_queue: List[str] = []
        self._running = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the scheduler lifecycle."""
        self._running = True

    async def stop(self) -> None:
        """Stop the scheduler and cancel active graph tasks."""
        self._running = False
        for graph_id, task in list(self._graph_tasks.items()):
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self._graph_tasks.pop(graph_id, None)
        self._recovery_queue.clear()

    async def submit(self, graph: ExecutionGraph) -> bool:
        """Submit a graph for execution. Idempotent by graph_id."""
        async with self._lock:
            if not self._running:
                return False
            existing = self._graphs.get(graph.graph_id) or self._store.get(graph.graph_id)
            if existing is not None:
                self._graphs[graph.graph_id] = existing
                if existing.phase in _TERMINAL_GRAPH_PHASES:
                    self._set_graph_future_result(graph.graph_id, existing)
                    return True
                if graph.graph_id in self._recovery_queue:
                    self._ensure_graph_future(graph.graph_id)
                    return True
                if graph.graph_id in self._graph_tasks:
                    return True
                if len(self._graph_tasks) >= self._max_concurrent_graphs:
                    return False
                self._graph_tasks[graph.graph_id] = asyncio.create_task(
                    self._run_graph(graph.graph_id),
                    name=f"subagent_graph_resume:{graph.graph_id}",
                )
                self._ensure_graph_future(graph.graph_id)
                return True
            if graph.graph_id in self._graph_tasks:
                return True
            if len(self._graph_tasks) >= self._max_concurrent_graphs:
                return False

            ready = tuple(self._compute_ready_units(graph, None))
            state = GraphExecutionState(graph=graph, ready_units=ready)
            self._graphs[graph.graph_id] = state
            self._store.save(state)
            if self._progress_tracker is not None:
                self._progress_tracker.register_graph(graph)
            await self._emit_graph_event(graph.op_id, graph.graph_id, GraphExecutionPhase.CREATED, state)

            self._ensure_graph_future(graph.graph_id)
            self._graph_tasks[graph.graph_id] = asyncio.create_task(
                self._run_graph(graph.graph_id),
                name=f"subagent_graph:{graph.graph_id}",
            )
            return True

    async def recover_inflight(self) -> None:
        """Recover all non-terminal graphs from durable storage."""
        async with self._lock:
            if not self._running:
                return
            for graph_id, state in sorted(self._store.load_inflight().items()):
                if graph_id in self._graph_tasks:
                    continue
                self._graphs[graph_id] = state
                self._ensure_graph_future(graph_id)
                if graph_id in self._recovery_queue:
                    continue
                if len(self._graph_tasks) >= self._max_concurrent_graphs:
                    self._recovery_queue.append(graph_id)
                    continue
                self._graph_tasks[graph_id] = asyncio.create_task(
                    self._run_graph(graph_id),
                    name=f"subagent_graph_recover:{graph_id}",
                )

    async def abort(self, graph_id: str) -> bool:
        """Cancel an active graph if present."""
        task = self._graph_tasks.get(graph_id)
        if task is None:
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return True

    async def wait_for_graph(
        self,
        graph_id: str,
        timeout_s: Optional[float] = None,
    ) -> GraphExecutionState:
        """Wait for a graph to reach a terminal state."""
        fut = self._graph_futures.get(graph_id)
        if fut is None:
            state = self._graphs.get(graph_id) or self._store.get(graph_id)
            if state is None:
                raise RuntimeError(f"unknown_execution_graph:{graph_id}")
            return state
        if timeout_s is None:
            return await fut
        return await asyncio.wait_for(fut, timeout=timeout_s)

    def get_merged_patches(self, graph_id: str) -> Dict[str, RepoPatch]:
        """Return merged repo patches for a completed graph."""
        return dict(self._merged_patches.get(graph_id, {}))

    def has_graph(self, graph_id: str) -> bool:
        """Return True if the graph is known to the scheduler."""
        return graph_id in self._graphs or graph_id in self._graph_tasks

    def health(self) -> Dict[str, Any]:
        """Return lightweight scheduler health data."""
        return {
            "running": self._running,
            "active_graphs": sorted(self._graph_tasks.keys()),
            "queued_graphs": list(self._recovery_queue),
            "max_concurrent_graphs": self._max_concurrent_graphs,
            "completed_graphs": sorted(self._merged_patches.keys()),
        }

    async def _run_graph(self, graph_id: str) -> None:
        state = self._graphs[graph_id]
        graph = state.graph
        try:
            while True:
                pending = self._pending_units(graph, state)
                if not pending:
                    decisions = self._merge_coordinator.build_barrier_batches(graph, state.results)
                    merged = self._merge_coordinator.merge_repo_patches(decisions, state.results)
                    self._merged_patches[graph.graph_id] = merged
                    state = self._update_state(
                        state,
                        phase=GraphExecutionPhase.COMPLETED,
                        ready_units=(),
                        running_units=(),
                    )
                    self._graphs[graph_id] = state
                    self._store.save(state)
                    for decision in decisions:
                        await self._emit_event(
                            EventType.MERGE_DECISION_RECORDED,
                            {
                                "graph_id": graph.graph_id,
                                "repo": decision.repo,
                                "barrier_id": decision.barrier_id,
                                "decision_hash": decision.decision_hash,
                                "merged_unit_ids": list(decision.merged_unit_ids),
                            },
                            op_id=graph.op_id,
                        )
                    await self._emit_graph_event(
                        graph.op_id,
                        graph.graph_id,
                        GraphExecutionPhase.COMPLETED,
                        state,
                    )
                    self._finish_graph(graph_id, state)
                    return

                ready = self._compute_ready_units(graph, state)
                selected, deferred = self._select_ready_batch(graph, ready)

                # Slice 5 Arc B — MemoryPressureGate consultation before L3
                # fan-out. Direct enforce (per operator authorization): if the
                # gate clamps N_requested → N_allowed, move overflow into the
                # deferred queue so it gets replayed on the next loop iteration.
                # Zero work loss. Gate-disabled → pass-through (no clamp). Every
                # decision is logged + SSE-published (allow / clamp / disabled /
                # probe_fail) so operators have a §8 audit trail.
                if selected:
                    decision = self._consult_memory_gate(
                        len(selected), graph_id=graph_id,
                    )
                    if decision is not None and decision.n_allowed < len(selected):
                        overflow = list(selected[decision.n_allowed:])
                        selected = list(selected[:decision.n_allowed])
                        deferred = sorted(list(deferred) + overflow)

                if not selected:
                    state = self._update_state(
                        state,
                        phase=GraphExecutionPhase.FAILED,
                        ready_units=tuple(sorted(deferred)),
                        running_units=(),
                        last_error="no_schedulable_ready_units",
                    )
                    self._graphs[graph_id] = state
                    self._store.save(state)
                    await self._emit_graph_event(
                        graph.op_id,
                        graph.graph_id,
                        GraphExecutionPhase.FAILED,
                        state,
                    )
                    self._finish_graph(graph_id, state)
                    return

                state = self._update_state(
                    state,
                    phase=GraphExecutionPhase.RUNNING,
                    ready_units=tuple(sorted(deferred)),
                    running_units=tuple(sorted(selected)),
                )
                self._graphs[graph_id] = state
                self._store.save(state)
                await self._emit_graph_event(
                    graph.op_id,
                    graph.graph_id,
                    GraphExecutionPhase.RUNNING,
                    state,
                )

                tasks = await self._run_selected_units(graph, selected)

                failure_seen = False
                for unit_id in selected:
                    result = tasks[unit_id].result()
                    state = self._apply_result(state, result, deferred)
                    self._graphs[graph_id] = state
                    self._store.save(state)
                    await self._emit_unit_event(
                        graph.op_id,
                        graph.graph_id,
                        graph.unit_map[unit_id],
                        result.status,
                        result=result,
                    )
                    self._emit_result_command(graph, result)
                    if result.status is not WorkUnitState.COMPLETED:
                        failure_seen = True

                if failure_seen:
                    terminal_phase = (
                        GraphExecutionPhase.CANCELLED
                        if state.cancelled_units
                        else GraphExecutionPhase.FAILED
                    )
                    state = self._update_state(
                        state,
                        phase=terminal_phase,
                        ready_units=(),
                        running_units=(),
                        last_error=state.last_error or "work_unit_failed",
                    )
                    self._graphs[graph_id] = state
                    self._store.save(state)
                    await self._emit_graph_event(
                        graph.op_id,
                        graph.graph_id,
                        terminal_phase,
                        state,
                    )
                    self._finish_graph(graph_id, state)
                    return
        except asyncio.CancelledError:
            state = self._update_state(
                state,
                phase=GraphExecutionPhase.CANCELLED,
                ready_units=(),
                running_units=(),
                last_error="graph_cancelled",
            )
            self._graphs[graph_id] = state
            self._store.save(state)
            await self._emit_graph_event(
                graph.op_id,
                graph.graph_id,
                GraphExecutionPhase.CANCELLED,
                state,
            )
            self._finish_graph(graph_id, state)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("[SubagentScheduler] graph=%s failed: %s", graph_id, exc)
            state = self._update_state(
                state,
                phase=GraphExecutionPhase.FAILED,
                ready_units=(),
                running_units=(),
                last_error=str(exc),
            )
            self._graphs[graph_id] = state
            self._store.save(state)
            await self._emit_graph_event(
                graph.op_id,
                graph.graph_id,
                GraphExecutionPhase.FAILED,
                state,
            )
            self._finish_graph(graph_id, state)

    async def _execute_unit_guarded(
        self,
        graph: ExecutionGraph,
        unit: WorkUnitSpec,
    ) -> WorkUnitResult:
        try:
            return await self._executor.execute(graph, unit)
        except asyncio.CancelledError:
            now = time.monotonic_ns()
            return WorkUnitResult(
                unit_id=unit.unit_id,
                repo=unit.repo,
                status=WorkUnitState.CANCELLED,
                patch=None,
                attempt_count=1,
                started_at_ns=now,
                finished_at_ns=now,
                failure_class="cancelled",
                error="cancelled",
                causal_parent_id=graph.causal_trace_id,
            )
        except Exception as exc:  # noqa: BLE001
            now = time.monotonic_ns()
            return WorkUnitResult(
                unit_id=unit.unit_id,
                repo=unit.repo,
                status=WorkUnitState.FAILED,
                patch=None,
                attempt_count=1,
                started_at_ns=now,
                finished_at_ns=now,
                failure_class="infra",
                error=str(exc),
                causal_parent_id=graph.causal_trace_id,
            )

    def _pending_units(
        self,
        graph: ExecutionGraph,
        state: GraphExecutionState,
    ) -> List[str]:
        terminal = set(state.completed_units) | set(state.failed_units) | set(state.cancelled_units)
        return sorted(uid for uid in graph.unit_map if uid not in terminal)

    def _compute_ready_units(
        self,
        graph: ExecutionGraph,
        state: Optional[GraphExecutionState],
    ) -> List[str]:
        completed = set(state.completed_units) if state is not None else set()
        failed = set(state.failed_units) if state is not None else set()
        cancelled = set(state.cancelled_units) if state is not None else set()
        running = set(state.running_units) if state is not None else set()

        ready: List[str] = []
        for unit in graph.units:
            if unit.unit_id in completed | failed | cancelled | running:
                continue
            if all(dep in completed for dep in unit.dependency_ids):
                ready.append(unit.unit_id)
        return sorted(ready)

    def _consult_memory_gate(
        self,
        n_requested: int,
        *,
        graph_id: str,
    ) -> Optional[Any]:
        """Slice 5 Arc B — consult MemoryPressureGate + log + publish SSE.

        Returns the ``FanoutDecision`` (or ``None`` on any failure —
        scheduler must not break when the gate is unavailable).

        Classifies the decision into one of four deterministic
        dispositions for the SSE payload + log line:

          * ``allow``      — OK level, no clamp
          * ``clamp``      — requested > allowed; overflow gets deferred
          * ``disabled``   — gate master flag off (pass-through)
          * ``probe_fail`` — probe raised / returned unreliable data

        Log every decision (INFO normally, WARNING on clamp) and fire
        one SSE frame per decision so operators get a full §8 audit
        trail of fan-out pressure behavior. Scheduler call rate is
        bounded by graph-execution cadence.
        """
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                get_default_gate,
            )
            gate = get_default_gate()
            decision = gate.can_fanout(n_requested)
        except Exception:  # noqa: BLE001 — gate must not break scheduler
            logger.debug(
                "[SubagentScheduler] memory gate consultation failed "
                "(non-fatal)", exc_info=True,
            )
            return None

        # Deterministic disposition classification
        reason = decision.reason_code
        if reason == "memory_pressure_gate.disabled":
            disposition = "disabled"
        elif reason.startswith("memory_pressure_gate.probe_"):
            disposition = "probe_fail"
        elif decision.n_allowed < decision.n_requested:
            disposition = "clamp"
        else:
            disposition = "allow"

        log_fn = logger.warning if disposition == "clamp" else logger.info
        log_fn(
            "[SubagentScheduler] fanout_gate: graph=%s disposition=%s "
            "requested=%d allowed=%d level=%s reason=%s",
            graph_id, disposition, decision.n_requested,
            decision.n_allowed, decision.level.value, decision.reason_code,
        )

        # Best-effort SSE publish
        try:
            from backend.core.ouroboros.governance.ide_observability_stream import (
                publish_memory_fanout_decision_event,
            )
            publish_memory_fanout_decision_event(
                graph_id=graph_id,
                disposition=disposition,
                decision=decision,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SubagentScheduler] fanout SSE publish failed (non-fatal)",
                exc_info=True,
            )

        return decision

    def _select_ready_batch(
        self,
        graph: ExecutionGraph,
        ready: Sequence[str],
    ) -> Tuple[List[str], List[str]]:
        selected: List[str] = []
        deferred: List[str] = []
        owned_paths = set()

        for unit_id in sorted(ready):
            unit = graph.unit_map[unit_id]
            unit_paths = set(unit.effective_owned_paths)
            if owned_paths & unit_paths:
                deferred.append(unit_id)
                continue
            selected.append(unit_id)
            owned_paths.update(unit_paths)
            if len(selected) >= graph.concurrency_limit:
                deferred.extend(sorted(uid for uid in ready if uid not in selected and uid not in deferred))
                break

        return selected, sorted(deferred)

    @staticmethod
    def _apply_result(
        state: GraphExecutionState,
        result: WorkUnitResult,
        deferred: Sequence[str],
    ) -> GraphExecutionState:
        running = set(state.running_units)
        running.discard(result.unit_id)
        completed = set(state.completed_units)
        failed = set(state.failed_units)
        cancelled = set(state.cancelled_units)
        results = dict(state.results)
        results[result.unit_id] = result

        last_error = state.last_error
        if result.status is WorkUnitState.COMPLETED:
            completed.add(result.unit_id)
        elif result.status is WorkUnitState.CANCELLED:
            cancelled.add(result.unit_id)
            last_error = result.error or last_error
        else:
            failed.add(result.unit_id)
            last_error = result.error or last_error

        return GraphExecutionState(
            graph=state.graph,
            phase=state.phase,
            ready_units=tuple(sorted(deferred)),
            running_units=tuple(sorted(running)),
            completed_units=tuple(sorted(completed)),
            failed_units=tuple(sorted(failed)),
            cancelled_units=tuple(sorted(cancelled)),
            results=results,
            last_error=last_error,
        )

    @staticmethod
    def _update_state(
        state: GraphExecutionState,
        *,
        phase: GraphExecutionPhase,
        ready_units: Sequence[str],
        running_units: Sequence[str],
        last_error: Optional[str] = None,
    ) -> GraphExecutionState:
        return GraphExecutionState(
            graph=state.graph,
            phase=phase,
            ready_units=tuple(sorted(ready_units)),
            running_units=tuple(sorted(running_units)),
            completed_units=state.completed_units,
            failed_units=state.failed_units,
            cancelled_units=state.cancelled_units,
            results=dict(state.results),
            last_error=state.last_error if last_error is None else last_error,
        )

    async def _run_selected_units(
        self,
        graph: ExecutionGraph,
        selected: Sequence[str],
    ) -> Dict[str, asyncio.Task]:
        """Execute one wave of ready work units with cancellation-safe cleanup."""
        tasks: Dict[str, asyncio.Task] = {}
        for unit_id in selected:
            unit = graph.unit_map[unit_id]
            await self._emit_unit_event(graph.op_id, graph.graph_id, unit, WorkUnitState.RUNNING)
            tasks[unit_id] = asyncio.create_task(
                self._execute_unit_guarded(graph, unit),
                name=f"work_unit:{graph.graph_id}:{unit_id}",
            )

        try:
            await asyncio.gather(*(tasks[unit_id] for unit_id in selected))
            return tasks
        except asyncio.CancelledError:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise

    def _ensure_graph_future(self, graph_id: str) -> asyncio.Future:
        future = self._graph_futures.get(graph_id)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            self._graph_futures[graph_id] = future
        return future

    def _set_graph_future_result(self, graph_id: str, state: GraphExecutionState) -> None:
        future = self._ensure_graph_future(graph_id)
        if not future.done():
            future.set_result(state)

    def _finish_graph(self, graph_id: str, state: GraphExecutionState) -> None:
        self._set_graph_future_result(graph_id, state)
        self._graph_tasks.pop(graph_id, None)
        if self._running and self._recovery_queue:
            asyncio.create_task(
                self._resume_queued_recoveries(),
                name=f"subagent_recovery_resume:{graph_id}",
            )

    async def _resume_queued_recoveries(self) -> None:
        async with self._lock:
            while self._running and self._recovery_queue:
                if len(self._graph_tasks) >= self._max_concurrent_graphs:
                    return
                graph_id = self._recovery_queue.pop(0)
                if graph_id in self._graph_tasks:
                    continue
                if graph_id not in self._graphs:
                    continue
                self._ensure_graph_future(graph_id)
                self._graph_tasks[graph_id] = asyncio.create_task(
                    self._run_graph(graph_id),
                    name=f"subagent_graph_recover:{graph_id}",
                )

    def _emit_result_command(self, graph: ExecutionGraph, result: WorkUnitResult) -> None:
        cmd = CommandEnvelope(
            source_layer="L3",
            target_layer="L1",
            command_type=CommandType.REPORT_WORK_UNIT_RESULT,
            payload={
                "graph_id": graph.graph_id,
                "op_id": graph.op_id,
                "unit_id": result.unit_id,
                "repo": result.repo,
                "status": result.status.value,
                "failure_class": result.failure_class,
                "error": result.error,
            },
            ttl_s=300.0,
        )
        self._command_bus.try_put(cmd)

    async def _emit_graph_event(
        self,
        op_id: str,
        graph_id: str,
        phase: GraphExecutionPhase,
        state: GraphExecutionState,
    ) -> None:
        await self._emit_event(
            EventType.EXECUTION_GRAPH_STATE_CHANGED,
            {
                "graph_id": graph_id,
                "phase": phase.value,
                "ready_units": list(state.ready_units),
                "running_units": list(state.running_units),
                "completed_units": list(state.completed_units),
                "failed_units": list(state.failed_units),
                "cancelled_units": list(state.cancelled_units),
                "last_error": state.last_error,
            },
            op_id=op_id,
        )

    async def _emit_unit_event(
        self,
        op_id: str,
        graph_id: str,
        unit: WorkUnitSpec,
        status: WorkUnitState,
        *,
        result: Optional[WorkUnitResult] = None,
    ) -> None:
        payload = {
            "graph_id": graph_id,
            "unit_id": unit.unit_id,
            "repo": unit.repo,
            "status": status.value,
            "barrier_id": unit.barrier_id,
            "owned_paths": list(unit.effective_owned_paths),
        }
        if result is not None:
            payload.update(
                {
                    "failure_class": result.failure_class,
                    "error": result.error,
                    "runtime_ms": round(
                        (result.finished_at_ns - result.started_at_ns) / 1_000_000,
                        3,
                    ),
                    "causal_parent_id": result.causal_parent_id,
                }
            )
        await self._emit_event(
            EventType.WORK_UNIT_STATE_CHANGED,
            payload,
            op_id=op_id,
        )

    async def _emit_event(
        self,
        event_type: EventType,
        payload: Dict[str, Any],
        *,
        op_id: str,
    ) -> None:
        if self._event_emitter is None:
            return
        await self._event_emitter.emit(
            EventEnvelope(
                source_layer="L1",
                event_type=event_type,
                payload=payload,
                op_id=op_id,
            )
        )
