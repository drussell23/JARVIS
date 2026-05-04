"""Slice 5b D — CIGW (Continuous Invariant Gradient Watcher)
observability GET routes.

Loopback-only, rate-limited, CORS-aware read surface mirroring
``register_invariant_drift_routes`` (Move 4 Slice 5),
``register_confidence_probe_routes`` (Slice 5b A),
``register_coherence_routes`` (Slice 5b B), and
``register_quorum_routes`` (Slice 5b C). Operators query CIGW
watcher / collector / observer state via GET endpoints + the SSE
``EVENT_TYPE_CIGW_REPORT_RECORDED`` /
``EVENT_TYPE_CIGW_BASELINE_UPDATED`` events for live updates.

Routes:

  * ``GET /observability/gradient``          — flag state +
    threshold knobs + observer config + history size + recent
    aggregate stats (single consolidated dashboard endpoint)
  * ``GET /observability/gradient/config``   — env-knob snapshot
    (5 severity thresholds + observer cadence + history dir)
  * ``GET /observability/gradient/history``  — recent
    ``StampedGradientReport`` history via
    :func:`read_gradient_history`. Supports ``limit`` query param
    (the underlying reader is tail-N by file-position; no
    ``since_ts`` filter — stamped records carry no ts field)
  * ``GET /observability/gradient/stats``    — aggregate
    insights via :func:`compare_recent_gradient_history` —
    consumes the existing comparator pipeline
  * ``GET /observability/gradient/outcomes`` — closed-enum vocab:
    surfaces ``MeasurementKind`` + ``GradientSeverity`` +
    ``GradientOutcome`` for clients rendering severity chips

All routes:

  * Master-flag-gated per request via :func:`cigw_enabled`
    (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + verification.gradient_*
    modules ONLY.
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
    EVENT_TYPE_CIGW_BASELINE_UPDATED,
    EVENT_TYPE_CIGW_REPORT_RECORDED,
)
from backend.core.ouroboros.governance.verification.gradient_comparator import (  # noqa: E501
    CIGW_COMPARATOR_SCHEMA_VERSION,
)
from backend.core.ouroboros.governance.verification.gradient_observer import (  # noqa: E501
    CIGW_OBSERVER_SCHEMA_VERSION,
    cigw_history_max_records,
    cigw_history_path,
    cigw_observer_drift_multiplier,
    cigw_observer_enabled,
    cigw_observer_failure_backoff_ceiling_s,
    cigw_observer_interval_default_s,
    cigw_observer_liveness_pulse_passes,
    compare_recent_gradient_history,
    read_gradient_history,
)
from backend.core.ouroboros.governance.verification.gradient_watcher import (  # noqa: E501
    CIGW_SCHEMA_VERSION,
    GradientOutcome,
    GradientSeverity,
    MeasurementKind,
    cigw_critical_threshold_pct,
    cigw_enabled,
    cigw_high_threshold_pct,
    cigw_low_threshold_pct,
    cigw_medium_threshold_pct,
    cigw_rolling_window_size,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query-param clamps — bounded so an unfriendly client cannot trigger
# unbounded reads. Mirrors Slice 5b B/C clamp discipline.
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


def _build_threshold_dict() -> Dict[str, Any]:
    """Snapshot of severity-threshold knobs. NEVER raises."""
    try:
        return {
            "rolling_window_size": cigw_rolling_window_size(),
            "low_threshold_pct": cigw_low_threshold_pct(),
            "medium_threshold_pct": cigw_medium_threshold_pct(),
            "high_threshold_pct": cigw_high_threshold_pct(),
            "critical_threshold_pct": (
                cigw_critical_threshold_pct()
            ),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_observer_config_dict() -> Dict[str, Any]:
    """Snapshot of observer cadence knobs. NEVER raises."""
    try:
        return {
            "interval_default_s": (
                cigw_observer_interval_default_s()
            ),
            "drift_multiplier": (
                cigw_observer_drift_multiplier()
            ),
            "failure_backoff_ceiling_s": (
                cigw_observer_failure_backoff_ceiling_s()
            ),
            "liveness_pulse_passes": (
                cigw_observer_liveness_pulse_passes()
            ),
            "history_max_records": cigw_history_max_records(),
            "history_path": str(cigw_history_path()),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_history_size() -> int:
    """Best-effort line-count of the JSONL store. NEVER raises."""
    try:
        return len(
            read_gradient_history(
                limit=cigw_history_max_records(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _safe_recent_stats_dict(
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Best-effort recent comparison stats. NEVER raises."""
    try:
        report = compare_recent_gradient_history(limit=limit)
        return report.to_dict()
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _GradientRoutesHandler:
    """aiohttp route handler for the ``/observability/gradient``
    family. Mirror of ``_QuorumRoutesHandler`` shape from Slice 5b C."""

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
        if not cigw_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": CIGW_SCHEMA_VERSION,
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
        """``GET /observability/gradient`` — single consolidated
        dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": CIGW_SCHEMA_VERSION,
                "schemas": {
                    "watcher": CIGW_SCHEMA_VERSION,
                    "comparator": CIGW_COMPARATOR_SCHEMA_VERSION,
                    "observer": CIGW_OBSERVER_SCHEMA_VERSION,
                },
                "flags": {
                    "cigw_enabled": cigw_enabled(),
                    "cigw_observer_enabled": (
                        cigw_observer_enabled()
                    ),
                },
                "thresholds": _build_threshold_dict(),
                "observer_config": _build_observer_config_dict(),
                "history_size": _safe_history_size(),
                "recent_stats": _safe_recent_stats_dict(),
                "sse_event_types": {
                    "report_recorded": (
                        EVENT_TYPE_CIGW_REPORT_RECORDED
                    ),
                    "baseline_updated": (
                        EVENT_TYPE_CIGW_BASELINE_UPDATED
                    ),
                },
                "measurement_kinds": [
                    k.value for k in MeasurementKind
                ],
                "severity_levels": [
                    s.value for s in GradientSeverity
                ],
                "outcome_kinds": [
                    o.value for o in GradientOutcome
                ],
            },
        )

    async def handle_config(self, request: Any) -> Any:
        """``GET /observability/gradient/config`` — env-knob
        snapshot for operator inspection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": CIGW_SCHEMA_VERSION,
                "thresholds": _build_threshold_dict(),
                "observer_config": _build_observer_config_dict(),
                "flags": {
                    "cigw_enabled": cigw_enabled(),
                    "cigw_observer_enabled": (
                        cigw_observer_enabled()
                    ),
                },
            },
        )

    async def handle_history(self, request: Any) -> Any:
        """``GET /observability/gradient/history`` — recent
        StampedGradientReport history."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        try:
            history = read_gradient_history(limit=limit)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[GradientObservability] read_gradient_history "
                "raised: %s", exc,
            )
            history = ()
        records = [s.to_dict() for s in history]
        return _json_response(
            {
                "schema_version": CIGW_OBSERVER_SCHEMA_VERSION,
                "records": records,
                "limit": limit,
                "count": len(records),
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/gradient/stats`` — aggregate
        comparison via existing comparator pipeline."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        return _json_response(
            {
                "schema_version": CIGW_COMPARATOR_SCHEMA_VERSION,
                "stats": _safe_recent_stats_dict(limit=limit),
                "limit": limit,
            },
        )

    async def handle_outcomes(self, request: Any) -> Any:
        """``GET /observability/gradient/outcomes`` — closed-enum
        vocabulary so clients render severity chips without
        hardcoding strings."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": CIGW_SCHEMA_VERSION,
                "measurement_kinds": [
                    k.value for k in MeasurementKind
                ],
                "severity_levels": [
                    s.value for s in GradientSeverity
                ],
                "outcome_kinds": [
                    o.value for o in GradientOutcome
                ],
                "sse_event_types": {
                    "report_recorded": (
                        EVENT_TYPE_CIGW_REPORT_RECORDED
                    ),
                    "baseline_updated": (
                        EVENT_TYPE_CIGW_BASELINE_UPDATED
                    ),
                },
            },
        )


# ---------------------------------------------------------------------------
# Public API — register_gradient_routes
# ---------------------------------------------------------------------------


def register_gradient_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the gradient GET routes on a caller-supplied aiohttp
    Application. Mirrors ``register_quorum_routes`` from
    Slice 5b C.

    Routes:
      * ``GET /observability/gradient``          — overview
      * ``GET /observability/gradient/config``   — env knobs
      * ``GET /observability/gradient/history``  — report history
      * ``GET /observability/gradient/stats``    — comparator
      * ``GET /observability/gradient/outcomes`` — enum vocabulary

    Master flag check is per-request inside the handler so route
    mounting itself is safe to call regardless of flag state."""
    handler = _GradientRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/gradient", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/gradient/config", handler.handle_config,
    )
    app.router.add_get(
        "/observability/gradient/history", handler.handle_history,
    )
    app.router.add_get(
        "/observability/gradient/stats", handler.handle_stats,
    )
    app.router.add_get(
        "/observability/gradient/outcomes",
        handler.handle_outcomes,
    )


__all__ = [
    "register_gradient_routes",
]
