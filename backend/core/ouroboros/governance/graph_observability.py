"""Path D.1 — observability surface for L3 execution-graph
progress.

Exposes:

  * ``GET /observability/execution-graph`` — list of active +
    recently retained graphs (digest projection, no event
    history)
  * ``GET /observability/execution-graph/{graph_id}`` — full
    detail for one graph (units, critical path, runtime)

Auto-mounted by :mod:`observability_route_registry` (§32.11
Slice 3) via canonical ``register_routes(app, *,
rate_limit_check, cors_headers)`` entry point — zero edits to
the boot path.

Read-only. Authority asymmetry is structural:

  * NEVER imports orchestrator / iron_gate / policy / providers
    / candidate_generator / urgency_router / change_engine /
    semantic_guardian (AST-pinned).
  * NEVER calls mutating tracker methods (``register_graph``,
    ``unsubscribe_all``, ``bind``).
  * Composes :func:`get_default_tracker` only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


GRAPH_OBSERVABILITY_SCHEMA_VERSION: str = (
    "graph_observability.1"
)


def _serialize_graph(gp) -> Dict[str, Any]:
    """Pure projection of a GraphProgress to a JSON-safe dict.
    Compact form — no event_history (which can grow large)."""
    try:
        phase = getattr(gp, "phase", None)
        return {
            "graph_id": getattr(gp, "graph_id", ""),
            "op_id": getattr(gp, "op_id", ""),
            "planner_id": getattr(gp, "planner_id", ""),
            "schema_version": getattr(gp, "schema_version", ""),
            "concurrency_limit": int(
                getattr(gp, "concurrency_limit", 0),
            ),
            "plan_digest": getattr(gp, "plan_digest", ""),
            "phase": (
                getattr(phase, "value", str(phase or ""))
            ),
            "completion_pct": float(gp.completion_pct()),
            "runtime_ms": float(gp.runtime_ms),
            "is_terminal": bool(gp.is_terminal),
            "unit_count": len(gp.units),
            "last_error": getattr(gp, "last_error", "")[:256],
        }
    except Exception:  # noqa: BLE001 — defensive
        return {
            "graph_id": getattr(gp, "graph_id", ""),
            "error": "projection_failed",
        }


def _serialize_graph_detail(gp) -> Dict[str, Any]:
    """Detail projection — adds units + critical path."""
    out = _serialize_graph(gp)
    units: List[Dict[str, Any]] = []
    try:
        for unit in gp.units.values():
            state = getattr(unit, "state", None)
            units.append({
                "unit_id": getattr(unit, "unit_id", ""),
                "state": (
                    getattr(state, "value", str(state or ""))
                ),
                "is_terminal": bool(
                    getattr(unit, "is_terminal", False),
                ),
            })
    except Exception:  # noqa: BLE001 — defensive
        units = []
    out["units"] = units
    try:
        out["critical_path"] = list(gp.critical_path())
    except Exception:  # noqa: BLE001 — defensive
        out["critical_path"] = []
    return out


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Auto-mount entry point. NEVER raises."""
    try:
        from aiohttp import web
    except ImportError:
        logger.debug(
            "[GraphObservability] aiohttp unavailable — skip"
        )
        return

    def _get_tracker():
        try:
            from backend.core.ouroboros.governance.autonomy.execution_graph_progress import (  # noqa: E501
                get_default_tracker,
            )
            return get_default_tracker()
        except Exception:  # noqa: BLE001 — defensive
            return None

    async def _handle_list(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            tracker = _get_tracker()
            if tracker is None:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                tracked = tracker.all_tracked()
                stats = tracker.stats()
            except Exception as exc:  # noqa: BLE001
                return web.json_response(
                    {
                        "error": "internal",
                        "detail": str(exc)[:128],
                    },
                    status=500,
                )
            payload = {
                "schema_version": (
                    GRAPH_OBSERVABILITY_SCHEMA_VERSION
                ),
                "stats": stats,
                "graphs": [
                    _serialize_graph(gp)
                    for gp in tracked[:200]
                ],
            }
            response = web.json_response(payload)
            if cors_headers is not None:
                try:
                    cors_headers(response)
                except Exception:  # noqa: BLE001 — defensive
                    pass
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[GraphObservability] list raised: %s", exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    async def _handle_detail(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            graph_id = request.match_info.get("graph_id", "")
            if not graph_id:
                return web.json_response(
                    {"error": "missing_graph_id"}, status=400,
                )
            tracker = _get_tracker()
            if tracker is None:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                gp = tracker.snapshot(graph_id)
            except Exception as exc:  # noqa: BLE001
                return web.json_response(
                    {
                        "error": "internal",
                        "detail": str(exc)[:128],
                    },
                    status=500,
                )
            if gp is None:
                return web.json_response(
                    {"error": "not_found", "graph_id": graph_id},
                    status=404,
                )
            payload = _serialize_graph_detail(gp)
            payload["schema_version"] = (
                GRAPH_OBSERVABILITY_SCHEMA_VERSION
            )
            response = web.json_response(payload)
            if cors_headers is not None:
                try:
                    cors_headers(response)
                except Exception:  # noqa: BLE001 — defensive
                    pass
            response.headers["Cache-Control"] = "no-store"
            return response
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[GraphObservability] detail raised: %s", exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    try:
        app.router.add_get(
            "/observability/execution-graph", _handle_list,
        )
        app.router.add_get(
            "/observability/execution-graph/{graph_id}",
            _handle_detail,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[GraphObservability] route mount raised: %s", exc,
        )


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``graph_observability_authority_asymmetry`` — module
         purity + no mutating tracker calls.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graph_observability.py"
    )

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"graph_observability.py MUST "
                            f"NOT import {module!r}"
                        )
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr in (
                        "register_graph",
                        "unsubscribe_all",
                        "bind",
                    )
                ):
                    violations.append(
                        f"graph_observability.py is read-only; "
                        f"MUST NOT call .{fn.attr}() on tracker"
                    )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "graph_observability_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Path D.1 — observability surface purity + "
                "read-only contract."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "GRAPH_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
    "register_shipped_invariants",
]
