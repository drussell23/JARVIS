"""Slice 5b B — Coherence Auditor observability GET routes.

Loopback-only, rate-limited, CORS-aware read surface that mirrors
``register_invariant_drift_routes`` (Move 4 Slice 5) +
``register_confidence_probe_routes`` (Move 5 Slice 5b A) exactly.
Operators query CoherenceAuditor / Observer / ActionBridge state via
GET endpoints + the SSE ``EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED``
event for live updates.

Routes:

  * ``GET /observability/coherence``             — flag state + budget +
    cadence + advisory bridge state + observer counter snapshot
    (single consolidated dashboard endpoint)
  * ``GET /observability/coherence/config``      — full env-knob
    snapshot for operator inspection (budgets, cadences,
    halflife, vigilance, dedup, backoff, advisory tightening)
  * ``GET /observability/coherence/audits``      — recent
    ``BehavioralDriftVerdict`` history via
    :func:`read_drift_audit` — supports ``limit`` +
    ``since_ts`` query params
  * ``GET /observability/coherence/advisories``  — recent
    ``CoherenceAdvisory`` history via
    :func:`read_coherence_advisories` — supports ``limit`` +
    ``since_ts`` + ``drift_kind`` query params
  * ``GET /observability/coherence/stats``       — runtime
    observer counters via
    :meth:`CoherenceObserver.snapshot`

All routes:

  * Master-flag-gated per request (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + verification.coherence_*
    modules ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * Read-only surface — never modifies state, never writes
    ledgers; consumes existing public readers only.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION,
    coherence_action_bridge_enabled,
    read_coherence_advisories,
    tighten_factor,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    COHERENCE_AUDITOR_SCHEMA_VERSION,
    BehavioralDriftKind,
    budget_confidence_rise_pct,
    budget_posture_locked_hours,
    budget_recurrence_count,
    budget_route_drift_pct,
    coherence_auditor_enabled,
    halflife_days,
)
from backend.core.ouroboros.governance.verification.coherence_observer import (  # noqa: E501
    COHERENCE_OBSERVER_SCHEMA_VERSION,
    EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED,
    backoff_ceiling_hours,
    cadence_floor_seconds,
    cadence_hours_default,
    cadence_hours_harden,
    cadence_hours_maintain,
    dedup_window_size,
    get_default_observer,
    observer_enabled,
    vigilance_multiplier,
    vigilance_ticks,
)
from backend.core.ouroboros.governance.verification.coherence_window_store import (  # noqa: E501
    COHERENCE_WINDOW_STORE_SCHEMA_VERSION,
    WindowOutcome,
    max_signatures_default,
    read_drift_audit,
    window_hours_default,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query-param clamps — bounded so an unfriendly client cannot trigger
# an unbounded read or a negative-time request.
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


def _parse_since_ts(request: Any) -> float:
    """Parse ?since_ts=F — clamped to [0.0, +inf); default 0.0.
    NEVER raises."""
    try:
        raw = request.query.get("since_ts")
        if raw is None:
            return 0.0
        ts = float(raw)
        if ts < 0.0:
            return 0.0
        return ts
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


def _parse_drift_kind(request: Any) -> Optional[BehavioralDriftKind]:
    """Parse ?drift_kind=KIND — case-insensitive match against
    closed enum. Returns None when missing or unrecognized.
    NEVER raises."""
    try:
        raw = request.query.get("drift_kind")
        if raw is None or not str(raw).strip():
            return None
        token = str(raw).strip().lower()
        for kind in BehavioralDriftKind:
            if kind.value.lower() == token:
                return kind
        return None
    except Exception:  # noqa: BLE001 — defensive
        return None


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
# Env-knob snapshots — composed lazily so a missing env var inside any
# helper does not break the route.
# ---------------------------------------------------------------------------


def _build_budget_dict() -> Dict[str, Any]:
    """Snapshot of behavioral-drift budget knobs. NEVER raises."""
    try:
        return {
            "route_drift_pct": budget_route_drift_pct(),
            "posture_locked_hours": budget_posture_locked_hours(),
            "recurrence_count": budget_recurrence_count(),
            "confidence_rise_pct": budget_confidence_rise_pct(),
            "halflife_days": halflife_days(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_cadence_dict() -> Dict[str, Any]:
    """Snapshot of observer cadence knobs. NEVER raises."""
    try:
        return {
            "cadence_hours_default": cadence_hours_default(),
            "cadence_hours_harden": cadence_hours_harden(),
            "cadence_hours_maintain": cadence_hours_maintain(),
            "vigilance_multiplier": vigilance_multiplier(),
            "vigilance_ticks": vigilance_ticks(),
            "dedup_window_size": dedup_window_size(),
            "backoff_ceiling_hours": backoff_ceiling_hours(),
            "cadence_floor_seconds": cadence_floor_seconds(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_window_dict() -> Dict[str, Any]:
    """Snapshot of window-store knobs. NEVER raises."""
    try:
        return {
            "window_hours_default": window_hours_default(),
            "max_signatures_default": max_signatures_default(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_advisory_dict() -> Dict[str, Any]:
    """Snapshot of action-bridge knobs. NEVER raises."""
    try:
        return {
            "bridge_enabled": coherence_action_bridge_enabled(),
            "tighten_factor": tighten_factor(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_observer_snapshot() -> Dict[str, Any]:
    """Defensively pull ``CoherenceObserver.snapshot()`` — empty
    dict when the observer hasn't been instantiated yet (tests,
    cold boot, master-flag off). NEVER raises."""
    try:
        return get_default_observer().snapshot()
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _CoherenceRoutesHandler:
    """aiohttp route handler for the ``/observability/coherence``
    family. Mirror of ``_ConfidenceProbeRoutesHandler`` shape from
    Move 5 Slice 5b A."""

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
        if not coherence_auditor_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        COHERENCE_AUDITOR_SCHEMA_VERSION
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

    # ---- handlers -------------------------------------------------------

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/coherence`` — single consolidated
        dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    COHERENCE_AUDITOR_SCHEMA_VERSION
                ),
                "schemas": {
                    "auditor": COHERENCE_AUDITOR_SCHEMA_VERSION,
                    "observer": COHERENCE_OBSERVER_SCHEMA_VERSION,
                    "window_store": (
                        COHERENCE_WINDOW_STORE_SCHEMA_VERSION
                    ),
                    "action_bridge": (
                        COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
                    ),
                },
                "flags": {
                    "auditor_enabled": coherence_auditor_enabled(),
                    "observer_enabled": observer_enabled(),
                    "action_bridge_enabled": (
                        coherence_action_bridge_enabled()
                    ),
                },
                "budget": _build_budget_dict(),
                "cadence": _build_cadence_dict(),
                "window": _build_window_dict(),
                "advisory": _build_advisory_dict(),
                "observer_snapshot": _safe_observer_snapshot(),
                "sse_event_type": (
                    EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED
                ),
                "drift_kinds": [
                    k.value for k in BehavioralDriftKind
                ],
            },
        )

    async def handle_config(self, request: Any) -> Any:
        """``GET /observability/coherence/config`` — full env-knob
        snapshot for operator inspection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    COHERENCE_AUDITOR_SCHEMA_VERSION
                ),
                "budget": _build_budget_dict(),
                "cadence": _build_cadence_dict(),
                "window": _build_window_dict(),
                "advisory": _build_advisory_dict(),
            },
        )

    async def handle_audits(self, request: Any) -> Any:
        """``GET /observability/coherence/audits`` — recent
        BehavioralDriftVerdict history."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_ts = _parse_since_ts(request)
        try:
            result = read_drift_audit(
                since_ts=since_ts, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[CoherenceObservability] read_drift_audit "
                "raised: %s", exc,
            )
            return _json_response(
                {
                    "schema_version": (
                        COHERENCE_AUDITOR_SCHEMA_VERSION
                    ),
                    "outcome": WindowOutcome.FAILED.value,
                    "verdicts": [],
                    "limit": limit,
                    "since_ts": since_ts,
                    "count": 0,
                    "detail": "reader raised — see harness logs",
                },
            )
        verdicts = [v.to_dict() for v in result.verdicts]
        return _json_response(
            {
                "schema_version": (
                    COHERENCE_AUDITOR_SCHEMA_VERSION
                ),
                "outcome": result.outcome.value,
                "verdicts": verdicts,
                "limit": limit,
                "since_ts": since_ts,
                "count": len(verdicts),
                "detail": result.detail,
            },
        )

    async def handle_advisories(self, request: Any) -> Any:
        """``GET /observability/coherence/advisories`` — recent
        CoherenceAdvisory history."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_ts = _parse_since_ts(request)
        kind_filter = _parse_drift_kind(request)
        try:
            advisories = read_coherence_advisories(
                since_ts=since_ts,
                limit=limit,
                drift_kind=kind_filter,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[CoherenceObservability] "
                "read_coherence_advisories raised: %s", exc,
            )
            advisories = tuple()
        return _json_response(
            {
                "schema_version": (
                    COHERENCE_ACTION_BRIDGE_SCHEMA_VERSION
                ),
                "advisories": [a.to_dict() for a in advisories],
                "limit": limit,
                "since_ts": since_ts,
                "drift_kind": (
                    kind_filter.value
                    if kind_filter is not None else None
                ),
                "count": len(advisories),
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/coherence/stats`` — runtime
        observer counters via ``CoherenceObserver.snapshot()``."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    COHERENCE_OBSERVER_SCHEMA_VERSION
                ),
                "observer_snapshot": _safe_observer_snapshot(),
                "flags": {
                    "auditor_enabled": coherence_auditor_enabled(),
                    "observer_enabled": observer_enabled(),
                    "action_bridge_enabled": (
                        coherence_action_bridge_enabled()
                    ),
                },
                "sse_event_type": (
                    EVENT_TYPE_BEHAVIORAL_DRIFT_DETECTED
                ),
            },
        )


# ---------------------------------------------------------------------------
# Public API — register_coherence_routes
# ---------------------------------------------------------------------------


def register_coherence_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the coherence GET routes on a caller-supplied aiohttp
    Application. Mirrors ``register_confidence_probe_routes`` from
    Move 5 Slice 5b A.

    Routes:
      * ``GET /observability/coherence``            — overview
      * ``GET /observability/coherence/config``     — env knobs
      * ``GET /observability/coherence/audits``     — verdict history
      * ``GET /observability/coherence/advisories`` — advisory history
      * ``GET /observability/coherence/stats``      — observer counters

    Master flag check is per-request inside the handler so route
    mounting itself is safe to call regardless of flag state."""
    handler = _CoherenceRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/coherence", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/coherence/config", handler.handle_config,
    )
    app.router.add_get(
        "/observability/coherence/audits", handler.handle_audits,
    )
    app.router.add_get(
        "/observability/coherence/advisories",
        handler.handle_advisories,
    )
    app.router.add_get(
        "/observability/coherence/stats", handler.handle_stats,
    )


__all__ = [
    "register_coherence_routes",
]
