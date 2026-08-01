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
import os
import time
from typing import Any, Callable, Optional, Tuple

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


def require_preshaped_units() -> bool:
    """``JARVIS_SWARM_REQUIRE_PRESHAPED_UNITS`` (default false).

    The ROLLBACK for the eligibility fix below, not a second gate. ON
    restores the original predicate exactly — swarm only for units that
    already carry a shape — which is the behaviour that made the master gate
    inert. It exists so the change can be reverted without touching
    ``JARVIS_SWARM_ORCHESTRATOR_ENABLED``, never as a default.
    """
    return os.environ.get(
        "JARVIS_SWARM_REQUIRE_PRESHAPED_UNITS", "false",
    ).strip().lower() in ("1", "true", "yes", "on")


def swarm_eligible(unit: WorkUnitSpec, *, shape: Any = None) -> bool:
    """Whether this unit CAN be shaped into a caged swarm worker. NEVER raises.

    THE PREDICATE CARRIED ALMOST NO INFORMATION
    --------------------------------------------
    The version this replaces asked for a non-empty ``target_files`` and a
    non-empty ``goal``. ``WorkUnitSpec.__post_init__`` already raises without
    either, so the ``target_files`` branch could never be False for a
    constructible unit — the original circularity inverted rather than
    repaired: the first predicate answered no to everything (``eligible ⟺
    already shaped``), the second answered yes to nearly everything.

    "Nearly", and the exception is instructive. ``if not self.goal`` is False
    for ``"   "``, so a whitespace-only goal IS constructible and the
    ``goal.strip()`` branch really did reject it. That check is kept below —
    measured, a taskless unit against a parseable file still scores
    ``confidence=0.50``, because confidence blends "I read the targets" with
    "I understood the task". Deleting it as redundant would have widened
    eligibility to workers with no instruction.

    What both versions shared is guessing at proxies for a judgement that
    lives somewhere else.

    So this asks the component that actually knows. ``synthesize_worker_spec``
    inspects the target files by AST and classifies the goal's intent, and it
    reports what it managed with ``confidence`` — 0.90 for a goal it
    understood against a file it parsed, 0.00 for the degenerate read-only
    fallback it emits when the sub-goal is unparseable or the files are not
    there. Eligibility is that verdict, not a re-derivation of it.

    ``shape`` lets the caller pass a shape it has ALREADY synthesized.
    Synthesis reads and parses every target file; the routing decision and the
    execution path both need it, and doing it twice per unit would be the cost
    of asking the question in the first place. Passing it through means the
    honest predicate is also the cheaper one.

    THE ORIGINAL BUG, kept because both failures share one cause
    -------------------------------------------------------------
    The first predicate asked ``unit.is_swarm_worker`` — True only when the
    unit ALREADY carried a ``system_prompt_template`` / ``allowed_tools`` /
    ``worker_role``. But the invoker's whole job is to SYNTHESIZE that shape,
    and every production graph builder (``parallel_dispatch``,
    ``iteration_planner``, ``providers``, ``meta_goal_aggregator``,
    ``graph_coalescer``) leaves all five fields ``None``. The only writer is
    ``SwarmOrchestrator.define_worker``, which has no production caller. So
    the invoker would only shape a unit that was already shaped, and
    ``JARVIS_SWARM_ORCHESTRATOR_ENABLED=true`` still did nothing.

    Both versions failed the same way: each invented a LOCAL test for a
    question the synthesizer answers. Marking, then preconditions, now the
    synthesizer's own verdict — and only the third one cannot drift from it,
    because it IS it.

    Fail-CLOSED in the same direction as everything downstream: no derivable
    shape → legacy executor, byte-identical. A unit that cannot be inspected
    cannot be caged, and an uncaged worker must never run.
    """
    try:
        # An explicitly pre-shaped unit stays eligible — the orchestrator's
        # own `define_worker` path keeps working unchanged.
        if bool(getattr(unit, "is_swarm_worker", False)):
            return True
        if require_preshaped_units():
            return False
        if shape is None:
            from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (  # noqa: E501
                synthesize_worker_spec,
            )
            shape = synthesize_worker_spec(unit)
        if not _has_instruction(unit):
            return False
        if shape is None:
            return False
        # ABSENT confidence is not zero confidence.
        #
        # The floor below is `synthesize_worker_spec`'s own fail-CLOSED signal
        # read back, so it means something only for shapes IT produced. A
        # shape from an injected `define_worker` — the orchestrator's
        # `define_worker` path, or a test's — may carry no `confidence` field
        # at all, and defaulting that to 0.0 rejected it: a perfectly good
        # custom shape silently routed to the legacy executor, which is the
        # inertness class this whole predicate exists to have escaped.
        #
        # Present-and-0.0 still fails: that IS the synthesizer reporting it
        # could not read the sub-goal.
        confidence = getattr(shape, "confidence", None)
        if confidence is None:
            return True
        try:
            return float(confidence) > _min_shape_confidence()
        except (TypeError, ValueError):
            return True      # unreadable field -> not ours to judge
    except Exception:  # noqa: BLE001
        return False


def _has_instruction(unit: Any) -> bool:
    """Does this unit tell a worker what to DO? NEVER raises.

    A worker with no instruction cannot be caged usefully however well its
    files parse. ``WorkUnitSpec.__post_init__`` rejects ``goal=""`` but
    accepts ``goal="   "`` — ``if not self.goal`` is False for whitespace — so
    this catches a unit the type system lets through, and measured, the
    synthesizer scores that unit ``confidence=0.50`` on the strength of the
    FILES alone. Confidence blends "I read the targets" with "I understood the
    task"; a taskless worker fails the second even when the first went
    perfectly.

    Separate from the confidence floor because it must run BEFORE synthesis:
    synthesis reads and parses every target file, and paying that for a unit
    already known to be ineligible is waste the routing path should not incur.
    """
    try:
        return bool(str(getattr(unit, "goal", "") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _min_shape_confidence() -> float:
    """The floor a synthesized shape must clear to be worth caging.

    Default ``0.0``, and the comparison is STRICTLY greater — so the bar is
    "the synthesizer derived something", not a tuned number.

    That is not an arbitrary constant, it is the synthesizer's own fail-CLOSED
    signal read back. ``synthesize_worker_spec`` documents that "an
    unparseable / empty sub-goal yields the minimal read-only shape", and
    measured against real units that shape carries ``confidence=0.00`` while
    every unit it could actually inspect scores 0.40–0.90. Zero is the value
    the synthesizer emits when it could not do its job.

    Raising it (``JARVIS_SWARM_MIN_SHAPE_CONFIDENCE=0.5``) narrows the swarm to
    high-confidence shapes without touching this file — the knob an operator
    wants after watching a soak, expressed in the units the synthesizer
    already reports.
    """
    try:
        return max(0.0, min(1.0, float(os.environ.get(
            "JARVIS_SWARM_MIN_SHAPE_CONFIDENCE", "0.0") or 0.0)))
    except (TypeError, ValueError):
        return 0.0


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
        route, shape = self._route_decision(graph, unit)
        if not route:
            return await self._legacy.execute(graph, unit)
        # The shape the DECISION was made on is the shape that gets caged.
        # Re-synthesizing here would parse every target file a second time and,
        # worse, could disagree with the verdict that routed the unit — a
        # worker caged from a shape nobody approved.
        return await self._execute_swarm(graph, unit, shape=shape)

    # -- routing decision -------------------------------------------------

    def _route_decision(
        self, graph: ExecutionGraph, unit: WorkUnitSpec,
    ) -> Tuple[bool, Any]:
        """``(route_to_swarm, shape)``. Swarm iff master gate ON AND graph
        parallelizable AND unit ELIGIBLE.

        Returns the SHAPE alongside the verdict because eligibility is now the
        synthesizer's verdict rather than a guess about it — so deciding
        produces the very artefact the swarm path needs. Handing it forward
        keeps synthesis to once per unit and makes it impossible for the shape
        that was judged and the shape that gets caged to differ.

        The cheap gates run FIRST: with the master flag off, no file is read
        and no AST is parsed, so an OFF swarm costs exactly what it did before.
        """
        try:
            from backend.core.ouroboros.governance.autonomy.swarm_orchestrator import (
                is_orchestrator_enabled,
            )

            if not is_orchestrator_enabled():
                return False, None
            if not is_graph_parallelizable(graph):
                return False, None
            if bool(getattr(unit, "is_swarm_worker", False)):
                # Pre-shaped: `_synthesize` honours the injected
                # `define_worker`, so let the swarm path resolve it as before.
                return True, None
            if require_preshaped_units():
                return False, None
            # CHEAP GATES BEFORE SYNTHESIS. `_synthesize` runs the injected
            # `define_worker` and otherwise reads + AST-parses every target
            # file; a unit that cannot be eligible must not pay for that, and
            # a definer must not be called about a unit that was already
            # refused.
            if not _has_instruction(unit):
                return False, None
            shape = self._synthesize(unit)
            return swarm_eligible(unit, shape=shape), shape
        except Exception:  # noqa: BLE001 -- any decision error -> legacy (safe).
            logger.debug(
                "[SwarmInvoker] route decision raised -> legacy (non-fatal)",
                exc_info=True,
            )
            return False, None

    def _should_route_swarm(self, graph: ExecutionGraph, unit: WorkUnitSpec) -> bool:
        """Back-compat boolean view of :meth:`_route_decision`. NEVER raises."""
        return self._route_decision(graph, unit)[0]

    # -- the swarm path ---------------------------------------------------

    async def _execute_swarm(
        self, graph: ExecutionGraph, unit: WorkUnitSpec, *, shape: Any = None,
    ) -> WorkUnitResult:
        # 1. + 2. synthesize the worker shape + build its cage. Fail-CLOSED:
        #    any failure here -> FAILED unit, NEVER an uncaged execution.
        #
        # `shape` arrives from the routing decision, which had to synthesize it
        # to judge eligibility. None (a pre-shaped unit, or a direct call) ->
        # synthesize here, unchanged.
        try:
            shape = shape if shape is not None else self._synthesize(unit)
        except Exception as exc:  # noqa: BLE001 -- synthesis failure -> fail-CLOSED.
            return self._failed(
                graph, unit, "swarm_synthesis", f"worker_synthesis_failed:{exc}"
            )

        # Calibrate the PRIOR against observed evidence for this shape class.
        # Tighten-only and fail-open: a broken calibrator returns the
        # synthesizer's output unchanged, so this can cost the op nothing.
        try:
            from backend.core.ouroboros.governance.autonomy.cage_calibration import (
                calibrate_shape,
            )
            shape = calibrate_shape(shape)
        except Exception:  # noqa: BLE001 -- calibration is advisory, never fatal.
            logger.debug("[SwarmInvoker] cage calibration skipped", exc_info=True)

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
            _result = await self._legacy.execute(graph, unit)
            # The ONE seam where a caged worker finishes. Recording here means
            # a worker cannot complete without leaving evidence of what it
            # actually did with what it was granted.
            try:
                from backend.core.ouroboros.governance.autonomy.cage_calibration import (
                    observe_unit,
                )
                observe_unit(shape, getattr(built, "backend", None), _result)
            except Exception:  # noqa: BLE001 -- telemetry never fails the unit.
                logger.debug("[SwarmInvoker] cage observation skipped",
                             exc_info=True)
            return _result
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
