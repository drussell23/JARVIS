"""M9 Slice 4 — CuriosityGradient observability HTTP routes
(PRD §30.5.1).

Loopback-only, rate-limited, CORS-aware HTTP read surface
mirroring :mod:`epistemic_budget_observability` (Upgrade 1
Slice 4) and :mod:`action_outcome_memory_observability` (M11).
Operators query per-cluster curiosity state via GET endpoints +
the SSE ``EVENT_TYPE_CURIOSITY_CHANGED`` event for live
transitions.

Routes (PRD §30.5.1 Slice 4):

  * ``GET /observability/curiosity`` — overview + top-K cluster
    snapshots (sorted by magnitude descending)
  * ``GET /observability/curiosity/region/{cluster_id}`` —
    per-cluster detail with full sample trail from JSONL replay

All routes:

  * Master-flag-gated per request via
    :func:`curiosity_gradient_enabled` (live-toggle without
    re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * **READ-ONLY** — never mutates collector state, never writes
    JSONL. The only mutation surface is :mod:`curiosity_repl`'s
    ``/curiosity reset <id>`` (operator-explicit). HTTP routes
    NEVER call :meth:`CuriosityCollector.reset_cluster` or
    :meth:`record_*`. AST-pinned at Slice 5.

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + aiohttp.web + ``curiosity_gradient`` +
    ``curiosity_collector`` ONLY.
  * NEVER imports orchestrator / iron_gate /
    candidate_generator / providers / urgency_router /
    semantic_guardian / tool_executor / change_engine /
    subagent_scheduler / auto_action_router / policy /
    sensor_governor / strategic_direction.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.curiosity_collector import (
    CuriosityCollector,
    curiosity_history_dir,
    curiosity_persist_enabled,
    curiosity_recurrence_loop_threshold,
    curiosity_window_size,
    get_default_collector,
    read_observations_for_cluster,
)
from backend.core.ouroboros.governance.curiosity_gradient import (
    CURIOSITY_GRADIENT_SCHEMA_VERSION,
    CuriosityDecayReason,
    CuriositySource,
    curiosity_gradient_enabled,
    curiosity_halflife_days,
    curiosity_min_samples,
    curiosity_multiplier_ceiling,
    curiosity_multiplier_floor,
    curiosity_source_weight_logprob,
    curiosity_source_weight_prophecy,
    curiosity_source_weight_recurrence,
    curiosity_stale_focus_hours,
)

logger = logging.getLogger(__name__)


_DEFAULT_TOP_LIMIT: int = 50
_MAX_TOP_LIMIT: int = 1000


def _parse_limit(request: Any) -> int:
    """Parse ?limit=N — clamped to [1, _MAX_TOP_LIMIT]; default
    _DEFAULT_TOP_LIMIT. NEVER raises."""
    try:
        raw = request.query.get("limit")
        if raw is None:
            return _DEFAULT_TOP_LIMIT
        n = int(raw)
        if n < 1:
            return 1
        if n > _MAX_TOP_LIMIT:
            return _MAX_TOP_LIMIT
        return n
    except Exception:  # noqa: BLE001 — defensive
        return _DEFAULT_TOP_LIMIT


def _json_response(payload: dict, *, status: int = 200) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response.
    Lazy import of aiohttp.web."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _build_config_dict() -> Dict[str, Any]:
    """Snapshot of operator-relevant env knobs. NEVER raises."""
    try:
        return {
            "halflife_days": curiosity_halflife_days(),
            "min_samples": curiosity_min_samples(),
            "stale_focus_hours": curiosity_stale_focus_hours(),
            "window_size": curiosity_window_size(),
            "recurrence_loop_threshold": (
                curiosity_recurrence_loop_threshold()
            ),
            "persist_enabled": curiosity_persist_enabled(),
            "history_dir": str(curiosity_history_dir()),
            "source_weights": {
                "logprob": curiosity_source_weight_logprob(),
                "prophecy": curiosity_source_weight_prophecy(),
                "recurrence": (
                    curiosity_source_weight_recurrence()
                ),
            },
            "multiplier_floor": curiosity_multiplier_floor(),
            "multiplier_ceiling": (
                curiosity_multiplier_ceiling()
            ),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _CuriosityRoutesHandler:
    """aiohttp route handler for the
    ``/observability/curiosity`` family. Mirrors
    :class:`_EpistemicBudgetRoutesHandler` (Upgrade 1)."""

    def __init__(
        self,
        *,
        collector: Optional[CuriosityCollector] = None,
        rate_limit_check: Optional[
            Callable[[Any], bool]
        ] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._collector = collector
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _resolved_collector(self) -> CuriosityCollector:
        return (
            self._collector
            if self._collector is not None
            else get_default_collector()
        )

    def _gate(self, request: Any) -> Optional[Any]:
        if not curiosity_gradient_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        CURIOSITY_GRADIENT_SCHEMA_VERSION
                    ),
                },
                status=503,
            )
        if self._rate_limit_check is not None:
            try:
                if not self._rate_limit_check(request):
                    return _json_response(
                        {"error": "rate_limited"},
                        status=429,
                    )
            except Exception:  # noqa: BLE001 — defensive
                pass
        return None

    # ---- handlers -------------------------------------------------

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/curiosity`` — overview + top-K
        cluster snapshots, sorted by magnitude descending.
        Operator can ?limit=N to reduce payload."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            collector = self._resolved_collector()
            limit = _parse_limit(request)
            scores = collector.snapshot_all()
            # Sort by magnitude descending, then by samples_count
            # descending for stable ordering at equal magnitudes
            sorted_scores = sorted(
                scores,
                key=lambda s: (-s.magnitude, -s.samples_count),
            )[:limit]
            return _json_response(
                {
                    "schema_version": (
                        CURIOSITY_GRADIENT_SCHEMA_VERSION
                    ),
                    "flags": {
                        "master_enabled": (
                            curiosity_gradient_enabled()
                        ),
                    },
                    "config": _build_config_dict(),
                    "tracked_count": len(scores),
                    "scores": [s.to_dict() for s in sorted_scores],
                    "limit": limit,
                    "source_kinds": [
                        k.value for k in CuriositySource
                    ],
                    "decay_reasons": [
                        k.value for k in CuriosityDecayReason
                    ],
                    "sse_event_type": "curiosity_changed",
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_observability] overview raised: "
                "%s", exc,
            )
            return _json_response(
                {"error": "snapshot_failed"},
                status=500,
            )

    async def handle_region_detail(self, request: Any) -> Any:
        """``GET /observability/curiosity/region/{cluster_id}`` —
        per-cluster detail with full sample trail (read from
        JSONL replay, capped by ?limit=N)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            cluster_id = (
                request.match_info.get(
                    "cluster_id", "",
                ) or ""
            ).strip()
        except Exception:  # noqa: BLE001 — defensive
            cluster_id = ""
        if not cluster_id:
            return _json_response(
                {"error": "missing_cluster_id"},
                status=400,
            )
        try:
            collector = self._resolved_collector()
            score = collector.score_for_cluster(cluster_id)
            limit = _parse_limit(request)
            # JSONL replay — bounded by ?limit (defaults 50)
            observations = read_observations_for_cluster(
                cluster_id, limit=limit,
            )
            payload = score.to_dict()
            payload["observations"] = [
                {
                    "source": o.source.value,
                    "value": float(o.value),
                    "at_unix": float(o.at_unix),
                    "op_id": o.op_id,
                }
                for o in observations
            ]
            payload["observations_count"] = len(observations)
            payload["sse_event_type"] = "curiosity_changed"
            return _json_response(payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[curiosity_observability] region detail "
                "raised: %s", exc,
            )
            return _json_response(
                {"error": "projection_failed"},
                status=500,
            )


# ---------------------------------------------------------------------------
# Router-mount helper
# ---------------------------------------------------------------------------


def register_routes(
    app: Any,
    *,
    collector: Optional[CuriosityCollector] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Register the ``/observability/curiosity`` family on the
    supplied aiohttp ``Application``. Idempotent at the route
    level (re-mounting raises on duplicate routes — caller's
    responsibility)."""
    handler = _CuriosityRoutesHandler(
        collector=collector,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/curiosity", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/curiosity/region/{cluster_id}",
        handler.handle_region_detail,
    )


__all__ = [
    "register_routes",
]
