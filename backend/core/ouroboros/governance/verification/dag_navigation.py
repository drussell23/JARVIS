"""Priority 2 Slice 4 — Causality DAG navigation surface.

Rendering + SSE-publishing helpers consumed by the three navigation
surfaces (REPL, IDE GET, SSE).  This module owns:

  * ``render_dag_for_record`` — ASCII tree renderer (bounded depth)
  * ``render_dag_drift`` — node-set delta + structural distance
  * ``render_dag_stats`` — DAG aggregate summary
  * ``render_dag_counterfactuals`` — counterfactual branch listing
  * ``publish_dag_fork_event`` — SSE event publisher

Master flag: ``JARVIS_DAG_NAVIGATION_ENABLED`` (default false).
Three independent sub-flags for selective enablement:
  * ``JARVIS_DAG_NAVIGATION_REPL_ENABLED``
  * ``JARVIS_DAG_NAVIGATION_GET_ENABLED``
  * ``JARVIS_DAG_NAVIGATION_SSE_ENABLED``

Authority invariants (AST-pinned):
  * Imports ONLY: stdlib + ``verification.causality_dag`` +
    ``determinism.decision_runtime`` (DecisionRecord).
  * NO imports of: orchestrator, phase_runners, candidate_generator,
    iron_gate, change_engine, policy, semantic_guardian, providers,
    urgency_router.
  * NEVER raises from any public method.
  * Read-only — never mutates ctx or ledger.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.determinism.decision_runtime import (
    DecisionRecord,
)
from backend.core.ouroboros.governance.verification.causality_dag import (
    CausalityDAG,
    build_dag,
    dag_query_enabled,
    drift_threshold_knob,
)

logger = logging.getLogger(__name__)

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MAX_RENDER_BYTES = 16 * 1024

# ---------------------------------------------------------------------------
# Master flag + sub-flags
# ---------------------------------------------------------------------------


def dag_navigation_enabled() -> bool:
    """``JARVIS_DAG_NAVIGATION_ENABLED`` (default ``true`` —
    graduated in Priority 2 Slice 6).

    Master flag governing whether the DAG navigation surfaces (REPL
    `/postmortems dag` subcommands, IDE GET endpoints, SSE
    dag_fork_detected event) are active. Three independent
    sub-flags govern each surface; all default to "on when master
    is on". Hot-revert: explicit false."""
    raw = os.environ.get(
        "JARVIS_DAG_NAVIGATION_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default (Slice 6 — was false in Slice 4)
    return raw in _TRUTHY


def _repl_enabled() -> bool:
    if not dag_navigation_enabled():
        return False
    raw = os.environ.get(
        "JARVIS_DAG_NAVIGATION_REPL_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # on when master is on
    return raw in _TRUTHY


def _get_enabled() -> bool:
    if not dag_navigation_enabled():
        return False
    raw = os.environ.get(
        "JARVIS_DAG_NAVIGATION_GET_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in _TRUTHY


def _sse_enabled() -> bool:
    if not dag_navigation_enabled():
        return False
    raw = os.environ.get(
        "JARVIS_DAG_NAVIGATION_SSE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True
    return raw in _TRUTHY


# ---------------------------------------------------------------------------
# ASCII tree renderer
# ---------------------------------------------------------------------------


def render_dag_for_record(
    dag: CausalityDAG,
    record_id: str,
    depth: int = 4,
) -> str:
    """Render an ASCII tree centered on ``record_id``.

    Bounded by ``depth`` upstream + downstream.  NEVER raises."""
    try:
        sub = dag.subgraph(record_id, max_depth=depth)
        if sub.is_empty:
            return f"[dag] record {record_id!r} not found"

        lines: List[str] = [f"[dag] tree for {record_id} (depth={depth})"]
        topo = sub.topological_order()
        if not topo:
            topo = sub.record_ids

        for rid in topo:
            rec = sub.node(rid)
            if rec is None:
                continue
            parents = sub.parents(rid)
            marker = ">> " if rid == record_id else "   "
            cf = " [CF]" if rec.counterfactual_of else ""
            parent_str = f" <- {', '.join(parents)}" if parents else ""
            lines.append(
                f"{marker}{rid} ({rec.phase}/{rec.kind}){cf}{parent_str}"
            )

        return _clip("\n".join(lines))
    except Exception:  # noqa: BLE001
        return f"[dag] render error for {record_id}"


# ---------------------------------------------------------------------------
# Drift renderer
# ---------------------------------------------------------------------------


def render_dag_drift(
    dag_a: CausalityDAG,
    dag_b: CausalityDAG,
    label_a: str = "session-a",
    label_b: str = "session-b",
) -> str:
    """Render a node-set delta between two DAGs.  NEVER raises."""
    try:
        ids_a = set(dag_a.record_ids)
        ids_b = set(dag_b.record_ids)
        only_a = ids_a - ids_b
        only_b = ids_b - ids_a
        common = ids_a & ids_b
        total = len(ids_a | ids_b)
        delta = len(only_a) + len(only_b)
        ratio = delta / max(1, total)
        threshold = drift_threshold_knob()
        drifted = ratio >= threshold

        lines = [
            f"[dag drift] {label_a} vs {label_b}",
            f"  {label_a}: {len(ids_a)} nodes",
            f"  {label_b}: {len(ids_b)} nodes",
            f"  common: {len(common)}",
            f"  only in {label_a}: {len(only_a)}",
            f"  only in {label_b}: {len(only_b)}",
            f"  delta ratio: {ratio:.2f} (threshold: {threshold:.2f})",
            f"  drift detected: {drifted}",
        ]
        if only_a and len(only_a) <= 10:
            lines.append(f"  {label_a}-only: {', '.join(sorted(only_a))}")
        if only_b and len(only_b) <= 10:
            lines.append(f"  {label_b}-only: {', '.join(sorted(only_b))}")

        return _clip("\n".join(lines))
    except Exception:  # noqa: BLE001
        return "[dag drift] render error"


# ---------------------------------------------------------------------------
# Stats renderer
# ---------------------------------------------------------------------------


def render_dag_stats(dag: CausalityDAG) -> str:
    """Render DAG aggregate stats.  NEVER raises."""
    try:
        topo = dag.topological_order()
        cf_count = sum(
            1 for rid in dag.record_ids
            if (dag.node(rid) or DecisionRecord(
                record_id="", session_id="", op_id="", phase="",
                kind="", ordinal=0, inputs_hash="", output_repr="",
                monotonic_ts=0, wall_ts=0,
            )).counterfactual_of is not None
        )
        lines = [
            "[dag stats]",
            f"  nodes: {dag.node_count}",
            f"  edges: {dag.edge_count}",
            f"  counterfactual forks: {cf_count}",
            f"  topological order valid: {len(topo) == dag.node_count}",
        ]
        return _clip("\n".join(lines))
    except Exception:  # noqa: BLE001
        return "[dag stats] render error"


# ---------------------------------------------------------------------------
# Counterfactual renderer
# ---------------------------------------------------------------------------


def render_dag_counterfactuals(
    dag: CausalityDAG,
    record_id: str,
) -> str:
    """Render counterfactual branches for a record.  NEVER raises."""
    try:
        branches = dag.counterfactual_branches(record_id)
        if not branches:
            return f"[dag] no counterfactual branches for {record_id}"

        lines = [
            f"[dag] counterfactual branches for {record_id} "
            f"({len(branches)} found):"
        ]
        for bid in branches:
            rec = dag.node(bid)
            if rec is None:
                lines.append(f"  {bid} (record missing)")
            else:
                lines.append(f"  {bid} ({rec.phase}/{rec.kind})")

        return _clip("\n".join(lines))
    except Exception:  # noqa: BLE001
        return f"[dag] counterfactual render error for {record_id}"


# ---------------------------------------------------------------------------
# SSE publisher
# ---------------------------------------------------------------------------


EVENT_TYPE_DAG_FORK_DETECTED = "dag_fork_detected"


def publish_dag_fork_event(
    *,
    record_id: str,
    counterfactual_id: str,
    session_id: str,
) -> Optional[str]:
    """Publish a ``dag_fork_detected`` SSE event.

    Best-effort — NEVER raises.  Returns event-id on success, None
    on failure / disabled."""
    if not _sse_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_DAG_FORK_DETECTED,
            op_id=record_id,
            payload={
                "record_id": str(record_id),
                "counterfactual_id": str(counterfactual_id),
                "session_id": str(session_id),
                "wall_ts": time.time(),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[dag_navigation] publish_dag_fork_event failed",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# REPL dispatcher (dag family)
# ---------------------------------------------------------------------------


def dispatch_dag_command(
    argv: List[str],
    *,
    session_id: Optional[str] = None,
) -> str:
    """Dispatch a ``/postmortems dag ...`` subcommand.

    Subcommands:
      * ``for-record <id>``
      * ``fork-counterfactuals <id>``
      * ``drift <session-a> <session-b>``
      * ``stats``

    NEVER raises — returns a rendered string."""
    try:
        if not _repl_enabled():
            return "[dag] disabled — set JARVIS_DAG_NAVIGATION_ENABLED=true"

        if not dag_query_enabled():
            return "[dag] DAG query disabled — set JARVIS_CAUSALITY_DAG_QUERY_ENABLED=true"

        if not argv:
            return _dag_help()

        sub = str(argv[0]).strip().lower()

        if sub == "for-record":
            if len(argv) < 2:
                return "[dag] usage: dag for-record <record-id>"
            rid = str(argv[1])
            dag = build_dag(session_id)
            return render_dag_for_record(dag, rid)

        if sub == "fork-counterfactuals":
            if len(argv) < 2:
                return "[dag] usage: dag fork-counterfactuals <record-id>"
            rid = str(argv[1])
            dag = build_dag(session_id)
            return render_dag_counterfactuals(dag, rid)

        if sub == "drift":
            if len(argv) < 3:
                return "[dag] usage: dag drift <session-a> <session-b>"
            dag_a = build_dag(str(argv[1]))
            dag_b = build_dag(str(argv[2]))
            return render_dag_drift(
                dag_a, dag_b,
                label_a=str(argv[1]),
                label_b=str(argv[2]),
            )

        if sub == "stats":
            dag = build_dag(session_id)
            return render_dag_stats(dag)

        return _dag_help()
    except Exception:  # noqa: BLE001
        return "[dag] dispatch error"


def _dag_help() -> str:
    return "\n".join([
        "[dag] subcommands:",
        "  dag for-record <record-id>          render causal tree",
        "  dag fork-counterfactuals <record-id> list what-if branches",
        "  dag drift <session-a> <session-b>   pairwise graph diff",
        "  dag stats                           DAG aggregates",
    ])


# ---------------------------------------------------------------------------
# GET endpoint handler
# ---------------------------------------------------------------------------


def handle_dag_session(session_id: str) -> Dict[str, Any]:
    """Build DAG for session, return summary dict.  NEVER raises."""
    try:
        if not _get_enabled():
            return {"error": True, "reason_code": "dag_navigation.disabled"}
        if not dag_query_enabled():
            return {"error": True, "reason_code": "dag_query.disabled"}
        dag = build_dag(session_id)
        return {
            "session_id": session_id,
            "node_count": dag.node_count,
            "edge_count": dag.edge_count,
            "record_ids": list(dag.record_ids[:1000]),
        }
    except Exception:  # noqa: BLE001
        return {"error": True, "reason_code": "dag_navigation.error"}


def handle_dag_record(record_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
    """Build DAG, extract subgraph for record.  NEVER raises."""
    try:
        if not _get_enabled():
            return {"error": True, "reason_code": "dag_navigation.disabled"}
        if not dag_query_enabled():
            return {"error": True, "reason_code": "dag_query.disabled"}
        dag = build_dag(session_id)
        rec = dag.node(record_id)
        if rec is None:
            return {"error": True, "reason_code": "dag_navigation.not_found"}
        sub = dag.subgraph(record_id, max_depth=4)
        return {
            "record_id": record_id,
            "record": rec.to_dict(),
            "parents": list(dag.parents(record_id)),
            "children": list(dag.children(record_id)),
            "counterfactual_branches": list(
                dag.counterfactual_branches(record_id),
            ),
            "subgraph_node_count": sub.node_count,
        }
    except Exception:  # noqa: BLE001
        return {"error": True, "reason_code": "dag_navigation.error"}


def handle_dag_diff(
    record_id_a: str, record_id_b: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Q2 Slice 6 — compute deterministic state-diff between two
    records in the same DAG.

    Returns the ``RecordDiff.to_dict()`` shape with:
      ``outcome``, ``record_id_a``, ``record_id_b``, ``changes``,
      ``fields_total``, ``fields_changed``, ``detail``.

    Errors mirror handle_dag_record's vocabulary:
      ``dag_navigation.disabled`` / ``dag_query.disabled`` →
      surface-master gates, ``dag_navigation.not_found`` →
      either record id absent in the DAG, ``dag_navigation.error``
      → defensive sentinel.

    Both records MUST belong to the same session DAG (caller
    supplies ``session_id`` once; both ids resolved against the
    same ``CausalityDAG`` instance). Cross-session diffing is
    intentionally NOT supported — semantically meaningless without
    a shared causality root.

    NEVER raises."""
    try:
        if not _get_enabled():
            return {"error": True, "reason_code": "dag_navigation.disabled"}
        if not dag_query_enabled():
            return {"error": True, "reason_code": "dag_query.disabled"}
        dag = build_dag(session_id)
        rec_a = dag.node(record_id_a)
        if rec_a is None:
            return {
                "error": True,
                "reason_code": "dag_navigation.not_found",
                "missing": record_id_a,
            }
        rec_b = dag.node(record_id_b)
        if rec_b is None:
            return {
                "error": True,
                "reason_code": "dag_navigation.not_found",
                "missing": record_id_b,
            }
        # Lazy substrate import — keeps this module's import
        # graph clean (dag_record_diff lives one tier above).
        from backend.core.ouroboros.governance.verification.dag_record_diff import (
            compute_record_diff,
        )
        result = compute_record_diff(
            record_a=rec_a.to_dict(),
            record_b=rec_b.to_dict(),
            record_id_a=record_id_a,
            record_id_b=record_id_b,
        )
        return result.to_dict()
    except Exception:  # noqa: BLE001
        return {"error": True, "reason_code": "dag_navigation.error"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clip(text: str) -> str:
    try:
        encoded = text.encode("ascii", errors="replace")
    except Exception:  # noqa: BLE001
        encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > _MAX_RENDER_BYTES:
        encoded = encoded[:_MAX_RENDER_BYTES] + b"\n... (clipped)"
    return encoded.decode("ascii", errors="replace")


__all__ = [
    "EVENT_TYPE_DAG_FORK_DETECTED",
    "dag_navigation_enabled",
    "dispatch_dag_command",
    "handle_dag_diff",
    "handle_dag_record",
    "handle_dag_session",
    "publish_dag_fork_event",
    "render_dag_counterfactuals",
    "render_dag_drift",
    "render_dag_for_record",
    "render_dag_stats",
]
