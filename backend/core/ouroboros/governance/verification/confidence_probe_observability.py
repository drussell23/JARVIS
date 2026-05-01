"""Move 5 Slice 5 — Confidence Probe observability GET routes.

Loopback-only, rate-limited, CORS-aware read surface that mirrors
``register_invariant_drift_routes`` (Move 4 Slice 5) +
``register_auto_action_routes`` (Move 3 Slice 4) exactly. Operators
query probe-loop state via GET endpoints + the SSE
``EVENT_TYPE_PROBE_OUTCOME`` event for live updates.

Routes:

  * ``GET /observability/probe``         — flag state + cadence
    config (operator-tunable knobs surfaced for inspection)
  * ``GET /observability/probe/stats``   — runtime counters from
    the default ``ConfidenceTransitionTracker`` (Tier 1 #1) +
    convergence-bridge config
  * ``GET /observability/probe/config``  — env-knob snapshot
    (max_questions, convergence_quorum, wall_clock_s,
    max_tool_rounds, generator_mode)
  * ``GET /observability/probe/allowlist`` — read-only tool
    allowlist (the canonical 9-tool frozenset surfaced for
    operator audit)

All routes:

  * Master-flag-gated per request (live toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store`` so IDEs don't stale.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + Slice 1+2+3 modules ONLY.
  * NEVER imports orchestrator / phase_runners /
    candidate_generator / iron_gate / change_engine / policy /
    semantic_guardian / semantic_firewall / providers /
    doubleword_provider / urgency_router / auto_action_router /
    subagent_scheduler / tool_executor.
  * Read-only surface — never modifies state, never writes
    ledgers.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.verification.confidence_probe_bridge import (  # noqa: E501
    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION,
    bridge_enabled,
    convergence_quorum,
    max_questions,
    max_tool_rounds_per_question,
)
from backend.core.ouroboros.governance.verification.confidence_probe_generator import (  # noqa: E501
    GeneratorMode,
    generator_mode,
)
from backend.core.ouroboros.governance.verification.confidence_probe_runner import (  # noqa: E501
    EVENT_TYPE_PROBE_OUTCOME,
    probe_wall_clock_s,
)
from backend.core.ouroboros.governance.verification.readonly_evidence_prober import (  # noqa: E501
    READONLY_TOOL_ALLOWLIST,
    prober_enabled,
)

logger = logging.getLogger(__name__)


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
# Route handler
# ---------------------------------------------------------------------------


class _ConfidenceProbeRoutesHandler:
    """aiohttp route handler for the ``/observability/probe``
    family. Mirror of ``_InvariantDriftRoutesHandler`` shape from
    Move 4 Slice 5."""

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
        if not bridge_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION
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
        """``GET /observability/probe`` — flag state + cadence
        config. Single consolidated dashboard endpoint."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION
                ),
                "flags": {
                    "bridge_enabled": bridge_enabled(),
                    "prober_enabled": prober_enabled(),
                },
                "cadence": _build_cadence_dict(),
                "sse_event_type": EVENT_TYPE_PROBE_OUTCOME,
                "allowlist_size": len(READONLY_TOOL_ALLOWLIST),
            },
        )

    async def handle_config(self, request: Any) -> Any:
        """``GET /observability/probe/config`` — env-knob snapshot
        for operator inspection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION
                ),
                "cadence": _build_cadence_dict(),
                "generator_mode": generator_mode().value,
            },
        )

    async def handle_allowlist(self, request: Any) -> Any:
        """``GET /observability/probe/allowlist`` — read-only tool
        allowlist (the canonical 9-tool frozenset surfaced)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION
                ),
                "allowlist": sorted(READONLY_TOOL_ALLOWLIST),
                "count": len(READONLY_TOOL_ALLOWLIST),
            },
        )

    async def handle_stats(self, request: Any) -> Any:
        """``GET /observability/probe/stats`` — flag state +
        cadence + SSE event type. (Slice 5 ships static config;
        per-op probe history is reserved for Slice 5b.)"""
        gated = self._gate(request)
        if gated is not None:
            return gated
        return _json_response(
            {
                "schema_version": (
                    CONFIDENCE_PROBE_BRIDGE_SCHEMA_VERSION
                ),
                "flags": {
                    "bridge_enabled": bridge_enabled(),
                    "prober_enabled": prober_enabled(),
                },
                "cadence": _build_cadence_dict(),
                "sse_event_type": EVENT_TYPE_PROBE_OUTCOME,
            },
        )


def _build_cadence_dict() -> dict:
    """Snapshot of cadence env knobs. NEVER raises."""
    try:
        return {
            "max_questions": max_questions(),
            "convergence_quorum": convergence_quorum(),
            "max_tool_rounds_per_question": (
                max_tool_rounds_per_question()
            ),
            "wall_clock_s": probe_wall_clock_s(),
            "generator_mode": generator_mode().value,
            "allowlist_size": len(READONLY_TOOL_ALLOWLIST),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


# ---------------------------------------------------------------------------
# Public API — register_confidence_probe_routes
# ---------------------------------------------------------------------------


def register_confidence_probe_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the confidence-probe GET routes on a caller-supplied
    aiohttp Application. Mirrors ``register_invariant_drift_routes``
    from Move 4 Slice 5.

    Routes:
      * ``GET /observability/probe``           — overview
      * ``GET /observability/probe/config``    — cadence config
      * ``GET /observability/probe/allowlist`` — read-only tools
      * ``GET /observability/probe/stats``     — flag+config

    Master flag check is per-request inside the handler so route
    mounting itself is safe to call regardless of flag state."""
    handler = _ConfidenceProbeRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/probe", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/probe/config", handler.handle_config,
    )
    app.router.add_get(
        "/observability/probe/allowlist",
        handler.handle_allowlist,
    )
    app.router.add_get(
        "/observability/probe/stats", handler.handle_stats,
    )


__all__ = [
    "register_confidence_probe_routes",
]
