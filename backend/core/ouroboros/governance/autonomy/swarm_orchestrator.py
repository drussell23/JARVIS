"""swarm_orchestrator — the Fleet Commander's dynamic-instantiation layer.

Phase 1a (G1): for each sub-goal, the :class:`SwarmOrchestrator`
DYNAMICALLY synthesizes a worker shape (role, tool allowlist, mutation +
context budgets) via AST/semantic inspection (the Golden Rule —
``worker_synthesizer.synthesize_worker_spec``), fills the additive swarm
fields of a ``WorkUnitSpec``, builds an ``ExecutionGraph``, and SUBMITS it
to the EXISTING ``SubagentScheduler``. No new scheduler is written.

Elastic adaptive fan-out (§3.1): the graph's ``concurrency_limit`` is set
from a live ``MemoryPressureGate`` decision (``elastic_fanout``) — burst
< 65%, hold 65-80%, freeze > 80% (fail-CLOSED to freeze on an unreadable
probe). Pending workers beyond the permitted level are held in a FIFO
queue (no drop) for a later drain.

**Gated ``JARVIS_SWARM_ORCHESTRATOR_ENABLED`` (default false).** OFF -> the
orchestrator is inert: it never synthesizes swarm fields and (by design)
callers fall back to the existing fixed-type scheduler path, which behaves
byte-identically to today. The ``WorkUnitSpec`` swarm fields stay None.

Phase 1a workers run isolated-but-silent: NO AgentMessageBus (§4 / Phase
1c) and NO EphemeralMemorySandbox (§5 / Phase 1b) are wired here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

from backend.core.ouroboros.governance.autonomy.elastic_fanout import (
    ElasticFanoutDecision,
    FanoutAction,
    PendingFanoutQueue,
    base_floor,
    decide_fanout,
)
from backend.core.ouroboros.governance.autonomy.subagent_types import (
    ExecutionGraph,
    WorkUnitSpec,
)
from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
    WorkerShape,
    render_worker_system_prompt,
    synthesize_worker_spec,
)

logger = logging.getLogger(__name__)

_SWARM_SCHEMA_VERSION = "swarm.graph.1a"


def is_orchestrator_enabled() -> bool:
    """Master gate. Default false -> the orchestrator is inert."""
    return os.environ.get("JARVIS_SWARM_ORCHESTRATOR_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass(frozen=True)
class SubGoal:
    """A decomposed sub-goal handed to the swarm.

    Duck-typed: anything with ``.goal`` + ``.target_files`` works with the
    synthesizer; this is the canonical typed carrier.
    """

    unit_id: str
    repo: str
    goal: str
    target_files: Tuple[str, ...]
    dependency_ids: Tuple[str, ...] = ()
    owned_paths: Tuple[str, ...] = ()


class SwarmOrchestrator:
    """Dynamic worker instantiation on the existing SubagentScheduler."""

    def __init__(
        self,
        *,
        scheduler: Optional[Any] = None,
        memory_pressure_gate: Optional[Any] = None,
        project_root: Optional[str] = None,
    ) -> None:
        """Parameters
        ----------
        scheduler:
            The EXISTING ``SubagentScheduler``. ``submit`` delegates to it.
        memory_pressure_gate:
            The EXISTING ``MemoryPressureGate`` used for elastic fan-out.
            When None, a default gate is lazily constructed at decision
            time (so the module imports clean).
        project_root:
            Base for resolving relative target_files during AST inspection.
        """
        self._scheduler = scheduler
        self._gate = memory_pressure_gate
        self._project_root = project_root or os.environ.get("JARVIS_PROJECT_ROOT") or None
        self._pending = PendingFanoutQueue()

    # -- the Golden Rule entry point -------------------------------------

    def define_worker(self, sub_goal: Any) -> WorkUnitSpec:
        """Synthesize a worker shape from the sub-goal and fill a WorkUnitSpec.

        Applies ``synthesize_worker_spec`` (AST/semantic inspection — NO
        static role table), renders the worker system prompt, and returns a
        ``WorkUnitSpec`` with the additive swarm fields populated.
        """
        shape: WorkerShape = synthesize_worker_spec(
            sub_goal, project_root=self._project_root,
        )

        goal = str(getattr(sub_goal, "goal", "") or "")
        raw_targets = getattr(sub_goal, "target_files", ()) or ()
        target_files = tuple(str(t) for t in raw_targets)
        owned = tuple(str(p) for p in (getattr(sub_goal, "owned_paths", ()) or ()))
        scope = owned or target_files

        prompt = render_worker_system_prompt(
            role=shape.role,
            goal=goal,
            scope_paths=list(scope),
            allowed_tools=shape.allowed_tools,
            mutation_budget=shape.mutation_budget,
            read_only=shape.read_only,
        )

        unit = WorkUnitSpec(
            unit_id=str(getattr(sub_goal, "unit_id", "") or "swarm-unit"),
            repo=str(getattr(sub_goal, "repo", "") or "JARVIS"),
            goal=goal,
            target_files=target_files,
            dependency_ids=tuple(
                str(d) for d in (getattr(sub_goal, "dependency_ids", ()) or ())
            ),
            owned_paths=owned,
            # --- synthesized swarm fields (the Golden Rule output) -------
            system_prompt_template=prompt,
            allowed_tools=shape.allowed_tools,
            mutation_budget=shape.mutation_budget,
            context_budget_tokens=shape.context_budget_tokens,
            worker_role=shape.role,
        )
        logger.info(
            "[SwarmOrchestrator] define_worker unit=%s role=%r tools=%s "
            "mutation_budget=%d read_only=%s conf=%.2f",
            unit.unit_id, shape.role, list(shape.allowed_tools),
            shape.mutation_budget, shape.read_only, shape.confidence,
        )
        return unit

    # -- graph assembly ---------------------------------------------------

    def _resolve_gate(self) -> Any:
        if self._gate is not None:
            return self._gate
        from backend.core.ouroboros.governance.memory_pressure_gate import (
            MemoryPressureGate,
        )
        self._gate = MemoryPressureGate()
        return self._gate

    def compute_fanout(self, n_workers: int) -> ElasticFanoutDecision:
        """Compute the elastic fan-out decision for ``n_workers`` (§3.1)."""
        gate = self._resolve_gate()
        return decide_fanout(
            gate=gate,
            current_concurrency=base_floor(),
            n_pending=max(0, int(n_workers) - base_floor()),
        )

    def build_graph(
        self,
        sub_goals: Sequence[Any],
        *,
        op_id: str = "swarm-op",
        graph_id: Optional[str] = None,
        planner_id: str = "swarm_orchestrator",
    ) -> ExecutionGraph:
        """Build an ExecutionGraph of synthesized workers.

        Honors the elastic fan-out decision: the graph's
        ``concurrency_limit`` is the permitted concurrency. Sub-goals
        beyond the permitted level are still encoded as units in the DAG
        (the scheduler runs them as concurrency frees) AND their ids are
        held in the FIFO pending queue under FREEZE/HOLD so the
        orchestrator can drain them explicitly.
        """
        units: List[WorkUnitSpec] = [self.define_worker(sg) for sg in sub_goals]
        if not units:
            raise ValueError("build_graph requires at least one sub-goal")

        decision = self.compute_fanout(len(units))
        permitted = max(1, decision.permitted_concurrency)

        # Under FREEZE/HOLD, hold the workers beyond the permitted floor in
        # the FIFO queue (no drop) — they remain encoded in the graph but
        # the orchestrator tracks them as pending for an explicit drain.
        if decision.action is not FanoutAction.BURST:
            for unit in units[permitted:]:
                self._pending.enqueue(unit.unit_id)
            logger.info(
                "[SwarmOrchestrator] fan-out=%s permitted=%d held_pending=%d "
                "reason=%s",
                decision.action.value, permitted,
                max(0, len(units) - permitted), decision.reason,
            )

        graph = ExecutionGraph(
            graph_id=graph_id or "swarm-graph",
            op_id=op_id,
            planner_id=planner_id,
            schema_version=_SWARM_SCHEMA_VERSION,
            units=tuple(units),
            concurrency_limit=permitted,
        )
        return graph

    @property
    def pending(self) -> PendingFanoutQueue:
        """The FIFO queue of workers held under backpressure."""
        return self._pending

    # -- submission to the EXISTING scheduler -----------------------------

    async def submit(self, graph: ExecutionGraph) -> bool:
        """Submit the graph to the EXISTING SubagentScheduler.

        Inert when the master gate is OFF (returns False without touching
        the scheduler) — OFF -> the scheduler path is byte-identical to
        today because the orchestrator never runs.
        """
        if not is_orchestrator_enabled():
            logger.debug(
                "[SwarmOrchestrator] master gate OFF -> inert "
                "(graph=%s not submitted)", graph.graph_id,
            )
            return False
        if self._scheduler is None:
            raise RuntimeError(
                "SwarmOrchestrator.submit requires a scheduler; none was provided"
            )
        return await self._scheduler.submit(graph)
