"""Gap #3 Slice 1 — L3 worktree topology substrate primitive.

Pure-stdlib projection of the SubagentScheduler's in-memory DAG
state into a frozen, JSON-serializable topology record. Closes
the §2 Deep Observability gap: operators can see the per-graph
DAG topology + per-unit lifecycle + worktree-on-disk
correspondence without instrumenting the scheduler or shelling
out themselves.

## What it is

A *projection* — pure read over caller-supplied inputs:

  1. ``SubagentScheduler``-shaped object (duck-typed; tests pass
     a stub). Reads ``_graphs`` (the authoritative in-memory DAG
     state).
  2. Optional pre-fetched list of git worktree paths from
     ``WorktreeManager`` (so the substrate stays sync + pure;
     Slice 2's HTTP layer does the async git query).

Returns a frozen ``WorktreeTopology`` carrying:

  * Per-graph projection (``GraphTopology``): graph_id, op_id,
    phase, plan_digest, causal_trace_id, plus the unit DAG.
  * Per-unit projection (``WorktreeNode``): unit_id, repo, goal,
    target_files, owned_paths, lifecycle state, attempt count,
    on-disk worktree correspondence.
  * Per-edge projection (``TopologyEdge``): from_unit_id,
    to_unit_id, edge_kind.
  * Summary aggregates: per-state counts, per-phase counts,
    orphan worktree count (paths that exist on disk but no unit
    references them).

## What it does NOT do (later slices)

  * No HTTP exposure — Slice 2 ships the GET endpoints in
    ``ide_observability.py``.
  * No SSE — Slice 3 ships the publish hooks at scheduler
    transitions.
  * No git I/O — Slice 2 calls a small async helper that yields
    the path list to feed this substrate.

## Cage discipline (load-bearing)

  * **Read-only**: every projection method returns frozen records;
    consumers cannot mutate scheduler state through this surface.
  * **Duck-typed scheduler**: the substrate accepts any object
    exposing ``_graphs: Mapping[str, GraphExecutionState]`` so
    tests can pass minimal stubs without booting a full
    SubagentScheduler.
  * **NEVER raises**: every public function returns a structured
    ``WorktreeTopology`` even on degenerate inputs. The
    ``outcome`` field carries the diagnostic; consumers branch on
    that, not on exceptions.

## Default-off

``JARVIS_WORKTREE_TOPOLOGY_ENABLED`` (default ``false`` until
Slice 5 graduation). When off, ``compute_worktree_topology`` short-
circuits to ``outcome=DISABLED`` and returns an empty record.

## Authority surface (AST-pinned by Slice 5)

  * Imports: stdlib + ``autonomy.subagent_types`` (frozen DAG
    types) ONLY. Does NOT import the scheduler module itself
    (duck-typed).
  * MUST NOT import: orchestrator / iron_gate / policy_engine /
    risk_engine / change_engine / tool_executor / providers /
    candidate_generator / semantic_guardian / semantic_firewall /
    scoped_tool_backend / worktree_manager (writer side — read-
    only substrate consumes git output as a string sequence; it
    does not invoke any process).
  * No filesystem I/O, no subprocess, no env mutation, no network.
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import (
    Any, Dict, List, Mapping, Optional, Sequence, Tuple,
)

from backend.core.ouroboros.governance.autonomy.subagent_types import (
    GraphExecutionPhase,
    GraphExecutionState,
    WorkUnitState,
)

logger = logging.getLogger(__name__)


WORKTREE_TOPOLOGY_SCHEMA_VERSION: str = "worktree_topology.1"


# ---------------------------------------------------------------------------
# Master flag (default-off until Slice 5 graduation)
# ---------------------------------------------------------------------------


def worktree_topology_enabled() -> bool:
    """``JARVIS_WORKTREE_TOPOLOGY_ENABLED`` (default ``true`` —
    graduated 2026-05-02 in Gap #3 Slice 5). The substrate is a
    pure read-only projection over scheduler in-memory state +
    caller-supplied git worktree paths; structurally safe to
    enable by default. Operator hot-reverts via explicit
    ``=false``.

    Empty / unset / whitespace = graduated default. NEVER raises."""
    try:
        raw = os.environ.get(
            "JARVIS_WORKTREE_TOPOLOGY_ENABLED", "",
        ).strip().lower()
        if raw == "":
            return True  # graduated 2026-05-02
        return raw in ("1", "true", "yes", "on")
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Closed 5-value taxonomy of projection outcomes
# ---------------------------------------------------------------------------


class TopologyOutcome(str, enum.Enum):
    """Outcome of one ``compute_worktree_topology`` call. Closed
    taxonomy.

    ``OK``                — projection succeeded; ``graphs``
                             populated (possibly empty if the
                             scheduler has no live graphs).
    ``EMPTY``             — scheduler exposes no in-memory graphs
                             (post-cleanup or pre-submit). Distinct
                             from OK so consumers can render
                             ``no active graphs`` UX vs. an empty
                             list mid-flight.
    ``DISABLED``          — master flag off; no projection rendered.
    ``SCHEDULER_INVALID`` — supplied scheduler does not expose the
                             expected ``_graphs`` mapping (defense
                             against caller misuse).
    ``FAILED``            — defensive sentinel; consumer should
                             render an error state."""

    OK = "ok"
    EMPTY = "empty"
    DISABLED = "disabled"
    SCHEDULER_INVALID = "scheduler_invalid"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Closed edge-kind vocabulary
# ---------------------------------------------------------------------------


class EdgeKind(str, enum.Enum):
    """Closed 2-value edge taxonomy.

    ``DEPENDENCY`` — explicit ``unit.dependency_ids`` edge.
    ``BARRIER``    — implicit edge inserted between two units that
                      share a ``barrier_id`` (tests may inject
                      barrier edges for clarity in the rendered
                      graph; not authoritative — the scheduler
                      uses dependency_ids only)."""

    DEPENDENCY = "dependency"
    BARRIER = "barrier"


# ---------------------------------------------------------------------------
# Frozen records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorktreeNode:
    """One unit in a graph projection — frozen so downstream
    SSE/IDE consumers cannot mutate scheduler state through us."""

    unit_id: str
    repo: str
    goal: str
    target_files: Tuple[str, ...]
    owned_paths: Tuple[str, ...]
    dependency_ids: Tuple[str, ...]
    state: WorkUnitState
    barrier_id: str
    has_worktree: bool
    worktree_path: str
    attempt_count: int
    schema_version: str = WORKTREE_TOPOLOGY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "repo": self.repo,
            "goal": self.goal,
            "target_files": list(self.target_files),
            "owned_paths": list(self.owned_paths),
            "dependency_ids": list(self.dependency_ids),
            "state": self.state.value,
            "barrier_id": self.barrier_id,
            "has_worktree": self.has_worktree,
            "worktree_path": self.worktree_path,
            "attempt_count": self.attempt_count,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class TopologyEdge:
    """One DAG edge in a graph projection."""

    from_unit_id: str
    to_unit_id: str
    edge_kind: EdgeKind

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from_unit_id": self.from_unit_id,
            "to_unit_id": self.to_unit_id,
            "edge_kind": self.edge_kind.value,
        }


@dataclass(frozen=True)
class GraphTopology:
    """Per-graph projection (one entry in
    ``WorktreeTopology.graphs``)."""

    graph_id: str
    op_id: str
    planner_id: str
    plan_digest: str
    causal_trace_id: str
    phase: GraphExecutionPhase
    concurrency_limit: int
    nodes: Tuple[WorktreeNode, ...]
    edges: Tuple[TopologyEdge, ...]
    last_error: str
    updated_at_ns: int
    checksum: str
    schema_version: str = WORKTREE_TOPOLOGY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "op_id": self.op_id,
            "planner_id": self.planner_id,
            "plan_digest": self.plan_digest,
            "causal_trace_id": self.causal_trace_id,
            "phase": self.phase.value,
            "concurrency_limit": self.concurrency_limit,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "last_error": self.last_error,
            "updated_at_ns": self.updated_at_ns,
            "checksum": self.checksum,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class TopologySummary:
    """Aggregate counts across the whole projection."""

    total_graphs: int
    total_units: int
    units_by_state: Dict[str, int]
    graphs_by_phase: Dict[str, int]
    units_with_worktree: int
    orphan_worktree_count: int
    orphan_worktree_paths: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_graphs": self.total_graphs,
            "total_units": self.total_units,
            "units_by_state": dict(self.units_by_state),
            "graphs_by_phase": dict(self.graphs_by_phase),
            "units_with_worktree": self.units_with_worktree,
            "orphan_worktree_count": self.orphan_worktree_count,
            "orphan_worktree_paths": list(
                self.orphan_worktree_paths,
            ),
        }


@dataclass(frozen=True)
class WorktreeTopology:
    """Top-level projection. ``outcome`` is the closed-taxonomy
    discriminant; consumers branch on it before reading ``graphs``."""

    outcome: TopologyOutcome
    graphs: Tuple[GraphTopology, ...]
    summary: TopologySummary
    detail: str
    captured_at_ns: int
    schema_version: str = WORKTREE_TOPOLOGY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "outcome": self.outcome.value,
            "graphs": [g.to_dict() for g in self.graphs],
            "summary": self.summary.to_dict(),
            "detail": self.detail,
            "captured_at_ns": self.captured_at_ns,
            "schema_version": self.schema_version,
        }


_EMPTY_SUMMARY = TopologySummary(
    total_graphs=0,
    total_units=0,
    units_by_state={},
    graphs_by_phase={},
    units_with_worktree=0,
    orphan_worktree_count=0,
    orphan_worktree_paths=(),
)


# ---------------------------------------------------------------------------
# Worktree-path correspondence helpers
# ---------------------------------------------------------------------------
#
# WorktreeManager creates worktrees under
# ``<worktree_base>/<safe_branch_name>`` where ``safe_branch_name``
# is ``branch_name.replace("/", "__").replace(" ", "_")``. The
# scheduler convention is to name the branch ``unit-<unit_id>`` so
# the worktree path basename ends in the unit_id (mod safe-mangling).
#
# We do NOT re-implement the safe-name mangling here — instead we
# match by SUFFIX: a path's basename ends with the unit_id (after
# stripping the canonical "unit-" prefix from the basename and the
# unit_id). This is robust to mangling because "/" and " " never
# appear in unit_ids by scheduler convention.


def _unit_id_from_worktree_path(path: str) -> str:
    """Extract the unit_id from a worktree path basename. Returns
    empty string when the path doesn't follow the scheduler's
    ``unit-<unit_id>`` convention. NEVER raises."""
    try:
        if not path:
            return ""
        # Hand-rolled basename — keeps substrate FS-import-free
        # for defense-in-depth against the FS-token AST pin. A
        # basic string split is sufficient for the canonical
        # ``<base>/unit-<id>`` layout.
        base = path.rstrip("/").rsplit("/", 1)[-1]
        # The branch convention is "unit-<id>" → mangled basename
        # also starts with "unit-".
        if not base.startswith("unit-"):
            return ""
        return base[len("unit-"):]
    except Exception:  # noqa: BLE001 — defensive
        return ""


def _build_worktree_index(
    worktree_paths: Sequence[str],
) -> Dict[str, str]:
    """Map ``unit_id -> path`` from a flat list of worktree
    directory paths. Skips paths that don't follow the scheduler
    convention (logged at DEBUG; not an error — operator may have
    other worktrees). NEVER raises."""
    out: Dict[str, str] = {}
    try:
        for p in worktree_paths or ():
            try:
                uid = _unit_id_from_worktree_path(str(p))
                if uid:
                    out[uid] = str(p)
            except Exception:  # noqa: BLE001 — per-entry defensive
                continue
    except Exception:  # noqa: BLE001 — defensive
        pass
    return out


# ---------------------------------------------------------------------------
# Internal: per-graph projection
# ---------------------------------------------------------------------------


def _state_for_unit(
    unit_id: str, state: GraphExecutionState,
) -> WorkUnitState:
    """Derive a unit's lifecycle state from the
    ``GraphExecutionState`` ready/running/completed/failed/
    cancelled tuples. Falls back to PENDING when a unit appears in
    none of the categorized tuples (default after CREATED, before
    schedule)."""
    try:
        if unit_id in state.completed_units:
            return WorkUnitState.COMPLETED
        if unit_id in state.running_units:
            return WorkUnitState.RUNNING
        if unit_id in state.failed_units:
            return WorkUnitState.FAILED
        if unit_id in state.cancelled_units:
            return WorkUnitState.CANCELLED
        # Ready and unscheduled both render as PENDING for the
        # operator — only RUNNING and the terminal kinds carry
        # operational signal.
        return WorkUnitState.PENDING
    except Exception:  # noqa: BLE001 — defensive
        return WorkUnitState.PENDING


def _attempt_count_for_unit(
    unit_id: str, state: GraphExecutionState,
) -> int:
    """Read attempt_count from the unit's WorkUnitResult when
    available; else 0. NEVER raises."""
    try:
        result = state.results.get(unit_id)
        if result is None:
            return 0
        return int(getattr(result, "attempt_count", 0) or 0)
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _project_graph(
    state: GraphExecutionState,
    worktree_by_unit: Mapping[str, str],
) -> GraphTopology:
    """Pure projection of one ``GraphExecutionState`` into a
    frozen ``GraphTopology``. NEVER raises (caller wraps in a
    try/except for the rare degenerate case)."""
    nodes: List[WorktreeNode] = []
    edges: List[TopologyEdge] = []
    barrier_groups: Dict[str, List[str]] = {}
    for unit in state.graph.units:
        try:
            unit_state = _state_for_unit(unit.unit_id, state)
            wt_path = worktree_by_unit.get(unit.unit_id, "")
            nodes.append(WorktreeNode(
                unit_id=unit.unit_id,
                repo=unit.repo,
                goal=unit.goal,
                target_files=unit.target_files,
                owned_paths=unit.effective_owned_paths,
                dependency_ids=unit.dependency_ids,
                state=unit_state,
                barrier_id=unit.barrier_id,
                has_worktree=bool(wt_path),
                worktree_path=wt_path,
                attempt_count=_attempt_count_for_unit(
                    unit.unit_id, state,
                ),
            ))
            for dep in unit.dependency_ids:
                edges.append(TopologyEdge(
                    from_unit_id=dep,
                    to_unit_id=unit.unit_id,
                    edge_kind=EdgeKind.DEPENDENCY,
                ))
            if unit.barrier_id:
                barrier_groups.setdefault(
                    unit.barrier_id, [],
                ).append(unit.unit_id)
        except Exception:  # noqa: BLE001 — per-unit defensive
            continue

    # Add barrier edges between units sharing a barrier_id (if more
    # than one unit; barrier of size 1 is a no-op). Sorted for
    # determinism so the projection checksum is stable.
    for _barrier_id, members in barrier_groups.items():
        if len(members) < 2:
            continue
        sorted_members = sorted(members)
        for i in range(len(sorted_members) - 1):
            edges.append(TopologyEdge(
                from_unit_id=sorted_members[i],
                to_unit_id=sorted_members[i + 1],
                edge_kind=EdgeKind.BARRIER,
            ))

    return GraphTopology(
        graph_id=state.graph.graph_id,
        op_id=state.graph.op_id,
        planner_id=state.graph.planner_id,
        plan_digest=state.graph.plan_digest,
        causal_trace_id=state.graph.causal_trace_id,
        phase=state.phase,
        concurrency_limit=state.graph.concurrency_limit,
        nodes=tuple(nodes),
        edges=tuple(edges),
        last_error=state.last_error,
        updated_at_ns=state.updated_at_ns,
        checksum=state.checksum,
    )


# ---------------------------------------------------------------------------
# Public: compute_worktree_topology (total decision function)
# ---------------------------------------------------------------------------


def compute_worktree_topology(
    *,
    scheduler: Any,
    git_worktree_paths: Sequence[str] = (),
    enabled_override: Optional[bool] = None,
) -> WorktreeTopology:
    """Pure projection of scheduler in-memory state +
    caller-supplied git worktree paths into a frozen
    ``WorktreeTopology``.

    Decision tree (top-down, first match wins):

      1. Master flag off → ``DISABLED``.
      2. Scheduler missing the expected ``_graphs`` attribute →
         ``SCHEDULER_INVALID``.
      3. ``_graphs`` is empty → ``EMPTY`` (distinct from OK so
         operator UX can render "no active graphs").
      4. Otherwise → ``OK`` + per-graph projections + summary.

    ``enabled_override`` short-circuits the master flag — intended
    for tests; production callers leave it ``None``.

    NEVER raises. Returns a ``WorktreeTopology`` on every code
    path."""
    captured_at_ns = time.monotonic_ns()
    try:
        is_enabled = (
            enabled_override
            if enabled_override is not None
            else worktree_topology_enabled()
        )
        if not is_enabled:
            return WorktreeTopology(
                outcome=TopologyOutcome.DISABLED,
                graphs=(),
                summary=_EMPTY_SUMMARY,
                detail="master_flag_off",
                captured_at_ns=captured_at_ns,
            )

        graphs_attr = getattr(scheduler, "_graphs", None)
        if not isinstance(graphs_attr, Mapping):
            return WorktreeTopology(
                outcome=TopologyOutcome.SCHEDULER_INVALID,
                graphs=(),
                summary=_EMPTY_SUMMARY,
                detail=(
                    f"scheduler missing _graphs mapping "
                    f"(got {type(graphs_attr).__name__})"
                ),
                captured_at_ns=captured_at_ns,
            )

        worktree_by_unit = _build_worktree_index(git_worktree_paths)
        all_unit_ids: List[str] = []

        graph_projections: List[GraphTopology] = []
        for graph_id, state in sorted(graphs_attr.items()):
            try:
                if not isinstance(state, GraphExecutionState):
                    continue
                projection = _project_graph(state, worktree_by_unit)
                graph_projections.append(projection)
                for node in projection.nodes:
                    all_unit_ids.append(node.unit_id)
            except Exception:  # noqa: BLE001 — per-graph defensive
                logger.debug(
                    "[WorktreeTopology] per-graph projection "
                    "raised for graph_id=%s", graph_id,
                )
                continue

        if not graph_projections:
            return WorktreeTopology(
                outcome=TopologyOutcome.EMPTY,
                graphs=(),
                summary=_EMPTY_SUMMARY,
                detail="scheduler has no in-memory graphs",
                captured_at_ns=captured_at_ns,
            )

        summary = _build_summary(
            graph_projections, worktree_by_unit, all_unit_ids,
        )
        return WorktreeTopology(
            outcome=TopologyOutcome.OK,
            graphs=tuple(graph_projections),
            summary=summary,
            detail="",
            captured_at_ns=captured_at_ns,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort defensive
        logger.debug(
            "[WorktreeTopology] compute_worktree_topology "
            "raised: %s", exc,
        )
        return WorktreeTopology(
            outcome=TopologyOutcome.FAILED,
            graphs=(),
            summary=_EMPTY_SUMMARY,
            detail=f"compute_failed:{type(exc).__name__}",
            captured_at_ns=captured_at_ns,
        )


def _build_summary(
    graph_projections: Sequence[GraphTopology],
    worktree_by_unit: Mapping[str, str],
    all_unit_ids: Sequence[str],
) -> TopologySummary:
    """Aggregate counts from per-graph projections + cross-
    reference worktree paths to identify orphans (worktrees that
    exist on disk but no live unit references them)."""
    units_by_state: Dict[str, int] = {}
    graphs_by_phase: Dict[str, int] = {}
    units_with_worktree = 0

    for graph in graph_projections:
        graphs_by_phase[graph.phase.value] = (
            graphs_by_phase.get(graph.phase.value, 0) + 1
        )
        for node in graph.nodes:
            units_by_state[node.state.value] = (
                units_by_state.get(node.state.value, 0) + 1
            )
            if node.has_worktree:
                units_with_worktree += 1

    referenced_unit_ids = frozenset(all_unit_ids)
    orphan_paths: List[str] = []
    for unit_id, path in worktree_by_unit.items():
        if unit_id not in referenced_unit_ids:
            orphan_paths.append(path)
    orphan_paths.sort()

    return TopologySummary(
        total_graphs=len(graph_projections),
        total_units=len(all_unit_ids),
        units_by_state=units_by_state,
        graphs_by_phase=graphs_by_phase,
        units_with_worktree=units_with_worktree,
        orphan_worktree_count=len(orphan_paths),
        orphan_worktree_paths=tuple(orphan_paths),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EdgeKind",
    "GraphTopology",
    "TopologyEdge",
    "TopologyOutcome",
    "TopologySummary",
    "WORKTREE_TOPOLOGY_SCHEMA_VERSION",
    "WorktreeNode",
    "WorktreeTopology",
    "compute_worktree_topology",
    "worktree_topology_enabled",
]
