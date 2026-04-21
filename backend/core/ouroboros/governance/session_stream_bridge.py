"""
Session stream bridge — SessionIndex / BookmarkStore → SSE broker.
==================================================================

Extension arc Slice 3 of the Session History Browser. Wires the
existing listener hooks on :class:`SessionIndex` and
:class:`BookmarkStore` to the
:class:`~ide_observability_stream.StreamEventBroker` so IDE clients
subscribed to ``GET /observability/stream`` see "a new session
landed" and "operator pinned/unpinned/bookmarked a session" events
without polling.

Authority boundary (pinned by graduation)
-----------------------------------------

* §1 read-only — the bridge is **push-only**. The broker is a
  transport; it never reaches back into the index or store.
* This module does NOT import orchestrator / policy_engine /
  iron_gate / risk_tier_floor / semantic_guardian / tool_executor /
  candidate_generator / change_engine. Grep-enforced at graduation.
* The bridge lives outside session_browser so that module stays
  authority-free (the same pattern Plan Approval used in
  :func:`ide_observability_stream.bridge_plan_approval_to_broker`).

Event-type mapping
------------------

Index → broker:
    ``session_record_added``   → :data:`EVENT_TYPE_SESSION_ADDED`
    ``session_rescan_complete`` → :data:`EVENT_TYPE_SESSION_RESCAN`

BookmarkStore → broker:
    ``bookmark_added``    → :data:`EVENT_TYPE_SESSION_BOOKMARKED`
    ``bookmark_removed``  → :data:`EVENT_TYPE_SESSION_UNBOOKMARKED`
    ``bookmark_pinned``   → :data:`EVENT_TYPE_SESSION_PINNED`
    ``bookmark_unpinned`` → :data:`EVENT_TYPE_SESSION_UNPINNED`

Each broker publish stuffs the session id into the ``op_id`` slot
so subscribers using ``?op_id=<session-id>`` can filter to one
session's timeline.

Payload discipline: summary-only. SSE frames stay small; full
record projection is available via ``GET /observability/sessions/<id>``
(Slice 4).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Mapping, Optional

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_SESSION_ADDED,
    EVENT_TYPE_SESSION_BOOKMARKED,
    EVENT_TYPE_SESSION_PINNED,
    EVENT_TYPE_SESSION_RESCAN,
    EVENT_TYPE_SESSION_UNBOOKMARKED,
    EVENT_TYPE_SESSION_UNPINNED,
    StreamEventBroker,
    get_default_broker,
    stream_enabled,
)

logger = logging.getLogger("Ouroboros.SessionStreamBridge")


SESSION_STREAM_BRIDGE_SCHEMA_VERSION: str = "session_stream_bridge.v1"


# ---------------------------------------------------------------------------
# Projection helpers — summary-only, SSE-frame-bounded
# ---------------------------------------------------------------------------


def _summarize_record(projection: Mapping[str, Any]) -> Dict[str, Any]:
    """Trim a :meth:`SessionRecord.project` to an SSE-sized summary.

    IDE clients fetch the full projection via
    ``GET /observability/sessions/<session_id>`` when they need it.
    """
    return {
        "session_id": projection.get("session_id") or "",
        "short_session_id": projection.get("short_session_id") or "",
        "stop_reason": projection.get("stop_reason") or "",
        "ops_total": projection.get("ops_total") or 0,
        "ops_applied": projection.get("ops_applied") or 0,
        "cost_spent_usd": projection.get("cost_spent_usd") or 0.0,
        "ok_outcome": bool(projection.get("ok_outcome")),
        "parse_error": bool(projection.get("parse_error")),
        "has_replay_html": bool(projection.get("has_replay_html")),
        "mtime_iso": projection.get("mtime_iso") or "",
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bridge_session_index_to_broker(
    index: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
) -> Callable[[], None]:
    """Wire :class:`SessionIndex` listener hooks to the broker.

    Returns an unsubscribe callable. Best-effort: if
    :func:`stream_enabled` is ``false`` the bridge still installs
    (so callers don't need to defensively re-wire when the env flag
    flips) — the broker publish short-circuits inside
    :func:`publish_task_event` callers, but the broker itself still
    accepts events regardless of the flag (the SSE *handler* is
    what gates on ``stream_enabled``).
    """
    if index is None:
        # Late import avoids a module-load cycle (broker module
        # doesn't know about session_browser; session_browser
        # doesn't know about the broker).
        from backend.core.ouroboros.governance.session_browser import (
            get_default_session_index,
        )
        index = get_default_session_index()
    if broker is None:
        broker = get_default_broker()

    def _publish(payload: Dict[str, Any]) -> None:
        event_type = payload.get("event_type")
        if event_type == "session_record_added":
            session_id = str(payload.get("session_id") or "")
            projection = payload.get("projection") or {}
            summary = _summarize_record(projection)
            broker.publish(
                EVENT_TYPE_SESSION_ADDED, session_id, summary,
            )
        elif event_type == "session_rescan_complete":
            new_or_updated = payload.get("new_or_updated") or []
            # Bounded — a scan that adds thousands of records in one
            # shot would bloat the frame; clip to 32.
            if isinstance(new_or_updated, list):
                clipped = [str(s) for s in new_or_updated[:32]]
            else:
                clipped = []
            broker.publish(
                EVENT_TYPE_SESSION_RESCAN,
                "",  # rescan has no single session id
                {
                    "new_or_updated": clipped,
                    "new_or_updated_overflow": (
                        isinstance(new_or_updated, list)
                        and len(new_or_updated) > 32
                    ),
                    "total_records": payload.get("total_records") or 0,
                    "scanned_at_ts": payload.get("scanned_at_ts") or 0.0,
                },
            )

    return index.on_change(_publish)


def bridge_bookmark_store_to_broker(
    store: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
) -> Callable[[], None]:
    """Wire :class:`BookmarkStore` listener hooks to the broker.

    Fires on bookmark add / remove / pin / unpin.
    """
    if store is None:
        from backend.core.ouroboros.governance.session_browser import (
            get_default_bookmark_store,
        )
        store = get_default_bookmark_store()
    if broker is None:
        broker = get_default_broker()

    _MAP = {
        "bookmark_added": EVENT_TYPE_SESSION_BOOKMARKED,
        "bookmark_removed": EVENT_TYPE_SESSION_UNBOOKMARKED,
        "bookmark_pinned": EVENT_TYPE_SESSION_PINNED,
        "bookmark_unpinned": EVENT_TYPE_SESSION_UNPINNED,
    }

    def _publish(payload: Dict[str, Any]) -> None:
        event_type = payload.get("event_type")
        broker_type = _MAP.get(str(event_type or ""))
        if broker_type is None:
            return  # Unknown bookmark event — stay silent
        session_id = str(payload.get("session_id") or "")
        summary: Dict[str, Any] = {"session_id": session_id}
        if "note" in payload:
            summary["note"] = str(payload.get("note") or "")[:500]
        if "pinned" in payload:
            summary["pinned"] = bool(payload.get("pinned"))
        broker.publish(broker_type, session_id, summary)

    return store.on_change(_publish)


def bridge_session_browser_to_broker(
    browser: Optional[Any] = None,
    broker: Optional[StreamEventBroker] = None,
) -> Callable[[], None]:
    """Convenience: bridge both the index AND the bookmark store of
    a single browser instance in one call.

    Returns an unsubscribe that tears both bridges down.
    """
    if browser is None:
        from backend.core.ouroboros.governance.session_browser import (
            get_default_session_browser,
        )
        browser = get_default_session_browser()
    if broker is None:
        broker = get_default_broker()
    unsub_index = bridge_session_index_to_broker(
        index=browser.index, broker=broker,
    )
    unsub_store = bridge_bookmark_store_to_broker(
        store=browser.bookmarks, broker=broker,
    )

    def _unsub_both() -> None:
        try:
            unsub_index()
        except Exception:  # noqa: BLE001
            logger.debug("[SessionBridge] index unsub failed", exc_info=True)
        try:
            unsub_store()
        except Exception:  # noqa: BLE001
            logger.debug("[SessionBridge] store unsub failed", exc_info=True)

    return _unsub_both


__all__ = [
    "SESSION_STREAM_BRIDGE_SCHEMA_VERSION",
    "bridge_bookmark_store_to_broker",
    "bridge_session_browser_to_broker",
    "bridge_session_index_to_broker",
]

_ = (List, stream_enabled)  # silence unused-import guards
