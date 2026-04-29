"""Priority 2 Slice 3 — Causality DAG construction primitive.

Pure-data graph builder that reads the JSONL ledger and produces a
navigable DAG.  Every ``DecisionRecord`` becomes a node; explicit
``parent_record_ids`` become edges; a reverse-edge index enables O(1)
child lookups.

ROOT PROBLEM this module closes:

  Records in ``decisions.jsonl`` have parent pointers (Slice 1) but
  no in-memory graph representation.  Operators cannot:

  1. Render the upstream causal tree of any decision
  2. Detect counterfactual branches (HypothesisProbe what-ifs)
  3. Compare two sessions' decision graphs for drift
  4. Topologically sort a session to detect cyclic corruption

``CausalityDAG`` is the SUBSTRATE for all four.  Slices 4/5/6
compose on top; this module owns the graph construction + traversal
+ clustering primitives.

OPERATOR'S DESIGN CONSTRAINTS APPLIED:

  * **Asynchronous** — ``build_dag`` is synchronous (read-time
    construction from append-only JSONL), but DAG is immutable after
    construction and safe for cross-task sharing without locks.
  * **Dynamic** — every threshold (max_records, max_depth, drift
    node delta) lives in env-tunable knobs with defensive clamping.
  * **Adaptive** — master flag ``JARVIS_CAUSALITY_DAG_QUERY_ENABLED``
    (default false) gates all I/O; when off, ``build_dag()`` returns
    an empty DAG instantly.  Hot-revert path: ``export
    JARVIS_CAUSALITY_DAG_QUERY_ENABLED=false``.
  * **Intelligent** — ``cluster_kind()`` implements heuristic
    clustering for confidence-collapse and repeated-failure patterns.
  * **Robust** — every public method NEVER raises.  Corrupt ledger
    rows are skipped defensively.  Cycle detection returns empty
    topological order + logs warning (DAG invariant violation).
  * **No hardcoding** — every bound is FlagRegistry-typed env knob.
  * **Leverages existing** — imports ``DecisionRecord`` schema +
    ``_ledger_dir`` from ``decision_runtime.py``.  Reads the same
    ``decisions.jsonl`` that Slice 1.2 writes.  ZERO duplication.

AUTHORITY INVARIANTS (AST-pinned in tests):

  * Imports ONLY: stdlib + ``determinism.decision_runtime``
    (``DecisionRecord``, ``SCHEMA_VERSION``, ``_ledger_dir``).
  * NO imports of: orchestrator, phase_runners, candidate_generator,
    iron_gate, change_engine, policy, semantic_guardian, providers,
    urgency_router.
  * NEVER raises from any public method.
  * Read-only over the ledger — never modifies.
  * Bounded by max_records + max_depth — no unbounded traversal.

COST CONTRACT PRESERVATION (§26.6):

  * DAG construction is read-only.  No DAG node carries an
    escalation directive; only diagnostic data.
  * DAG queries cannot mutate ``ctx.route`` — advisory only.
  * No provider-module imports in this module.
"""
from __future__ import annotations

import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import (
    Any, Dict, FrozenSet, List, Mapping, Optional, Sequence,
    Tuple,
)

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    SCHEMA_VERSION,
    DecisionRecord,
    _ledger_dir,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def dag_query_enabled() -> bool:
    """``JARVIS_CAUSALITY_DAG_QUERY_ENABLED`` (default ``true`` —
    graduated in Priority 2 Slice 6).

    Master flag governing whether ``build_dag()`` performs I/O to
    read the ledger.  When off (hot-revert), ``build_dag()`` returns
    an empty ``CausalityDAG`` immediately — no file open, no parsing.

    Hot-revert path: ``export JARVIS_CAUSALITY_DAG_QUERY_ENABLED=false``.

    Asymmetric env semantics — empty/whitespace = unset = graduated
    default-true; explicit truthy enables; explicit falsy hot-reverts."""
    raw = os.environ.get(
        "JARVIS_CAUSALITY_DAG_QUERY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 6 — was false in Slice 3)
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# Env-tunable knobs with defensive bounds
# ---------------------------------------------------------------------------


def _read_int_knob(
    name: str, default: int, lo: int, hi: int,
) -> int:
    """Read an integer env knob, clamp to [lo, hi]. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        val = int(raw)
        return max(lo, min(hi, val))
    except (ValueError, TypeError):
        return default


def _read_float_knob(
    name: str, default: float, lo: float, hi: float,
) -> float:
    """Read a float env knob, clamp to [lo, hi]. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return default
        val = float(raw)
        return max(lo, min(hi, val))
    except (ValueError, TypeError):
        return default


def max_records_knob() -> int:
    """``JARVIS_DAG_MAX_RECORDS`` — max records ``build_dag()`` will
    ingest.  Default 100_000; clamped to [1, 1_000_000]."""
    return _read_int_knob(
        "JARVIS_DAG_MAX_RECORDS", 100_000, 1, 1_000_000,
    )


def max_depth_knob() -> int:
    """``JARVIS_DAG_MAX_DEPTH`` — max traversal depth for
    ``subgraph()``.  Default 8; clamped to [1, 64]."""
    return _read_int_knob("JARVIS_DAG_MAX_DEPTH", 8, 1, 64)


def drift_threshold_knob() -> float:
    """``JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD`` — minimum node-set
    delta ratio to classify as drift.  Default 0.30; clamped to
    [0.01, 1.0]."""
    return _read_float_knob(
        "JARVIS_DAG_DRIFT_NODE_DELTA_THRESHOLD", 0.30, 0.01, 1.0,
    )


# ---------------------------------------------------------------------------
# CausalityDAG — frozen graph container
# ---------------------------------------------------------------------------


class CausalityDAG:
    """Immutable navigable DAG over ``DecisionRecord`` nodes.

    Constructed via ``build_dag()``.  All public methods NEVER raise —
    invalid record_id lookups return ``None`` or empty tuples.

    The graph is:
      * ``_nodes`` — record_id → DecisionRecord
      * ``_edges`` — record_id → parent_record_ids (upstream links)
      * ``_reverse`` — record_id → child_record_ids (downstream links)

    Frozen after construction.  Thread-safe for concurrent reads
    without locks."""

    __slots__ = ("_nodes", "_edges", "_reverse", "_frozen")

    def __init__(
        self,
        nodes: Optional[Dict[str, DecisionRecord]] = None,
        edges: Optional[Dict[str, Tuple[str, ...]]] = None,
    ) -> None:
        self._nodes: Dict[str, DecisionRecord] = dict(nodes or {})
        self._edges: Dict[str, Tuple[str, ...]] = dict(edges or {})
        # Build reverse-edge index for O(1) child lookups
        reverse: Dict[str, List[str]] = {}
        for rid, parents in self._edges.items():
            for pid in parents:
                reverse.setdefault(pid, []).append(rid)
        self._reverse: Dict[str, Tuple[str, ...]] = {
            k: tuple(v) for k, v in reverse.items()
        }
        self._frozen = True

    # -- properties --------------------------------------------------------

    @property
    def node_count(self) -> int:
        """Total number of nodes in the DAG."""
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        """Total number of edges (sum of all parent links)."""
        return sum(len(p) for p in self._edges.values())

    @property
    def record_ids(self) -> Tuple[str, ...]:
        """All record_ids in insertion order."""
        return tuple(self._nodes.keys())

    @property
    def is_empty(self) -> bool:
        return len(self._nodes) == 0

    # -- O(1) lookups ------------------------------------------------------

    def node(self, record_id: str) -> Optional[DecisionRecord]:
        """Return the DecisionRecord for ``record_id``, or None.
        NEVER raises."""
        try:
            return self._nodes.get(str(record_id))
        except Exception:  # noqa: BLE001 — defensive
            return None

    def parents(self, record_id: str) -> Tuple[str, ...]:
        """Return parent_record_ids for ``record_id``.  Empty tuple
        if not found or leaf node.  NEVER raises."""
        try:
            return self._edges.get(str(record_id), ())
        except Exception:  # noqa: BLE001 — defensive
            return ()

    def children(self, record_id: str) -> Tuple[str, ...]:
        """Return child record_ids (records whose parent_record_ids
        contain this record_id).  O(1) via reverse-edge index.
        NEVER raises."""
        try:
            return self._reverse.get(str(record_id), ())
        except Exception:  # noqa: BLE001 — defensive
            return ()

    # -- bounded traversal -------------------------------------------------

    def subgraph(
        self,
        record_id: str,
        max_depth: Optional[int] = None,
    ) -> "CausalityDAG":
        """Bounded BFS upstream + downstream from ``record_id``.

        ``max_depth`` defaults to ``JARVIS_DAG_MAX_DEPTH`` env knob.
        Returns a new CausalityDAG containing only the reachable
        subgraph.  NEVER raises — returns empty DAG on invalid input.

        max_depth=0 returns only the target node itself."""
        try:
            rid = str(record_id)
            if rid not in self._nodes:
                return CausalityDAG()

            depth = max_depth if max_depth is not None else max_depth_knob()
            depth = max(0, depth)

            visited: Dict[str, DecisionRecord] = {}
            sub_edges: Dict[str, Tuple[str, ...]] = {}

            # BFS — collect upstream (parents) + downstream (children)
            queue: deque[Tuple[str, int]] = deque()
            queue.append((rid, 0))
            seen: set = {rid}

            while queue:
                current, d = queue.popleft()
                rec = self._nodes.get(current)
                if rec is not None:
                    visited[current] = rec
                    # Preserve only edges to nodes we'll visit
                    parents = self._edges.get(current, ())
                    sub_edges[current] = tuple(
                        p for p in parents if p in self._nodes
                    )

                if d < depth:
                    # Upstream
                    for pid in self._edges.get(current, ()):
                        if pid not in seen and pid in self._nodes:
                            seen.add(pid)
                            queue.append((pid, d + 1))
                    # Downstream
                    for cid in self._reverse.get(current, ()):
                        if cid not in seen and cid in self._nodes:
                            seen.add(cid)
                            queue.append((cid, d + 1))

            return CausalityDAG(nodes=visited, edges=sub_edges)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[causality_dag] subgraph failed for %s",
                record_id, exc_info=True,
            )
            return CausalityDAG()

    # -- counterfactual branches -------------------------------------------

    def counterfactual_branches(
        self, record_id: str,
    ) -> Tuple[str, ...]:
        """Return child records where ``counterfactual_of == record_id``.
        NEVER raises."""
        try:
            rid = str(record_id)
            result: List[str] = []
            for cid in self._reverse.get(rid, ()):
                rec = self._nodes.get(cid)
                if rec is not None and rec.counterfactual_of == rid:
                    result.append(cid)
            return tuple(result)
        except Exception:  # noqa: BLE001 — defensive
            return ()

    # -- topological order -------------------------------------------------

    def topological_order(self) -> Tuple[str, ...]:
        """Return record_ids in topological order (Kahn's algorithm).

        If a cycle is detected (DAG invariant violation — indicates
        ledger corruption), returns an EMPTY tuple and logs a warning.
        NEVER raises."""
        try:
            if not self._nodes:
                return ()

            # In-degree computation
            in_degree: Dict[str, int] = {
                rid: 0 for rid in self._nodes
            }
            for rid, parents in self._edges.items():
                if rid in in_degree:
                    for pid in parents:
                        if pid in in_degree:
                            in_degree[rid] = in_degree.get(rid, 0) + 1

            # Kahn's BFS
            queue: deque[str] = deque()
            for rid, deg in in_degree.items():
                if deg == 0:
                    queue.append(rid)

            order: List[str] = []
            while queue:
                rid = queue.popleft()
                order.append(rid)
                for cid in self._reverse.get(rid, ()):
                    if cid in in_degree:
                        in_degree[cid] -= 1
                        if in_degree[cid] == 0:
                            queue.append(cid)

            if len(order) != len(self._nodes):
                logger.warning(
                    "[causality_dag] cycle detected in DAG — "
                    "topological order incomplete (%d/%d nodes). "
                    "This indicates ledger corruption.",
                    len(order), len(self._nodes),
                )
                return ()

            return tuple(order)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[causality_dag] topological_order failed",
                exc_info=True,
            )
            return ()

    # -- heuristic clustering ----------------------------------------------

    def cluster_kind(
        self,
        records: Sequence[DecisionRecord],
    ) -> str:
        """Heuristic clustering of a set of records.

        Current patterns detected:

        * ``"confidence_collapse_cluster"`` — ≥3 records whose ``kind``
          contains ``"confidence_drop"`` share a recent ancestor
          (within depth 4).
        * ``"repeated_failure_cluster"`` — ≥3 records with
          ``kind`` containing ``"fail"`` share a common op_id.
        * ``"counterfactual_fork_cluster"`` — ≥2 records are
          counterfactual branches of the same parent.
        * ``"unknown"`` — no recognized pattern.

        Advisory only — does not mutate anything.  NEVER raises."""
        try:
            if not records or len(records) < 2:
                return "unknown"

            # Pattern 1: confidence_collapse_cluster
            confidence_drops = [
                r for r in records
                if "confidence_drop" in (r.kind or "")
            ]
            if len(confidence_drops) >= 3:
                # Check if they share a recent ancestor
                if self._share_ancestor(confidence_drops, depth=4):
                    return "confidence_collapse_cluster"

            # Pattern 2: repeated_failure_cluster
            failures = [
                r for r in records
                if "fail" in (r.kind or "").lower()
            ]
            if len(failures) >= 3:
                op_ids = {r.op_id for r in failures}
                if len(op_ids) == 1:
                    return "repeated_failure_cluster"

            # Pattern 3: counterfactual_fork_cluster
            cf_parents = {
                r.counterfactual_of for r in records
                if r.counterfactual_of
            }
            if cf_parents:
                for parent_id in cf_parents:
                    forks = [
                        r for r in records
                        if r.counterfactual_of == parent_id
                    ]
                    if len(forks) >= 2:
                        return "counterfactual_fork_cluster"

            return "unknown"
        except Exception:  # noqa: BLE001 — defensive
            return "unknown"

    def _share_ancestor(
        self,
        records: Sequence[DecisionRecord],
        depth: int,
    ) -> bool:
        """Check if a set of records share a common ancestor within
        ``depth`` hops upstream.  NEVER raises."""
        try:
            if len(records) < 2:
                return False

            def _ancestors(rid: str, max_d: int) -> FrozenSet[str]:
                visited: set = set()
                queue: deque[Tuple[str, int]] = deque([(rid, 0)])
                while queue:
                    current, d = queue.popleft()
                    if current in visited:
                        continue
                    visited.add(current)
                    if d < max_d:
                        for pid in self._edges.get(current, ()):
                            if pid not in visited:
                                queue.append((pid, d + 1))
                return frozenset(visited)

            ancestor_sets = [
                _ancestors(r.record_id, depth)
                for r in records
            ]

            # Check pairwise intersection
            common = ancestor_sets[0]
            for s in ancestor_sets[1:]:
                common = common & s
                if not common:
                    return False

            return len(common) > 0
        except Exception:  # noqa: BLE001 — defensive
            return False

    # -- repr --------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"CausalityDAG(nodes={self.node_count}, "
            f"edges={self.edge_count})"
        )


# ---------------------------------------------------------------------------
# DAG builder — reads the JSONL ledger
# ---------------------------------------------------------------------------


def _resolve_ledger_path(session_id: str) -> Path:
    """Resolve the per-session decisions.jsonl path.  Mirrors
    ``DecisionRuntime._resolved_path()`` conventions.  NEVER raises."""
    try:
        base = _ledger_dir()
        sid = str(session_id).strip() or "default"
        return base / sid / "decisions.jsonl"
    except Exception:  # noqa: BLE001 — defensive
        return Path(".jarvis/determinism/default/decisions.jsonl")


def build_dag(
    session_id: Optional[str] = None,
    *,
    max_records: Optional[int] = None,
    ledger_path: Optional[Path] = None,
) -> CausalityDAG:
    """Build a CausalityDAG from the per-session JSONL ledger.

    Parameters
    ----------
    session_id : str, optional
        Session to load.  ``None`` → reads from
        ``OUROBOROS_BATTLE_SESSION_ID`` env, falls back to
        ``"default"``.
    max_records : int, optional
        Override for ``JARVIS_DAG_MAX_RECORDS``.  Clamped to
        [1, 1_000_000].
    ledger_path : Path, optional
        Direct path override (for testing).  When set, bypasses
        session_id resolution.

    Returns
    -------
    CausalityDAG
        Populated DAG, or empty DAG on master-off / missing ledger /
        error.

    NEVER raises.  Master-flag-gated: when
    ``JARVIS_CAUSALITY_DAG_QUERY_ENABLED=false``, returns empty DAG
    immediately with zero I/O."""
    try:
        if not dag_query_enabled():
            return CausalityDAG()

        # Resolve session_id
        sid = session_id
        if sid is None or not str(sid).strip():
            sid = os.environ.get(
                "OUROBOROS_BATTLE_SESSION_ID", "",
            ).strip() or "default"
        sid = str(sid).strip()

        # Resolve path
        path = ledger_path or _resolve_ledger_path(sid)

        if not path.exists():
            logger.debug(
                "[causality_dag] ledger not found at %s — "
                "returning empty DAG",
                path,
            )
            return CausalityDAG()

        # Resolve max_records
        cap = max_records if max_records is not None else max_records_knob()
        cap = max(1, min(1_000_000, cap))

        nodes: Dict[str, DecisionRecord] = {}
        edges: Dict[str, Tuple[str, ...]] = {}
        count = 0

        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                if count >= cap:
                    break
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, Mapping):
                    continue
                rec = DecisionRecord.from_dict(payload)
                if rec is None:
                    continue
                nodes[rec.record_id] = rec
                edges[rec.record_id] = rec.parent_record_ids
                count += 1

        return CausalityDAG(nodes=nodes, edges=edges)
    except Exception:  # noqa: BLE001 — defensive
        logger.debug(
            "[causality_dag] build_dag failed — returning empty DAG",
            exc_info=True,
        )
        return CausalityDAG()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "CausalityDAG",
    "build_dag",
    "dag_query_enabled",
    "drift_threshold_knob",
    "max_depth_knob",
    "max_records_knob",
]
