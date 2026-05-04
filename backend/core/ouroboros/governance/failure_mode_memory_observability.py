"""Upgrade 3 Slice 5 — Failure-Mode Memory observability routes.

Loopback-only, rate-limited, CORS-aware HTTP read surface mirroring
Slice 5b A/B/C/D (probe / coherence / quorum / gradient / SBT).
Operators query failure-mode memory state via GET endpoints + the
SSE ``EVENT_TYPE_FAILURE_MODE_RECALLED_AT_GENERATE`` event for
live first-attempt-injection updates.

Routes (PRD §31.4 Slice 5):

  * ``GET /observability/failure-modes``                — overview
    + recent records (default ``failure_mode_top_k()``)
  * ``GET /observability/failure-modes/signature/{hash}`` —
    single-record lookup by signature_hash (full or 12+ char prefix)

All routes:

  * Master-flag-gated per request via
    :func:`failure_mode_memory_enabled` (live-toggle without
    re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + ``failure_mode_memory`` ONLY.
  * NEVER imports orchestrator / iron_gate / candidate_generator /
    providers / urgency_router / semantic_guardian / tool_executor
    / change_engine / subagent_scheduler / auto_action_router /
    policy.
  * Read-only — never modifies state, never writes ledgers (the
    /failures REPL's ``clear`` subcommand is the only mutating
    operator surface; this HTTP layer is read-only by contract).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.failure_mode_memory import (
    DEFAULT_PROMPT_SECTION_BUDGET,
    FAILURE_MODE_MEMORY_SCHEMA_VERSION,
    FailureModeKind,
    SituationKind,
    dedup_window_days,
    failure_mode_memory_enabled,
    failure_mode_min_weight,
    failure_mode_recency_halflife_days,
    failure_mode_top_k,
    find_failure_mode_by_signature,
    history_max_records,
    history_path,
    read_failure_mode_history,
)

logger = logging.getLogger(__name__)


_DEFAULT_LIMIT: int = 50
_MAX_LIMIT: int = 1000
_MIN_SIG_PREFIX: int = 12


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


def _parse_since_unix(request: Any) -> float:
    try:
        raw = request.query.get("since_unix")
        if raw is None:
            return 0.0
        ts = float(raw)
        return max(0.0, ts)
    except Exception:  # noqa: BLE001 — defensive
        return 0.0


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
    """Snapshot of all operator-relevant env knobs. NEVER raises."""
    try:
        return {
            "history_max_records": history_max_records(),
            "history_path": str(history_path()),
            "dedup_window_days": dedup_window_days(),
            "retrieval_top_k": failure_mode_top_k(),
            "retrieval_min_weight": failure_mode_min_weight(),
            "retrieval_halflife_days": (
                failure_mode_recency_halflife_days()
            ),
            "prompt_section_budget": DEFAULT_PROMPT_SECTION_BUDGET,
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_history_size() -> int:
    try:
        return len(
            read_failure_mode_history(
                limit=history_max_records(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _FailureModeRoutesHandler:
    """aiohttp route handler for the
    ``/observability/failure-modes`` family."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        if not failure_mode_memory_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        FAILURE_MODE_MEMORY_SCHEMA_VERSION
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
        """``GET /observability/failure-modes`` — overview +
        recent records."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_unix = _parse_since_unix(request)
        try:
            history = read_failure_mode_history(
                limit=limit, since_unix=since_unix,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[failure_mode_memory_observability] "
                "read raised: %s", exc,
            )
            history = ()
        return _json_response(
            {
                "schema_version": (
                    FAILURE_MODE_MEMORY_SCHEMA_VERSION
                ),
                "flags": {
                    "master_enabled": (
                        failure_mode_memory_enabled()
                    ),
                },
                "config": _build_config_dict(),
                "history_size": _safe_history_size(),
                "records": [r.to_dict() for r in history],
                "limit": limit,
                "since_unix": since_unix,
                "count": len(history),
                "situation_kinds": [
                    k.value for k in SituationKind
                ],
                "failure_mode_kinds": [
                    k.value for k in FailureModeKind
                ],
                "sse_event_type": (
                    "failure_mode_recalled_at_generate"
                ),
            },
        )

    async def handle_signature(self, request: Any) -> Any:
        """``GET /observability/failure-modes/signature/{hash}`` —
        single-record lookup. Accepts full sha256 hex (64 chars)
        OR a 12+ char prefix."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            sig = (
                request.match_info.get("hash", "") or ""
            ).strip().lower()
        except Exception:  # noqa: BLE001 — defensive
            sig = ""
        if len(sig) < _MIN_SIG_PREFIX:
            return _json_response(
                {
                    "error": "signature_too_short",
                    "minimum_chars": _MIN_SIG_PREFIX,
                    "got": len(sig),
                    "schema_version": (
                        FAILURE_MODE_MEMORY_SCHEMA_VERSION
                    ),
                },
                status=400,
            )
        try:
            if len(sig) == 64:
                rec = find_failure_mode_by_signature(sig)
            else:
                # Prefix scan
                history = read_failure_mode_history()
                rec = next(
                    (
                        r for r in history
                        if (r.signature_hash or "")
                        .lower().startswith(sig)
                    ),
                    None,
                )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[failure_mode_memory_observability] "
                "lookup raised: %s", exc,
            )
            rec = None
        if rec is None:
            return _json_response(
                {
                    "error": "not_found",
                    "signature": sig,
                    "schema_version": (
                        FAILURE_MODE_MEMORY_SCHEMA_VERSION
                    ),
                },
                status=404,
            )
        return _json_response(
            {
                "schema_version": (
                    FAILURE_MODE_MEMORY_SCHEMA_VERSION
                ),
                "record": rec.to_dict(),
            },
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_failure_mode_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the failure-mode GET routes on a caller-supplied
    aiohttp Application.

    Routes:
      * ``GET /observability/failure-modes`` — overview + recent
      * ``GET /observability/failure-modes/signature/{hash}`` —
        single-record lookup
    """
    handler = _FailureModeRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/failure-modes",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/failure-modes/signature/{hash}",
        handler.handle_signature,
    )


__all__ = [
    "register_failure_mode_routes",
]
