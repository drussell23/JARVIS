"""Upgrade 1 Slice 4 — EpistemicBudget observability HTTP routes
(PRD §31.2).

Loopback-only, rate-limited, CORS-aware HTTP read surface
mirroring :mod:`action_outcome_memory_observability` (M11) and
:mod:`failure_mode_memory_observability` (Upgrade 3). Operators
query per-op budget state via GET endpoints + the SSE
``EVENT_TYPE_BUDGET_ACTION_TAKEN`` event for live updates.

Routes (PRD §31.2 Slice 4):

  * ``GET /observability/budget`` — overview + currently-tracked
    op snapshot
  * ``GET /observability/budget/{op_id}`` — single per-op detail

All routes:

  * Master-flag-gated per request via
    :func:`epistemic_budget_enabled` (live-toggle without
    re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via the caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned by Slice 5):

  * Imports stdlib + aiohttp.web + ``epistemic_budget`` ONLY.
  * NEVER imports orchestrator / iron_gate /
    candidate_generator / providers / urgency_router /
    semantic_guardian / tool_executor / change_engine /
    subagent_scheduler / auto_action_router / policy /
    epistemic_budget_executor_hook (executor-hook is the
    authority side; observability is read-only).
  * Read-only — never mutates tracker state.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from backend.core.ouroboros.governance.epistemic_budget import (
    BudgetOutcome,
    EPISTEMIC_BUDGET_SCHEMA_VERSION,
    EpistemicBudgetTracker,
    epistemic_budget_enabled,
    epistemic_confidence_drop_threshold,
    epistemic_max_rounds,
    epistemic_sbt_branch_cap,
    epistemic_tracker_ttl_s,
    get_default_tracker,
    get_max_calls_per_probe,
)

logger = logging.getLogger(__name__)


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
    """Snapshot of operator-relevant env knobs. NEVER raises."""
    try:
        return {
            "max_rounds": epistemic_max_rounds(),
            "confidence_drop_threshold": (
                epistemic_confidence_drop_threshold()
            ),
            "sbt_branch_cap": epistemic_sbt_branch_cap(),
            "probe_call_cap": get_max_calls_per_probe(),
            "tracker_ttl_s": epistemic_tracker_ttl_s(),
        }
    except Exception:  # noqa: BLE001 — defensive
        return {}


def _safe_snapshot(
    tracker: EpistemicBudgetTracker,
) -> Dict[str, Any]:
    """Project all tracked budgets to JSON-safe dicts.
    NEVER raises."""
    try:
        budgets = tracker.snapshot_all()
    except Exception:  # noqa: BLE001 — defensive
        budgets = tuple()
    out = []
    for b in budgets:
        try:
            out.append(b.to_dict())
        except Exception:  # noqa: BLE001 — defensive
            continue
    return {
        "tracked_count": len(out),
        "budgets": out,
    }


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


class _EpistemicBudgetRoutesHandler:
    """aiohttp route handler for the ``/observability/budget``
    family. Mirrors :class:`_ActionOutcomeRoutesHandler` (M11)."""

    def __init__(
        self,
        *,
        tracker: Optional[EpistemicBudgetTracker] = None,
        rate_limit_check: Optional[
            Callable[[Any], bool]
        ] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._tracker = tracker
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _resolved_tracker(self) -> EpistemicBudgetTracker:
        return (
            self._tracker
            if self._tracker is not None
            else get_default_tracker()
        )

    def _gate(self, request: Any) -> Optional[Any]:
        if not epistemic_budget_enabled():
            return _json_response(
                {
                    "error": "disabled",
                    "schema_version": (
                        EPISTEMIC_BUDGET_SCHEMA_VERSION
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

    # ---- handlers -----------------------------------------------------

    async def handle_overview(self, request: Any) -> Any:
        """``GET /observability/budget`` — overview + all
        currently-tracked op snapshots."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            tracker = self._resolved_tracker()
            snapshot = _safe_snapshot(tracker)
            return _json_response(
                {
                    "schema_version": (
                        EPISTEMIC_BUDGET_SCHEMA_VERSION
                    ),
                    "flags": {
                        "master_enabled": (
                            epistemic_budget_enabled()
                        ),
                    },
                    "config": _build_config_dict(),
                    "tracked_count": (
                        snapshot["tracked_count"]
                    ),
                    "budgets": snapshot["budgets"],
                    "outcome_kinds": [
                        k.value for k in BudgetOutcome
                    ],
                    "sse_event_type": "budget_action_taken",
                },
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget_observability] overview "
                "raised: %s", exc,
            )
            return _json_response(
                {"error": "snapshot_failed"},
                status=500,
            )

    async def handle_detail(self, request: Any) -> Any:
        """``GET /observability/budget/{op_id}`` — single per-op
        detail with full trajectory."""
        gated = self._gate(request)
        if gated is not None:
            return gated
        try:
            op_id = (
                request.match_info.get("op_id", "") or ""
            ).strip()
        except Exception:  # noqa: BLE001 — defensive
            op_id = ""
        if not op_id:
            return _json_response(
                {"error": "missing_op_id"},
                status=400,
            )
        try:
            tracker = self._resolved_tracker()
            budget = tracker.get(op_id)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget_observability] detail get "
                "raised: %s", exc,
            )
            budget = None
        if budget is None:
            return _json_response(
                {
                    "error": "op_not_tracked",
                    "op_id": op_id,
                    "schema_version": (
                        EPISTEMIC_BUDGET_SCHEMA_VERSION
                    ),
                },
                status=404,
            )
        try:
            payload = budget.to_dict()
            # Augment with full per-sample trajectory for the
            # detail endpoint (overview omits this for size).
            try:
                samples = (
                    budget.confidence_trajectory.samples
                )
                payload["trajectory"]["samples"] = [
                    {
                        "confidence": float(s.confidence),
                        "at_unix": float(s.at_unix),
                        "at_round_index": int(
                            s.at_round_index,
                        ),
                    }
                    for s in samples
                ]
            except Exception:  # noqa: BLE001 — defensive
                pass
            payload["sse_event_type"] = "budget_action_taken"
            return _json_response(payload)
        except Exception as exc:  # noqa: BLE001 — defensive
            logger.debug(
                "[epistemic_budget_observability] detail "
                "projection raised: %s", exc,
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
    tracker: Optional[EpistemicBudgetTracker] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Register the ``/observability/budget`` family on the
    supplied aiohttp ``Application``. Idempotent at the route
    level (re-mounting raises on duplicate routes — caller's
    responsibility)."""
    handler = _EpistemicBudgetRoutesHandler(
        tracker=tracker,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/budget", handler.handle_overview,
    )
    app.router.add_get(
        "/observability/budget/{op_id}",
        handler.handle_detail,
    )


__all__ = [
    "register_routes",
]
