"""swarm_invoker -- the live caller seam for the SwarmOrchestrator.

THE GAP this closes: ``SwarmOrchestrator.submit()`` / ``define_worker()`` +
``worker_synthesizer`` + ``SubagentFactory`` + ``EpistemicDeadlockBreaker``
were BUILT + TESTED but had NO live caller -- setting
``JARVIS_SWARM_ORCHESTRATOR_ENABLED=true`` did nothing because nothing
invoked them. This module is that invoker.

It sits between the :class:`SubagentScheduler` and its per-unit executor.
The scheduler already builds the AgentMessageBus (Phase 1c) + the
EphemeralMemorySandbox (Phase 1b) + the worktree isolation -- the invoker
adds the missing piece: **dynamic worker synthesis + the per-worker cage**.

Routing decision (per unit, fail-CLOSED):

  * master gate ``JARVIS_SWARM_ORCHESTRATOR_ENABLED`` OFF              -> legacy
  * graph NOT genuinely multi-node parallelizable (concurrency_limit
    <= 1, or < 2 independent collision-partitioned units)             -> legacy
  * unit is NOT a swarm-synthesized worker (legacy fixed-type unit)   -> legacy
  * otherwise -> SWARM: synthesize the worker shape (``worker_synthesizer``
    -- the Golden Rule, NO static role enum), build its ``ScopedToolBackend``
    cage (``SubagentFactory.build``), then execute the unit through the
    EXISTING executor (which carries the worktree + sandbox). The cage is the
    structural proof the worker is properly shaped BEFORE any execution.

**Fail-CLOSED (the Sovereign mutation-cage invariant):** if synthesis fails,
or a worker cannot be caged (build raises / returns no ``ScopedToolBackend``),
the unit returns ``WorkUnitResult(FAILED, failure_class="swarm_*")`` -- it is
NEVER run uncaged. A synthesized worker can only ever be LESS capable than the
cage; a worker with no cage does not run at all.

**Deadlock breaker:** an ``EpistemicDeadlockBreaker`` shatter that bubbles a
:class:`DeadlockInterruptedException` out of the worker round-trip (the
message-bus clarification loop) is caught here and converted to a FAILED unit
(``failure_class="swarm_deadlock"``). The ``DAGComposer`` already treats a
FAILED unit as a ComposeFailure -> legacy serial, so a shattered deadlock is
never a silent loss and never a hang.

**Gated default-OFF byte-identical:** OFF -> ``execute`` delegates straight to
the legacy executor; no synthesis, no factory, no breaker -- byte-identical to
pre-swarm.

REUSE-ONLY: this module writes NO new synthesizer, NO new cage, NO new
executor, NO new bus/sandbox/worktree. It is purely the routing seam.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitResult,
    WorkUnitSpec,
    WorkUnitState,
)

logger = logging.getLogger("Ouroboros.SwarmInvoker")


# ---------------------------------------------------------------------------
# Parallelizability of the collision-partitioned DAG
# ---------------------------------------------------------------------------


def _independent_root_units(graph: ExecutionGraph) -> int:
    """Count units with NO dependencies (DAG roots) -- the units that can run
    concurrently in the first wave.

    The graph's units are already collision-partitioned (the scheduler's
    ``_select_ready_batch`` enforces disjoint ``owned_paths`` per wave), so a
    root unit is a genuinely-independent parallelizable unit. We count roots
    (in-degree 0) as the conservative measure of "are there >1 units that can
    run at once?".
    """
    roots = 0
    for unit in graph.units:
        if not unit.dependency_ids:
            roots += 1
    return roots


def is_graph_parallelizable(graph: ExecutionGraph) -> bool:
    """True iff the DAG is genuinely multi-node parallelizable.

    Requires BOTH:
      * ``concurrency_limit > 1`` (the graph permits parallelism), AND
      * at least 2 independent (dependency-free root) units that can run in
        the same wave.

    A single-node DAG, a fully-serial dependency chain, or a graph clamped to
    ``concurrency_limit == 1`` is NOT parallelizable -> the swarm does not
    engage (the legacy executor runs it). Fail-CLOSED on a malformed graph
    (any error -> not parallelizable).
    """
    try:
        if graph.concurrency_limit <= 1:
            return False
        if len(graph.units) <= 1:
            return False
        return _independent_root_units(graph) >= 2
    except Exception:  # noqa: BLE001 -- malformed graph -> not parallelizable.
        return False


# ---------------------------------------------------------------------------
# The invoker
# ---------------------------------------------------------------------------


class SwarmUnitInvoker:
    """Per-unit routing seam: legacy executor <-> dynamic worker synthesis.

    Parameters
    ----------
    legacy_executor:
        The EXISTING per-unit executor (``GenerationSubagentExecutor``). Its
        ``execute(graph, unit)`` carries the worktree isolation + the
        EphemeralMemorySandbox -- the swarm path reuses it verbatim (no new
        executor). Used directly on the legacy/OFF path.
    define_worker:
        Callable ``sub_goal -> WorkerShape`` -- the synthesis entry point
        (the Golden Rule). Defaults to
        ``worker_synthesizer.synthesize_worker_spec``. Injectable for tests.
    build_worker:
        Callable ``(shape, *, worker_id, goal, scope_paths, bus, graph_id) ->
        BuiltWorker`` -- builds the ScopedToolBackend cage. Defaults to a
        lazily-constructed ``SubagentFactory().build``. Injectable for tests.
    get_bus:
        Optional ``graph_id -> bus | None`` accessor (the scheduler's per-graph
        AgentMessageBus). When present, the worker is given voice via the
        factory's bus wiring. None -> silent worker (byte-identical to 1c-off).
    project_root:
        Base for resolving relative ``target_files`` during AST synthesis.
    """

    def __init__(
        self,
        *,
        legacy_executor: Any,
        define_worker: Optional[Callable[[Any], Any]] = None,
        build_worker: Optional[Callable[..., Any]] = None,
        get_bus: Optional[Callable[[str], Any]] = None,
        project_root: Optional[str] = None,
    ) -> None:
        self._legacy = legacy_executor
        self._define_worker = define_worker
        self._build_worker = build_worker
        self._get_bus = get_bus
        self._project_root = project_root
        self._factory: Any = None  # lazily constructed SubagentFactory

    # -- the scheduler-facing entry point ---------------------------------

    async def execute(self, graph: ExecutionGraph, unit: WorkUnitSpec) -> WorkUnitResult:
        """Route one unit. SWARM when eligible, else the legacy executor.

        OFF / non-parallelizable / legacy-unit -> delegates straight to the
        legacy executor (byte-identical). Eligible -> synthesize + cage +
        caged execute, fail-CLOSED on synthesis/cage failure, deadlock-aware.
        """
        if not self._should_route_swarm(graph, unit):
            return await self._legacy.execute(graph, unit)
        return await self._execute_swarm(graph, unit)

    # -- routing decision -------------------------------------------------

    def _should_route_swarm(self, graph: ExecutionGraph, unit: WorkUnitSpec) -> bool:
        """Swarm iff master gate ON AND graph parallelizable AND swarm unit."""
        try:
            from backend.core.ouroboros.governance.autonomy.swarm_orchestrator import (
                is_orchestrator_enabled,
            )

            if not is_orchestrator_enabled():
                return False
            if not is_graph_parallelizable(graph):
                return False
            return bool(getattr(unit, "is_swarm_worker", False))
        except Exception:  # noqa: BLE001 -- any decision error -> legacy (safe).
            logger.debug(
                "[SwarmInvoker] route decision raised -> legacy (non-fatal)",
                exc_info=True,
            )
            return False

    # -- the swarm path ---------------------------------------------------

    async def _execute_swarm(
        self, graph: ExecutionGraph, unit: WorkUnitSpec
    ) -> WorkUnitResult:
        # 1. + 2. synthesize the worker shape + build its cage. Fail-CLOSED:
        #    any failure here -> FAILED unit, NEVER an uncaged execution.
        try:
            shape = self._synthesize(unit)
        except Exception as exc:  # noqa: BLE001 -- synthesis failure -> fail-CLOSED.
            return self._failed(
                graph, unit, "swarm_synthesis", f"worker_synthesis_failed:{exc}"
            )

        try:
            built = self._cage(graph, unit, shape)
        except Exception as exc:  # noqa: BLE001 -- cage failure -> fail-CLOSED.
            return self._failed(
                graph, unit, "swarm_cage", f"worker_cage_failed:{exc}"
            )

        # The structural invariant: NO worker runs without a ScopedToolBackend
        # cage. A built worker with no backend is a half-wired capability ->
        # refuse to execute (fail-CLOSED).
        if built is None or getattr(built, "backend", None) is None:
            return self._failed(
                graph, unit, "swarm_cage",
                "worker_uncaged:no_scoped_tool_backend",
            )

        logger.info(
            "[SwarmInvoker] swarm worker unit=%s role=%r caged -> execute "
            "(worktree+sandbox via existing executor)",
            unit.unit_id, getattr(shape, "role", "?"),
        )

        # 3. execute the caged unit through the EXISTING executor (worktree +
        #    sandbox live there). The deadlock breaker shatter bubbles a
        #    DeadlockInterruptedException out of the worker round-trip -> we
        #    catch it and convert to a FAILED unit (never a hang, never a
        #    silent loss -- DAGComposer treats FAILED as ComposeFailure).
        try:
            return await self._legacy.execute(graph, unit)
        except Exception as exc:  # noqa: BLE001 -- deadlock or worker fault.
            if self._is_deadlock(exc):
                logger.warning(
                    "[SwarmInvoker] unit=%s epistemic deadlock shattered -> "
                    "FAILED (dissolved -> legacy serial via DAGComposer)",
                    unit.unit_id,
                )
                return self._failed(
                    graph, unit, "swarm_deadlock",
                    f"epistemic_deadlock:{exc}",
                )
            raise

    # -- synthesis + cage (REUSE the existing modules) --------------------

    def _synthesize(self, unit: WorkUnitSpec) -> Any:
        """Synthesize the WorkerShape from the sub-goal (the Golden Rule).

        REUSES ``worker_synthesizer.synthesize_worker_spec`` -- shape is
        DERIVED from AST/semantic inspection, never a static role lookup.
        """
        if self._define_worker is not None:
            return self._define_worker(unit)
        from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
            synthesize_worker_spec,
        )

        return synthesize_worker_spec(unit, project_root=self._project_root)

    def _cage(self, graph: ExecutionGraph, unit: WorkUnitSpec, shape: Any) -> Any:
        """Build the ScopedToolBackend cage from the synthesized shape.

        REUSES ``SubagentFactory.build`` -- the per-worker allowlist + mutation
        count gate. Wires the worker's voice from the scheduler's per-graph bus
        when available (the factory gates voice on the bus master flag).
        """
        bus = None
        if self._get_bus is not None:
            try:
                bus = self._get_bus(graph.graph_id)
            except Exception:  # noqa: BLE001 -- no bus -> silent worker.
                bus = None

        scope_paths = list(unit.effective_owned_paths)

        if self._build_worker is not None:
            return self._build_worker(
                shape,
                worker_id=unit.unit_id,
                goal=unit.goal,
                scope_paths=scope_paths,
                bus=bus,
                graph_id=graph.graph_id,
            )

        if self._factory is None:
            from backend.core.ouroboros.governance.autonomy.subagent_factory import (
                SubagentFactory,
            )

            self._factory = SubagentFactory()
        return self._factory.build(
            shape,
            worker_id=unit.unit_id,
            goal=unit.goal,
            scope_paths=scope_paths,
            bus=bus,
            graph_id=graph.graph_id,
        )

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _is_deadlock(exc: BaseException) -> bool:
        """True iff ``exc`` is an EpistemicDeadlockBreaker shatter."""
        try:
            from backend.core.ouroboros.governance.autonomy.deadlock_breaker import (
                DeadlockInterruptedException,
            )

            return isinstance(exc, DeadlockInterruptedException)
        except Exception:  # noqa: BLE001
            # Fall back to a structural check so a missing import never lets a
            # deadlock escape as an uncaught exception.
            return type(exc).__name__ == "DeadlockInterruptedException"

    @staticmethod
    def _failed(
        graph: ExecutionGraph,
        unit: WorkUnitSpec,
        failure_class: str,
        error: str,
    ) -> WorkUnitResult:
        """Build a terminal FAILED result (fail-CLOSED). Never raises."""
        now = time.monotonic_ns()
        return WorkUnitResult(
            unit_id=unit.unit_id,
            repo=unit.repo,
            status=WorkUnitState.FAILED,
            patch=None,
            attempt_count=1,
            started_at_ns=now,
            finished_at_ns=now,
            failure_class=failure_class,
            error=error,
            causal_parent_id=getattr(graph, "causal_trace_id", ""),
        )
