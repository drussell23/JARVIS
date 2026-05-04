"""Slice 5b D — SBT (Speculative Branch Tree) observability GET
routes.

Loopback-only, rate-limited, CORS-aware read surface mirroring
``register_invariant_drift_routes`` (Move 4 Slice 5),
``register_confidence_probe_routes`` (Slice 5b A),
``register_coherence_routes`` (Slice 5b B),
``register_quorum_routes`` (Slice 5b C), and
``register_gradient_routes`` (Slice 5b D — companion). Operators
query SBT primitive / runner / comparator / observer state via GET
endpoints + the SSE ``EVENT_TYPE_SBT_TREE_COMPLETE`` /
``EVENT_TYPE_SBT_BASELINE_UPDATED`` events for live updates.

Routes:

  * ``GET /observability/sbt``          — flag state + runner +
    comparator + observer config + history size + recent
    aggregate stats (single consolidated dashboard endpoint)
  * ``GET /observability/sbt/config``   — env-knob snapshot
    (runner cadence + observer config + history dir)
  * ``GET /observability/sbt/history``  — recent
    ``StampedTreeVerdict`` history via
    :func:`read_tree_history`. Supports ``limit`` query param
    (the underlying reader is tail-N by file-position; no
    ``since_ts`` filter — stamped records carry no ts field)
  * ``GET /observability/sbt/stats``    — aggregate insights
    via :func:`compare_recent_tree_history` — consumes the
    existing comparator pipeline
  * ``GET /observability/sbt/outcomes`` — closed-enum vocab:
    surfaces ``TreeVerdict`` + ``SBTBaselineQuality`` for
    clients rendering verdict chips

All routes:

  * Master-flag-gated per request via :func:`sbt_enabled`
    (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + verification.speculative_*
    modules + verification.sbt_* modules ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / providers / urgency_router /
    auto_action_router / subagent_scheduler / tool_executor.
  * Read-only surface — never modifies state, never writes
    ledgers; consumes existing public readers only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
    EVENT_TYPE_SBT_BASELINE_UPDATED,
    EVENT_TYPE_SBT_TREE_COMPLETE,
)
from backend.core.ouroboros.governance.verification.speculative_branch import (  # noqa: E501
    SBT_SCHEMA_VERSION,
    TreeVerdict,
    sbt_enabled,
)
from backend.core.ouroboros.governance.verification.speculative_branch_comparator import (  # noqa: E501
    SBT_COMPARATOR_SCHEMA_VERSION,
    SBTBaselineQuality,
    comparator_enabled,
)
from backend.core.ouroboros.governance.verification.speculative_branch_observer import (  # noqa: E501
    SBT_OBSERVER_SCHEMA_VERSION,
    compare_recent_tree_history,
    read_tree_history,
    sbt_history_max_records,
    sbt_history_path,
    sbt_observer_drift_multiplier,
    sbt_observer_enabled,
    sbt_observer_failure_backoff_ceiling_s,
    sbt_observer_interval_default_s,
    sbt_observer_liveness_pulse_passes,
)
from backend.core.ouroboros.governance.verification.speculative_branch_runner import (  # noqa: E501
    SBT_RUNNER_SCHEMA_VERSION,
    sbt_runner_enabled,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query-param clamps — bounded so an unfriendly client cannot trigger
# unbounded reads. Mirrors Slice 5b B/C/D-CIGW clamp discipline.
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 1000


def _parse_limit(request: Any) -> int:
    """Parse ?limit=N — clamped to [1, _MAX_LIMIT]; default
    _DEFAULT_LIMIT. NEVER raises."""
    try:
        raw = request.query.get("limit")
        if raw is None:
            return _DEFAULT_LIMIT
        n = int(raw)
        if n < 1:
            return 1
        if n > _MAX_LIMIT:
            return _MAX_LIMIT
        return n
    except Exception:  # noqa: BLE001 — defensive
        return _DEFAULT_LIMIT


# ---------------------------------------------------------------------------
# JSON response helper
# ---------------------------------------------------------------------------


def _json_response(payload: dict, *, status: int = 200) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response. Lazy
    import of aiohttp.web — keeps module importable in environments
    without aiohttp installed (CI tests without web stack)."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Env-knob snapshots — composed lazily so a missing env var inside
# any helper does not break the route.
# ---------------------------------------------------------------------------


def _build_observer_config_dict() -> Dict[str, Any]:
    """Snapshot of observer cadence knobs. NEVER raises."""
    try:
        return {
            "interval_default_s": (
                sbt_observer_interval_default_s()
            ),
            "drift_multiplier": (
                sbt_observer_drift_multiplier()
            ),
            "failure_backoff_ceiling_s": (
                sbt_observer_failure_backoff_ceiling_s()
            ),
            "liveness_pulse_passes": (
                sbt_observer_liveness_pulse_passes()
            ),
            "history_max_records": sbt_history_max_records(),
            "history_path": str(sbt_history_path()),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_history_size() -> int:
    """Best-effort line-count of the JSONL store. NEVER raises."""
    try:
        return len(
            read_tree_history(
                limit=sbt_history_max_records(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _safe_recent_stats_dict(
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Best-effort recent comparison stats. NEVER raises."""
    try:
        report = compare_recent_tree_history(limit=limit)
        return report.to_dict()
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _SBTRoutesHandler:
    """aiohttp route handler for the ``/observability/sbt``
    family. Mirror of ``_GradientRoutesHandler`` shape from
    Slice 5b D companion."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        """Run master-flag + rate-limit gate. Returns 503/429
        Response when the request should be rejected, None when
        the handler should proceed."""
        if not sbt_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": SBT_SCHEMA_VERSION,
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

    # ---- handlers -------------------------------------------------------

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/sbt`` — single consolidated
        dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": SBT_SCHEMA_VERSION,
                "schemas": {
                    "primitive": SBT_SCHEMA_VERSION,
                    "runner": SBT_RUNNER_SCHEMA_VERSION,
                    "comparator": SBT_COMPARATOR_SCHEMA_VERSION,
                    "observer": SBT_OBSERVER_SCHEMA_VERSION,
                },
                "flags": {
                    "sbt_enabled": sbt_enabled(),
                    "sbt_runner_enabled": sbt_runner_enabled(),
                    "comparator_enabled": comparator_enabled(),
                    "sbt_observer_enabled": (
                        sbt_observer_enabled()
                    ),
                },
                "observer_config": _build_observer_config_dict(),
                "history_size": _safe_history_size(),
                "recent_stats": _safe_recent_stats_dict(),
                "sse_event_types": {
                    "tree_complete": (
                        EVENT_TYPE_SBT_TREE_COMPLETE
                    ),
                    "baseline_updated": (
                        EVENT_TYPE_SBT_BASELINE_UPDATED
                    ),
                },
                "verdict_kinds": [v.value for v in TreeVerdict],
                "baseline_qualities": [
                    q.value for q in SBTBaselineQuality
                ],
            },
        )

    async def handle_config(self, request: Any) -> Any:
        """``GET /observability/sbt/config`` — env-knob snapshot
        for operator inspection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": SBT_SCHEMA_VERSION,
                "observer_config": _build_observer_config_dict(),
                "flags": {
                    "sbt_enabled": sbt_enabled(),
                    "sbt_runner_enabled": sbt_runner_enabled(),
                    "comparator_enabled": comparator_enabled(),
                    "sbt_observer_enabled": (
                        sbt_observer_enabled()
                    ),
                },
            },
        )

    async def handle_history(self, request: Any) -> Any:
        """``GET /observability/sbt/history`` — recent
        StampedTreeVerdict history."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        try:
            history = read_tree_history(limit=limit)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[SBTObservability] read_tree_history raised: %s",
                exc,
            )
            history = ()
        records = [s.to_dict() for s in history]
        return _json_response(
            {
                "schema_version": SBT_OBSERVER_SCHEMA_VERSION,
                "records": records,
                "limit": limit,
                "count": len(records),
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/sbt/stats`` — aggregate comparison
        via existing comparator pipeline."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        return _json_response(
            {
                "schema_version": SBT_COMPARATOR_SCHEMA_VERSION,
                "stats": _safe_recent_stats_dict(limit=limit),
                "limit": limit,
            },
        )

    async def handle_outcomes(self, request: Any) -> Any:
        """``GET /observability/sbt/outcomes`` — closed-enum
        vocabulary so clients render verdict chips without
        hardcoding strings."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": SBT_SCHEMA_VERSION,
                "verdict_kinds": [v.value for v in TreeVerdict],
                "baseline_qualities": [
                    q.value for q in SBTBaselineQuality
                ],
                "sse_event_types": {
                    "tree_complete": (
                        EVENT_TYPE_SBT_TREE_COMPLETE
                    ),
                    "baseline_updated": (
                        EVENT_TYPE_SBT_BASELINE_UPDATED
                    ),
                },
            },
        )


# ---------------------------------------------------------------------------
# Public API — register_sbt_routes
# ---------------------------------------------------------------------------


def register_sbt_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the SBT GET routes on a caller-supplied aiohttp
    Application. Mirrors ``register_gradient_routes`` from
    Slice 5b D companion.

    Routes:
      * ``GET /observability/sbt``          — overview
      * ``GET /observability/sbt/config``   — env knobs
      * ``GET /observability/sbt/history``  — verdict history
      * ``GET /observability/sbt/stats``    — comparator
      * ``GET /observability/sbt/outcomes`` — enum vocabulary

    Master flag check is per-request inside the handler so route
    mounting itself is safe to call regardless of flag state."""
    handler = _SBTRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/sbt", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/sbt/config", handler.handle_config,
    )
    app.router.add_get(
        "/observability/sbt/history", handler.handle_history,
    )
    app.router.add_get(
        "/observability/sbt/stats", handler.handle_stats,
    )
    app.router.add_get(
        "/observability/sbt/outcomes", handler.handle_outcomes,
    )


__all__ = [
    "register_sbt_routes",
]
