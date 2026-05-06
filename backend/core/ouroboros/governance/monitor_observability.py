"""Path D.2 — observability surface for L3 ExecutionMonitor.

Exposes:

  * ``GET /observability/execution-monitor`` — current snapshot
    (failure_rate, resource_violation_rate, total_recorded,
    status_distribution).
  * ``GET /observability/execution-monitor/recent`` — last N
    outcomes (default 10, cap 200) via ``limit`` query param.

Auto-mounted by :mod:`observability_route_registry` (§32.11
Slice 3) via canonical ``register_routes(app, *,
rate_limit_check, cors_headers)`` entry point — zero edits to
the boot path.

Read-only. Authority asymmetry is structural:

  * NEVER imports orchestrator / iron_gate / policy / providers
    / candidate_generator / urgency_router / change_engine /
    semantic_guardian (AST-pinned).
  * NEVER calls mutating monitor methods (``record``).
  * Composes :func:`get_default_monitor` only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


MONITOR_OBSERVABILITY_SCHEMA_VERSION: str = (
    "monitor_observability.1"
)


def _serialize_outcome(o) -> Dict[str, Any]:
    try:
        status = getattr(o, "status", None)
        return {
            "op_id": getattr(o, "op_id", "")[:64],
            "status": (
                getattr(status, "name", str(status or ""))
            ),
            "duration_ms": float(getattr(o, "duration_ms", 0.0)),
            "memory_peak_mb": float(
                getattr(o, "memory_peak_mb", 0.0),
            ),
            "call_depth": int(getattr(o, "call_depth", 0)),
            "is_terminal": bool(
                getattr(o, "is_terminal", False),
            ),
            "is_resource_violation": bool(
                getattr(o, "is_resource_violation", False),
            ),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {"error": "projection_failed"}


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
            "[MonitorObservability] aiohttp unavailable — skip"
        )
        return

    def _get_monitor():
        try:
            from backend.core.ouroboros.governance.autonomy.execution_monitor import (  # noqa: E501
                get_default_monitor,
            )
            return get_default_monitor()
        except Exception:  # noqa: BLE001 — defensive
            return None

    async def _handle_snapshot(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            monitor = _get_monitor()
            if monitor is None:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                snap = monitor.to_dict()
            except Exception as exc:  # noqa: BLE001 — defensive
                return web.json_response(
                    {
                        "error": "internal",
                        "detail": str(exc)[:128],
                    },
                    status=500,
                )
            payload: Dict[str, Any] = {
                "schema_version": (
                    MONITOR_OBSERVABILITY_SCHEMA_VERSION
                ),
                **snap,
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
                "[MonitorObservability] snapshot raised: %s",
                exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    async def _handle_recent(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            limit_raw = request.query.get("limit", "10")
            try:
                limit = max(1, min(200, int(limit_raw)))
            except (TypeError, ValueError):
                limit = 10
            monitor = _get_monitor()
            if monitor is None:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                recent = monitor.get_recent_outcomes(
                    limit=limit,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                return web.json_response(
                    {
                        "error": "internal",
                        "detail": str(exc)[:128],
                    },
                    status=500,
                )
            payload = {
                "schema_version": (
                    MONITOR_OBSERVABILITY_SCHEMA_VERSION
                ),
                "limit": limit,
                "outcomes": [
                    _serialize_outcome(o) for o in recent
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
                "[MonitorObservability] recent raised: %s", exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    try:
        app.router.add_get(
            "/observability/execution-monitor",
            _handle_snapshot,
        )
        app.router.add_get(
            "/observability/execution-monitor/recent",
            _handle_recent,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[MonitorObservability] route mount raised: %s",
            exc,
        )


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``monitor_observability_authority_asymmetry`` — module
         purity + no mutating monitor calls.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "monitor_observability.py"
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
                            f"monitor_observability.py MUST "
                            f"NOT import {module!r}"
                        )
            if isinstance(node, ast.Call):
                fn = node.func
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "record"
                    and isinstance(fn.value, ast.Name)
                ):
                    rcv = fn.value.id.lower()
                    if (
                        "monitor" in rcv
                        or "execution" in rcv
                    ):
                        violations.append(
                            "monitor_observability.py is "
                            "read-only; MUST NOT call "
                            ".record() on the monitor"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "monitor_observability_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Path D.2 — observability surface purity + "
                "read-only contract."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "MONITOR_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
    "register_shipped_invariants",
]
