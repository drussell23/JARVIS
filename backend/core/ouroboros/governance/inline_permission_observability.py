"""IDE observability — Inline Permission surface (Slice 4).

Read-only HTTP GET endpoints + SSE bridge exposing the per-tool-call
prompt queue and remembered-allow store to local IDE / editor clients.
Follows the Gap #6 / Problem #7 pattern:

* Four GET endpoints on the existing :class:`EventChannelServer`
  (loopback-only, rate-limited, CORS-allowlisted, schema-versioned).
* A bridge function that forwards controller + store lifecycle events
  into the existing :class:`StreamEventBroker` so SSE subscribers see
  prompts + grants without a second stream.
* Deny-by-default master switch (Slice 5 graduates).
* Grep-pinned authority invariant: this module MUST NOT import
  orchestrator / policy_engine / iron_gate / risk_tier / gate /
  tool_executor / semantic_guardian. Pure read surface.

Sanitization posture
--------------------

SSE payloads and GET responses echo ONLY the structured projections
emitted by :class:`InlinePromptController._project` and
:meth:`RememberedAllowStore.project_for_stream`. Both are sanitized at
the source: prompt projections carry ``arg_preview`` (truncated, never
the full raw fingerprint); grant projections carry ``pattern_preview``
(firewall-redacted via :func:`sanitize_for_firewall`). Raw tool args
and unredacted patterns never reach this module.

Authority posture
-----------------

Every mutation path (allow / deny / grant / revoke) stays in the REPL
surface. This module is a view layer; it creates no new operator
primitives. Bidirectional IDE approval is explicitly *out of scope*
for Slice 4 and deferred to a future ticket with its own §1 review.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from aiohttp import web

from backend.core.ouroboros.governance.ide_observability import (
    IDE_OBSERVABILITY_SCHEMA_VERSION,
    _cors_origin_patterns,
    assert_loopback_only,
)

logger = logging.getLogger(__name__)


INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION = "1.0"


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def inline_permission_observability_enabled() -> bool:
    """Master switch.

    Default: **``true``** (graduated in Slice 5 after Slices 1–4 shipped
    the full read-only GET + bridge-to-SSE stack with 201 governance
    tests green plus a live-fire proof
    (``scripts/livefire_inline_permission.py``) that exercises the
    complete stack end-to-end). Explicit ``"false"`` reverts to the
    pre-graduation deny-by-default posture — this is the runtime kill
    switch that makes the observability surface return 403 so port
    scanners see no signal.

    Note that graduation does NOT flip the authorization half:
    ``JARVIS_INLINE_PERMISSION_ENABLED`` stays default ``false`` by
    deliberate design (operator choice, mirrors Problem #7's Slice 5
    posture). Turning the tool hook on is an operator opt-in because
    the middleware's :class:`OpApprovedScope` wiring still flows from
    test / battle-harness injection rather than the production
    orchestrator — graduating BOTH flags would prompt on every
    unscoped edit/write/bash. Observability alone is safe: it exposes
    what the middleware *would* do, which is a pure safety benefit.

    The loopback-binding assertion + rate-limit caps + CORS allowlist
    + authority-invariant grep pin all remain in force regardless of
    this flag — graduation flips opt-in friction, NOT authority
    surface.
    """
    return os.environ.get(
        "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_ENABLED", "true",
    ).strip().lower() == "true"


def _rate_limit_per_min() -> int:
    try:
        return max(1, int(os.environ.get(
            "JARVIS_IDE_INLINE_PERMISSION_OBSERVABILITY_RATE_LIMIT_PER_MIN",
            "120",
        )))
    except (TypeError, ValueError):
        return 120


# ---------------------------------------------------------------------------
# Id regexes
# ---------------------------------------------------------------------------

# prompt_id is "{op_id}:{call_id}:{hex8}" — contains colons.
_PROMPT_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,256}$")

# grant_id is "ga-<10 hex>" — fixed shape.
_GRANT_ID_RE = re.compile(r"^ga-[0-9a-f]{6,32}$")


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class InlinePermissionObservabilityRouter:
    """Mounts the four inline-permission GET routes on an aiohttp app.

    Usage (from :class:`EventChannelServer.start`)::

        from backend.core.ouroboros.governance.inline_permission_observability \\
            import InlinePermissionObservabilityRouter
        assert_loopback_only(self._host)
        InlinePermissionObservabilityRouter().register_routes(app)

    Own rate-tracker, independent of the Gap #6 router budget.
    """

    def __init__(self) -> None:
        self._rate_tracker: Dict[str, List[float]] = {}

    def register_routes(self, app: "web.Application") -> None:
        app.router.add_get(
            "/observability/permissions/prompts",
            self._handle_prompts_list,
        )
        app.router.add_get(
            "/observability/permissions/prompts/{prompt_id}",
            self._handle_prompt_detail,
        )
        app.router.add_get(
            "/observability/permissions/grants",
            self._handle_grants_list,
        )
        app.router.add_get(
            "/observability/permissions/grants/{grant_id}",
            self._handle_grant_detail,
        )

    # --- request-path helpers --------------------------------------------

    def _client_key(self, request: "web.Request") -> str:
        peer = getattr(request, "remote", "") or "unknown"
        return str(peer)

    def _check_rate_limit(self, client_key: str) -> bool:
        limit = _rate_limit_per_min()
        now = time.monotonic()
        window_start = now - 60.0
        history = self._rate_tracker.setdefault(client_key, [])
        while history and history[0] < window_start:
            history.pop(0)
        if len(history) >= limit:
            return False
        history.append(now)
        return True

    def _cors_headers(self, request: "web.Request") -> Dict[str, str]:
        origin = request.headers.get("Origin", "") or ""
        if not origin:
            return {}
        for pattern in _cors_origin_patterns():
            try:
                if re.match(pattern, origin):
                    return {
                        "Access-Control-Allow-Origin": origin,
                        "Vary": "Origin",
                        "Access-Control-Allow-Methods": "GET, OPTIONS",
                    }
            except re.error:
                continue
        return {}

    def _json_response(
        self,
        request: "web.Request",
        status: int,
        payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION,
                **payload,
            }
        resp = web.json_response(payload, status=status)
        for k, v in self._cors_headers(request).items():
            resp.headers[k] = v
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error_response(
        self,
        request: "web.Request",
        status: int,
        reason_code: str,
    ) -> Any:
        return self._json_response(
            request, status=status,
            payload={"error": True, "reason_code": reason_code},
        )

    # --- guards ----------------------------------------------------------

    def _guard(self, request: "web.Request") -> Optional[Any]:
        if not inline_permission_observability_enabled():
            return self._error_response(
                request, 403, "inline_permission_observability.disabled",
            )
        if not self._check_rate_limit(self._client_key(request)):
            return self._error_response(
                request, 429, "inline_permission_observability.rate_limited",
            )
        return None

    # --- handlers --------------------------------------------------------

    async def _handle_prompts_list(self, request: "web.Request") -> Any:
        """GET /observability/permissions/prompts — list pending prompts.

        Shape::

            {
              "schema_version": "1.0",
              "prompts": [ { ...projection... }, ... ],
              "count": N
            }
        """
        guard = self._guard(request)
        if guard is not None:
            return guard
        controller = _load_controller()
        if controller is None:
            return self._error_response(
                request, 503,
                "inline_permission_observability.controller_unavailable",
            )
        snapshots = [
            s for s in controller.snapshot_all()
            if s.get("state") == "pending"
        ]
        return self._json_response(
            request, 200,
            {"prompts": snapshots, "count": len(snapshots)},
        )

    async def _handle_prompt_detail(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        prompt_id = request.match_info.get("prompt_id", "")
        if not _PROMPT_ID_RE.match(prompt_id):
            return self._error_response(
                request, 400,
                "inline_permission_observability.malformed_prompt_id",
            )
        controller = _load_controller()
        if controller is None:
            return self._error_response(
                request, 503,
                "inline_permission_observability.controller_unavailable",
            )
        snap = controller.snapshot(prompt_id)
        if snap is None:
            return self._error_response(
                request, 404,
                "inline_permission_observability.unknown_prompt_id",
            )
        return self._json_response(request, 200, {"prompt": snap})

    async def _handle_grants_list(self, request: "web.Request") -> Any:
        """GET /observability/permissions/grants — active grants (per CWD).

        Returns an empty list when the store module is unavailable or
        when no grants exist in the current repo scope.
        """
        guard = self._guard(request)
        if guard is not None:
            return guard
        store = _load_store()
        if store is None:
            return self._json_response(
                request, 200,
                {"grants": [], "count": 0},
            )
        grants = store.list_active()
        projections = [store.project_for_stream(g) for g in grants]
        return self._json_response(
            request, 200,
            {"grants": projections, "count": len(projections)},
        )

    async def _handle_grant_detail(self, request: "web.Request") -> Any:
        guard = self._guard(request)
        if guard is not None:
            return guard
        grant_id = request.match_info.get("grant_id", "")
        if not _GRANT_ID_RE.match(grant_id):
            return self._error_response(
                request, 400,
                "inline_permission_observability.malformed_grant_id",
            )
        store = _load_store()
        if store is None:
            return self._error_response(
                request, 503,
                "inline_permission_observability.store_unavailable",
            )
        g = store.get(grant_id)
        if g is None:
            return self._error_response(
                request, 404,
                "inline_permission_observability.unknown_grant_id",
            )
        return self._json_response(
            request, 200,
            {"grant": store.project_for_stream(g)},
        )


# ---------------------------------------------------------------------------
# Bridge — controller + store → StreamEventBroker
# ---------------------------------------------------------------------------


_GRANTS_SENTINEL_OP_ID = "inline_perm_grants"
"""SSE ``op_id`` used for grant events. Prompt events carry the real op_id
so ?op_id=X filters work; grants are repo-scoped, not op-scoped, and use
this sentinel so clients can still filter to grant events alone."""


def bridge_inline_permission_to_broker(
    *,
    controller: Any,
    store: Optional[Any] = None,
    broker: Optional[Any] = None,
) -> Callable[[], None]:
    """Subscribe controller + store transitions; publish to the SSE broker.

    Returns a composite unsubscribe callback that removes both listeners.

    Controller → publishes one of:
        ``inline_prompt_pending``
        ``inline_prompt_allowed``
        ``inline_prompt_denied``
        ``inline_prompt_expired``
        ``inline_prompt_paused``
    with the controller's bounded projection as payload.

    Store → publishes ``inline_grant_created`` or ``inline_grant_revoked``
    with the sanitized grant projection as payload.

    Listener exceptions are swallowed (best-effort § observability); the
    controller / store paths never block on SSE consumers.
    """
    from backend.core.ouroboros.governance.ide_observability_stream import (
        get_default_broker,
        stream_enabled,
        _VALID_EVENT_TYPES,
    )

    bk = broker or get_default_broker()

    # Pinned-inline so the closure captures the exact event-types allowed
    # by THIS broker version. Defends against an older broker instance
    # silently dropping new event types.
    _inline_event_types = frozenset({
        "inline_prompt_pending",
        "inline_prompt_allowed",
        "inline_prompt_denied",
        "inline_prompt_expired",
        "inline_prompt_paused",
        "inline_grant_created",
        "inline_grant_revoked",
    })
    # Sanity: if the broker doesn't know these, skip (defensive).
    missing = _inline_event_types - set(_VALID_EVENT_TYPES)
    if missing:
        logger.warning(
            "[InlinePermissionBridge] broker missing event types: %s",
            sorted(missing),
        )

    def _on_controller_transition(payload: Dict[str, Any]) -> None:
        if not stream_enabled():
            return
        event_type = payload.get("event_type", "")
        if event_type not in _inline_event_types:
            return
        projection = payload.get("projection") or {}
        op_id = projection.get("op_id", "") or ""
        try:
            bk.publish(event_type, op_id, projection)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[InlinePermissionBridge] controller publish failed: %s", exc,
            )

    def _on_store_change(payload: Dict[str, Any]) -> None:
        if not stream_enabled():
            return
        event_type = payload.get("event_type", "")
        if event_type not in _inline_event_types:
            return
        projection = payload.get("projection") or {}
        try:
            bk.publish(event_type, _GRANTS_SENTINEL_OP_ID, projection)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[InlinePermissionBridge] store publish failed: %s", exc,
            )

    unsub_controller = controller.on_transition(_on_controller_transition)
    unsub_store: Optional[Callable[[], None]] = None
    if store is not None:
        unsub_store = store.on_change(_on_store_change)

    def _unsub_all() -> None:
        try:
            unsub_controller()
        except Exception:  # noqa: BLE001
            pass
        if unsub_store is not None:
            try:
                unsub_store()
            except Exception:  # noqa: BLE001
                pass

    logger.info(
        "[InlinePermissionBridge] attached "
        "controller=%s store=%s broker=%s",
        type(controller).__name__,
        type(store).__name__ if store is not None else "-",
        type(bk).__name__,
    )
    return _unsub_all


# ---------------------------------------------------------------------------
# Lazy accessors (reach into singletons without circular imports)
# ---------------------------------------------------------------------------


def _load_controller() -> Optional[Any]:
    try:
        from backend.core.ouroboros.governance.inline_permission_prompt import (
            get_default_controller,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[InlinePermissionObservability] controller import failed: %s", exc,
        )
        return None
    try:
        return get_default_controller()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[InlinePermissionObservability] controller init failed: %s", exc,
        )
        return None


def _load_store() -> Optional[Any]:
    try:
        from pathlib import Path
        from backend.core.ouroboros.governance.inline_permission_memory import (
            get_store_for_repo,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[InlinePermissionObservability] store import failed: %s", exc,
        )
        return None
    try:
        return get_store_for_repo(Path.cwd())
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[InlinePermissionObservability] store init failed: %s", exc,
        )
        return None


__all__ = [
    "INLINE_PERMISSION_OBSERVABILITY_SCHEMA_VERSION",
    "InlinePermissionObservabilityRouter",
    "bridge_inline_permission_to_broker",
    "inline_permission_observability_enabled",
]

# Silence unused-import guards on the re-export line.
_ = (IDE_OBSERVABILITY_SCHEMA_VERSION, assert_loopback_only)
