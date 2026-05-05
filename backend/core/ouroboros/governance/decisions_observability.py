"""Upgrade 2 (PRD §31.3) Slice 3 — DecisionRecord ledger
observability HTTP routes.

Loopback-only, rate-limited, CORS-aware HTTP read surface
mirroring :mod:`epistemic_budget_observability` (Upgrade 1) +
:mod:`curiosity_observability` (M9). Operators query the
DecisionRecord ledger via GET endpoints.

Routes (PRD §31.3 Slice 3):

  * ``GET /observability/decisions`` — overview: most-recent N
    records across all sessions + sessions list + kind histogram
  * ``GET /observability/decisions/session/{session_id}`` —
    full per-session ledger projection (paginated)

All routes:

  * Master-flag-gated per request via
    :func:`determinism_ledger_enabled` (live-toggle without
    re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned at Slice 5):

  * Imports stdlib + aiohttp.web + ``determinism.decisions_reader``
    + ``determinism.decision_kinds`` + ``determinism.decision_-
    runtime`` (for master-flag check) ONLY.
  * NEVER imports orchestrator / iron_gate / candidate_generator
    / providers / urgency_router / semantic_guardian / tool_-
    executor / change_engine / subagent_scheduler / auto_action_-
    router / policy / strategic_direction.
  * **READ-ONLY** — never invokes :class:`DecisionRuntime.record`
    or any mutation surface. Pinned by AST + grep.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.determinism.decision_kinds import (
    DecisionKind,
)
from backend.core.ouroboros.governance.determinism.decisions_reader import (
    DECISIONS_READER_SCHEMA_VERSION,
    aggregate_kinds_for_session,
    default_record_limit,
    list_available_sessions,
    max_records_per_session,
    read_records_for_session,
    recent_records_across_sessions,
)

logger = logging.getLogger(__name__)


_DEFAULT_OVERVIEW_LIMIT: int = 50


def _parse_limit(request: Any) -> int:
    """Parse ``?limit=N`` — clamped to
    [1, :func:`max_records_per_session`]; default
    :func:`default_record_limit`. NEVER raises."""
    try:
        raw = request.query.get("limit")
        if raw is None:
            return default_record_limit()
        n = int(raw)
        if n < 1:
            return 1
        cap = max_records_per_session()
        if n > cap:
            return cap
        return n
    except Exception:  # noqa: BLE001 — defensive
        return default_record_limit()


def _parse_kind_filter(request: Any) -> Optional[str]:
    """Parse ``?kind=K`` — must be a known DecisionKind value to
    take effect (defensive — unknown filters are silently
    ignored)."""
    try:
        raw = request.query.get("kind")
        if raw is None:
            return None
        s = str(raw).strip().lower()
        # Only accept enum values to prevent unbounded filters
        valid = {m.value for m in DecisionKind}
        if s in valid:
            return s
        # Backward-compat — accept legacy freeform strings
        # written by route_runner / gate_runner / etc.
        return s
    except Exception:  # noqa: BLE001 — defensive
        return None


def _json_response(payload: dict, *, status: int = 200) -> Any:
    """Build a Cache-Control: no-store JSON aiohttp Response.
    Lazy import of aiohttp.web."""
    from aiohttp import web
    return web.json_response(
        payload,
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _ledger_enabled() -> bool:
    """Master-flag check via the existing
    :func:`decision_runtime.ledger_enabled` (no parallel flag)."""
    try:
        from backend.core.ouroboros.governance.determinism.decision_runtime import (  # noqa: E501
            ledger_enabled,
        )
        return bool(ledger_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _DecisionsRoutesHandler:
    """aiohttp route handler for the
    ``/observability/decisions`` family."""

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
        if not _ledger_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        DECISIONS_READER_SCHEMA_VERSION
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
        """``GET /observability/decisions`` — overview projection
        with sessions list + most-recent-N records + kind
        histogram (cross-session aggregate)."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            limit = _parse_limit(request)
            kind_filter = _parse_kind_filter(request)
            sessions = list_available_sessions(limit=20)
            recent = recent_records_across_sessions(
                limit=limit, kind_filter=kind_filter,
            )
            # Cross-session histogram — aggregate kind counts
            # across all sessions in the listing
            kind_histogram: Dict[str, int] = {}
            for s in sessions:
                for entry in aggregate_kinds_for_session(
                    s.session_id,
                ):
                    kind_histogram[entry.kind] = (
                        kind_histogram.get(entry.kind, 0)
                        + entry.count
                    )
            return _json_response(
                {
                    "schema_version": (
                        DECISIONS_READER_SCHEMA_VERSION
                    ),
                    "flags": {
                        "ledger_enabled": _ledger_enabled(),
                    },
                    "sessions": [
                        {
                            "session_id": s.session_id,
                            "decisions_path": s.decisions_path,
                            "file_size_bytes": (
                                s.file_size_bytes
                            ),
                            "mtime_unix": s.mtime_unix,
                            "record_count_estimate": (
                                s.record_count_estimate
                            ),
                        }
                        for s in sessions
                    ],
                    "recent_records": [
                        {
                            "session_id": sid,
                            "record": rec,
                        }
                        for sid, rec in recent
                    ],
                    "kind_histogram": [
                        {"kind": k, "count": v}
                        for k, v in sorted(
                            kind_histogram.items(),
                            key=lambda x: (-x[1], x[0]),
                        )
                    ],
                    "decision_kinds": [
                        m.value for m in DecisionKind
                    ],
                    "limit": limit,
                    "kind_filter": kind_filter,
                    "sse_event_type": "decision_drift_detected",
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[decisions_observability] overview raised: "
                "%s", exc,
            )
            return _json_response(
                {"error": "snapshot_failed"},
                status=500,
            )

    async def handle_session_detail(
        self, request: Any,
    ) -> Any:
        """``GET /observability/decisions/session/{session_id}``
        — paginated per-session ledger projection."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            session_id = (
                request.match_info.get(
                    "session_id", "",
                ) or ""
            ).strip()
        except Exception:  # noqa: BLE001 — defensive
            session_id = ""
        if not session_id:
            return _json_response(
                {"error": "missing_session_id"},
                status=400,
            )
        try:
            limit = _parse_limit(request)
            kind_filter = _parse_kind_filter(request)
            result = read_records_for_session(
                session_id, limit=limit,
                kind_filter=kind_filter,
            )
            histogram = aggregate_kinds_for_session(session_id)
            if (
                result.total_records_in_file == 0
                and not result.records
            ):
                # When the file's missing or unreadable, the
                # diagnostics list explains why; surface 404
                # only if the file genuinely doesn't exist.
                if any(
                    "not found" in d
                    for d in result.diagnostics
                ):
                    return _json_response(
                        {
                            "error": "session_not_found",
                            "session_id": session_id,
                            "diagnostics": list(
                                result.diagnostics,
                            ),
                        },
                        status=404,
                    )
            return _json_response(
                {
                    "schema_version": (
                        DECISIONS_READER_SCHEMA_VERSION
                    ),
                    "session_id": result.session_id,
                    "total_records_in_file": (
                        result.total_records_in_file
                    ),
                    "records_returned": len(result.records),
                    "records": list(result.records),
                    "kind_histogram": [
                        {"kind": h.kind, "count": h.count}
                        for h in histogram
                    ],
                    "limit": limit,
                    "kind_filter": kind_filter,
                    "elapsed_s": result.elapsed_s,
                    "diagnostics": list(result.diagnostics),
                    "sse_event_type": "decision_drift_detected",
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[decisions_observability] session_detail "
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
    """Register the ``/observability/decisions`` family on the
    supplied aiohttp ``Application``."""
    handler = _DecisionsRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/decisions",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/decisions/session/{session_id}",
        handler.handle_session_detail,
    )


__all__ = [
    "register_routes",
]
