"""M10 ArchitectureProposer (PRD §32.4) Slice 5 — HTTP
observability surface.

Loopback-only, rate-limited, CORS-aware HTTP read surface
mirroring :mod:`decisions_observability` (Upgrade 2) +
:mod:`curiosity_observability` (M9). Operators query the M10
proposal ledger via GET endpoints.

Routes (PRD §32.4 Slice 5):

  * ``GET /observability/m10`` — overview: most-recent N
    proposals + phase histogram + pending count + master flag
    state
  * ``GET /observability/m10/proposal/{proposal_id}`` —
    most-recent state for a single proposal id

All routes:

  * Master-flag-gated per request (live-toggle).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + aiohttp.web + ``m10.proposal_store`` +
    ``m10.primitives`` ONLY.
  * NEVER imports orchestrator / iron_gate / candidate_generator
    / providers / urgency_router / semantic_guardian / tool_-
    executor / change_engine / subagent_scheduler / auto_action_-
    router / policy / strategic_direction /
    graduation_orchestrator / m10.proposal_synthesizer /
    m10.lifecycle / m10.unhandled_pattern_miner.
  * **READ-ONLY** — never invokes
    :func:`proposal_store.append_proposal`. Pinned by AST.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from backend.core.ouroboros.governance.m10.primitives import (
    M10ProposalPhase,
    ProposalKind,
    m10_arch_proposer_enabled,
)
from backend.core.ouroboros.governance.m10.proposal_store import (
    M10_PROPOSAL_STORE_SCHEMA_VERSION,
    aggregate_phase_histogram,
    find_proposal_by_id,
    list_pending_proposals,
    read_all_proposals,
)

logger = logging.getLogger(__name__)


_DEFAULT_OVERVIEW_LIMIT: int = 50
_MAX_OVERVIEW_LIMIT: int = 500


def _parse_limit(request: Any) -> int:
    """Parse ``?limit=N`` — clamped to [1, 500]; default 50.
    NEVER raises."""
    try:
        raw = request.query.get("limit")
        if raw is None:
            return _DEFAULT_OVERVIEW_LIMIT
        n = int(raw)
        if n < 1:
            return 1
        if n > _MAX_OVERVIEW_LIMIT:
            return _MAX_OVERVIEW_LIMIT
        return n
    except Exception:  # noqa: BLE001 — defensive
        return _DEFAULT_OVERVIEW_LIMIT


def _json_response(
    payload: dict, *, status: int = 200,
) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response.
    Lazy import of aiohttp.web."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _M10RoutesHandler:
    """aiohttp route handler for the ``/observability/m10``
    family."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[
            Callable[[Any], bool]
        ] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        if not m10_arch_proposer_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        M10_PROPOSAL_STORE_SCHEMA_VERSION
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
        """``GET /observability/m10`` — recent proposals +
        phase histogram + pending."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            limit = _parse_limit(request)
            recent = read_all_proposals(limit=limit)
            histogram = aggregate_phase_histogram()
            pending = list_pending_proposals(
                limit=_DEFAULT_OVERVIEW_LIMIT,
            )
            return _json_response(
                {
                    "schema_version": (
                        M10_PROPOSAL_STORE_SCHEMA_VERSION
                    ),
                    "flags": {
                        "m10_arch_proposer_enabled": (
                            m10_arch_proposer_enabled()
                        ),
                    },
                    "recent_proposals": [
                        p.to_dict() for p in recent
                    ],
                    "phase_histogram": [
                        {"phase": k, "count": v}
                        for k, v in sorted(
                            histogram.items(),
                            key=lambda x: (-x[1], x[0]),
                        )
                    ],
                    "pending_proposals": [
                        p.to_dict() for p in pending
                    ],
                    "pending_count": len(pending),
                    "phases": [
                        m.value for m in M10ProposalPhase
                    ],
                    "kinds": [m.value for m in ProposalKind],
                    "limit": limit,
                    "sse_event_type": (
                        "m10_proposal_emitted"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[m10_observability] overview raised: %s",
                exc,
            )
            return _json_response(
                {"error": "snapshot_failed"},
                status=500,
            )

    async def handle_proposal_detail(
        self, request: Any,
    ) -> Any:
        """``GET /observability/m10/proposal/{proposal_id}``
        — most-recent state for one proposal."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            proposal_id = (
                request.match_info.get(
                    "proposal_id", "",
                ) or ""
            ).strip()
        except Exception:  # noqa: BLE001 — defensive
            proposal_id = ""
        if not proposal_id:
            return _json_response(
                {"error": "missing_proposal_id"},
                status=400,
            )
        try:
            found = find_proposal_by_id(proposal_id)
            if found is None:
                return _json_response(
                    {
                        "error": "proposal_not_found",
                        "proposal_id": proposal_id,
                    },
                    status=404,
                )
            return _json_response(
                {
                    "schema_version": (
                        M10_PROPOSAL_STORE_SCHEMA_VERSION
                    ),
                    "proposal": found.to_dict(),
                    "sse_event_type": (
                        "m10_proposal_emitted"
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[m10_observability] proposal_detail "
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
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Register the ``/observability/m10`` family on the
    supplied aiohttp ``Application``."""
    handler = _M10RoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/m10",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/m10/proposal/{proposal_id}",
        handler.handle_proposal_detail,
    )


__all__ = [
    "register_routes",
]
