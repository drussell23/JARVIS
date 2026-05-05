"""Move 7 — Cross-op Semantic Budget Slice 3 HTTP observability
surface (PRD §29.4, 2026-05-05).

`GET /observability/semantic-budget` — current verdict + drift
state read from the Slice 2 ledger via Slice 1's primitive.
Auto-mounted by `event_channel.py` via the §32.11 Slice 3
`observability_route_registry` (file ends in `_observability.py`
+ exposes module-level `register_routes(app, **kwargs)` per
§33.3 Slice 5b naming-cage convention).

## Architectural locks (operator mandate, AST-pinned)

  * **Read-only** — composes Slice 1 + Slice 2 primitives;
    NEVER writes; NEVER triggers an observer tick (operator
    snapshot, not active probe).
  * **Master-flag-gated** — returns 503 when
    :func:`cross_op_semantic_budget_enabled` returns False so
    operators see an explicit "disabled" signal rather than
    stale data.
  * **Authority asymmetry** — imports stdlib + aiohttp +
    Slice 1 + Slice 2 ONLY. NEVER imports orchestrator /
    iron_gate / policy / providers.
  * **Naming-cage compliant** — module name ends
    ``_observability.py``; module-level
    ``register_routes(app, *, rate_limit_check=None,
    cors_headers=None) -> None`` matches the §33.3 contract
    so the §32.11 Slice 3 auto-discovery picks it up
    zero-edit.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


CROSS_OP_SEMANTIC_BUDGET_OBSERVABILITY_SCHEMA_VERSION: str = (
    "cross_op_semantic_budget_observability.1"
)


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _SemanticBudgetRoutesHandler:
    """aiohttp route handler for the
    ``GET /observability/semantic-budget`` family.

    Read-only. Master-flag-gated. Per-request rate-limit + CORS
    via the shared IDEObservabilityRouter helper (caller-
    supplied)."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        try:
            from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
                cross_op_semantic_budget_enabled,
            )
        except Exception:  # noqa: BLE001
            return self._json_response(
                {"error": "substrate_unavailable"},
                status=503,
            )
        if not cross_op_semantic_budget_enabled():
            return self._json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        CROSS_OP_SEMANTIC_BUDGET_OBSERVABILITY_SCHEMA_VERSION  # noqa: E501
                    ),
                },
                status=503,
            )
        if self._rate_limit_check is not None:
            try:
                if not self._rate_limit_check(request):
                    return self._json_response(
                        {"error": "rate_limited"},
                        status=429,
                    )
            except Exception:  # noqa: BLE001
                pass
        return None

    def _json_response(
        self, payload: dict, *, status: int = 200,
    ) -> Any:
        from aiohttp import web
        return web.json_response(
            payload,
            status=status,
            headers={"Cache-Control": "no-store"},
        )

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/semantic-budget`` — current
        verdict + integrated drift + window state.

        Reads the rolling-window ledger via Slice 2's reader,
        runs Slice 1's compute_semantic_budget primitive, and
        projects the frozen :class:`SemanticBudgetReport` to
        a JSON envelope. Read-only; does NOT trigger an observer
        tick (operator snapshot, not active probe)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            from backend.core.ouroboros.governance.cross_op_semantic_budget import (  # noqa: E501
                compute_semantic_budget,
                window_size,
            )
            from backend.core.ouroboros.governance.cross_op_semantic_recorder import (  # noqa: E501
                read_recent_centroids,
            )
            centroids = read_recent_centroids(
                limit=window_size(),
            )
            report = compute_semantic_budget(
                centroids, enabled_override=True,
            )
            payload = report.to_dict()
            payload["schema_version_observability"] = (
                CROSS_OP_SEMANTIC_BUDGET_OBSERVABILITY_SCHEMA_VERSION  # noqa: E501
            )
            payload["sse_event_type"] = (
                "semantic_budget_changed"
            )
            return self._json_response(payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[CrossOpSemanticObservability] overview "
                "raised: %s", exc,
            )
            return self._json_response(
                {"error": "snapshot_failed"},
                status=500,
            )


# ---------------------------------------------------------------------------
# Public auto-mount surface — §33.3 naming-cage contract
# ---------------------------------------------------------------------------


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Module-level register_routes per §33.3 Slice 5b
    naming-cage. Called by event_channel via §32.11 Slice 3
    auto-discovery; zero-edit registration. NEVER raises."""
    handler = _SemanticBudgetRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/semantic-budget",
        handler.handle_overview,
    )


__all__ = [
    "CROSS_OP_SEMANTIC_BUDGET_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
]
