"""
AgenticPlanSubagent — Phase B Directed Acyclic Graph planner (Manifesto §2).

Implements the PlanExecutor protocol defined in subagent_orchestrator.py.
Given a parent op's description + target_files, produces a strict DAG
of implementation units that GENERATE can consume for parallel multi-
file work. Output is self-validated via ``dag_validator.validate_plan_dag``
before return — invalid DAGs are refused, not emitted.

Manifesto §2 (Directed Acyclic Graph) — architectural constraint:
    PLAN cannot output flat task lists. It must output strict,
    mathematically verifiable dependency DAGs.

    This implementation enforces:
      * Each unit declares unit_id, dependency_ids, owned_paths,
        acceptance_tests, optional barrier_id.
      * DAG is acyclic (validated).
      * Every unit reachable from some root (validated).
      * Parallel branches have disjoint owned_paths (validated).
      * Every unit has acceptance_test coverage or explicit rationale.

Step-1 behavior (this first cut): deterministic file-partition.
    * Each target file becomes one unit.
    * Units are independent (no dependency edges) — GENERATE gets
      full parallelism on multi-file ops.
    * owned_paths = [target_file] — strictly disjoint across units.
    * acceptance_tests = heuristic match against tests/ directory
      (file_stem naming convention). If no match found,
      no_test_rationale is auto-populated with "no existing test
      matches this file path; caller to add coverage post-plan".
    * unit_id is "unit_{N}_{file_stem}" for human readability.

Step-2 (future): LLM-driven DAG refinement.
    The deterministic partition above is the floor — a correct-but-
    naive DAG that always validates. An LLM upgrade can enrich the
    DAG with:
      * Actual dependency_ids derived from import-graph analysis
      * Barrier convergence points at shared-module boundaries
      * Richer acceptance_tests derived from test_strategy reasoning
    The upgrade is additive: replace ``_partition_deterministic``
    with an LLM-driven path that returns the same typed tuple. The
    validator remains the boundary — if the LLM emits an invalid
    DAG, we fall back to the deterministic partition rather than
    return an invalid result.

Cost: $0.00 per plan in deterministic mode (no LLM calls). Matches
Phase 1 economic thesis — LLM as orchestrator, code as worker.

Hard-kill discipline (Manifesto §3 Disciplined Concurrency):
    Any future LLM-driven branch MUST wrap provider calls in the
    asyncio.wait({task}, timeout=soft+30s) hard-kill pattern from
    providers.py:5257. A wedged planner cannot paralyze the
    orchestrator's PLAN phase.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.dag_validator import (
    DagValidationResult,
    validate_plan_dag,
)
from backend.core.ouroboros.governance.subagent_contracts import (
    SubagentContext,
    SubagentFinding,
    SubagentResult,
    SubagentStatus,
    SubagentType,
)

logger = logging.getLogger(__name__)


# ============================================================================
# AgenticPlanSubagent
# ============================================================================


class AgenticPlanSubagent:
    """PlanExecutor implementation producing a validated plan DAG.

    The Step-1 implementation is deterministic (file-partition strategy).
    Step-2 will layer LLM-driven DAG refinement; the constructor already
    holds the structural seams so the upgrade is additive, not a rewrite.

    Constructor is DI-friendly:

        planner = AgenticPlanSubagent(project_root=Path("/repo"))

        # Future Step-2: pass a planner_llm callable to enrich the DAG.
        planner = AgenticPlanSubagent(
            project_root=Path("/repo"),
            llm_planner=my_llm_planner,
        )
    """

    def __init__(
        self,
        project_root: Path,
        *,
        llm_planner: Optional[
            Callable[[Dict[str, Any]], "asyncio.Future"]
        ] = None,
        llm_budget_s: float = 45.0,
    ) -> None:
        self._root = Path(project_root)
        # Step-2 seam — kept unused in Step 1. A future LLM planner
        # returns an awaitable that resolves to a list of unit dicts.
        self._llm_planner = llm_planner
        self._llm_budget_s = float(llm_budget_s)

    # ------------------------------------------------------------------
    # PlanExecutor protocol
    # ------------------------------------------------------------------

    async def plan(self, ctx: SubagentContext) -> SubagentResult:
        """Produce a validated plan DAG for ctx.request.plan_target.

        Returns a well-formed SubagentResult with type_payload carrying
        the typed DAG tuple. Never raises except for
        asyncio.CancelledError.
        """
        started_ns = time.time_ns()
        plan_target = getattr(ctx.request, "plan_target", None)
        if not plan_target:
            return self._malformed_input_result(
                ctx, started_ns,
                detail="plan_target missing from request",
            )

        op_description = str(plan_target.get("op_description", ""))
        target_files: Tuple[str, ...] = tuple(
            plan_target.get("target_files", ()) or ()
        )
        if not target_files:
            return self._malformed_input_result(
                ctx, started_ns,
                detail="plan_target.target_files is empty; "
                "single-file ops should skip PLAN entirely",
            )

        try:
            # Step 1: deterministic partition.
            units = self._partition_deterministic(
                target_files=target_files,
                op_description=op_description,
            )
            # Step 1-½: self-validate the DAG before returning. §2
            # mathematical verification is non-negotiable.
            validation = validate_plan_dag(units)
            if not validation.valid:
                # Deterministic partition shouldn't produce an invalid
                # DAG — if it does, the bug is in _partition_deterministic.
                # Surface as a FAILED status with the validation errors so
                # the regression test catches the drift immediately.
                return self._validation_failure_result(
                    ctx, started_ns, validation=validation, units=units,
                )
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — structural defense
            logger.exception(
                "[AgenticPlanSubagent] unexpected failure sub=%s",
                ctx.subagent_id,
            )
            return self._internal_failure_result(ctx, started_ns, error=e)

        # Typed DAG payload — tuple-of-tuple so SubagentResult stays frozen.
        # Each unit is itself a tuple of (key, value) pairs for the same
        # reason. Consumers convert via dict(unit) on the way out.
        dag_units: Tuple[Tuple[Tuple[str, Any], ...], ...] = tuple(
            tuple(sorted(u.items())) for u in units
        )
        # Edges derived from dependency_ids (shown separately for
        # observability; the unit list is the canonical source).
        dag_edges: Tuple[Tuple[str, str], ...] = tuple(
            (src, str(u.get("unit_id")))
            for u in units
            for src in (u.get("dependency_ids", ()) or ())
        )
        # execution_graph 2d.1 shape (tuple-of-tuple so SubagentResult
        # stays frozen). This matches the schema producers.py emits for
        # cross-repo execution graphs (_SCHEMA_VERSION_EXECUTION_GRAPH)
        # — consumers (orchestrator PLAN-shadow, future GENERATE
        # consumption) convert via dict() on the way out. Shape:
        #   schema_version / graph_id / planner_id /
        #   concurrency_limit / units
        execution_graph = _build_execution_graph_payload(
            units=units,
            op_description=op_description,
            concurrency_limit=len(validation.parallel_branches) + 1,
        )
        payload: Tuple[Tuple[str, Any], ...] = (
            ("dag_units", dag_units),
            ("dag_edges", dag_edges),
            ("unit_count", validation.unit_count),
            ("edge_count", validation.edge_count),
            ("root_count", validation.root_count),
            ("parallel_branches", validation.parallel_branches),
            ("validation_valid", validation.valid),
            ("validation_errors", validation.errors),
            # Adapter key — 2d.1-shaped execution_graph for downstream
            # consumers that expect the schema producers.py defines.
            ("execution_graph", execution_graph),
        )

        # Findings: one per unit for the observability pipeline. The
        # Phase 1 ledger + CommProtocol consumers read these without
        # needing to understand the DAG shape.
        findings = tuple(
            SubagentFinding(
                category="pattern",
                description=(
                    f"plan_unit={u.get('unit_id')} "
                    f"paths={list(u.get('owned_paths', ()))} "
                    f"deps={list(u.get('dependency_ids', ()))}"
                ),
                file_path=(
                    u.get("owned_paths", [None])[0]
                    or str(target_files[0])
                ),
                line=0,
                evidence=",".join(u.get("acceptance_tests", ()) or ())[:200],
                relevance=0.8,
            )
            for u in units
        )

        finished_ns = time.time_ns()
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.PLAN,
            status=SubagentStatus.COMPLETED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=finished_ns,
            findings=findings,
            files_read=target_files,
            search_queries=(),
            summary=(
                f"PLAN dag units={validation.unit_count} "
                f"edges={validation.edge_count} "
                f"roots={validation.root_count} "
                f"parallel_pairs={len(validation.parallel_branches)}"
            ),
            cost_usd=0.0,                   # deterministic — no LLM
            tool_calls=1,
            tool_diversity=1,
            provider_used="deterministic",
            fallback_triggered=False,
            type_payload=payload,
        )

    # ------------------------------------------------------------------
    # Step-1 deterministic partition strategy
    # ------------------------------------------------------------------

    def _partition_deterministic(
        self,
        *,
        target_files: Tuple[str, ...],
        op_description: str,
    ) -> List[Dict[str, Any]]:
        """Partition target_files into independent DAG units.

        Strategy (floor, not ceiling):
          * One unit per target file.
          * No dependency edges — GENERATE can parallelize all units.
          * owned_paths = [file]; disjoint by construction.
          * acceptance_tests = glob tests/ for matching test file;
            fall back to no_test_rationale if no match.
          * unit_id = "unit_{N}_{file_stem}" — stable ordering + human
            readable.

        This is the minimum correct DAG for multi-file ops. LLM-driven
        refinement replaces THIS METHOD without touching anything else.
        """
        units: List[Dict[str, Any]] = []
        for idx, path in enumerate(target_files):
            stem = Path(path).stem
            unit_id = f"unit_{idx:02d}_{_sanitize_id(stem)}"
            tests = self._discover_acceptance_tests(path)
            unit: Dict[str, Any] = {
                "unit_id": unit_id,
                "dependency_ids": (),
                "owned_paths": (path,),
                "acceptance_tests": tests,
                "barrier_id": "",
            }
            if not tests:
                unit["no_test_rationale"] = (
                    f"no existing test matches {path!r}; caller to add "
                    f"coverage post-plan for goal: {op_description[:120]!r}"
                )
            units.append(unit)
        return units

    def _discover_acceptance_tests(self, target_file: str) -> Tuple[str, ...]:
        """Glob the tests/ tree for test files matching the target file stem.

        Heuristic — looks for ``tests/**/test_{stem}.py``. If the target
        file is already a test file (stem starts with ``test_``), returns
        the file itself as its own acceptance test.
        """
        stem = Path(target_file).stem
        if stem.startswith("test_"):
            return (target_file,)
        tests_dir = self._root / "tests"
        if not tests_dir.is_dir():
            return ()
        try:
            matches = [
                str(p.relative_to(self._root))
                for p in tests_dir.rglob(f"test_{stem}.py")
            ]
        except (OSError, ValueError):
            return ()
        # Cap at 5 to keep type_payload tight.
        return tuple(sorted(matches)[:5])

    # ------------------------------------------------------------------
    # Failure result helpers
    # ------------------------------------------------------------------

    def _malformed_input_result(
        self, ctx: SubagentContext, started_ns: int, *, detail: str,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.PLAN,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class="MalformedPlanInput",
            error_detail=detail,
            provider_used="deterministic",
        )

    def _validation_failure_result(
        self,
        ctx: SubagentContext,
        started_ns: int,
        *,
        validation: DagValidationResult,
        units: List[Dict[str, Any]],
    ) -> SubagentResult:
        """Deterministic partition produced an invalid DAG — this is a
        bug in the partitioner, not in the caller's input. Surface with
        enough detail that the regression test can pin the cause.
        """
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.PLAN,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class="InvalidPlanDag",
            error_detail=(
                "deterministic partition emitted invalid DAG: "
                + "; ".join(validation.errors[:3])
            ),
            provider_used="deterministic",
            type_payload=(
                ("dag_units", tuple(
                    tuple(sorted(u.items())) for u in units
                )),
                ("validation_valid", False),
                ("validation_errors", validation.errors),
            ),
        )

    def _internal_failure_result(
        self, ctx: SubagentContext, started_ns: int, *, error: Exception,
    ) -> SubagentResult:
        return SubagentResult(
            subagent_id=ctx.subagent_id,
            subagent_type=SubagentType.PLAN,
            status=SubagentStatus.FAILED,
            goal=ctx.request.goal,
            started_at_ns=started_ns,
            finished_at_ns=time.time_ns(),
            error_class=type(error).__name__,
            error_detail=str(error)[:500],
            provider_used="deterministic",
        )


# ============================================================================
# Helpers
# ============================================================================


_ID_SAFE_RE = re.compile(r"[^a-z0-9]")


def _sanitize_id(s: str) -> str:
    """Lowercase + non-alphanum stripped, truncated to 32 chars.

    unit_id is used as a dictionary key and shows up in log lines, so
    we keep it ASCII and short. Empty results fall back to "x" to
    avoid "unit_00_".
    """
    cleaned = _ID_SAFE_RE.sub("", (s or "").lower())[:32]
    return cleaned or "x"


_PLANNER_ID = "AgenticPlanSubagent/deterministic"


def _build_execution_graph_payload(
    *,
    units: List[Dict[str, Any]],
    op_description: str,
    concurrency_limit: int,
) -> Tuple[Tuple[str, Any], ...]:
    """Build the ``execution_graph 2d.1``-shaped tuple-of-tuple payload.

    Matches the schema ``providers.py`` declares at
    ``_SCHEMA_VERSION_EXECUTION_GRAPH``. Tuple-of-tuple form (not dict)
    because ``SubagentResult.type_payload`` is frozen. Consumers that
    need a dict call ``dict(payload)`` on the way out.

    ``graph_id`` is a deterministic sha256[:16] of the sorted unit_ids
    plus the op_description prefix — stable across reruns on the same
    input, useful for dedup and cross-session correlation in telemetry.
    """
    unit_id_join = ",".join(sorted(str(u.get("unit_id", "")) for u in units))
    hash_material = f"{op_description[:200]}|{unit_id_join}".encode("utf-8")
    graph_id = hashlib.sha256(hash_material).hexdigest()[:16]

    units_payload: Tuple[Tuple[Tuple[str, Any], ...], ...] = tuple(
        (
            ("unit_id", str(u.get("unit_id", ""))),
            ("dependency_ids", tuple(
                str(d) for d in (u.get("dependency_ids", ()) or ())
            )),
            ("owned_paths", tuple(
                str(p) for p in (u.get("owned_paths", ()) or ())
            )),
            ("acceptance_tests", tuple(
                str(t) for t in (u.get("acceptance_tests", ()) or ())
            )),
            ("barrier_id", str(u.get("barrier_id", "") or "")),
        )
        for u in units
    )

    return (
        ("schema_version", "2d.1"),
        ("graph_id", graph_id),
        ("planner_id", _PLANNER_ID),
        ("concurrency_limit", max(1, int(concurrency_limit))),
        ("units", units_payload),
    )


def build_default_plan_factory(
    project_root: Path,
) -> Callable[[], AgenticPlanSubagent]:
    """Factory helper matching the pattern of build_default_{explore,review}_factory.

    The default factory wires AgenticPlanSubagent with NO LLM planner
    — Step-1 is deterministic. An LLM-driven factory can be swapped in
    by the operator via a custom factory function.
    """
    def _factory() -> AgenticPlanSubagent:
        return AgenticPlanSubagent(project_root=project_root)
    return _factory
