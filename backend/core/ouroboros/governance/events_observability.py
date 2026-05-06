"""Path D.3 — observability surface for L1 EventEmitter.

Exposes ``GET /observability/events`` returning the aggregate
:meth:`EventEmitter.snapshot_all` projection. Auto-mounted by
:mod:`observability_route_registry` (§32.11 Slice 3) via the
canonical ``register_routes(app, *, rate_limit_check,
cors_headers)`` entry point — zero edits to the boot path.

Read-only. Authority asymmetry is structural — no
orchestrator / iron_gate / policy / providers imports.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


EVENTS_OBSERVABILITY_SCHEMA_VERSION: str = (
    "events_observability.1"
)


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
            "[EventsObservability] aiohttp unavailable — skip"
        )
        return

    async def _handle_snapshot(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            try:
                from backend.core.ouroboros.governance.autonomy.event_emitter import (  # noqa: E501
                    EventEmitter,
                )
            except ImportError:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                agg = EventEmitter.snapshot_all()
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
                    EVENTS_OBSERVABILITY_SCHEMA_VERSION
                ),
                **agg,
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
                "[EventsObservability] handler raised: %s", exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    try:
        app.router.add_get(
            "/observability/events", _handle_snapshot,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[EventsObservability] route mount raised: %s", exc,
        )


def register_shipped_invariants() -> list:
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/events_observability.py"
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
                            f"events_observability.py MUST "
                            f"NOT import {module!r}"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "events_observability_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Path D.3 — observability surface purity."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "EVENTS_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
    "register_shipped_invariants",
]
