"""Move 4 Slice 5 — InvariantDrift observability GET routes.

Loopback-only, rate-limited, CORS-aware read surface that mirrors
``register_auto_action_routes`` exactly. Operators query the current
baseline + recent history + observer/bridge counters via four
endpoints. The ``GovernedLoopService`` boot path mounts these routes
through ``EventChannel`` alongside the existing IDE observability
surfaces.

Routes:

  * ``GET /observability/invariant-drift``           — current
    baseline + recent history (last N snapshots)
  * ``GET /observability/invariant-drift/stats``     — observer +
    bridge counters
  * ``GET /observability/invariant-drift/baseline``  — current
    baseline JSON only (compact response for IDE polling)
  * ``GET /observability/invariant-drift/history?limit=N`` —
    recent history snapshots (newest last)

All routes:

  * Master-flag-gated per request (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check (loopback +
    per-IP cap inherited from the IDE observability shared helper).
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + invariant_drift_auditor +
    invariant_drift_observer + invariant_drift_store +
    invariant_drift_auto_action_bridge ONLY.
  * NEVER imports orchestrator / phase_runners / candidate_generator
    / iron_gate / change_engine / policy / semantic_guardian /
    semantic_firewall / providers / doubleword_provider /
    urgency_router / subagent_scheduler.
  * Read-only surface — never modifies state, never writes ledger.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.invariant_drift_auditor import (
    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION,
    invariant_drift_auditor_enabled,
)
from backend.core.ouroboros.governance.invariant_drift_auto_action_bridge import (  # noqa: E501
    InvariantDriftAutoActionBridge,
    bridge_enabled,
)
from backend.core.ouroboros.governance.invariant_drift_observer import (
    InvariantDriftObserver,
    base_interval_s,
    dedup_window,
    get_default_observer,
    observer_enabled,
    posture_multipliers,
    vigilance_factor,
    vigilance_ticks,
)
from backend.core.ouroboros.governance.invariant_drift_store import (
    InvariantDriftStore,
    get_default_store,
)

logger = logging.getLogger(__name__)


_OBSERVABILITY_DEFAULT_LIMIT: int = 100
_OBSERVABILITY_MAX_LIMIT: int = 500


# ---------------------------------------------------------------------------
# JSON response helper (mirrors auto_action_router._json_response)
# ---------------------------------------------------------------------------


def _json_response(payload: dict, *, status: int = 200) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response. Lazy
    import of aiohttp.web — keeps the module importable in
    environments without aiohttp installed (CI tests without the
    web stack)."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _parse_limit(query: Any) -> int:
    """Parse + clamp the ``limit`` query param. Defaults to 100,
    capped at 500. Mirrors auto_action_router._parse_limit."""
    raw = (query or {}).get("limit", "")
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return _OBSERVABILITY_DEFAULT_LIMIT
    return max(1, min(_OBSERVABILITY_MAX_LIMIT, v))


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _InvariantDriftRoutesHandler:
    """aiohttp route handler for the
    ``/observability/invariant-drift`` family. Mirror of
    ``_AutoActionRoutesHandler`` shape exactly."""

    def __init__(
        self,
        *,
        store: Optional[InvariantDriftStore] = None,
        observer: Optional[InvariantDriftObserver] = None,
        bridge: Optional[InvariantDriftAutoActionBridge] = None,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        # Store / observer resolved lazily on first request — env
        # overrides at boot time take effect without re-mounting.
        self._store_override = store
        self._observer_override = observer
        self._bridge_override = bridge
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _store(self) -> InvariantDriftStore:
        if self._store_override is not None:
            return self._store_override
        return get_default_store()

    def _observer(self) -> InvariantDriftObserver:
        if self._observer_override is not None:
            return self._observer_override
        return get_default_observer()

    def _gate(self, request: Any) -> Optional[Any]:
        """Run the master-flag + rate-limit gate. Returns a Response
        when the request should be rejected, None when the handler
        should proceed."""
        if not invariant_drift_auditor_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
                    ),
                },
                status=503,
            )
        if self._rate_limit_check is not None:
            try:
                if not self._rate_limit_check(request):
                    return _json_response(
                        {"error": "rate_limited"}, status=429,
                    )
            except Exception:  # noqa: BLE001 — defensive
                pass
        return None

    # ---- handlers --------------------------------------------------------

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/invariant-drift`` — current baseline
        + recent history + flag/observer/bridge state. The single
        consolidated dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(getattr(request, "query", {}))
        store = self._store()
        try:
            baseline = store.load_baseline()
            history = store.load_history(limit=limit)
        except Exception:  # noqa: BLE001 — defensive
            baseline, history = None, []
        try:
            store_stats = store.stats()
        except Exception:  # noqa: BLE001 — defensive
            store_stats = {}
        try:
            obs_stats = self._observer().stats()
        except Exception:  # noqa: BLE001 — defensive
            obs_stats = {}
        return _json_response(
            {
                "schema_version": (
                    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
                ),
                "flags": {
                    "auditor_enabled": (
                        invariant_drift_auditor_enabled()
                    ),
                    "observer_enabled": observer_enabled(),
                    "bridge_enabled": bridge_enabled(),
                },
                "baseline": (
                    baseline.to_dict()
                    if baseline is not None else None
                ),
                "history_count": len(history),
                "history": [s.to_dict() for s in history],
                "store_stats": store_stats,
                "observer_stats": obs_stats,
                "limit": limit,
            },
        )

    async def handle_baseline(self, request: Any) -> Any:
        """``GET /observability/invariant-drift/baseline`` — current
        baseline only (compact response for tight-loop polling)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            baseline = self._store().load_baseline()
        except Exception:  # noqa: BLE001 — defensive
            baseline = None
        return _json_response(
            {
                "schema_version": (
                    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
                ),
                "baseline": (
                    baseline.to_dict()
                    if baseline is not None else None
                ),
                "has_baseline": baseline is not None,
            },
        )

    async def handle_history(self, request: Any) -> Any:
        """``GET /observability/invariant-drift/history?limit=N`` —
        recent history snapshots (newest last)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(getattr(request, "query", {}))
        try:
            history = self._store().load_history(limit=limit)
        except Exception:  # noqa: BLE001 — defensive
            history = []
        return _json_response(
            {
                "schema_version": (
                    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
                ),
                "limit": limit,
                "count": len(history),
                "history": [s.to_dict() for s in history],
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/invariant-drift/stats`` — observer
        + bridge counters + composed cadence config."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            obs_stats = self._observer().stats()
        except Exception:  # noqa: BLE001 — defensive
            obs_stats = {}
        try:
            store_stats = self._store().stats()
        except Exception:  # noqa: BLE001 — defensive
            store_stats = {}
        try:
            cadence = {
                "base_interval_s": base_interval_s(),
                "vigilance_ticks": vigilance_ticks(),
                "vigilance_factor": vigilance_factor(),
                "dedup_window": dedup_window(),
                "posture_multipliers": dict(posture_multipliers()),
            }
        except Exception:  # noqa: BLE001 — defensive
            cadence = {}
        return _json_response(
            {
                "schema_version": (
                    INVARIANT_DRIFT_AUDITOR_SCHEMA_VERSION
                ),
                "flags": {
                    "auditor_enabled": (
                        invariant_drift_auditor_enabled()
                    ),
                    "observer_enabled": observer_enabled(),
                    "bridge_enabled": bridge_enabled(),
                },
                "observer_stats": obs_stats,
                "store_stats": store_stats,
                "cadence": cadence,
            },
        )


# ---------------------------------------------------------------------------
# Public API — register_invariant_drift_routes
# ---------------------------------------------------------------------------


def register_invariant_drift_routes(
    app: Any,
    *,
    store: Optional[InvariantDriftStore] = None,
    observer: Optional[InvariantDriftObserver] = None,
    bridge: Optional[InvariantDriftAutoActionBridge] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the invariant-drift GET routes on a caller-supplied
    aiohttp Application. Mirrors ``register_auto_action_routes``.

    Routes:
      * ``GET /observability/invariant-drift``           — overview
      * ``GET /observability/invariant-drift/baseline``  — current
        baseline only
      * ``GET /observability/invariant-drift/history``   — history
      * ``GET /observability/invariant-drift/stats``     — counters
        + cadence config

    Master flag check is done per-request inside the handler so the
    route mounting itself is safe to call regardless of flag state
    (allows live toggle without re-mounting)."""
    handler = _InvariantDriftRoutesHandler(
        store=store,
        observer=observer,
        bridge=bridge,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/invariant-drift",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/invariant-drift/baseline",
        handler.handle_baseline,
    )
    app.router.add_get(
        "/observability/invariant-drift/history",
        handler.handle_history,
    )
    app.router.add_get(
        "/observability/invariant-drift/stats",
        handler.handle_stats,
    )


__all__ = [
    "register_invariant_drift_routes",
]
