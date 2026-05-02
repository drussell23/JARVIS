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

import json
import logging
import os
import re
import time
from pathlib import Path
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

# Session ids include ``:`` and ``.`` (timestamp format). Match the
# session_browser module's regex so this surface accepts the same
# ids the browser does.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# Record ids in the CausalityDAG are derived from DecisionRecord
# stamps. Match the same character class as session_id so URL
# routing accepts them; cap longer (256) to accommodate phase-
# capture composite ids that include phase + ordinal segments.
_RECORD_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,256}$")


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

    def __init__(
        self,
        session_dir: Optional[Path] = None,
        *,
        scheduler: Optional[Any] = None,
        worktree_manager: Optional[Any] = None,
    ) -> None:
        # sliding-window rate tracker: { client_key -> [ts_epoch_s, ...] }
        self._rate_tracker: Dict[str, List[float]] = {}
        # W3(7) Slice 6 — optional session_dir for /observability/cancels.
        # When None (default — IDE-only deployments without a battle-test
        # harness), the cancel routes return 503 cleanly. GLS sets this at
        # construction time when the router is mounted on the harness app.
        self._session_dir: Optional[Path] = session_dir
        # Gap #3 Slice 2 — optional scheduler + worktree_manager refs
        # for the /observability/worktrees topology projection. When
        # either is None the routes return 503 cleanly (graceful
        # degradation matches the cancel-route discipline above).
        # Duck-typed (Any) so test fixtures can supply minimal stubs
        # without booting the full SubagentScheduler.
        self._scheduler: Optional[Any] = scheduler
        self._worktree_manager: Optional[Any] = worktree_manager

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get("/observability/health", self._handle_health)
        app.router.add_get("/observability/tasks", self._handle_task_list)
        app.router.add_get(
            "/observability/tasks/{op_id}", self._handle_task_detail,
        )
        # Problem #7 Slice 4 — plan approval surface.
        app.router.add_get(
            "/observability/plans", self._handle_plan_list,
        )
        app.router.add_get(
            "/observability/plans/{op_id}", self._handle_plan_detail,
        )
        # Session History Browser extension arc Slice 4 — sessions surface.
        app.router.add_get(
            "/observability/sessions", self._handle_session_list,
        )
        app.router.add_get(
            "/observability/sessions/{session_id}",
            self._handle_session_detail,
        )
        # DirectionInferrer Slice 3 — strategic posture surface.
        app.router.add_get(
            "/observability/posture", self._handle_posture_current,
        )
        app.router.add_get(
            "/observability/posture/history",
            self._handle_posture_history,
        )
        # FlagRegistry Slice 3 — flag + verb introspection surface.
        app.router.add_get(
            "/observability/flags", self._handle_flags_list,
        )
        app.router.add_get(
            "/observability/flags/unregistered",
            self._handle_flags_unregistered,
        )
        app.router.add_get(
            "/observability/flags/{name}",
            self._handle_flag_detail,
        )
        app.router.add_get(
            "/observability/verbs", self._handle_verbs_list,
        )
        # SensorGovernor + MemoryPressureGate — Wave 1 #3 Slice 3.
        app.router.add_get(
            "/observability/governor", self._handle_governor_snapshot,
        )
        app.router.add_get(
            "/observability/governor/history",
            self._handle_governor_history,
        )
        app.router.add_get(
            "/observability/memory-pressure",
            self._handle_memory_pressure,
        )
        # W3(7) Slice 6 — Class D/E/F cancel record surface.
        app.router.add_get(
            "/observability/cancels", self._handle_cancel_list,
        )
        app.router.add_get(
            "/observability/cancels/{cancel_id}",
            self._handle_cancel_detail,
        )
        # W2(4) Slice 3 — curiosity question record surface.
        app.router.add_get(
            "/observability/curiosity", self._handle_curiosity_list,
        )
        app.router.add_get(
            "/observability/curiosity/{question_id}",
            self._handle_curiosity_detail,
        )
        # Time-Travel Debugging Slice 1 (2026-05-02) — CausalityDAG
        # navigation surface. Delegates to verification.dag_navigation
        # handlers (the substrate) which already check
        # JARVIS_DAG_NAVIGATION_GET_ENABLED + dag_query_enabled().
        # Activates the IDE-side consumer for the navigable session
        # graph (Causality DAG primitive shipped by Priority #2;
        # GET surface was structurally orphaned until this slice).
        app.router.add_get(
            "/observability/dag/{session_id}",
            self._handle_dag_session,
        )
        app.router.add_get(
            "/observability/dag/{session_id}/{record_id}",
            self._handle_dag_record,
        )
        # Q2 Slice 6 — deterministic state-diff between two DAG
        # records. Both ids in path; same-session only by design.
        app.router.add_get(
            "/observability/dag/{session_id}/diff/{record_id_a}/{record_id_b}",
            self._handle_dag_diff,
        )
        # Priority #3 Slice 5b — Counterfactual Replay surface.
        app.router.add_get(
            "/observability/replay/health",
            self._handle_replay_health,
        )
        # Gap #3 Slice 2 — L3 worktree topology projection.
        # Read-only over scheduler in-memory state + git worktree
        # paths. 503 cleanly when scheduler/worktree_manager
        # references are not wired (graceful degradation).
        app.router.add_get(
            "/observability/worktrees",
            self._handle_worktrees_list,
        )
        app.router.add_get(
            "/observability/worktrees/{graph_id}",
            self._handle_worktree_detail,
        )
        app.router.add_get(
            "/observability/replay/baseline",
            self._handle_replay_baseline,
        )
        app.router.add_get(
            "/observability/replay/verdicts",
            self._handle_replay_verdicts,
        )
        app.router.add_get(
            "/observability/replay/history",
            self._handle_replay_history,
        )
        # Q4 Priority #2 Slice 4 — closure-loop observability surface.
        # Read-only over closure_loop_history.jsonl + AdaptationLedger.
        # Master-flag-off → 403 (port-scanner discipline). When the
        # closure-loop is in shadow mode (master off), the routes
        # respond 403 — operators rely on /adapt for the underlying
        # ledger view and on /observability/replay/* for upstream
        # state.
        app.router.add_get(
            "/observability/closure-loop",
            self._handle_closure_loop_health,
        )
        app.router.add_get(
            "/observability/closure-loop/history",
            self._handle_closure_loop_history,
        )
        app.router.add_get(
            "/observability/closure-loop/pending",
            self._handle_closure_loop_pending,
        )
        app.router.add_get(
            "/observability/closure-loop/stats",
            self._handle_closure_loop_stats,
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
                # Which data domains are live — documented surface
                # contract IDE clients feature-detect against.
                "surface": "tasks,plans,sessions",
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

    # ------------------------------------------------------------------
    # Plan Approval routes (problem #7 Slice 4)
    # ------------------------------------------------------------------

    async def _handle_plan_list(self, request: "web.Request") -> Any:
        """GET /observability/plans — list op_ids with registered plans.

        Same deny-by-default + rate-limit + CORS discipline as the
        task routes. Returns an array of projections, one per plan
        (pending + terminal), sorted by op_id.

        Shape::

            {
              "schema_version": "1.0",
              "plans": [
                {"op_id": "op-a", "state": "pending",
                 "expires_ts": 14123.4, "reviewer": "", "reason": ""},
                ...
              ],
              "count": N
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
        from backend.core.ouroboros.governance.plan_approval import (
            get_default_controller,
        )
        controller = get_default_controller()
        summaries = []
        for snap in controller.snapshot_all():
            # Summary only — full plan JSON lives at /plans/{op_id}.
            summaries.append({
                "op_id": snap["op_id"],
                "state": snap["state"],
                "created_ts": snap["created_ts"],
                "expires_ts": snap["expires_ts"],
                "reviewer": snap["reviewer"],
                "reason": snap["reason"],
            })
        summaries.sort(key=lambda s: s["op_id"])
        return self._json_response(
            request, 200,
            {"plans": summaries, "count": len(summaries)},
        )

    async def _handle_plan_detail(self, request: "web.Request") -> Any:
        """GET /observability/plans/{op_id} — full plan projection.

        Returns 404 on unknown op_id; 400 on malformed. Echoes the
        full controller projection including the plan payload so
        IDE clients can render the same schema-plan.1 structure
        that the REPL shows.
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
        from backend.core.ouroboros.governance.plan_approval import (
            get_default_controller,
        )
        controller = get_default_controller()
        snap = controller.snapshot(op_id)
        if snap is None:
            return self._error_response(
                request, 404, "ide_observability.unknown_op_id",
            )
        return self._json_response(request, 200, snap)

    # ------------------------------------------------------------------
    # Session Browser routes (extension arc Slice 4)
    # ------------------------------------------------------------------

    def _projected_session(
        self, rec: Any, bookmark: Optional[Any],
    ) -> Dict[str, Any]:
        """Compose the session projection shipped over the wire.

        Extends :meth:`SessionRecord.project` with the three
        operator-owned bits BookmarkStore holds: ``bookmarked``,
        ``pinned``, ``bookmark_note``, ``bookmark_ts``.
        """
        p = dict(rec.project())
        p["bookmarked"] = bookmark is not None
        p["pinned"] = bool(bookmark is not None and bookmark.pinned)
        p["bookmark_note"] = bookmark.note if bookmark is not None else None
        p["bookmark_ts"] = (
            bookmark.created_at_iso if bookmark is not None else None
        )
        return p

    def _parse_bool_query(
        self, value: Optional[str],
    ) -> Optional[bool]:
        """Lenient bool parser for query strings.

        Accepts ``"true"`` / ``"false"`` (case-insensitive); anything
        else → ``None`` (treat as "filter not applied").
        """
        if value is None:
            return None
        v = value.strip().lower()
        if v == "true":
            return True
        if v == "false":
            return False
        return None

    async def _handle_session_list(self, request: "web.Request") -> Any:
        """GET /observability/sessions — list of session projections.

        Query params (all optional):
          * ``ok``          — ``true``/``false``  filter by ok_outcome
          * ``bookmarked``  — ``true``/``false``  only bookmarked / not
          * ``pinned``      — ``true``/``false``  only pinned / not
          * ``has_replay``  — ``true``/``false``  has replay.html or not
          * ``parse_error`` — ``true``/``false``  corrupt records only / not
          * ``prefix``      — session_id prefix (regex-bound)
          * ``limit``       — 1..1000 (default 100)

        Shape::

            {
              "schema_version": "1.0",
              "sessions": [<projection>, ...],
              "count": N
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
        # Parse limit
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        if limit < 1 or limit > 1000:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        # Parse filters
        filters: Dict[str, Any] = {}
        ok_flag = self._parse_bool_query(request.query.get("ok"))
        if ok_flag is not None:
            filters["ok_outcome"] = ok_flag
        has_replay_flag = self._parse_bool_query(
            request.query.get("has_replay"),
        )
        if has_replay_flag is not None:
            filters["has_replay"] = has_replay_flag
        parse_error_flag = self._parse_bool_query(
            request.query.get("parse_error"),
        )
        if parse_error_flag is not None:
            filters["parse_error"] = parse_error_flag
        prefix = request.query.get("prefix", "").strip()
        if prefix:
            if not _SESSION_ID_RE.match(prefix):
                return self._error_response(
                    request, 400,
                    "ide_observability.malformed_prefix",
                )
            filters["session_id_prefix"] = prefix
        # These two post-filter on bookmark state, not on index fields.
        bookmarked_flag = self._parse_bool_query(
            request.query.get("bookmarked"),
        )
        pinned_flag = self._parse_bool_query(request.query.get("pinned"))

        # Lazy import to avoid a module-load cycle.
        from backend.core.ouroboros.governance.session_browser import (
            get_default_session_browser,
        )
        browser = get_default_session_browser()
        browser.index.scan()
        if filters:
            records = browser.index.filter(**filters)
        else:
            records = browser.index.all_records()
        bookmarks_by_id = {
            bm.session_id: bm for bm in browser.bookmarks.list_all()
        }

        sessions: List[Dict[str, Any]] = []
        for r in records:
            bm = bookmarks_by_id.get(r.session_id)
            is_bookmarked = bm is not None
            is_pinned = is_bookmarked and bm.pinned
            if bookmarked_flag is True and not is_bookmarked:
                continue
            if bookmarked_flag is False and is_bookmarked:
                continue
            if pinned_flag is True and not is_pinned:
                continue
            if pinned_flag is False and is_pinned:
                continue
            sessions.append(self._projected_session(r, bm))
            if len(sessions) >= limit:
                break
        return self._json_response(
            request, 200,
            {"sessions": sessions, "count": len(sessions)},
        )

    async def _handle_session_detail(self, request: "web.Request") -> Any:
        """GET /observability/sessions/{session_id} — full projection.

        404 on unknown session id; 400 on malformed. The shape mirrors
        the list-item projection with the same ``bookmarked`` /
        ``pinned`` / ``bookmark_note`` / ``bookmark_ts`` overlay.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        session_id = request.match_info.get("session_id", "")
        if not _SESSION_ID_RE.match(session_id):
            return self._error_response(
                request, 400,
                "ide_observability.malformed_session_id",
            )
        from backend.core.ouroboros.governance.session_browser import (
            get_default_session_browser,
        )
        browser = get_default_session_browser()
        rec = browser.show(session_id)
        if rec is None:
            return self._error_response(
                request, 404,
                "ide_observability.unknown_session_id",
            )
        bookmarks_by_id = {
            bm.session_id: bm for bm in browser.bookmarks.list_all()
        }
        bm = bookmarks_by_id.get(session_id)
        return self._json_response(
            request, 200,
            self._projected_session(rec, bm),
        )

    # --- DirectionInferrer Slice 3 — posture surface ----------------------

    @staticmethod
    def _posture_master_enabled() -> bool:
        """Authority-free gate — the posture surface inherits the
        DirectionInferrer master switch. When the switch is off we 403
        so port scanners see no signal about what's behind the route."""
        try:
            from backend.core.ouroboros.governance.direction_inferrer import (
                is_enabled,
            )
        except ImportError:
            return False
        return is_enabled()

    @staticmethod
    def _project_reading(reading: Any) -> Dict[str, Any]:
        """Bounded projection of a PostureReading — no internal fields
        beyond the documented public surface."""
        return {
            "posture": reading.posture.value,
            "confidence": reading.confidence,
            "inferred_at": reading.inferred_at,
            "signal_bundle_hash": reading.signal_bundle_hash,
            "all_scores": [
                {"posture": p.value, "score": s}
                for p, s in reading.all_scores
            ],
            "evidence": [
                {
                    "signal_name": c.signal_name,
                    "raw_value": c.raw_value,
                    "normalized": c.normalized,
                    "weight": c.weight,
                    "contribution_score": c.contribution_score,
                }
                for c in reading.evidence
            ],
        }

    async def _handle_posture_current(self, request: "web.Request") -> Any:
        """GET /observability/posture — current StrategicPosture reading.

        Shape::

            {
              "schema_version": "1.0",
              "posture": "EXPLORE|CONSOLIDATE|HARDEN|MAINTAIN",
              "confidence": 0.96,
              "inferred_at": 1745263489.12,
              "signal_bundle_hash": "2a291ca2",
              "all_scores": [{"posture": "EXPLORE", "score": 0.73}, ...],
              "evidence": [{"signal_name": "feat_ratio",
                            "raw_value": 0.78, "normalized": 0.78,
                            "weight": 1.0, "contribution_score": 0.78}, ...]
            }

        204 when the store has no current reading (observer hasn't
        cycled yet) — distinguished from 403 (flag off) and 404 (never
        used — posture has no ``{id}`` path variant).
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._posture_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.posture_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.posture_observer import (
                get_default_store,
            )
            store = get_default_store()
            reading = store.load_current()
        except Exception:  # noqa: BLE001 — defensive: no reading rather than 500
            logger.debug("[IDEObservability] posture_current failed", exc_info=True)
            reading = None

        if reading is None:
            return self._json_response(
                request, 200,
                {"reading": None, "reason_code": "posture.no_current"},
            )
        return self._json_response(
            request, 200, self._project_reading(reading),
        )

    async def _handle_posture_history(self, request: "web.Request") -> Any:
        """GET /observability/posture/history?limit=N — ring-buffer tail.

        ``limit`` defaults to 20, clamped to ``[1, 256]``. Readings are
        returned oldest-first so clients can append new entries without
        reordering.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._posture_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.posture_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        raw_limit = request.query.get("limit", "20")
        try:
            limit = max(1, min(256, int(raw_limit)))
        except (TypeError, ValueError):
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            from backend.core.ouroboros.governance.posture_observer import (
                get_default_store,
            )
            store = get_default_store()
            readings = store.load_history(limit=limit)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug("[IDEObservability] posture_history failed", exc_info=True)
            readings = []
        return self._json_response(
            request, 200,
            {
                "readings": [self._project_reading(r) for r in readings],
                "count": len(readings),
                "limit": limit,
            },
        )

    # --- FlagRegistry Slice 3 — flag + verb introspection --------------------

    @staticmethod
    def _flag_registry_enabled() -> bool:
        """Second gate — surface inherits FlagRegistry master switch."""
        try:
            from backend.core.ouroboros.governance.flag_registry import is_enabled
        except ImportError:
            return False
        return is_enabled()

    @staticmethod
    def _project_flag_spec(spec: Any) -> Dict[str, Any]:
        """Bounded projection — matches FlagSpec.to_dict() — no extra
        fields leaked beyond the documented public shape."""
        return spec.to_dict()

    @staticmethod
    def _project_verb_spec(spec: Any) -> Dict[str, Any]:
        return {
            "name": spec.name,
            "one_line": spec.one_line,
            "category": spec.category,
            "since": spec.since,
        }

    def _flag_check_gates(self, request: "web.Request") -> Optional[Any]:
        """Returns an error response if any gate fails, else None."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._flag_registry_enabled():
            return self._error_response(
                request, 403, "ide_observability.flag_registry_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        return None

    async def _handle_flags_list(self, request: "web.Request") -> Any:
        """GET /observability/flags — all registered flags.

        Query params:
          ?category=CAT     Category enum value filter
          ?posture=P        Posture relevance filter (EXPLORE/CONSOLIDATE/...)
          ?search=Q         Case-insensitive substring on name + description
          ?limit=N          Clamp result count to N (default 500, max 1000)

        Filters are combined AND-wise: category → posture → search, each
        narrowing the previous result. Malformed category → 400.
        """
        err = self._flag_check_gates(request)
        if err is not None:
            return err
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                Category, ensure_seeded,
            )
            registry = ensure_seeded()
        except Exception:  # noqa: BLE001
            logger.debug("[IDEObservability] flag registry unavailable", exc_info=True)
            return self._json_response(
                request, 200,
                {"flags": [], "count": 0, "reason_code": "flags.unavailable"},
            )

        category_arg = request.query.get("category", "").strip().lower()
        posture_arg = request.query.get("posture", "").strip()
        search_arg = request.query.get("search", "").strip()
        try:
            limit = min(1000, max(1, int(request.query.get("limit", "500"))))
        except (TypeError, ValueError):
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )

        specs = registry.list_all()
        if category_arg:
            try:
                cat = Category(category_arg)
            except ValueError:
                return self._error_response(
                    request, 400, "ide_observability.malformed_category",
                )
            specs = [s for s in specs if s.category is cat]
        if posture_arg:
            # FlagSpec has a dict field (not hashable) so filter by name
            allowed_names = {
                s.name for s in registry.relevant_to_posture(posture_arg)
            }
            specs = [s for s in specs if s.name in allowed_names]
        if search_arg:
            q = search_arg.lower()
            specs = [
                s for s in specs
                if q in s.name.lower() or q in s.description.lower()
            ]

        specs = specs[:limit]
        return self._json_response(
            request, 200,
            {
                "flags": [self._project_flag_spec(s) for s in specs],
                "count": len(specs),
                "limit": limit,
            },
        )

    async def _handle_flag_detail(self, request: "web.Request") -> Any:
        """GET /observability/flags/{name} — full FlagSpec projection.

        404 on unknown flag; 400 on malformed name. Suggested similar
        names are included in 404 payload for client-side typo rendering.
        """
        err = self._flag_check_gates(request)
        if err is not None:
            return err
        name = request.match_info.get("name", "")
        # Flag names follow the JARVIS_ prefix + [A-Za-z0-9_] shape.
        if not re.match(r"^JARVIS_[A-Za-z0-9_]{1,128}$", name):
            return self._error_response(
                request, 400, "ide_observability.malformed_flag_name",
            )
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                ensure_seeded,
            )
            registry = ensure_seeded()
        except Exception:  # noqa: BLE001
            return self._error_response(
                request, 500, "ide_observability.registry_unavailable",
            )
        spec = registry.get_spec(name)
        if spec is None:
            suggestions = registry.suggest_similar(name, limit=3)
            return self._json_response(
                request, 404,
                {
                    "error": True,
                    "reason_code": "flags.unknown",
                    "name": name,
                    "suggestions": [
                        {"name": n, "distance": d} for n, d in suggestions
                    ],
                },
            )
        # Include current env value if set
        projection = self._project_flag_spec(spec)
        env_value = os.environ.get(name)
        if env_value is not None:
            projection["current_env_value"] = env_value
        return self._json_response(request, 200, projection)

    async def _handle_flags_unregistered(self, request: "web.Request") -> Any:
        """GET /observability/flags/unregistered — typo hunter.

        Lists JARVIS_* env vars present in process env that are NOT
        registered. Each entry includes Levenshtein-suggested matches.
        """
        err = self._flag_check_gates(request)
        if err is not None:
            return err
        try:
            from backend.core.ouroboros.governance.flag_registry import (
                ensure_seeded,
            )
            registry = ensure_seeded()
        except Exception:  # noqa: BLE001
            return self._json_response(
                request, 200,
                {"unregistered": [], "count": 0,
                 "reason_code": "flags.unavailable"},
            )
        hits = registry.unregistered_env()
        return self._json_response(
            request, 200,
            {
                "unregistered": [
                    {
                        "name": name,
                        "suggestions": [
                            {"name": n, "distance": d} for n, d in sugs
                        ],
                    }
                    for name, sugs in hits
                ],
                "count": len(hits),
            },
        )

    async def _handle_verbs_list(self, request: "web.Request") -> Any:
        """GET /observability/verbs — registered REPL verbs."""
        err = self._flag_check_gates(request)
        if err is not None:
            return err
        try:
            from backend.core.ouroboros.governance.help_dispatcher import (
                get_default_verb_registry,
            )
            verbs = get_default_verb_registry().list_all()
        except Exception:  # noqa: BLE001
            verbs = []
        return self._json_response(
            request, 200,
            {
                "verbs": [self._project_verb_spec(v) for v in verbs],
                "count": len(verbs),
            },
        )

    # --- SensorGovernor + MemoryPressureGate Slice 3 ----------------------

    @staticmethod
    def _sensor_governor_enabled() -> bool:
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                is_enabled,
            )
        except ImportError:
            return False
        return is_enabled()

    @staticmethod
    def _memory_gate_enabled() -> bool:
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                is_enabled,
            )
        except ImportError:
            return False
        return is_enabled()

    def _governor_check_gates(self, request: "web.Request") -> Optional[Any]:
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._sensor_governor_enabled():
            return self._error_response(
                request, 403, "ide_observability.governor_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        return None

    def _memory_check_gates(self, request: "web.Request") -> Optional[Any]:
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._memory_gate_enabled():
            return self._error_response(
                request, 403, "ide_observability.memory_gate_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        return None

    async def _handle_governor_snapshot(self, request: "web.Request") -> Any:
        """GET /observability/governor — current governor snapshot."""
        err = self._governor_check_gates(request)
        if err is not None:
            return err
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                ensure_seeded,
            )
            snap = ensure_seeded().snapshot()
        except Exception:  # noqa: BLE001
            logger.debug("[IDEObservability] governor snapshot failed", exc_info=True)
            return self._json_response(
                request, 200,
                {"snapshot": None, "reason_code": "governor.unavailable"},
            )
        return self._json_response(request, 200, snap)

    async def _handle_governor_history(self, request: "web.Request") -> Any:
        """GET /observability/governor/history?limit=N — recent decisions."""
        err = self._governor_check_gates(request)
        if err is not None:
            return err
        try:
            limit = max(1, min(512, int(request.query.get("limit", "20"))))
        except (TypeError, ValueError):
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            from backend.core.ouroboros.governance.sensor_governor import (
                ensure_seeded,
            )
            decisions = ensure_seeded().recent_decisions(limit=limit)
        except Exception:  # noqa: BLE001
            decisions = []
        return self._json_response(
            request, 200,
            {
                "decisions": [d.to_dict() for d in decisions],
                "count": len(decisions),
                "limit": limit,
            },
        )

    async def _handle_memory_pressure(self, request: "web.Request") -> Any:
        """GET /observability/memory-pressure — current pressure snapshot."""
        err = self._memory_check_gates(request)
        if err is not None:
            return err
        try:
            from backend.core.ouroboros.governance.memory_pressure_gate import (
                get_default_gate,
            )
            snap = get_default_gate().snapshot()
        except Exception:  # noqa: BLE001
            logger.debug("[IDEObservability] memory pressure snapshot failed",
                         exc_info=True)
            return self._json_response(
                request, 200,
                {"snapshot": None, "reason_code": "memory.unavailable"},
            )
        return self._json_response(request, 200, snap)

    # --- W3(7) Slice 6 — cancel record surface ---------------------------

    def _read_cancel_records(self) -> Tuple[List[Dict[str, Any]], int]:
        """Read all records from cancel_records.jsonl in session_dir.

        Returns ``(records, parse_error_count)``. Never raises.
        """
        if self._session_dir is None:
            return [], 0
        artifact = self._session_dir / "cancel_records.jsonl"
        if not artifact.exists():
            return [], 0
        records: List[Dict[str, Any]] = []
        parse_errors = 0
        try:
            text = artifact.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return [], 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
            except (ValueError, TypeError):
                parse_errors += 1
        return records, parse_errors

    async def _handle_cancel_list(self, request: "web.Request") -> Any:
        """GET /observability/cancels — list of CancelRecord projections.

        Query params (optional):
          * ``origin``  — filter by origin substring (e.g. ``D:`` for all
                          operator cancels, ``E:cost`` for cost-watchdog).
          * ``op_id``   — filter to a specific op (exact match).
          * ``limit``   — 1..1000 (default 100).

        Shape::

            {"schema_version": "1.0", "records": [...], "count": N,
             "parse_errors": K}

        503 when no session_dir is bound (IDE-only mount, no harness).
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._session_dir is None:
            return self._error_response(
                request, 503, "ide_observability.cancels_unavailable",
            )
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        if limit < 1 or limit > 1000:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        records, parse_errors = self._read_cancel_records()
        # Filters
        origin_filter = request.query.get("origin", "").strip()
        op_id_filter = request.query.get("op_id", "").strip()
        filtered = []
        for r in records:
            if origin_filter and not str(r.get("origin", "")).startswith(origin_filter):
                continue
            if op_id_filter and r.get("op_id") != op_id_filter:
                continue
            filtered.append(r)
        # Newest-last is the natural JSONL order; UI usually wants
        # newest-first, so reverse for the response.
        filtered.reverse()
        truncated = filtered[:limit]
        return self._json_response(
            request, 200,
            {
                "schema_version": "1.0",
                "records": truncated,
                "count": len(truncated),
                "parse_errors": parse_errors,
            },
        )

    async def _handle_cancel_detail(self, request: "web.Request") -> Any:
        """GET /observability/cancels/{cancel_id} — full CancelRecord by id."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._session_dir is None:
            return self._error_response(
                request, 503, "ide_observability.cancels_unavailable",
            )
        cancel_id = request.match_info.get("cancel_id", "").strip()
        if not cancel_id or not re.match(r"^[A-Za-z0-9_\-:.]{1,128}$", cancel_id):
            return self._error_response(
                request, 400, "ide_observability.malformed_cancel_id",
            )
        records, _ = self._read_cancel_records()
        for r in records:
            if r.get("cancel_id") == cancel_id:
                return self._json_response(request, 200, r)
        return self._error_response(
            request, 404, "ide_observability.cancel_not_found",
        )

    # --- W2(4) Slice 3 — curiosity record surface ------------------------

    def _read_curiosity_records(self) -> Tuple[List[Dict[str, Any]], int]:
        """Read all records from curiosity_ledger.jsonl in session_dir.

        Returns ``(records, parse_error_count)``. Never raises. Mirrors
        :meth:`_read_cancel_records` byte-for-byte (same JSONL contract).
        """
        if self._session_dir is None:
            return [], 0
        artifact = self._session_dir / "curiosity_ledger.jsonl"
        if not artifact.exists():
            return [], 0
        records: List[Dict[str, Any]] = []
        parse_errors = 0
        try:
            text = artifact.read_text(encoding="utf-8")
        except Exception:  # noqa: BLE001
            return [], 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if isinstance(rec, dict):
                    records.append(rec)
            except (ValueError, TypeError):
                parse_errors += 1
        return records, parse_errors

    async def _handle_curiosity_list(self, request: "web.Request") -> Any:
        """GET /observability/curiosity — list of CuriosityRecord projections.

        Query params (optional):
          * ``op_id``   — filter to a specific op (exact match).
          * ``result``  — filter by result substring (e.g. ``allowed`` for
                          successful charges, ``denied:`` for any deny).
          * ``limit``   — 1..1000 (default 100).

        Shape::

            {"schema_version": "1.0", "records": [...], "count": N,
             "parse_errors": K}

        503 when no session_dir is bound (IDE-only mount, no harness).
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._session_dir is None:
            return self._error_response(
                request, 503, "ide_observability.curiosity_unavailable",
            )
        try:
            limit = int(request.query.get("limit", "100"))
        except ValueError:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        if limit < 1 or limit > 1000:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        records, parse_errors = self._read_curiosity_records()
        # Filters
        op_id_filter = request.query.get("op_id", "").strip()
        result_filter = request.query.get("result", "").strip()
        filtered = []
        for r in records:
            if op_id_filter and r.get("op_id") != op_id_filter:
                continue
            if result_filter and not str(r.get("result", "")).startswith(
                result_filter
            ):
                continue
            filtered.append(r)
        # Newest-last is the natural JSONL order; UI usually wants
        # newest-first, so reverse for the response.
        filtered.reverse()
        truncated = filtered[:limit]
        return self._json_response(
            request, 200,
            {
                "schema_version": "1.0",
                "records": truncated,
                "count": len(truncated),
                "parse_errors": parse_errors,
            },
        )

    async def _handle_curiosity_detail(self, request: "web.Request") -> Any:
        """GET /observability/curiosity/{question_id} — full CuriosityRecord."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._session_dir is None:
            return self._error_response(
                request, 503, "ide_observability.curiosity_unavailable",
            )
        question_id = request.match_info.get("question_id", "").strip()
        if not question_id or not re.match(
            r"^[A-Za-z0-9_\-:.]{1,128}$", question_id,
        ):
            return self._error_response(
                request, 400, "ide_observability.malformed_question_id",
            )
        records, _ = self._read_curiosity_records()
        for r in records:
            if r.get("question_id") == question_id:
                return self._json_response(request, 200, r)
        return self._error_response(
            request, 404, "ide_observability.curiosity_not_found",
        )

    # --- Priority #3 Slice 5b — Counterfactual Replay surface ----------------

    @staticmethod
    def _replay_master_enabled() -> bool:
        """Surface inherits Slice 1's master flag — disabled
        master → 403 ``replay.disabled`` regardless of any sub-
        gate state."""
        try:
            from backend.core.ouroboros.governance.verification.counterfactual_replay import (
                counterfactual_replay_enabled,
            )
        except ImportError:
            return False
        return counterfactual_replay_enabled()

    def _replay_parse_limit(
        self, request: "web.Request", default: int = 50,
    ) -> Optional[int]:
        """Parse ``?limit=N`` query param. Returns int or None on
        malformed input (caller emits 400)."""
        raw = request.query.get("limit")
        if raw is None:
            return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        # Same upper bound as Slice 4's read_replay_history caller cap.
        return max(1, min(200, n))

    # ---- Time-Travel Debugging Slice 1 (2026-05-02) ---------------------
    # The CausalityDAG navigation surface. Both handlers delegate to
    # verification.dag_navigation handlers, which already check
    # JARVIS_DAG_NAVIGATION_GET_ENABLED + dag_query_enabled() and
    # NEVER raise. The IDE-side mapping translates the substrate's
    # closed-vocabulary reason_code strings into HTTP status codes.

    #: Substrate reason_code → HTTP status mapping. Sourced from the
    #: dag_navigation handlers' documented vocabulary; any new
    #: reason_code added there must be reflected here.
    _DAG_REASON_CODE_TO_STATUS: Dict[str, int] = {
        "dag_navigation.disabled": 403,
        "dag_query.disabled": 403,
        "dag_navigation.not_found": 404,
        "dag_navigation.error": 500,
    }

    def _dag_status_for_reason(self, reason_code: str) -> int:
        """Map a dag_navigation reason_code to an HTTP status.
        Defaults to 500 for unknown codes (defensive — surfaces
        unexpected substrate signals as server errors rather than
        silently masking them as 200)."""
        return self._DAG_REASON_CODE_TO_STATUS.get(
            str(reason_code or ""), 500,
        )

    async def _handle_dag_session(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/dag/{session_id}`` — session-level
        CausalityDAG summary.

        Response shape (success)::

            {
              "schema_version": "1.0",
              "session_id": "<id>",
              "node_count": <int>,
              "edge_count": <int>,
              "record_ids": ["<id>", ...]   # capped at 1000 by substrate
            }

        Errors mirror the substrate's reason_code vocabulary:
        ``dag_navigation.disabled`` / ``dag_query.disabled`` → 403,
        ``dag_navigation.error`` → 500.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        session_id = request.match_info.get("session_id", "") or ""
        if not _SESSION_ID_RE.match(session_id):
            return self._error_response(
                request, 400, "ide_observability.invalid_session_id",
            )
        try:
            from backend.core.ouroboros.governance.verification.dag_navigation import (
                handle_dag_session,
            )
            result = handle_dag_session(session_id)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] dag_session import/call failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500, "ide_observability.dag_session_error",
            )
        if isinstance(result, dict) and result.get("error"):
            reason = str(result.get("reason_code", "") or "")
            return self._error_response(
                request, self._dag_status_for_reason(reason), reason,
            )
        return self._json_response(request, 200, result)

    async def _handle_dag_record(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/dag/{session_id}/{record_id}`` —
        record-level navigation: full DecisionRecord + parents +
        children + counterfactual_branches + subgraph_node_count.

        Response shape (success)::

            {
              "schema_version": "1.0",
              "record_id": "<id>",
              "record": {...},                # DecisionRecord.to_dict()
              "parents": ["<id>", ...],
              "children": ["<id>", ...],
              "counterfactual_branches": [...],
              "subgraph_node_count": <int>
            }

        404 (``dag_navigation.not_found``) when the record_id is
        unknown in the session DAG; other errors map per
        ``_DAG_REASON_CODE_TO_STATUS``.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        session_id = request.match_info.get("session_id", "") or ""
        record_id = request.match_info.get("record_id", "") or ""
        if not _SESSION_ID_RE.match(session_id):
            return self._error_response(
                request, 400, "ide_observability.invalid_session_id",
            )
        if not _RECORD_ID_RE.match(record_id):
            return self._error_response(
                request, 400, "ide_observability.invalid_record_id",
            )
        try:
            from backend.core.ouroboros.governance.verification.dag_navigation import (
                handle_dag_record,
            )
            result = handle_dag_record(record_id, session_id=session_id)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] dag_record import/call failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500, "ide_observability.dag_record_error",
            )
        if isinstance(result, dict) and result.get("error"):
            reason = str(result.get("reason_code", "") or "")
            return self._error_response(
                request, self._dag_status_for_reason(reason), reason,
            )
        return self._json_response(request, 200, result)

    async def _handle_replay_health(self, request: "web.Request") -> Any:
        """GET /observability/replay/health — surface liveness +
        flag bundle.

        Shape::

            {
              "schema_version": "1.0",
              "enabled": true,
              "engine_enabled": true,
              "comparator_enabled": true,
              "observer_enabled": true,
              "history_path": ".jarvis/replay_history/replay.jsonl",
              "history_count": 42
            }

        Returns 403 ``replay.disabled`` when master flag is off
        (port-scanner discipline — no signal about the surface
        being available)."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._replay_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.replay_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.verification.counterfactual_replay_engine import (
                replay_engine_enabled,
            )
            from backend.core.ouroboros.governance.verification.counterfactual_replay_comparator import (
                comparator_enabled,
            )
            from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
                read_replay_history,
                replay_history_path,
                replay_observer_enabled,
            )
            history = read_replay_history()
            return self._json_response(
                request, 200,
                {
                    "enabled": True,
                    "engine_enabled": bool(replay_engine_enabled()),
                    "comparator_enabled": bool(comparator_enabled()),
                    "observer_enabled": bool(replay_observer_enabled()),
                    "history_path": str(replay_history_path()),
                    "history_count": len(history),
                },
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] replay_health failed", exc_info=True,
            )
            return self._error_response(
                request, 500, "ide_observability.replay_health_error",
            )

    async def _handle_replay_baseline(self, request: "web.Request") -> Any:
        """GET /observability/replay/baseline — current
        ComparisonReport over the recent history.

        Shape mirrors ``ComparisonReport.to_dict()`` (outcome +
        stats + tightening + detail)."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._replay_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.replay_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
                compare_recent_history,
            )
            report = compare_recent_history()
            return self._json_response(request, 200, report.to_dict())
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] replay_baseline failed", exc_info=True,
            )
            return self._error_response(
                request, 500, "ide_observability.replay_baseline_error",
            )

    async def _handle_replay_verdicts(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/replay/verdicts?limit=N — last N
        StampedVerdicts from the JSONL ring buffer (default 50,
        clamped [1, 200]).

        Each entry is the StampedVerdict.to_dict() shape (verdict
        + tightening + cluster_kind + schema_version)."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._replay_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.replay_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        limit = self._replay_parse_limit(request, default=50)
        if limit is None:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            from backend.core.ouroboros.governance.verification.counterfactual_replay_observer import (
                read_replay_history,
            )
            history = read_replay_history(limit=limit)
            return self._json_response(
                request, 200,
                {
                    "verdicts": [sv.to_dict() for sv in history],
                    "count": len(history),
                    "limit": limit,
                },
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] replay_verdicts failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.replay_verdicts_error",
            )

    async def _handle_replay_history(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/replay/history?limit=N — alias for
        verdicts under the conventional /history naming. Same
        bounded projection + same shape."""
        return await self._handle_replay_verdicts(request)

    # ----------------------------------------------------------------------
    # Q4 Priority #2 Slice 4 — closure-loop observability surface
    # ----------------------------------------------------------------------

    @staticmethod
    def _closure_loop_master_enabled() -> bool:
        """Master flag for the closure-loop. Default-FALSE — port-
        scanner discipline + operator cost-ramp."""
        try:
            from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
                closure_loop_orchestrator_enabled,
            )
        except ImportError:
            return False
        return closure_loop_orchestrator_enabled()

    def _closure_loop_parse_limit(
        self, request: "web.Request", default: int = 50,
    ) -> Optional[int]:
        """Parse ``?limit=N``; bounded [1, 200]. Returns None on
        malformed (caller emits 400)."""
        raw = request.query.get("limit")
        if raw is None:
            return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            return None
        return max(1, min(200, n))

    async def _handle_closure_loop_health(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/closure-loop`` — surface liveness +
        flag bundle + outcome histogram. Read-only over the
        observer's ``stats()`` projection.

        Shape::

            {
              "schema_version": "1.0",
              "enabled": true,
              "history_path": ".jarvis/closure_loop_history.jsonl",
              "history_count": 12,
              "observer_running": true,
              "outcome_histogram": {"proposed": 3, ...}
            }

        Returns 403 ``closure_loop.disabled`` when master flag is
        off (port-scanner discipline)."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._closure_loop_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.closure_loop_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
                get_default_observer as _get_obs,
            )
            from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
                closure_loop_history_path,
                read_closure_history,
            )
            obs = _get_obs()
            history = read_closure_history()
            stats = obs.stats()
            return self._json_response(
                request, 200,
                {
                    "enabled": True,
                    "history_path": str(closure_loop_history_path()),
                    "history_count": len(history),
                    "observer_running": bool(stats.get("is_running")),
                    "outcome_histogram": dict(
                        stats.get("outcome_histogram", {}),
                    ),
                },
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] closure_loop_health failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.closure_loop_health_error",
            )

    async def _handle_closure_loop_history(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/closure-loop/history?limit=N`` —
        last N :class:`ClosureLoopRecord` from the JSONL ring buffer
        (default 50, clamped [1, 200]). Each entry is the
        ``ClosureLoopRecord.to_dict()`` shape."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._closure_loop_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.closure_loop_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        limit = self._closure_loop_parse_limit(request, default=50)
        if limit is None:
            return self._error_response(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
                read_closure_history,
            )
            history = read_closure_history(limit=limit)
            return self._json_response(
                request, 200,
                {"records": [r.to_dict() for r in history]},
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] closure_loop_history failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.closure_loop_history_error",
            )

    async def _handle_closure_loop_pending(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/closure-loop/pending`` — pending
        AdaptationLedger proposals on the
        ``COHERENCE_AUDITOR_BUDGETS`` surface (i.e. closure-loop-
        emitted proposals waiting for operator approval). Cross-
        cuts to ``/adapt`` REPL — same underlying ledger. Read-only
        projection."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._closure_loop_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.closure_loop_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.adaptation.ledger import (  # noqa: E501
                AdaptationSurface,
                OperatorDecisionStatus,
                get_default_ledger,
            )
            ledger = get_default_ledger()
            rows = ledger.history(
                surface=AdaptationSurface.COHERENCE_AUDITOR_BUDGETS,
                limit=200,
            )
            pending = [
                p.to_dict() for p in rows
                if p.operator_decision is (
                    OperatorDecisionStatus.PENDING
                )
            ]
            return self._json_response(
                request, 200, {"proposals": pending},
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] closure_loop_pending failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.closure_loop_pending_error",
            )

    async def _handle_closure_loop_stats(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/closure-loop/stats`` — full observer
        telemetry projection (pass_index + counters + dedup ring
        size + outcome histogram + watermark). Same shape as
        :meth:`ClosureLoopObserver.stats`."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._closure_loop_master_enabled():
            return self._error_response(
                request, 403, "ide_observability.closure_loop_disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        try:
            from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
                get_default_observer as _get_obs,
            )
            stats = _get_obs().stats()
            return self._json_response(request, 200, stats)
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] closure_loop_stats failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.closure_loop_stats_error",
            )

    # ----------------------------------------------------------------------
    # Q2 Slice 6 — DAG record diff
    # ----------------------------------------------------------------------

    async def _handle_dag_diff(
        self, request: "web.Request",
    ) -> Any:
        """``GET /observability/dag/{session_id}/diff/{record_id_a}/{record_id_b}``
        — deterministic state-diff between two records in the
        same DAG.

        Response shape (success): the substrate's
        ``RecordDiff.to_dict()`` projection — outcome / changes /
        fields_total / fields_changed / detail.

        Errors mirror the substrate's reason_code vocabulary:
        ``dag_navigation.disabled`` / ``dag_query.disabled`` →
        403, ``dag_navigation.not_found`` → 404 (with ``missing``
        field carrying which id was absent), other errors → 500.
        Path-param malformation surfaces as 400."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        session_id = request.match_info.get("session_id", "") or ""
        record_id_a = request.match_info.get("record_id_a", "") or ""
        record_id_b = request.match_info.get("record_id_b", "") or ""
        if not _SESSION_ID_RE.match(session_id):
            return self._error_response(
                request, 400, "ide_observability.invalid_session_id",
            )
        if not _RECORD_ID_RE.match(record_id_a):
            return self._error_response(
                request, 400, "ide_observability.invalid_record_id",
            )
        if not _RECORD_ID_RE.match(record_id_b):
            return self._error_response(
                request, 400, "ide_observability.invalid_record_id",
            )
        try:
            from backend.core.ouroboros.governance.verification.dag_navigation import (
                handle_dag_diff,
            )
            result = handle_dag_diff(
                record_id_a, record_id_b, session_id=session_id,
            )
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] dag_diff import/call failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500, "ide_observability.dag_diff_error",
            )
        if isinstance(result, dict) and result.get("error"):
            reason = str(result.get("reason_code", "") or "")
            return self._error_response(
                request, self._dag_status_for_reason(reason), reason,
            )
        return self._json_response(request, 200, result)

    # ----------------------------------------------------------------------
    # Gap #3 Slice 2 — L3 worktree topology projection
    # ----------------------------------------------------------------------
    #
    # Read-only projection over scheduler in-memory state + git
    # worktree paths. The substrate (verification.worktree_topology)
    # is pure stdlib; the HTTP layer here owns the async git query.
    #
    # When scheduler/worktree_manager refs are not wired (default
    # constructor), the routes return 503 cleanly — same graceful-
    # degradation discipline as the cancel routes.
    #
    # Lazy substrate import keeps ide_observability's module-level
    # import surface unchanged. The substrate's master flag
    # (JARVIS_WORKTREE_TOPOLOGY_ENABLED) gates the actual
    # projection; the HTTP gate here is master + rate-limit only.

    async def _handle_worktrees_list(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/worktrees — full topology projection.

        Shape::

            {
              "schema_version": "...",
              "topology": {<WorktreeTopology.to_dict()>}
            }

        Returns 503 when scheduler/worktree_manager refs are not
        wired (graceful degradation), 403 when the observability
        master is off, 429 on rate limit, 500 on substrate failure.
        """
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._scheduler is None:
            return self._error_response(
                request, 503,
                "ide_observability.worktrees_scheduler_not_wired",
            )
        try:
            topology = await self._compute_topology()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] worktrees_list failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.worktrees_compute_error",
            )
        return self._json_response(
            request, 200,
            {"topology": topology.to_dict()},
        )

    async def _handle_worktree_detail(
        self, request: "web.Request",
    ) -> Any:
        """GET /observability/worktrees/{graph_id} — per-graph
        detail.

        Shape::

            {
              "schema_version": "...",
              "graph": {<GraphTopology.to_dict()>}
            }

        Returns 404 when graph_id is not in the projection (the
        scheduler may have completed and cleaned up between request
        and response — this is normal, not an error)."""
        if not ide_observability_enabled():
            return self._error_response(
                request, 403, "ide_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "ide_observability.rate_limited",
            )
        if self._scheduler is None:
            return self._error_response(
                request, 503,
                "ide_observability.worktrees_scheduler_not_wired",
            )
        graph_id = request.match_info.get("graph_id", "")
        # Reuse the session_id regex — graph_ids are bounded
        # printable ascii and respect the same character class.
        if not _SESSION_ID_RE.fullmatch(graph_id):
            return self._error_response(
                request, 400,
                "ide_observability.malformed_graph_id",
            )
        try:
            topology = await self._compute_topology()
        except Exception:  # noqa: BLE001 — defensive
            logger.debug(
                "[IDEObservability] worktree_detail failed",
                exc_info=True,
            )
            return self._error_response(
                request, 500,
                "ide_observability.worktrees_compute_error",
            )
        for graph in topology.graphs:
            if graph.graph_id == graph_id:
                return self._json_response(
                    request, 200,
                    {"graph": graph.to_dict()},
                )
        return self._error_response(
            request, 404,
            "ide_observability.worktree_graph_not_found",
        )

    async def _compute_topology(self) -> Any:
        """Lazy-substrate-import + async git query + projection.
        Best-effort: a worktree_manager that's wired but failing
        the git query degrades to an empty path list (the
        substrate still projects the scheduler view; orphan-
        detection just sees zero on-disk worktrees)."""
        # Lazy import — keeps module-level surface unchanged so the
        # existing pure-stdlib pin on top-level imports stays
        # truthful for the GET routes that don't use the substrate.
        from backend.core.ouroboros.governance.verification.worktree_topology import (  # noqa: E501
            compute_worktree_topology,
        )
        paths: List[str] = []
        if self._worktree_manager is not None:
            try:
                paths = list(
                    await self._worktree_manager.list_worktree_paths(),
                )
            except Exception:  # noqa: BLE001 — defensive
                paths = []
        return compute_worktree_topology(
            scheduler=self._scheduler,
            git_worktree_paths=paths,
        )



def register_shipped_invariants() -> list:
    """Gap #3 Slice 5 cage close — module-owned shipped-code
    invariant for the worktree GET routes.

    Pinned guarantees:
      * Both worktree route paths registered
        (``/observability/worktrees`` +
        ``/observability/worktrees/{graph_id}``).
      * Both handler methods present.
      * Constructor accepts ``scheduler`` + ``worktree_manager``
        kwargs (defense against refactor that drops the duck-
        typed wiring).
      * No authority-carrying imports in the new code path.

    NEVER raises. Discovery loop catches exceptions."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate_worktree_routes_surface(tree, source) -> tuple:
        violations = []
        required = (
            ('"/observability/worktrees"',
             'list route path must remain registered'),
            ('"/observability/worktrees/{graph_id}"',
             'detail route path must remain registered'),
            ('_handle_worktrees_list',
             'list handler method must remain'),
            ('_handle_worktree_detail',
             'detail handler method must remain'),
            ('_compute_topology',
             'shared compute helper must remain'),
            ('scheduler: Optional[Any] = None',
             'scheduler kwarg must remain on constructor'),
            ('worktree_manager: Optional[Any] = None',
             'worktree_manager kwarg must remain on constructor'),
        )
        for symbol, reason in required:
            if symbol not in source:
                violations.append(
                    f"ide_observability worktree routes "
                    f"dropped {symbol!r} — {reason}"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="gap3_ide_observability_worktrees_routes",
            target_file=(
                "backend/core/ouroboros/governance/ide_observability.py"
            ),
            description=(
                "Gap #3 Slice 2 GET routes: both worktree route "
                "paths registered + both handler methods + shared "
                "compute helper + constructor accepts scheduler + "
                "worktree_manager kwargs. Catches refactor that "
                "breaks the read-surface wiring."
            ),
            validate=_validate_worktree_routes_surface,
        ),
    ]
