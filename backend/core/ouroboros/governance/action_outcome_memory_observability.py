"""M11 Slice 5 — ActionOutcomeMemory observability HTTP routes.

Loopback-only, rate-limited, CORS-aware HTTP read surface
mirroring :mod:`failure_mode_memory_observability` (Upgrade 3
Slice 5). Operators query action-outcome memory state via GET
endpoints + the SSE
``EVENT_TYPE_ACTION_OUTCOME_RECALLED_AT_GENERATE`` event for live
first-attempt-injection updates.

Routes (PRD §30.5.3 Slice 5):

  * ``GET /observability/action-outcomes`` — overview + recent
    records (default ``action_outcome_top_k()``)
  * ``GET /observability/action-outcomes/cluster/{id}`` — per-
    cluster records (or ``_global`` for the fallback bucket)

All routes:

  * Master-flag-gated per request via
    :func:`action_outcome_memory_enabled` (live-toggle without
    re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by companion tests):

  * Imports stdlib + aiohttp.web + ``action_outcome_memory`` ONLY.
  * NEVER imports orchestrator / iron_gate / candidate_generator /
    providers / urgency_router / semantic_guardian / tool_executor
    / change_engine / subagent_scheduler / auto_action_router /
    policy.
  * Read-only — never modifies state, never writes ledgers.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.action_outcome_memory import (
    ACTION_OUTCOME_MEMORY_SCHEMA_VERSION,
    DEFAULT_ACTION_OUTCOME_PROMPT_BUDGET,
    OutcomeKind,
    action_outcome_memory_enabled,
    action_outcome_min_weight,
    action_outcome_polarity_mode,
    action_outcome_recency_halflife_days,
    action_outcome_top_k,
    cluster_jsonl_path,
    dedup_window_days,
    history_dir,
    max_records_per_cluster,
    read_action_outcomes_for_cluster,
    read_all_action_outcomes,
)

logger = logging.getLogger(__name__)


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
            "max_records_per_cluster": (
                max_records_per_cluster()
            ),
            "history_dir": str(history_dir()),
            "dedup_window_days": dedup_window_days(),
            "retrieval_top_k": action_outcome_top_k(),
            "retrieval_min_weight": action_outcome_min_weight(),
            "retrieval_halflife_days": (
                action_outcome_recency_halflife_days()
            ),
            "polarity_mode": action_outcome_polarity_mode(),
            "prompt_section_budget": (
                DEFAULT_ACTION_OUTCOME_PROMPT_BUDGET
            ),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_history_size() -> int:
    try:
        return len(
            read_all_action_outcomes(
                limit=50 * max_records_per_cluster(),
            ),
        )
    except Exception:  # noqa: BLE001 — defensive
        return 0


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _ActionOutcomeRoutesHandler:
    """aiohttp route handler for the
    ``/observability/action-outcomes`` family."""

    def __init__(
        self,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _gate(self, request: Any) -> Optional[Any]:
        if not action_outcome_memory_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
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
        """``GET /observability/action-outcomes`` — overview +
        recent records."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        limit = _parse_limit(request)
        since_unix = _parse_since_unix(request)
        try:
            history = read_all_action_outcomes(
                limit=limit, since_unix=since_unix,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[action_outcome_memory_observability] "
                "read raised: %s", exc,
            )
            history = ()
        return _json_response(
            {
                "schema_version": (
                    ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
                ),
                "flags": {
                    "master_enabled": (
                        action_outcome_memory_enabled()
                    ),
                },
                "config": _build_config_dict(),
                "history_size": _safe_history_size(),
                "records": [r.to_dict() for r in history],
                "limit": limit,
                "since_unix": since_unix,
                "count": len(history),
                "outcome_kinds": [
                    k.value for k in OutcomeKind
                ],
                "sse_event_type": (
                    "action_outcome_recalled_at_generate"
                ),
            },
        )

    async def handle_cluster(self, request: Any) -> Any:
        """``GET /observability/action-outcomes/cluster/{id}`` —
        per-cluster records."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            cluster_id = (
                request.match_info.get("id", "") or ""
            ).strip()
        except Exception:  # noqa: BLE001 — defensive
            cluster_id = ""
        limit = _parse_limit(request)
        since_unix = _parse_since_unix(request)
        try:
            records = read_action_outcomes_for_cluster(
                cluster_id, limit=limit, since_unix=since_unix,
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[action_outcome_memory_observability] "
                "cluster lookup raised: %s", exc,
            )
            records = ()
        return _json_response(
            {
                "schema_version": (
                    ACTION_OUTCOME_MEMORY_SCHEMA_VERSION
                ),
                "cluster_id": cluster_id or "_global",
                "cluster_path": str(
                    cluster_jsonl_path(cluster_id),
                ),
                "records": [r.to_dict() for r in records],
                "limit": limit,
                "since_unix": since_unix,
                "count": len(records),
            },
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def register_action_outcome_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount the action-outcome GET routes on a caller-supplied
    aiohttp Application.

    Routes:
      * ``GET /observability/action-outcomes`` — overview + recent
      * ``GET /observability/action-outcomes/cluster/{id}`` —
        per-cluster lookup
    """
    handler = _ActionOutcomeRoutesHandler(
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/action-outcomes",
        handler.handle_overview,
    )
    app.router.add_get(
        "/observability/action-outcomes/cluster/{id}",
        handler.handle_cluster,
    )


__all__ = [
    "register_action_outcome_routes",
]
