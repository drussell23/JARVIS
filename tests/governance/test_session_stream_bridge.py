"""Tests for session_stream_bridge (extension Slice 3)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.ide_observability_stream import (
    EVENT_TYPE_SESSION_ADDED,
    EVENT_TYPE_SESSION_BOOKMARKED,
    EVENT_TYPE_SESSION_PINNED,
    EVENT_TYPE_SESSION_RESCAN,
    EVENT_TYPE_SESSION_UNBOOKMARKED,
    EVENT_TYPE_SESSION_UNPINNED,
    StreamEventBroker,
    _VALID_EVENT_TYPES,
)
from backend.core.ouroboros.governance.session_browser import (
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
)
from backend.core.ouroboros.governance.session_stream_bridge import (
    SESSION_STREAM_BRIDGE_SCHEMA_VERSION,
    bridge_bookmark_store_to_broker,
    bridge_session_browser_to_broker,
    bridge_session_index_to_broker,
)


# ===========================================================================
# schema version
# ===========================================================================


def test_schema_version_pinned():
    assert SESSION_STREAM_BRIDGE_SCHEMA_VERSION == "session_stream_bridge.v1"


# ===========================================================================
# Event-type vocabulary extension
# ===========================================================================


def test_session_event_types_admitted_to_broker_vocab():
    for ev in (
        EVENT_TYPE_SESSION_ADDED,
        EVENT_TYPE_SESSION_RESCAN,
        EVENT_TYPE_SESSION_BOOKMARKED,
        EVENT_TYPE_SESSION_UNBOOKMARKED,
        EVENT_TYPE_SESSION_PINNED,
        EVENT_TYPE_SESSION_UNPINNED,
    ):
        assert ev in _VALID_EVENT_TYPES, ev


# ===========================================================================
# Broker helper — inspect history under the hood
# ===========================================================================


def _broker_events_by_type(broker: StreamEventBroker) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for ev in list(broker._history):  # noqa: SLF001 — test
        out.setdefault(ev.event_type, []).append(ev.to_dict())
    return out


def _mk_session(root: Path, session_id: str, *, stop_reason: str = "complete") -> None:
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "stop_reason": stop_reason,
        "stats": {"ops_total": 1, "ops_applied": 1},
    }))


# ===========================================================================
# Index bridge — scan fires session_added + session_rescan
# ===========================================================================


def test_index_scan_publishes_session_added(tmp_path: Path):
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    bridge_session_index_to_broker(index=index, broker=broker)
    _mk_session(sessions, "bt-new-001")
    index.scan()
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_ADDED in types
    assert types[EVENT_TYPE_SESSION_ADDED][0]["op_id"] == "bt-new-001"


def test_index_scan_publishes_rescan_complete(tmp_path: Path):
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    bridge_session_index_to_broker(index=index, broker=broker)
    _mk_session(sessions, "bt-a")
    index.scan()
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_RESCAN in types
    rescan = types[EVENT_TYPE_SESSION_RESCAN][0]
    assert rescan["payload"]["total_records"] == 1


def test_index_bridge_summary_shape_is_bounded(tmp_path: Path):
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    bridge_session_index_to_broker(index=index, broker=broker)
    _mk_session(sessions, "bt-shape-001")
    index.scan()
    types = _broker_events_by_type(broker)
    added = types[EVENT_TYPE_SESSION_ADDED][0]
    payload = added["payload"]
    # Summary keys we promise to emit — IDE consumers depend on these.
    for key in (
        "session_id", "short_session_id", "stop_reason",
        "ops_total", "ops_applied", "cost_spent_usd",
        "ok_outcome", "parse_error", "has_replay_html", "mtime_iso",
    ):
        assert key in payload, f"missing {key}"


def test_index_rescan_new_or_updated_clipped_to_32(tmp_path: Path):
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    bridge_session_index_to_broker(index=index, broker=broker)
    for i in range(40):
        _mk_session(sessions, f"bt-burst-{i:03d}")
    index.scan()
    types = _broker_events_by_type(broker)
    rescan = types[EVENT_TYPE_SESSION_RESCAN][-1]
    assert len(rescan["payload"]["new_or_updated"]) == 32
    assert rescan["payload"]["new_or_updated_overflow"] is True
    assert rescan["payload"]["total_records"] == 40


def test_index_bridge_unsubscribe_stops_events(tmp_path: Path):
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    unsub = bridge_session_index_to_broker(index=index, broker=broker)
    _mk_session(sessions, "bt-a")
    index.scan()
    baseline = len(list(broker._history))  # noqa: SLF001
    unsub()
    _mk_session(sessions, "bt-b")
    index.scan()
    after = len(list(broker._history))  # noqa: SLF001
    # After unsub, no new events land.
    assert after == baseline


# ===========================================================================
# Bookmark bridge
# ===========================================================================


def test_bookmark_bridge_add_publishes_session_bookmarked(tmp_path: Path):
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    store.add("bt-x", note="hello")
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_BOOKMARKED in types
    payload = types[EVENT_TYPE_SESSION_BOOKMARKED][0]["payload"]
    assert payload["session_id"] == "bt-x"
    assert payload["note"] == "hello"


def test_bookmark_bridge_remove_publishes_session_unbookmarked(tmp_path: Path):
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-x")
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    store.remove("bt-x")
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_UNBOOKMARKED in types


def test_bookmark_bridge_pin_publishes_session_pinned(tmp_path: Path):
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    store.pin("bt-x", note="keeper")
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_PINNED in types
    payload = types[EVENT_TYPE_SESSION_PINNED][0]["payload"]
    assert payload["session_id"] == "bt-x"
    assert payload["note"] == "keeper"


def test_bookmark_bridge_unpin_publishes_session_unpinned(tmp_path: Path):
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    store.pin("bt-x")
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    store.unpin("bt-x")
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_UNPINNED in types


def test_bookmark_bridge_noisy_payload_is_clipped(tmp_path: Path):
    """Notes over 500 chars get truncated before hitting the wire."""
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    store.add("bt-big", note="x" * 10_000)
    types = _broker_events_by_type(broker)
    payload = types[EVENT_TYPE_SESSION_BOOKMARKED][0]["payload"]
    assert len(payload["note"]) <= 500


def test_bookmark_bridge_unknown_event_type_is_silent(tmp_path: Path):
    """If a future bookmark event fires, the bridge ignores rather
    than publishes malformed frames."""
    broker = StreamEventBroker()
    store = BookmarkStore(bookmark_root=tmp_path)
    bridge_bookmark_store_to_broker(store=store, broker=broker)
    # Directly fire a made-up event on the store's internal listener hook.
    store._emit({  # noqa: SLF001 — test
        "event_type": "bookmark_future_event", "session_id": "bt-x",
    })
    types = _broker_events_by_type(broker)
    assert EVENT_TYPE_SESSION_BOOKMARKED not in types
    assert EVENT_TYPE_SESSION_PINNED not in types


# ===========================================================================
# Browser-combined bridge
# ===========================================================================


def test_browser_bridge_combined_unsub_tears_both_down(tmp_path: Path):
    broker = StreamEventBroker()
    browser = SessionBrowser(
        index=SessionIndex(root=tmp_path / "sessions"),
        bookmarks=BookmarkStore(bookmark_root=tmp_path / "bm"),
    )
    (tmp_path / "sessions").mkdir()
    unsub = bridge_session_browser_to_broker(browser=browser, broker=broker)
    # Fire events on both channels
    _mk_session(tmp_path / "sessions", "bt-q")
    browser.index.scan()
    browser.pin("bt-q")
    baseline = len(list(broker._history))  # noqa: SLF001
    unsub()
    # Post-unsub: nothing new lands
    _mk_session(tmp_path / "sessions", "bt-r")
    browser.index.scan()
    browser.pin("bt-r")
    assert len(list(broker._history)) == baseline  # noqa: SLF001


# ===========================================================================
# Authority invariant — bridge does not publish for unbridged stores
# ===========================================================================


def test_bridge_is_push_only_broker_never_mutates_index(tmp_path: Path):
    """Authority: publishing to the broker does not back-propagate."""
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    _mk_session(sessions, "bt-a")
    index.scan()
    snapshot_before = index.all_records()
    # Publish unrelated session_added to broker directly
    broker.publish(EVENT_TYPE_SESSION_ADDED, "bt-forged", {})
    snapshot_after = index.all_records()
    # Index has not mutated
    assert [r.session_id for r in snapshot_before] == [
        r.session_id for r in snapshot_after
    ]


# ===========================================================================
# SSE end-to-end — subscriber sees session_added frame
# ===========================================================================


@pytest.mark.asyncio
async def test_sse_subscriber_sees_session_added(tmp_path: Path):
    """Full broker → subscriber path for a single session_added event."""
    broker = StreamEventBroker()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    index = SessionIndex(root=sessions)
    bridge_session_index_to_broker(index=index, broker=broker)

    sub = broker.subscribe()
    assert sub is not None
    # Trigger an event
    _mk_session(sessions, "bt-live")
    index.scan()

    # Drain up to 4 frames; we expect session_added among them.
    types_seen = []
    for _ in range(4):
        try:
            ev = await asyncio.wait_for(sub.queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        types_seen.append(ev.event_type)
    assert EVENT_TYPE_SESSION_ADDED in types_seen
