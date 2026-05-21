"""HTTP read surface for evaluator-trace JSONL — mirrors
:mod:`decisions_observability` shape (PRD §31.3 Slice 3) for the
evaluator structural-probe substrate.

Auto-discovered + mounted at boot via
:func:`observability_route_registry.discover_and_mount_observability_routes`
through the canonical ``register_routes(app, *, rate_limit_check,
cors_headers)`` signature (PRD §33.3 Slice 5b naming-cage).

Routes:

  * ``GET /observability/evaluator_trace``
      Overview: most-recent N frames from the JSONL, optionally
      filtered by ``?since_seq=N``.

  * ``GET /observability/evaluator_trace/{seq}``
      Specific frame body by sequence number.

  * ``GET /observability/evaluator_trace/active_tasks``
      Live snapshot built on-demand (composes
      :func:`build_frame` — does NOT touch JSONL).

All routes:

  * Master-flag-gated per-request via :func:`evaluator_trace_enabled`
    (live-toggle without re-mounting).
  * Rate-limit-gated by the caller-supplied check.
  * CORS allowlist applied via caller-supplied callable.
  * ``Cache-Control: no-store``.
  * NEVER raises out of any handler — defensive everywhere.

Authority invariants (AST-pinned at Slice 4 spine tests):

  * Imports stdlib + aiohttp.web + ``evaluator_trace_observer`` ONLY.
  * NEVER imports orchestrator / iron_gate / candidate_generator /
    providers / urgency_router / semantic_guardian / tool_executor /
    change_engine / subagent_scheduler / auto_action_router / policy /
    strategic_direction.
  * **READ-ONLY** — never invokes the observer's mutation surface
    (``start`` / ``stop`` / ``run_one_cycle``). Pinned by AST + grep.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional

from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
    EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION,
    EvaluatorTraceFrame,
    build_frame,
    evaluator_trace_enabled,
)

logger = logging.getLogger("Ouroboros.EvaluatorTraceObservability")

EVALUATOR_TRACE_OBSERVABILITY_SCHEMA_VERSION: str = (
    EVALUATOR_TRACE_OBSERVER_SCHEMA_VERSION
)


_DEFAULT_OVERVIEW_LIMIT: int = 50
_MAX_OVERVIEW_LIMIT: int = 500


def _resolve_jsonl_path_for_handler() -> Path:
    """Late-binding JSONL path read so operators can rotate paths
    without re-mounting the route."""
    from backend.core.ouroboros.governance.swe_bench_pro.evaluator_trace_observer import (  # noqa: E501
        _resolve_jsonl_path,
    )
    return _resolve_jsonl_path()


def _parse_int_query(
    request: Any, name: str, default: int, *, lo: int, hi: int,
) -> int:
    try:
        raw = request.query.get(name)
        if raw is None:
            return default
        n = int(raw)
        if n < lo:
            return lo
        if n > hi:
            return hi
        return n
    except Exception:  # noqa: BLE001
        return default


def _read_frames_from_jsonl(
    path: Path,
    *,
    limit: int,
    since_seq: int = 0,
) -> List[Mapping[str, Any]]:
    """Read the last ``limit`` JSONL frames whose ``snapshot_seq``
    is greater than ``since_seq``. NEVER raises."""
    out: List[Mapping[str, Any]] = []
    try:
        if not path.exists():
            return out
        # JSONL is append-only; for "last N" we tail-read.
        with path.open("r", encoding="utf-8") as fh:
            lines = fh.readlines()
        # Walk from end backward, keep matching frames until limit.
        for raw_line in reversed(lines):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except (json.JSONDecodeError, ValueError):
                continue
            try:
                seq = int(obj.get("snapshot_seq", 0))
            except (TypeError, ValueError):
                continue
            if seq <= since_seq:
                continue
            out.append(obj)
            if len(out) >= limit:
                break
        out.reverse()
        return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EvTraceObs] JSONL read fault: %s", exc)
        return out


class _EvaluatorTraceRoutesHandler:

    def __init__(
        self,
        *,
        rate_limit_check: Optional[Callable[[Any], bool]] = None,
        cors_headers: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self._rate_limit_check = rate_limit_check
        self._cors_headers = cors_headers

    def _common_headers(self, request: Any) -> Mapping[str, str]:
        headers = {"Cache-Control": "no-store"}
        if self._cors_headers is not None:
            try:
                extra = self._cors_headers(request)
                if isinstance(extra, dict):
                    headers.update(extra)
            except Exception:  # noqa: BLE001
                pass
        return headers

    def _gated(self, request: Any):  # noqa: ANN202
        from aiohttp import web
        if self._rate_limit_check is not None:
            try:
                ok = self._rate_limit_check(request)
            except Exception:  # noqa: BLE001
                ok = False
            if not ok:
                return web.json_response(
                    {"error": "rate_limited"},
                    status=429,
                    headers=self._common_headers(request),
                )
        if not evaluator_trace_enabled():
            return web.json_response(
                {
                    "error": "evaluator_trace_disabled",
                    "schema_version": (
                        EVALUATOR_TRACE_OBSERVABILITY_SCHEMA_VERSION
                    ),
                },
                status=503,
                headers=self._common_headers(request),
            )
        return None

    async def handle_overview(self, request: Any):  # noqa: ANN201
        from aiohttp import web
        gated = self._gated(request)
        if gated is not None:
            return gated
        limit = _parse_int_query(
            request, "limit", _DEFAULT_OVERVIEW_LIMIT,
            lo=1, hi=_MAX_OVERVIEW_LIMIT,
        )
        since_seq = _parse_int_query(
            request, "since_seq", 0, lo=0, hi=10_000_000,
        )
        path = _resolve_jsonl_path_for_handler()
        frames = _read_frames_from_jsonl(
            path, limit=limit, since_seq=since_seq,
        )
        return web.json_response(
            {
                "schema_version": (
                    EVALUATOR_TRACE_OBSERVABILITY_SCHEMA_VERSION
                ),
                "limit": limit,
                "since_seq": since_seq,
                "jsonl_path": str(path),
                "frames": list(frames),
                "count": len(frames),
            },
            headers=self._common_headers(request),
        )

    async def handle_frame_by_seq(self, request: Any):  # noqa: ANN201
        from aiohttp import web
        gated = self._gated(request)
        if gated is not None:
            return gated
        try:
            seq = int(request.match_info.get("seq", "0"))
        except (TypeError, ValueError):
            return web.json_response(
                {"error": "bad_seq"},
                status=400,
                headers=self._common_headers(request),
            )
        path = _resolve_jsonl_path_for_handler()
        # Scan for the exact seq.
        try:
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        if int(obj.get("snapshot_seq", -1)) == seq:
                            return web.json_response(
                                obj,
                                headers=self._common_headers(request),
                            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[EvTraceObs] frame-by-seq fault: %s", exc)
        return web.json_response(
            {"error": "frame_not_found", "seq": seq},
            status=404,
            headers=self._common_headers(request),
        )

    async def handle_active_tasks(self, request: Any):  # noqa: ANN201
        from aiohttp import web
        gated = self._gated(request)
        if gated is not None:
            return gated
        session_id = str(request.query.get("session_id", "live") or "live")
        frame = build_frame(session_id=session_id, snapshot_seq=0)
        return web.json_response(
            frame.to_dict() if isinstance(frame, EvaluatorTraceFrame) else {},
            headers=self._common_headers(request),
        )


def register_routes(
    app: Any,
    *,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Any]] = None,
) -> None:
    """Mount evaluator-trace observability routes.

    Auto-discovered by ``discover_and_mount_observability_routes``.
    NEVER raises (route mounting failures are downgraded to DEBUG;
    other observability surfaces stay healthy)."""
    try:
        handler = _EvaluatorTraceRoutesHandler(
            rate_limit_check=rate_limit_check,
            cors_headers=cors_headers,
        )
        app.router.add_get(
            "/observability/evaluator_trace",
            handler.handle_overview,
        )
        app.router.add_get(
            "/observability/evaluator_trace/active_tasks",
            handler.handle_active_tasks,
        )
        app.router.add_get(
            "/observability/evaluator_trace/{seq}",
            handler.handle_frame_by_seq,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[EvTraceObs] register_routes fault: %s", exc)


__all__ = [
    "EVALUATOR_TRACE_OBSERVABILITY_SCHEMA_VERSION",
    "register_routes",
]
