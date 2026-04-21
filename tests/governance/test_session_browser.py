"""Slices 2+3+4 tests — SessionIndex + BookmarkStore + Browser + REPL."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.session_browser import (
    SESSION_BROWSER_SCHEMA_VERSION,
    Bookmark,
    BookmarkStore,
    SessionBrowser,
    SessionBrowserError,
    SessionDispatchResult,
    SessionIndex,
    dispatch_session_command,
    get_default_session_browser,
    reset_default_session_singletons,
    set_default_session_browser,
)
from backend.core.ouroboros.governance.session_record import (
    SessionRecord,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_session_singletons()
    yield
    reset_default_session_singletons()


# ===========================================================================
# Fixture helpers
# ===========================================================================


def _make_session(
    root: Path, session_id: str, *,
    ops_total: int = 1, ops_applied: int = 0,
    stop_reason: str = "idle_timeout",
    cost_spent_usd: float = 0.0,
    verify_pass: int = 0, verify_total: int = 0,
    with_replay: bool = False,
    corrupt_summary: bool = False,
) -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    summary: Dict[str, Any] = {
        "schema_version": "summary.v2",
        "stop_reason": stop_reason,
        "started_at": "2026-04-21T12:00:00+00:00",
        "ended_at": "2026-04-21T12:01:00+00:00",
        "duration_s": 60.0,
        "stats": {
            "ops_total": ops_total,
            "ops_applied": ops_applied,
            "verify": {"pass": verify_pass, "total": verify_total},
            "cost": {"spent_usd": cost_spent_usd, "budget_usd": 0.50},
        },
    }
    if corrupt_summary:
        (session_dir / "summary.json").write_text("{not valid json")
    else:
        (session_dir / "summary.json").write_text(json.dumps(summary))
    (session_dir / "debug.log").write_text("log start\nline 2\n")
    if with_replay:
        (session_dir / "replay.html").write_text("<html/>")
    return session_dir


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert SESSION_BROWSER_SCHEMA_VERSION == "session_browser.v1"


# ===========================================================================
# SessionIndex — scan + cache
# ===========================================================================


def test_scan_empty_root(tmp_path: Path):
    idx = SessionIndex(root=tmp_path)
    assert idx.scan() == []


def test_scan_missing_root(tmp_path: Path):
    idx = SessionIndex(root=tmp_path / "does-not-exist")
    assert idx.scan() == []


def test_scan_discovers_multiple_sessions(tmp_path: Path):
    _make_session(tmp_path, "bt-a")
    _make_session(tmp_path, "bt-b")
    _make_session(tmp_path, "bt-c")
    idx = SessionIndex(root=tmp_path)
    records = idx.scan()
    ids = {r.session_id for r in records}
    assert ids == {"bt-a", "bt-b", "bt-c"}


def test_scan_ignores_non_directory_entries(tmp_path: Path):
    _make_session(tmp_path, "bt-a")
    (tmp_path / "not-a-session.txt").write_text("junk")
    idx = SessionIndex(root=tmp_path)
    records = idx.scan()
    assert [r.session_id for r in records] == ["bt-a"]


def test_scan_ignores_bad_name_directories(tmp_path: Path):
    _make_session(tmp_path, "bt-valid")
    (tmp_path / "weird name with spaces").mkdir()
    idx = SessionIndex(root=tmp_path)
    records = idx.scan()
    # Only the valid-named one is indexed
    assert [r.session_id for r in records] == ["bt-valid"]


def test_scan_returns_sorted_by_mtime_desc(tmp_path: Path):
    # Create in order; set mtimes explicitly
    d1 = _make_session(tmp_path, "bt-1")
    d2 = _make_session(tmp_path, "bt-2")
    d3 = _make_session(tmp_path, "bt-3")
    # Set mtimes so bt-1 is oldest, bt-3 newest
    now = time.time()
    os.utime(d1, (now - 300, now - 300))
    os.utime(d2, (now - 200, now - 200))
    os.utime(d3, (now - 100, now - 100))
    idx = SessionIndex(root=tmp_path)
    records = idx.scan()
    assert [r.session_id for r in records] == ["bt-3", "bt-2", "bt-1"]


def test_scan_reuses_cache_when_mtime_unchanged(tmp_path: Path):
    _make_session(tmp_path, "bt-cache")
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    # Second scan should return same records without re-parse
    listener_events: List[Dict[str, Any]] = []
    idx.on_change(listener_events.append)
    idx.scan()  # should not produce new "session_record_added"
    # Listener gets the rescan_complete event, but no record_added
    kinds = [e["event_type"] for e in listener_events]
    assert "session_rescan_complete" in kinds
    assert "session_record_added" not in kinds


def test_scan_detects_new_session_added(tmp_path: Path):
    _make_session(tmp_path, "bt-first")
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    # Add a second session
    _make_session(tmp_path, "bt-second")
    records = idx.scan()
    assert len(records) == 2
    assert {r.session_id for r in records} == {"bt-first", "bt-second"}


def test_scan_re_parses_when_mtime_advances(tmp_path: Path):
    session_dir = _make_session(tmp_path, "bt-updated")
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    # Touch the dir's mtime forward to simulate an update
    new_time = time.time() + 10
    os.utime(session_dir, (new_time, new_time))
    events: List[Dict[str, Any]] = []
    idx.on_change(events.append)
    idx.scan()
    # A record_added event should fire again for bt-updated
    record_events = [e for e in events
                      if e["event_type"] == "session_record_added"]
    assert any(e["session_id"] == "bt-updated" for e in record_events)


def test_scan_force_re_parses_everything(tmp_path: Path):
    _make_session(tmp_path, "bt-force")
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    events: List[Dict[str, Any]] = []
    idx.on_change(events.append)
    idx.scan(force=True)
    record_events = [e for e in events
                      if e["event_type"] == "session_record_added"]
    assert any(e["session_id"] == "bt-force" for e in record_events)


def test_scan_evicts_removed_sessions(tmp_path: Path):
    d1 = _make_session(tmp_path, "bt-evicted")
    _make_session(tmp_path, "bt-kept")
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    # Remove the first session
    import shutil
    shutil.rmtree(d1)
    records = idx.scan()
    assert {r.session_id for r in records} == {"bt-kept"}


def test_scan_includes_corrupt_with_marker(tmp_path: Path):
    _make_session(tmp_path, "bt-corrupt", corrupt_summary=True)
    idx = SessionIndex(root=tmp_path)
    records = idx.scan()
    assert len(records) == 1
    assert records[0].parse_error is True


# ===========================================================================
# SessionIndex — filtering
# ===========================================================================


def _populated_index(tmp_path: Path) -> SessionIndex:
    _make_session(
        tmp_path, "bt-ok-cheap",
        ops_total=3, ops_applied=1, cost_spent_usd=0.010,
        verify_pass=1, verify_total=1, stop_reason="complete",
    )
    _make_session(
        tmp_path, "bt-ok-expensive",
        ops_total=10, ops_applied=5, cost_spent_usd=0.500,
        verify_pass=5, verify_total=5, stop_reason="complete",
    )
    _make_session(
        tmp_path, "bt-bad",
        ops_total=2, ops_applied=0, cost_spent_usd=0.050,
        stop_reason="crashed",
    )
    _make_session(
        tmp_path, "bt-with-replay",
        ops_total=1, ops_applied=1, cost_spent_usd=0.002,
        verify_pass=1, verify_total=1, stop_reason="idle_timeout",
        with_replay=True,
    )
    _make_session(
        tmp_path, "bt-corrupt", corrupt_summary=True,
    )
    idx = SessionIndex(root=tmp_path)
    idx.scan()
    return idx


def test_filter_ok_outcome(tmp_path: Path):
    idx = _populated_index(tmp_path)
    ok = idx.filter(ok_outcome=True)
    ids = {r.session_id for r in ok}
    # bt-bad has non-standard stop_reason; bt-corrupt parse_error
    assert "bt-bad" not in ids
    assert "bt-corrupt" not in ids


def test_filter_bad_outcome(tmp_path: Path):
    idx = _populated_index(tmp_path)
    bad = idx.filter(ok_outcome=False)
    ids = {r.session_id for r in bad}
    assert "bt-bad" in ids or "bt-corrupt" in ids


def test_filter_parse_error_only(tmp_path: Path):
    idx = _populated_index(tmp_path)
    broken = idx.filter(parse_error=True)
    assert {r.session_id for r in broken} == {"bt-corrupt"}


def test_filter_has_replay_only(tmp_path: Path):
    idx = _populated_index(tmp_path)
    replays = idx.filter(has_replay=True)
    assert {r.session_id for r in replays} == {"bt-with-replay"}


def test_filter_min_ops(tmp_path: Path):
    idx = _populated_index(tmp_path)
    big = idx.filter(min_ops=5)
    assert {r.session_id for r in big} == {"bt-ok-expensive"}


def test_filter_max_cost(tmp_path: Path):
    idx = _populated_index(tmp_path)
    cheap = idx.filter(max_cost_usd=0.050)
    ids = {r.session_id for r in cheap}
    assert "bt-ok-cheap" in ids
    assert "bt-with-replay" in ids
    assert "bt-ok-expensive" not in ids


def test_filter_session_id_prefix(tmp_path: Path):
    idx = _populated_index(tmp_path)
    result = idx.filter(session_id_prefix="bt-ok-")
    ids = {r.session_id for r in result}
    assert ids == {"bt-ok-cheap", "bt-ok-expensive"}


def test_filter_stop_reason_match(tmp_path: Path):
    idx = _populated_index(tmp_path)
    result = idx.filter(stop_reason="complete")
    ids = {r.session_id for r in result}
    assert ids == {"bt-ok-cheap", "bt-ok-expensive"}


def test_filter_since_until(tmp_path: Path):
    idx = _populated_index(tmp_path)
    all_r = idx.all_records()
    assert all_r
    mid_mtime = sorted(r.mtime_ts for r in all_r)[len(all_r) // 2]
    subset = idx.filter(since_ts=mid_mtime)
    assert all(r.mtime_ts >= mid_mtime for r in subset)


def test_recent_respects_limit(tmp_path: Path):
    idx = _populated_index(tmp_path)
    assert len(idx.recent(limit=3)) == 3
    assert len(idx.recent(limit=100)) == 5  # total is 5


# ===========================================================================
# SessionIndex listener hooks
# ===========================================================================


def test_index_on_change_fires_rescan_event(tmp_path: Path):
    _make_session(tmp_path, "bt-evt")
    idx = SessionIndex(root=tmp_path)
    events: List[Dict[str, Any]] = []
    idx.on_change(events.append)
    idx.scan()
    kinds = {e["event_type"] for e in events}
    assert "session_rescan_complete" in kinds
    assert "session_record_added" in kinds


def test_index_listener_exception_isolated(tmp_path: Path):
    _make_session(tmp_path, "bt-safe")
    idx = SessionIndex(root=tmp_path)

    def _bad(_e: Dict[str, Any]) -> None:
        raise RuntimeError("intentional")

    idx.on_change(_bad)
    good: List[Dict[str, Any]] = []
    idx.on_change(good.append)
    idx.scan()
    assert good  # good listener still fired


def test_index_unsub_stops_delivery(tmp_path: Path):
    idx = SessionIndex(root=tmp_path)
    events: List[Dict[str, Any]] = []
    unsub = idx.on_change(events.append)
    idx.scan()
    n1 = len(events)
    unsub()
    idx.scan()
    assert len(events) == n1


# ===========================================================================
# BookmarkStore — persistence + authority
# ===========================================================================


def test_bookmark_add_remove_round_trip(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    bm = store.add("bt-abc", note="investigate crash")
    assert bm.session_id == "bt-abc"
    assert store.has("bt-abc")
    assert store.remove("bt-abc") is True
    assert store.has("bt-abc") is False


def test_bookmark_remove_unknown_returns_false(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.remove("bt-nope") is False


def test_bookmark_rejects_malformed_id(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    with pytest.raises(SessionBrowserError):
        store.add("weird name with spaces")


def test_bookmark_persists_across_instance(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-persist", note="worth revisiting")
    # Fresh store reads from disk
    store2 = BookmarkStore(bookmark_root=tmp_path)
    assert store2.has("bt-persist")
    bms = store2.list_all()
    assert bms[0].note == "worth revisiting"


def test_bookmark_list_contains_all(tmp_path: Path):
    """Sort is by created_at_iso (second granularity); we test presence +
    count rather than strict order for back-to-back adds."""
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-a")
    store.add("bt-b")
    store.add("bt-c")
    ids = {bm.session_id for bm in store.list_all()}
    assert ids == {"bt-a", "bt-b", "bt-c"}


def test_bookmark_note_truncated(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    bm = store.add("bt-trunc", note="x" * 2000)
    assert len(bm.note) <= 500


def test_bookmark_corrupt_file_starts_empty(tmp_path: Path):
    (tmp_path / "session_bookmarks.json").write_text("{not json")
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.list_all() == []


def test_bookmark_non_list_file_starts_empty(tmp_path: Path):
    (tmp_path / "session_bookmarks.json").write_text('{"a": 1}')
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.list_all() == []


def test_bookmark_skips_invalid_entries(tmp_path: Path):
    # File contains 2 valid + 1 invalid id
    (tmp_path / "session_bookmarks.json").write_text(json.dumps([
        {"session_id": "bt-valid-1"},
        {"session_id": "has spaces"},  # invalid
        {"session_id": "bt-valid-2", "note": "hi"},
    ]))
    store = BookmarkStore(bookmark_root=tmp_path)
    ids = {bm.session_id for bm in store.list_all()}
    assert ids == {"bt-valid-1", "bt-valid-2"}


def test_bookmark_env_override(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(
        "JARVIS_SESSION_BOOKMARK_ROOT", str(tmp_path / "env"),
    )
    store = BookmarkStore()
    assert str(tmp_path / "env") in str(store.path)


def test_bookmark_overwrite_same_session(tmp_path: Path):
    store = BookmarkStore(bookmark_root=tmp_path)
    store.add("bt-x", note="first")
    store.add("bt-x", note="second")
    assert len(store.list_all()) == 1
    assert store.list_all()[0].note == "second"


# ===========================================================================
# SessionBrowser — list / show / recent / bookmark / replay
# ===========================================================================


def _make_browser(tmp_path: Path) -> SessionBrowser:
    idx = SessionIndex(root=tmp_path / "sessions")
    bookmarks = BookmarkStore(bookmark_root=tmp_path / "bookmarks")
    return SessionBrowser(index=idx, bookmarks=bookmarks)


def test_browser_list_records_rescans(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-late")
    records = browser.list_records()
    assert [r.session_id for r in records] == ["bt-late"]


def test_browser_list_with_filter(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(
        tmp_path / "sessions", "bt-ok",
        ops_total=1, ops_applied=1,
        verify_pass=1, verify_total=1,
        stop_reason="complete",
    )
    _make_session(
        tmp_path / "sessions", "bt-bad",
        ops_total=1, stop_reason="crashed",
    )
    ok_records = browser.list_records(filters={"ok_outcome": True})
    assert all(r.ok_outcome for r in ok_records)
    assert {r.session_id for r in ok_records} == {"bt-ok"}


def test_browser_list_limit(tmp_path: Path):
    browser = _make_browser(tmp_path)
    for i in range(5):
        _make_session(tmp_path / "sessions", f"bt-{i}")
    records = browser.list_records(limit=2)
    assert len(records) == 2


def test_browser_show_existing(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-show")
    r = browser.show("bt-show")
    assert r is not None
    assert r.session_id == "bt-show"


def test_browser_show_unknown_returns_none(tmp_path: Path):
    browser = _make_browser(tmp_path)
    assert browser.show("bt-missing") is None


def test_browser_recent(tmp_path: Path):
    browser = _make_browser(tmp_path)
    for i in range(15):
        _make_session(tmp_path / "sessions", f"bt-{i:02d}")
    recent = browser.recent(limit=5)
    assert len(recent) == 5


def test_browser_bookmark_unbookmark_round_trip(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-mark")
    bm = browser.bookmark("bt-mark", note="look here")
    assert bm.note == "look here"
    assert browser.bookmarks.has("bt-mark")
    assert browser.unbookmark("bt-mark") is True
    assert not browser.bookmarks.has("bt-mark")


def test_browser_list_bookmarks_with_records(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-bm1")
    browser.bookmark("bt-bm1", note="yes")
    browser.bookmark("bt-bm-ghost")  # not in index
    pairs = browser.list_bookmarks_with_records()
    lookup = {bm.session_id: rec for bm, rec in pairs}
    assert lookup["bt-bm1"] is not None
    assert lookup["bt-bm-ghost"] is None


def test_browser_replay_html_path(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-replay", with_replay=True)
    browser.index.scan()  # replay_html_path doesn't auto-rescan
    path = browser.replay_html_path("bt-replay")
    assert path is not None
    assert path.name == "replay.html"


def test_browser_replay_html_missing(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-noreplay")
    browser.index.scan()
    assert browser.replay_html_path("bt-noreplay") is None


# ===========================================================================
# Default singletons + set_default_session_browser
# ===========================================================================


def test_get_default_session_browser_singleton():
    a = get_default_session_browser()
    b = get_default_session_browser()
    assert a is b


def test_set_default_session_browser(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    assert get_default_session_browser() is browser


# ===========================================================================
# /session REPL dispatcher
# ===========================================================================


def test_repl_unmatched_falls_through():
    r = dispatch_session_command("/plan mode on")
    assert r.matched is False


def test_repl_default_shows_recent(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-xyz")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session")
    assert r.ok is True
    assert "bt-xyz" in r.text


def test_repl_default_empty_root(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session")
    assert r.ok is True
    assert "no sessions found" in r.text.lower()


def test_repl_list_filters_ok(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(
        tmp_path / "sessions", "bt-ok",
        ops_total=1, ops_applied=1,
        verify_pass=1, verify_total=1, stop_reason="complete",
    )
    _make_session(
        tmp_path / "sessions", "bt-bad",
        ops_total=1, stop_reason="crashed",
    )
    set_default_session_browser(browser)
    r = dispatch_session_command("/session list --ok")
    assert "bt-ok" in r.text
    assert "bt-bad" not in r.text


def test_repl_list_filters_bad(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(
        tmp_path / "sessions", "bt-ok",
        ops_total=1, ops_applied=1,
        verify_pass=1, verify_total=1, stop_reason="complete",
    )
    _make_session(
        tmp_path / "sessions", "bt-bad",
        ops_total=1, stop_reason="crashed",
    )
    set_default_session_browser(browser)
    r = dispatch_session_command("/session list --bad")
    assert "bt-bad" in r.text
    assert "bt-ok" not in r.text


def test_repl_list_prefix_filter(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-2026-a")
    _make_session(tmp_path / "sessions", "bt-2025-b")
    set_default_session_browser(browser)
    r = dispatch_session_command('/session list "--prefix=bt-2026-"')
    assert "bt-2026-a" in r.text
    assert "bt-2025-b" not in r.text


def test_repl_list_limit_flag(tmp_path: Path):
    browser = _make_browser(tmp_path)
    for i in range(5):
        _make_session(tmp_path / "sessions", f"bt-{i:02d}")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session list --limit 2")
    # Only 2 list entries, plus the header line
    body_lines = [l for l in r.text.splitlines() if "bt-" in l]
    assert len(body_lines) == 2


def test_repl_list_bad_flag():
    r = dispatch_session_command("/session list --not-a-flag")
    assert r.ok is False


def test_repl_show(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-show", ops_total=5)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session show bt-show")
    assert r.ok is True
    assert "bt-show" in r.text
    assert "ops_total         : 5" in r.text


def test_repl_show_unknown(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session show bt-nope")
    assert r.ok is False


def test_repl_show_short_form(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-short")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session bt-short")
    assert r.ok is True
    assert "bt-short" in r.text


def test_repl_recent_limit_n(tmp_path: Path):
    browser = _make_browser(tmp_path)
    for i in range(3):
        _make_session(tmp_path / "sessions", f"bt-{i}")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session recent 2")
    body_lines = [l for l in r.text.splitlines() if "bt-" in l]
    assert len(body_lines) == 2


def test_repl_recent_bad_number():
    r = dispatch_session_command("/session recent abc")
    assert r.ok is False


def test_repl_bookmark_round_trip(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-bm")
    set_default_session_browser(browser)
    r1 = dispatch_session_command("/session bookmark bt-bm has crash trace")
    assert r1.ok is True
    r2 = dispatch_session_command("/session bookmarks")
    assert r2.ok is True
    assert "bt-bm" in r2.text
    assert "has crash trace" in r2.text
    r3 = dispatch_session_command("/session unbookmark bt-bm")
    assert r3.ok is True


def test_repl_bookmark_bad_id(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session bookmark 'has spaces'")
    assert r.ok is False


def test_repl_bookmarks_empty(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session bookmarks")
    assert r.ok is True
    assert "no bookmarks" in r.text.lower()


def test_repl_unbookmark_unknown(tmp_path: Path):
    browser = _make_browser(tmp_path)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session unbookmark bt-nope")
    assert r.ok is False


def test_repl_replay_points_to_html(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-r", with_replay=True)
    set_default_session_browser(browser)
    r = dispatch_session_command("/session replay bt-r")
    assert r.ok is True
    assert "replay.html" in r.text


def test_repl_replay_missing(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-noreplay")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session replay bt-noreplay")
    assert r.ok is False


def test_repl_rescan(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-a")
    _make_session(tmp_path / "sessions", "bt-b")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session rescan")
    assert r.ok is True
    assert "2 record" in r.text


def test_repl_help():
    r = dispatch_session_command("/session help")
    assert r.ok is True
    assert "/session list" in r.text
    assert "/session bookmark" in r.text


def test_repl_bookmarks_marker_in_list(tmp_path: Path):
    browser = _make_browser(tmp_path)
    _make_session(tmp_path / "sessions", "bt-marked")
    _make_session(tmp_path / "sessions", "bt-unmarked")
    browser.bookmark("bt-marked")
    set_default_session_browser(browser)
    r = dispatch_session_command("/session")
    assert "★ bt-marked" in r.text or "★" in r.text


def test_repl_bad_show_missing_arg():
    r = dispatch_session_command("/session show")
    assert r.ok is False


def test_repl_bad_bookmark_missing_arg():
    r = dispatch_session_command("/session bookmark")
    assert r.ok is False


def test_repl_parse_error_on_bad_shlex():
    # Unclosed quote
    r = dispatch_session_command('/session list "--bad')
    assert r.ok is False
    assert "parse error" in r.text
