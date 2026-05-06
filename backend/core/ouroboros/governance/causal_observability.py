"""§31 U2 empirical wiring — Slice 4 observability surface.

Exposes ``GET /observability/causal/{record_id}?session={sid}``
returning the :class:`OpCausalFeatures` artifact for one op.
Auto-mounted by :mod:`observability_route_registry` (§32.11
Slice 3) via the canonical ``register_routes(app, *,
rate_limit_check, cors_headers)`` module-level entry point —
zero edits to the boot path.

Read-only. Authority asymmetry is structural:

  * NEVER imports orchestrator / iron_gate / policy / providers
    / candidate_generator / urgency_router / change_engine /
    semantic_guardian.
  * NEVER calls :meth:`DecisionRuntime.record` (or any other
    decision-write surface).
  * Composes :func:`causality_consumer.compute_op_causal_features`
    only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


CAUSAL_OBSERVABILITY_SCHEMA_VERSION: str = (
    "causal_observability.1"
)


# ---------------------------------------------------------------------------
# HTTP route — auto-mounted via §32.11 Slice 3 registry
# ---------------------------------------------------------------------------


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Auto-mount entry point. Composes the canonical aiohttp
    routing surface; NEVER raises.

    Routes:
      * ``GET /observability/causal/{record_id}`` — returns
        ``OpCausalFeatures.to_dict()`` for the requested op.
        Query param ``session`` carries the session_id (required
        for build_dag).

    Backward-compat: callers that pass a None ``rate_limit_check``
    skip rate-limiting (matches the ``decisions_observability``
    pattern).
    """
    try:
        from aiohttp import web
    except ImportError:
        logger.debug(
            "[CausalObservability] aiohttp unavailable — "
            "skipping route mount"
        )
        return

    async def _handle_get_features(request):
        try:
            if rate_limit_check is not None:
                if not rate_limit_check(request):
                    return web.json_response(
                        {"error": "rate_limited"},
                        status=429,
                    )
            record_id = request.match_info.get("record_id", "")
            session_id = request.query.get("session", "")
            if not record_id or not session_id:
                return web.json_response(
                    {
                        "error": "missing_params",
                        "detail": (
                            "record_id (path) + session "
                            "(query) required"
                        ),
                    },
                    status=400,
                )
            try:
                from backend.core.ouroboros.governance.causality_consumer import (  # noqa: E501
                    compute_op_causal_features,
                )
            except ImportError:
                return web.json_response(
                    {"error": "substrate_unavailable"},
                    status=503,
                )
            try:
                features = compute_op_causal_features(
                    session_id=session_id, record_id=record_id,
                )
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[CausalObservability] compute raised: %s",
                    exc,
                )
                return web.json_response(
                    {"error": "internal", "detail": str(exc)[:128]},
                    status=500,
                )
            payload = features.to_dict()
            payload["schema_version"] = (
                CAUSAL_OBSERVABILITY_SCHEMA_VERSION
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
                "[CausalObservability] handler raised: %s", exc,
            )
            return web.json_response(
                {"error": "internal"}, status=500,
            )

    try:
        app.router.add_get(
            "/observability/causal/{record_id}",
            _handle_get_features,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[CausalObservability] route mount raised: %s", exc,
        )


# ---------------------------------------------------------------------------
# AST pin
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``causal_observability_authority_asymmetry`` — module
         purity. Forbids orchestrator+iron_gate+policy+providers+
         candidate_generator+urgency_router+change_engine+
         semantic_guardian imports + ``DecisionRuntime.record``
         calls.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/causal_observability.py"
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
                            f"causal_observability.py MUST NOT "
                            f"import {module!r}"
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
                        "decision" in rcv
                        or "runtime" in rcv
                        or "ledger" in rcv
                    ):
                        violations.append(
                            "causal_observability.py is "
                            "read-only; MUST NOT call "
                            ".record() on decision/runtime/"
                            "ledger receiver"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "causal_observability_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "§31 U2 Slice 4 — observability surface "
                "purity + read-only contract."
            ),
            validate=_validate_authority_asymmetry,
        ),
    ]


__all__ = [
    "CAUSAL_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
    "register_shipped_invariants",
]
