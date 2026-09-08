"""Inheritance of the parent op's ADMITTED state by its L3 work units.

An execution-graph work unit runs its own GENERATE/VALIDATE against its own
``OperationContext``. That context was built bare
(``OperationContext.create(target_files, description, op_id, primary_repo,
repo_scope)``) — so for every unit the parent op's admission was thrown away:

* ``ctx.telemetry is None`` -> ``providers._ctx_schema_capability`` reads
  ``full_content_only``, so ``single_file_diff_requested`` is False and the
  2b.1-diff schema is STRUCTURALLY UNREACHABLE on the subagent path. This is
  the same defect ``submit_background`` had at the op level (89a9166e05); the
  L3 path was the half nobody had closed. A unit's ``[Schema]`` line reading
  ``capability=? brain=- served=-`` is this bug, not a routing fault.
* ``ctx.target_symbols == ()`` and no intake evidence -> the declared-symbol
  contract is VACUOUS for every unit. Both consumers on this path
  (``_validate_in_tree``'s differential ``acceptance_names``, and
  ``candidate_generator._declared_symbols_for`` via the generator) read a
  field that could only ever be empty, so a unit that hallucinated a no-op
  could not be refused — the very forgery class 3a9358f23f closed for
  top-level ops.

The parent's state has to reach the executor THROUGH THE GRAPH: the executor
interface is ``execute(graph, unit)`` and nothing else, and graphs are
persisted (``ExecutionGraphStore``) and replayed after a restart. So it is
stamped on :class:`ExecutionGraph` at construction, JSON-native, and survives
recovery. A graph built where no parent context is in scope keeps the empty
defaults and behaves exactly as it did before.

**The goal pointer is a POINTER, never a payload.** ``goal_id`` is carried so
``_declared_symbols_for`` can re-read the symbols from the SIGNED roadmap on
the other side, exactly as it does for a top-level op. The symbol tuple is
carried too, but only as the parent's own already-verified
``ctx.target_symbols`` — never as something a model wrote.
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("Ouroboros.ParentInheritance")

__all__ = [
    "goal_pointer_for",
    "inherit_into",
    "inherited_create_kwargs",
    "stamp_parent_context",
    "telemetry_from_json",
    "telemetry_to_json",
]


# ---------------------------------------------------------------------------
# TelemetryContext <-> JSON
#
# Both halves (HostTelemetry, RoutingIntentTelemetry) are flat frozen
# dataclasses of JSON-native scalars, so ``dataclasses.asdict`` round-trips
# them exactly. ``routing_actual`` is deliberately NOT carried: it is the
# parent's own post-execution outcome and says nothing about a unit.
# ---------------------------------------------------------------------------


def telemetry_to_json(telemetry: Any) -> str:
    """Serialize a ``TelemetryContext`` to JSON. ``""`` when absent/unusable.

    NEVER raises — telemetry that cannot be serialized simply is not
    inherited, and the unit degrades to the pre-inheritance behaviour.
    """
    if telemetry is None:
        return ""
    try:
        local = getattr(telemetry, "local_node", None)
        intent = getattr(telemetry, "routing_intent", None)
        if local is None or intent is None:
            return ""
        return json.dumps(
            {
                "local_node": dataclasses.asdict(local),
                "routing_intent": dataclasses.asdict(intent),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    except Exception:  # noqa: BLE001
        logger.debug("[ParentInheritance] telemetry_to_json degraded", exc_info=True)
        return ""


def telemetry_from_json(raw: str) -> Optional[Any]:
    """Rebuild a ``TelemetryContext`` from :func:`telemetry_to_json` output.

    Unknown keys are dropped rather than raising, so a graph persisted by an
    older build (or a newer one that grew a field) still yields the fields
    both sides share. Returns ``None`` when nothing usable is present.
    """
    if not raw:
        return None
    try:
        from backend.core.ouroboros.governance.op_context import (  # noqa: PLC0415
            HostTelemetry,
            RoutingIntentTelemetry,
            TelemetryContext,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        local = _construct(HostTelemetry, data.get("local_node"))
        intent = _construct(RoutingIntentTelemetry, data.get("routing_intent"))
        if local is None or intent is None:
            return None
        return TelemetryContext(local_node=local, routing_intent=intent)
    except Exception:  # noqa: BLE001
        logger.debug("[ParentInheritance] telemetry_from_json degraded", exc_info=True)
        return None


def _construct(cls: Any, payload: Any) -> Optional[Any]:
    """Build *cls* from *payload*, keeping only fields *cls* declares."""
    if not isinstance(payload, dict):
        return None
    names = {f.name for f in dataclasses.fields(cls)}
    return cls(**{k: v for k, v in payload.items() if k in names})


# ---------------------------------------------------------------------------
# Goal pointer
# ---------------------------------------------------------------------------


def goal_pointer_for(ctx: Any) -> str:
    """The signed goal's ``goal_id`` off *ctx*'s intake evidence, or ``""``.

    Reads the same two places ``_declared_symbols_for`` reads, in the same
    order (a delegated-provenance claim first, then the roadmap intake's own
    pointer) so the two can never disagree about which goal an op belongs to.
    """
    try:
        evidence = getattr(ctx, "intake_evidence", None)
        if not isinstance(evidence, dict) or not evidence:
            evidence = getattr(ctx, "evidence", None)
        if not isinstance(evidence, dict):
            return ""
        claim = evidence.get("provenance")
        if isinstance(claim, dict):
            gid = str(claim.get("goal_id", "") or "").strip()
            if gid:
                return gid
        return str(evidence.get("goal_id", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# Stamp (parent side) / apply (unit side)
# ---------------------------------------------------------------------------


def stamp_parent_context(graph: Any, ctx: Any) -> Any:
    """Return *graph* carrying the parent op's admitted state.

    Called wherever a graph is built with the parent ``OperationContext`` in
    scope. NEVER raises and never changes the plan: ``plan_digest`` enumerates
    its own fields, so the digest, the DAG and every unit are untouched.
    """
    if graph is None or ctx is None:
        return graph
    try:
        telemetry_json = telemetry_to_json(getattr(ctx, "telemetry", None))
        goal_id = goal_pointer_for(ctx)
        symbols = tuple(str(s) for s in (getattr(ctx, "target_symbols", ()) or ()))
        evidence_json = str(getattr(ctx, "intake_evidence_json", "") or "")
        if not (telemetry_json or goal_id or symbols or evidence_json):
            return graph
        stamped = dataclasses.replace(
            graph,
            goal_id=goal_id,
            target_symbols=symbols,
            parent_telemetry_json=telemetry_json,
            parent_intake_evidence_json=evidence_json,
        )
        logger.info(
            "[ParentInheritance] graph=%s op=%s inherits goal=%s symbols=%d telemetry=%s",
            getattr(graph, "graph_id", "?"), getattr(graph, "op_id", "?"),
            goal_id or "-", len(symbols), "yes" if telemetry_json else "no",
        )
        return stamped
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ParentInheritance] could not stamp graph=%s — units run without the "
            "parent's admission",
            getattr(graph, "graph_id", "?"), exc_info=True,
        )
        return graph


def inherit_into(subctx: Any, graph: Any) -> Any:
    """Return *subctx* carrying the telemetry *graph* inherited from its parent.

    Applied once, immediately after the unit's context is created. Telemetry
    goes on via ``with_telemetry`` so the hash chain advances exactly as it
    does for a top-level op; ``target_symbols`` and the evidence pointer reach
    ``create`` via :func:`inherited_create_kwargs`. NEVER raises — a unit that
    cannot inherit runs as it did before this existed, and says so.
    """
    if subctx is None or graph is None:
        return subctx
    raw = str(getattr(graph, "parent_telemetry_json", "") or "")
    if not raw:
        return subctx
    telemetry = telemetry_from_json(raw)
    if telemetry is None:
        logger.warning(
            "[ParentInheritance] graph=%s carried telemetry that would not decode — "
            "unit %s generates with capability=? (2b.1-diff unreachable)",
            getattr(graph, "graph_id", "?"), getattr(subctx, "op_id", "?"),
        )
        return subctx
    try:
        return subctx.with_telemetry(telemetry)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ParentInheritance] with_telemetry failed for unit %s",
            getattr(subctx, "op_id", "?"), exc_info=True,
        )
        return subctx


def inherited_create_kwargs(graph: Any) -> Dict[str, Any]:
    """``OperationContext.create`` kwargs a unit inherits from *graph*.

    Empty dict when the graph carries nothing (a graph built without a parent
    context in scope), which is exactly the pre-inheritance call.
    """
    out: Dict[str, Any] = {}
    try:
        symbols: Tuple[str, ...] = tuple(
            str(s) for s in (getattr(graph, "target_symbols", ()) or ())
        )
        if symbols:
            out["target_symbols"] = symbols
        evidence = str(getattr(graph, "parent_intake_evidence_json", "") or "")
        if evidence:
            out["intake_evidence_json"] = evidence
    except Exception:  # noqa: BLE001
        return {}
    return out
