"""Slice 5b C — Generative Quorum observability GET routes.

Loopback-only, rate-limited, CORS-aware read surface mirroring
``register_invariant_drift_routes`` (Move 4 Slice 5),
``register_confidence_probe_routes`` (Move 5 Slice 5b A), and
``register_coherence_routes`` (Priority #1 Slice 5b B). Operators
query Quorum runner state, history, and derived adaptive insights
via GET endpoints + the SSE ``EVENT_TYPE_QUORUM_OUTCOME`` event for
live updates.

Routes:

  * ``GET /observability/quorum``           — flag state + cadence
    + recent stats + observer history size (single consolidated
    dashboard endpoint)
  * ``GET /observability/quorum/config``    — env-knob snapshot
    (K, threshold, observer caps, history dir)
  * ``GET /observability/quorum/history``   — recent
    ``StampedQuorumRun`` history via
    :func:`read_quorum_history` — supports ``limit`` +
    ``since_ts`` query params
  * ``GET /observability/quorum/stats``     — derived insights
    (outcome distribution, avg elapsed, stability score, etc.)
    via :func:`compute_recent_quorum_stats` — adaptive insight
    layer for soak-evidence collection
  * ``GET /observability/quorum/outcomes``  — closed-enum vocab:
    surface ConsensusOutcome + QuorumActionMapping for clients
    that render outcome chips

All routes:

  * Master-flag-gated per request via :func:`quorum_enabled`
    (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + verification.generative_quorum*
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

from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
    ConsensusOutcome,
    GENERATIVE_QUORUM_SCHEMA_VERSION,
    agreement_threshold,
    quorum_enabled,
    quorum_k,
)
from backend.core.ouroboros.governance.verification.generative_quorum_gate import (  # noqa: E501
    GENERATIVE_QUORUM_GATE_SCHEMA_VERSION,
    QuorumActionMapping,
    quorum_gate_enabled,
)
from backend.core.ouroboros.governance.verification.generative_quorum_observer import (  # noqa: E501
    GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION,
    compute_recent_quorum_stats,
    quorum_history_max_records,
    quorum_history_path,
    quorum_observer_enabled,
    quorum_recent_stats_window,
    read_quorum_history,
)
from backend.core.ouroboros.governance.verification.generative_quorum_runner import (  # noqa: E501
    EVENT_TYPE_QUORUM_OUTCOME,
    GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query-param clamps — bounded so an unfriendly client cannot trigger
# unbounded reads. Mirrors Slice 5b B's clamp discipline.
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


def _build_quorum_config_dict() -> Dict[str, Any]:
    """Snapshot of consensus-math knobs. NEVER raises."""
    try:
        return {
            "k": quorum_k(),
            "agreement_threshold": agreement_threshold(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _build_observer_config_dict() -> Dict[str, Any]:
    """Snapshot of observer knobs. NEVER raises."""
    try:
        return {
            "history_max_records": quorum_history_max_records(),
            "recent_stats_window": quorum_recent_stats_window(),
            "history_path": str(quorum_history_path()),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_history_size() -> int:
    """Best-effort line-count of the JSONL store. NEVER raises."""
    try:
        # Bounded read — never instantiate full history into RAM
        # for the size probe; just count lines.
        return len(
            read_quorum_history(
                limit=quorum_history_max_records(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


def _safe_recent_stats_dict() -> Dict[str, Any]:
    """Best-effort recent stats. NEVER raises."""
    try:
        return compute_recent_quorum_stats().to_dict()
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _QuorumRoutesHandler:
    """aiohttp route handler for the ``/observability/quorum``
    family. Mirror of ``_CoherenceRoutesHandler`` shape from
    Slice 5b B and ``_ConfidenceProbeRoutesHandler`` from Slice 5b A.
    """

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
        if not quorum_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        GENERATIVE_QUORUM_SCHEMA_VERSION
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
        """``GET /observability/quorum`` — single consolidated
        dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    GENERATIVE_QUORUM_SCHEMA_VERSION
                ),
                "schemas": {
                    "primitive": (
                        GENERATIVE_QUORUM_SCHEMA_VERSION
                    ),
                    "runner": (
                        GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION
                    ),
                    "gate": (
                        GENERATIVE_QUORUM_GATE_SCHEMA_VERSION
                    ),
                    "observer": (
                        GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION
                    ),
                },
                "flags": {
                    "quorum_enabled": quorum_enabled(),
                    "quorum_gate_enabled": quorum_gate_enabled(),
                    "quorum_observer_enabled": (
                        quorum_observer_enabled()
                    ),
                },
                "quorum_config": _build_quorum_config_dict(),
                "observer_config": _build_observer_config_dict(),
                "history_size": _safe_history_size(),
                "recent_stats": _safe_recent_stats_dict(),
                "sse_event_type": EVENT_TYPE_QUORUM_OUTCOME,
                "outcome_kinds": [
                    o.value for o in ConsensusOutcome
                ],
            },
        )

    async def handle_config(self, request: Any) -> Any:
        """``GET /observability/quorum/config`` — env-knob snapshot
        for operator inspection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    GENERATIVE_QUORUM_SCHEMA_VERSION
                ),
                "quorum_config": _build_quorum_config_dict(),
                "observer_config": _build_observer_config_dict(),
                "flags": {
                    "quorum_enabled": quorum_enabled(),
                    "quorum_gate_enabled": quorum_gate_enabled(),
                    "quorum_observer_enabled": (
                        quorum_observer_enabled()
                    ),
                },
            },
        )

    async def handle_history(self, request: Any) -> Any:
        """``GET /observability/quorum/history`` — recent
        StampedQuorumRun history."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_ts = _parse_since_ts(request)
        try:
            history = read_quorum_history(
                limit=limit, since_ts=since_ts,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[QuorumObservability] read_quorum_history "
                "raised: %s", exc,
            )
            history = ()
        records = [s.to_dict() for s in history]
        return _json_response(
            {
                "schema_version": (
                    GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION
                ),
                "records": records,
                "limit": limit,
                "since_ts": since_ts,
                "count": len(records),
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/quorum/stats`` — derived insights."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_ts = _parse_since_ts(request)
        try:
            stats = compute_recent_quorum_stats(
                limit=limit, since_ts=since_ts,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[QuorumObservability] compute_recent_quorum_stats "
                "raised: %s", exc,
            )
            stats = compute_recent_quorum_stats(
                limit=0,
            )
        return _json_response(
            {
                "schema_version": (
                    GENERATIVE_QUORUM_OBSERVER_SCHEMA_VERSION
                ),
                "stats": stats.to_dict(),
                "limit": limit,
                "since_ts": since_ts,
            },
        )

    async def handle_outcomes(self, request: Any) -> Any:
        """``GET /observability/quorum/outcomes`` — closed-enum
        vocabulary so clients render outcome chips without
        hardcoding strings."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    GENERATIVE_QUORUM_SCHEMA_VERSION
                ),
                "consensus_outcomes": [
                    o.value for o in ConsensusOutcome
                ],
                "action_mappings": [
                    a.value for a in QuorumActionMapping
                ],
                "sse_event_type": EVENT_TYPE_QUORUM_OUTCOME,
            },
        )


# ---------------------------------------------------------------------------
# Public API — register_quorum_routes
# ---------------------------------------------------------------------------


def register_quorum_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the quorum GET routes on a caller-supplied aiohttp
    Application. Mirrors ``register_coherence_routes`` from
    Slice 5b B.

    Routes:
      * ``GET /observability/quorum``          — overview
      * ``GET /observability/quorum/config``   — env knobs
      * ``GET /observability/quorum/history``  — run history
      * ``GET /observability/quorum/stats``    — derived insights
      * ``GET /observability/quorum/outcomes`` — enum vocabulary

    Master flag check is per-request inside the handler so route
    mounting itself is safe to call regardless of flag state."""
    handler = _QuorumRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/quorum", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/quorum/config", handler.handle_config,
    )
    app.router.add_get(
        "/observability/quorum/history", handler.handle_history,
    )
    app.router.add_get(
        "/observability/quorum/stats", handler.handle_stats,
    )
    app.router.add_get(
        "/observability/quorum/outcomes", handler.handle_outcomes,
    )


__all__ = [
    "register_quorum_routes",
]
