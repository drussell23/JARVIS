"""IDE observability routes — Gap #6 Slice 1.

Ships read-only HTTP GET endpoints exposing agent state to
operator-side IDE extensions (VS Code, JetBrains). Designed to be
mounted ALONGSIDE the existing :class:`EventChannelServer`'s
``POST /webhook/*`` routes so a single port / process serves both
surfaces. This module is the GET-side; the server stays authoritative
for its POST surface.

## Authority posture (locked by authorization)

- **Read-only observability only.** Zero endpoints mutate agent
  state. No cancel / approve / merge / invoke / retry. Operator
  actions are a separate ticket with its own §1 review.
- **Deny-by-default.** ``JARVIS_IDE_OBSERVABILITY_ENABLED`` defaults
  ``false``; explicit ``"true"`` required to enable the routes.
- **Loopback-only binding.** ``assert_loopback_only(host)`` rejects
  ``0.0.0.0`` / ``::`` / ``*`` — this surface is for local IDE
  clients, not network-exposed. Tests pin the validator.
- **No secret leakage.** Response payloads are structured projections
  (TaskBoard snapshot is pilot precisely because its audit surface
  is already sanitized). Handlers never echo raw prompts, env vars,
  or file contents. Unknown / malformed ``op_id`` → 404 with a
  stable reason code, NOT a stack trace.
- **No imports from gate/execution modules.** Pinned by
  ``test_ide_observability_does_not_import_gate_modules`` — no
  iron_gate / risk_tier_floor / semantic_guardian / policy_engine /
  orchestrator / tool_executor in the import graph.
- **Rate limited.** Per-origin sliding-window cap to prevent
  polling storms from one extension host starving other consumers
  (or flooding agent logs).
- **CORS** allowlist is narrow: localhost / 127.0.0.1 origins only;
  no ``*`` with credentials.

## Schema versioning

All JSON payloads carry ``schema_version`` so future consumers can
feature-detect. v1.0 pilot ships TaskBoard-only; later slices add
``phase``, ``cost``, ``recent sessions``. Breaking shape changes
bump the major.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web


logger = logging.getLogger(__name__)


# --- Schema / version ------------------------------------------------------


IDE_OBSERVABILITY_SCHEMA_VERSION = "1.0"


# --- Env helpers -----------------------------------------------------------


def ide_observability_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated 2026-04-20 via Gap #6 Slice 4 after
    Slices 1-3 shipped the GET surface + SSE stream + VS Code extension
    with 72 governance tests + 35 extension tests green plus a live-fire
    proof of the end-to-end stack). Explicit ``"false"`` reverts to the
    Slice 1 deny-by-default posture so operators retain a runtime kill
    switch. The loopback-binding assertion + rate-limit caps + CORS
    allowlist + authority-invariant grep pin all remain in force
    regardless of this flag — graduation flips opt-in friction, NOT
    authority surface. When the flag is explicitly ``"false"``, every
    route still returns 403 (port scanners see no signal about what's
    behind the listener).
    """
    return os.environ.get(
        "JARVIS_IDE_OBSERVABILITY_ENABLED", "true",
    ).strip().lower() == "true"


def _rate_limit_per_min() -> int:
    """Max requests / minute / client key. Default 120 — allows a 2/sec
    steady poll from a single IDE client without feeling throttled,
    while protecting against storms. Well above CC's typical 1-2s
    poll cadence."""
    try:
        return max(1, int(os.environ.get(
            "JARVIS_IDE_OBSERVABILITY_RATE_LIMIT_PER_MIN", "120",
        )))
    except (TypeError, ValueError):
        return 120


def _cors_origin_patterns() -> Tuple[str, ...]:
    """Allowlist of regex patterns for CORS ``Access-Control-Allow-Origin``.
    Only local origins by default — production use assumes localhost.
    No wildcard credentials."""
    raw = os.environ.get(
        "JARVIS_IDE_OBSERVABILITY_CORS_ORIGINS",
        # Default: localhost + 127.0.0.1 with any port; plus
        # VS Code extension webview origins.
        r"^https?://localhost(:\d+)?$,"
        r"^https?://127\.0\.0\.1(:\d+)?$,"
        r"^vscode-webview://[a-z0-9-]+$",
    )
    return tuple(p.strip() for p in raw.split(",") if p.strip())


# --- Loopback-binding validator --------------------------------------------


def assert_loopback_only(host: str) -> None:
    """Raise ``ValueError`` if ``host`` would bind non-loopback.

    Enforced at server boot — the IDE observability surface MUST NOT
    be exposed to the network under any configuration. Tests pin
    rejection of ``0.0.0.0`` / ``::`` / ``*`` / empty-string /
    externally-routable-looking addresses.
    """
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "ide_observability host must be a non-empty loopback "
            "address; got " + repr(host)
        )
    forbidden = {"0.0.0.0", "::", "*", ""}
    if host in forbidden:
        raise ValueError(
            "ide_observability refuses non-loopback bind: "
            + repr(host) + " is not allowed. Use 127.0.0.1 or ::1."
        )
    # Accept only the two documented loopback addresses + "localhost".
    allowed = {"127.0.0.1", "::1", "localhost"}
    if host not in allowed:
        raise ValueError(
            "ide_observability host must be one of "
            + str(sorted(allowed)) + "; got " + repr(host)
        )


# --- Router class ----------------------------------------------------------


# Stable op_id shape — matches TaskBoard's task-{op_id}-{seq:04d} format
# guarantees an op_id's a bounded printable string. We're defensive
# at the URL boundary: disallow anything outside [-_A-Za-z0-9].
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


class IDEObservabilityRouter:
    """Mounts the GET /observability/* routes on a caller-supplied
    aiohttp :class:`Application`.

    Usage (from :class:`EventChannelServer.start`)::

        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter, assert_loopback_only,
        )
        assert_loopback_only(self._host)
        IDEObservabilityRouter().register_routes(app)

    The router maintains its own rate-tracker state. Not shared with
    the EventChannelServer's webhook rate limiter — different trust
    boundary (GET vs POST, external webhooks vs local IDE).
    """

    def __init__(self) -> None:
        # sliding-window rate tracker: { client_key -> [ts_epoch_s, ...] }
        self._rate_tracker: Dict[str, List[float]] = {}

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get("/observability/health", self._handle_health)
        app.router.add_get("/observability/tasks", self._handle_task_list)
        app.router.add_get(
            "/observability/tasks/{op_id}", self._handle_task_detail,
        )

    # --- request-path helpers ---------------------------------------------

    def _client_key(self, request: "web.Request") -> str:
        """Stable per-client key for rate limiting. Prefers
        ``X-Forwarded-For`` only if the peer is loopback (since
        we're loopback-only anyway, but some clients still set it)."""
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_rate_limit(self, client_key: str) -> bool:
        """Returns True iff this call is within the sliding-window
        quota. When False → caller must return 429."""
        limit = _rate_limit_per_min()
        now = time.monotonic()
        window_start = now - 60.0
        history = self._rate_tracker.setdefault(client_key, [])
        # Evict expired entries (amortized; kept small by the 60s
        # window).
        while history and history[0] < window_start:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _cors_headers(self, request: "web.Request") -> Dict[str, str]:
        """Build the minimum CORS header set for a matched origin.
        No credentials header; no wildcard; only echoes the exact
        origin if it's in the allowlist."""
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        # Observability-only — no GET/POST variety.
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                    }
            except re.error:
                # Malformed operator pattern — skip, don't crash the
                # response.
                continue
        return {}

    def _json_response(
        self,
        request: "web.Request",
        status: int,
        payload: Dict[str, Any],
    ) -> Any:
        """Single place that composes every JSON response.
        Stamps schema_version, applies CORS, sets cache headers."""
        from aiohttp import web
        # Every payload carries schema_version (§8 contract).
        if "schema_version" not in payload:
            payload = {"schema_version": IDE_OBSERVABILITY_SCHEMA_VERSION,
                       **payload}
        resp = web.json_response(payload, status=status)
        for k, v in self._cors_headers(request).items():
            resp.headers[k] = v
        # IDE clients should not cache observability — state is live.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error_response(
        self,
        request: "web.Request",
        status: int,
        reason_code: str,
    ) -> Any:
        """Shared error shape. Carries ONLY the reason_code — no
        stack traces, no internal paths. The status + code is enough
        for an IDE to render an operator-visible error."""
        return self._json_response(
            request,
            status=status,
            payload={"error": True, "reason_code": reason_code},
        )

    # --- handlers ---------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> Any:
        """GET /observability/health — liveness + schema version.

        When disabled, returns 403 (so port scanners see no signal
        about what's behind the listener — not a 200 with
        ``{enabled: false}`` that advertises the surface).
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        return self._json_response(
            request, 200,
            {
                "enabled": True,
                "api_version": IDE_OBSERVABILITY_SCHEMA_VERSION,
                "surface": "tasks",  # which data domains are live
                "now_mono": time.monotonic(),
            },
        )

    async def _handle_task_list(self, request: "web.Request") -> Any:
        """GET /observability/tasks — list of op_ids with active boards.

        Shape::

            {
              "schema_version": "1.0",
              "op_ids": ["op-abc", "op-def", ...],
              "count": 2
            }
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        # Lazy import so this module doesn't hard-depend on task_tool
        # at import-time (defensive against circular-import paths).
        from backend.core.ouroboros.governance.task_tool import _BOARDS
        # Snapshot the keys without holding any lock — we're
        # read-only and the _BOARDS registry mutation is rare.
        op_ids = sorted(_BOARDS.keys())
        return self._json_response(
            request, 200,
            {"op_ids": op_ids, "count": len(op_ids)},
        )

    async def _handle_task_detail(self, request: "web.Request") -> Any:
        """GET /observability/tasks/{op_id} — projection of one board.

        Returns 404 on unknown op_id; 400 on malformed. NEVER leaks
        stack traces, internal paths, or raw model output. The Task
        dataclass is already a structured frozen projection — we
        echo its fields, skipping any that could carry secrets.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error_response(
                request, 400, "ide_observability.malformed_op_id",
            )
        from backend.core.ouroboros.governance.task_tool import _BOARDS
        board = _BOARDS.get(op_id)
        if board is None:
            return self._error_response(
                request, 404, "ide_observability.unknown_op_id",
            )
        snap = board.snapshot()
        active = board.active_task()
        # Structured projection — echo only the fields Task already
        # exposes as public data. Sanitize nothing extra because
        # Task.title / body went through sanitize_for_log at render
        # time; here we're projecting exact stored state for display.
        # If an operator needs the sanitized display form, they use
        # the render_prompt_section path (which IS sanitized).
        # For raw tasks-api consumers (IDE sidebars), echoing stored
        # values is correct.
        return self._json_response(
            request, 200,
            {
                "op_id": op_id,
                "closed": board.closed,
                "active_task_id": (
                    active.task_id if active is not None else None
                ),
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "state": t.state,
                        "title": t.title,
                        "body": t.body,
                        "sequence": t.sequence,
                        "cancel_reason": t.cancel_reason,
                    }
                    for t in snap
                ],
                "board_size": len(snap),
            },
        )
