"""Tests for Session Browser extension Slice 2 — pinned + /session diff."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.session_browser import (
    Bookmark,
    BookmarkStore,
    SessionBrowser,
    SessionBrowserError,
    SessionIndex,
    dispatch_session_command,
)


# ===========================================================================
# Test harness helpers
# ===========================================================================


def _mk_session(
    root: Path, session_id: str,
    *,
    ops_total: int = 2, ops_applied: int = 1,
    stop_reason: str = "complete", cost: float = 0.10,
) -> Path:
    d = root / session_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "stop_reason": stop_reason,
        "stats": {
            "ops_total": ops_total, "ops_applied": ops_applied,
            "cost": {"spent_usd": cost},
        },
    }))
    return d


def _mk_browser(tmp_path: Path) -> SessionBrowser:
    return SessionBrowser(
        index=SessionIndex(root=tmp_path / "sessions"),
        bookmarks=BookmarkStore(bookmark_root=tmp_path / "bmroot"),
    )


# ===========================================================================
# Bookmark dataclass — backward-compat
# ===========================================================================


def test_bookmark_default_pinned_false():
    bm = Bookmark(session_id="x")
    assert bm.pinned is False


def test_bookmark_frozen_even_with_pinned():
    bm = Bookmark(session_id="x", pinned=True)
    with pytest.raises((AttributeError, TypeError)):
        bm.pinned = False  # type: ignore[misc]


# ===========================================================================
# BookmarkStore — pin/unpin
# ===========================================================================


def test_pin_new_session_creates_pinned_bookmark(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    bm = store.pin("bt-test-001")
    assert bm.pinned is True
    assert store.has("bt-test-001")
    assert store.is_pinned("bt-test-001")


def test_pin_preserves_existing_note(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-test-001", note="first run")
    store.pin("bt-test-001")
    pinned = store.list_pinned()
    assert len(pinned) == 1
    assert pinned[0].note == "first run"
    assert pinned[0].pinned is True


def test_pin_overrides_empty_note_with_new_note(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.pin("bt-test-001", note="important")
    assert store.list_pinned()[0].note == "important"


def test_unpin_clears_flag_but_keeps_bookmark(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.pin("bt-test-001")
    assert store.unpin("bt-test-001") is True
    assert store.has("bt-test-001")  # still bookmarked
    assert not store.is_pinned("bt-test-001")


def test_unpin_returns_false_when_not_pinned(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-test-001")  # bookmarked, not pinned
    assert store.unpin("bt-test-001") is False


def test_unpin_returns_false_for_unknown_session(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.unpin("bt-missing") is False


def test_pin_rejects_malformed_session_id(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    with pytest.raises(SessionBrowserError):
        store.pin("bad id with spaces")


def test_list_pinned_returns_only_pinned(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-a")  # bookmarked, not pinned
    store.pin("bt-b")
    store.pin("bt-c")
    pinned = {bm.session_id for bm in store.list_pinned()}
    assert pinned == {"bt-b", "bt-c"}


def test_remove_drops_pin_too(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.pin("bt-test-001")
    assert store.remove("bt-test-001") is True
    assert not store.is_pinned("bt-test-001")
    assert not store.has("bt-test-001")


# ===========================================================================
# Persistence — pinned survives round-trip, legacy JSON tolerated
# ===========================================================================


def test_pinned_persists_across_reload(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.pin("bt-persist", note="ship it")
    del store
    # New store instance reads from disk
    store2 = BookmarkStore(bookmark_root=tmp_path)
    pinned = store2.list_pinned()
    assert len(pinned) == 1
    assert pinned[0].session_id == "bt-persist"
    assert pinned[0].note == "ship it"
    assert pinned[0].pinned is True


def test_legacy_bookmark_json_without_pinned_key_tolerated(tmp_path: Path):
    """Pre-extension JSON had no `pinned` field — still must load."""
    legacy = [
        {
            "session_id": "bt-legacy-001",
            "note": "old-format",
            "created_at_iso": "2026-04-15T12:00:00+00:00",
        }
    ]
    (tmp_path / "session_bookmarks.json").write_text(json.dumps(legacy))
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.has("bt-legacy-001")
    assert not store.is_pinned("bt-legacy-001")


# ===========================================================================
# Listener hook on BookmarkStore
# ===========================================================================


def test_on_change_fires_on_add(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    store.add("bt-test-001", note="hello")
    types = [e["event_type"] for e in events]
    assert "bookmark_added" in types
    assert events[0]["session_id"] == "bt-test-001"


def test_on_change_fires_on_remove(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-test-001")
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    store.remove("bt-test-001")
    types = [e["event_type"] for e in events]
    assert "bookmark_removed" in types


def test_on_change_fires_on_pin_and_unpin(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    events: List[Dict[str, Any]] = []
    store.on_change(events.append)
    store.pin("bt-test-001")
    store.unpin("bt-test-001")
    types = [e["event_type"] for e in events]
    assert "bookmark_pinned" in types
    assert "bookmark_unpinned" in types


def test_on_change_unsubscribe_stops_events(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    events: List[Dict[str, Any]] = []
    unsub = store.on_change(events.append)
    store.add("bt-test-001")
    count_before = len(events)
    unsub()
    store.add("bt-test-002")
    assert len(events) == count_before


def test_on_change_listener_exception_never_raises(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    def _explode(_):
        raise RuntimeError("oops")
    store.on_change(_explode)
    # Must not raise
    store.pin("bt-test-001")
    assert store.is_pinned("bt-test-001")


# ===========================================================================
# SessionBrowser — pin passthrough + list_pinned_with_records
# ===========================================================================


def test_browser_pin_passthrough(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    browser.pin("bt-xyz", note="keep")
    assert browser.is_pinned("bt-xyz")


def test_browser_list_pinned_with_records_matches_index(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-a")
    _mk_session(sessions, "bt-b")
    browser.index.scan()
    browser.pin("bt-a")
    pairs = browser.list_pinned_with_records()
    # Exactly one pinned; the record comes from the index
    assert len(pairs) == 1
    bm, rec = pairs[0]
    assert bm.session_id == "bt-a"
    assert rec is not None
    assert rec.session_id == "bt-a"


def test_browser_list_pinned_with_records_unknown_session(tmp_path: Path):
    """Pinning a session that doesn't exist on disk yields (bm, None)."""
    browser = _mk_browser(tmp_path)
    browser.pin("bt-missing")
    pairs = browser.list_pinned_with_records()
    assert len(pairs) == 1
    bm, rec = pairs[0]
    assert bm.session_id == "bt-missing"
    assert rec is None


# ===========================================================================
# SessionBrowser.diff
# ===========================================================================


def test_browser_diff_happy_path(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-a", ops_total=3, ops_applied=2, cost=0.10)
    _mk_session(sessions, "bt-b", ops_total=5, ops_applied=5, cost=0.20)
    diff = browser.diff("bt-a", "bt-b")
    assert diff is not None
    assert diff.left_session_id == "bt-a"
    assert diff.right_session_id == "bt-b"


def test_browser_diff_unknown_session_returns_none(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    assert browser.diff("bt-nope", "bt-also-nope") is None


# ===========================================================================
# REPL /session diff
# ===========================================================================


def test_repl_diff_renders_both_ids(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-a")
    _mk_session(sessions, "bt-b")
    res = dispatch_session_command(
        "/session diff bt-a bt-b", browser=browser,
    )
    assert res.ok
    assert "bt-a" in res.text
    assert "bt-b" in res.text
    assert "Session diff" in res.text


def test_repl_diff_too_few_args(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command(
        "/session diff", browser=browser,
    )
    assert not res.ok
    assert "/session diff" in res.text


def test_repl_diff_unknown_session(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command(
        "/session diff bt-ghost bt-other-ghost", browser=browser,
    )
    assert not res.ok
    assert "unknown" in res.text.lower()


# ===========================================================================
# REPL /session pin / unpin / pinned
# ===========================================================================


def test_repl_pin_ok(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command(
        "/session pin bt-test-001 first big win", browser=browser,
    )
    assert res.ok
    assert "pinned" in res.text
    assert browser.is_pinned("bt-test-001")


def test_repl_pin_malformed_id(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command(
        "/session pin 'bad id'", browser=browser,
    )
    assert not res.ok


def test_repl_unpin_ok(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    browser.pin("bt-test-001")
    res = dispatch_session_command(
        "/session unpin bt-test-001", browser=browser,
    )
    assert res.ok
    assert not browser.is_pinned("bt-test-001")


def test_repl_unpin_not_pinned(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command(
        "/session unpin bt-test-001", browser=browser,
    )
    assert not res.ok
    assert "not pinned" in res.text


def test_repl_pinned_empty(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command("/session pinned", browser=browser)
    assert res.ok
    assert "no pinned" in res.text.lower()


def test_repl_pinned_lists_entries(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-a")
    browser.index.scan()
    browser.pin("bt-a", note="keeper")
    res = dispatch_session_command("/session pinned", browser=browser)
    assert res.ok
    assert "bt-a" in res.text
    assert "keeper" in res.text


def test_repl_default_entry_surfaces_pinned_section(tmp_path: Path):
    """/session with no args renders a pinned header before recent."""
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-recent-1")
    _mk_session(sessions, "bt-recent-2")
    _mk_session(sessions, "bt-pinned-1")
    browser.index.scan()
    browser.pin("bt-pinned-1", note="important")
    res = dispatch_session_command("/session", browser=browser)
    assert res.ok
    # Pinned section ordering guarantee: pinned block precedes recent block.
    idx_pinned_header = res.text.find("Pinned session")
    idx_recent_header = res.text.find("recent session")
    assert idx_pinned_header != -1, res.text
    assert idx_recent_header != -1, res.text
    assert idx_pinned_header < idx_recent_header


def test_repl_default_entry_no_pinned_section_when_none(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-recent-1")
    browser.index.scan()
    res = dispatch_session_command("/session", browser=browser)
    assert res.ok
    assert "Pinned session" not in res.text
    assert "recent session" in res.text


def test_repl_list_pinned_flag(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    sessions = tmp_path / "sessions"
    _mk_session(sessions, "bt-a")
    _mk_session(sessions, "bt-b")
    browser.index.scan()
    browser.pin("bt-a")
    res = dispatch_session_command(
        "/session list --pinned", browser=browser,
    )
    assert res.ok
    assert "bt-a" in res.text
    assert "bt-b" not in res.text


def test_repl_help_mentions_new_verbs(tmp_path: Path):
    browser = _mk_browser(tmp_path)
    res = dispatch_session_command("/session help", browser=browser)
    assert res.ok
    for kw in ("diff", "pin", "unpin", "pinned"):
        assert kw in res.text, f"help must mention {kw}"
