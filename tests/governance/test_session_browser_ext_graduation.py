"""Extension arc graduation pins — cross-session diff + pinned +
SSE bridge + GET /observability/sessions.

These pins guard against bit-rot of the four extensions layered on
top of the Session History Browser base arc. They are deliberately
short, deterministic, and unit-style: every pin should survive a
future refactor as-is if the authority / schema / shape contracts
are preserved.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# §1 Authority — new extension modules don't import gate/execution code
# ===========================================================================


_EXT_MODULES = [
    "backend/core/ouroboros/governance/session_diff.py",
    "backend/core/ouroboros/governance/session_stream_bridge.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _EXT_MODULES)
def test_extension_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_session_diff_schema_version_pinned():
    from backend.core.ouroboros.governance.session_diff import (
        SESSION_DIFF_SCHEMA_VERSION,
    )
    assert SESSION_DIFF_SCHEMA_VERSION == "session_diff.v1"


def test_session_stream_bridge_schema_version_pinned():
    from backend.core.ouroboros.governance.session_stream_bridge import (
        SESSION_STREAM_BRIDGE_SCHEMA_VERSION,
    )
    assert SESSION_STREAM_BRIDGE_SCHEMA_VERSION == "session_stream_bridge.v1"


# ===========================================================================
# Event vocabulary — 6 new session_* types admitted to broker
# ===========================================================================


def test_session_event_vocabulary_stable():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_SESSION_ADDED,
        EVENT_TYPE_SESSION_BOOKMARKED,
        EVENT_TYPE_SESSION_PINNED,
        EVENT_TYPE_SESSION_RESCAN,
        EVENT_TYPE_SESSION_UNBOOKMARKED,
        EVENT_TYPE_SESSION_UNPINNED,
        _VALID_EVENT_TYPES,
    )
    for ev in (
        EVENT_TYPE_SESSION_ADDED,
        EVENT_TYPE_SESSION_RESCAN,
        EVENT_TYPE_SESSION_BOOKMARKED,
        EVENT_TYPE_SESSION_UNBOOKMARKED,
        EVENT_TYPE_SESSION_PINNED,
        EVENT_TYPE_SESSION_UNPINNED,
    ):
        assert ev in _VALID_EVENT_TYPES, ev


def test_session_event_type_string_values_stable():
    """IDE consumers hardcode these — breaking renames are forbidden
    without a schema_version bump."""
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_SESSION_ADDED,
        EVENT_TYPE_SESSION_BOOKMARKED,
        EVENT_TYPE_SESSION_PINNED,
        EVENT_TYPE_SESSION_RESCAN,
        EVENT_TYPE_SESSION_UNBOOKMARKED,
        EVENT_TYPE_SESSION_UNPINNED,
    )
    assert EVENT_TYPE_SESSION_ADDED == "session_added"
    assert EVENT_TYPE_SESSION_RESCAN == "session_rescan"
    assert EVENT_TYPE_SESSION_BOOKMARKED == "session_bookmarked"
    assert EVENT_TYPE_SESSION_UNBOOKMARKED == "session_unbookmarked"
    assert EVENT_TYPE_SESSION_PINNED == "session_pinned"
    assert EVENT_TYPE_SESSION_UNPINNED == "session_unpinned"


# ===========================================================================
# REPL verb surface — /session diff / pin / unpin / pinned reachable
# ===========================================================================


@pytest.mark.parametrize("verb", ["diff", "pin", "unpin", "pinned"])
def test_repl_verb_registered(verb: str, tmp_path: Path):
    """Every extension verb dispatches to a handler, not to the
    short-form /session <session-id> fallback."""
    from backend.core.ouroboros.governance.session_browser import (
        BookmarkStore, SessionBrowser, SessionIndex,
        dispatch_session_command,
    )
    b = SessionBrowser(
        index=SessionIndex(root=tmp_path / "sessions"),
        bookmarks=BookmarkStore(bookmark_root=tmp_path / "bmroot"),
    )
    (tmp_path / "sessions").mkdir()
    # Verb without required args should error with the verb's own
    # usage string, NOT fall through to /session show.
    line = f"/session {verb}"
    res = dispatch_session_command(line, browser=b)
    if verb == "pinned":
        # No required arg — valid call; should be ok=True
        assert res.ok
    else:
        # Required args missing — usage error text must name the verb
        assert not res.ok
        assert verb in res.text


def test_repl_help_covers_all_extension_verbs(tmp_path: Path):
    from backend.core.ouroboros.governance.session_browser import (
        BookmarkStore, SessionBrowser, SessionIndex,
        dispatch_session_command,
    )
    b = SessionBrowser(
        index=SessionIndex(root=tmp_path / "sessions"),
        bookmarks=BookmarkStore(bookmark_root=tmp_path / "bmroot"),
    )
    (tmp_path / "sessions").mkdir()
    res = dispatch_session_command("/session help", browser=b)
    assert res.ok
    for verb in ("diff", "pin", "unpin", "pinned"):
        assert verb in res.text, f"help must mention {verb}"


# ===========================================================================
# GET /observability/sessions routes mounted
# ===========================================================================


def test_session_routes_registered_on_router():
    from aiohttp import web
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    app = web.Application()
    IDEObservabilityRouter().register_routes(app)
    paths = {
        getattr(r.resource, "canonical", None) for r in app.router.routes()
    }
    assert "/observability/sessions" in paths
    assert "/observability/sessions/{session_id}" in paths


# ===========================================================================
# Bookmark backward-compat — legacy JSON without `pinned` still loads
# ===========================================================================


def test_legacy_bookmark_json_loads_without_pinned_key(tmp_path: Path):
    import json as _json

    from backend.core.ouroboros.governance.session_browser import (
        BookmarkStore,
    )
    # Pre-extension JSON shape had no `pinned` field.
    legacy = [{
        "session_id": "bt-legacy-001",
        "note": "legacy",
        "created_at_iso": "2026-04-15T12:00:00+00:00",
    }]
    (tmp_path / "session_bookmarks.json").write_text(_json.dumps(legacy))
    store = BookmarkStore(bookmark_root=tmp_path)
    assert store.has("bt-legacy-001")
    assert not store.is_pinned("bt-legacy-001")


# ===========================================================================
# Docstring bit-rot — key extension concepts cited in the modules
# ===========================================================================


def test_session_diff_module_mentions_pure_and_read_only():
    import backend.core.ouroboros.governance.session_diff as m
    doc = (m.__doc__ or "").lower()
    assert "pure" in doc
    assert "read-only" in doc


def test_session_stream_bridge_module_mentions_push_only():
    import backend.core.ouroboros.governance.session_stream_bridge as m
    doc = (m.__doc__ or "").lower()
    # "push-only" IS the critical invariant: broker never reaches back
    # into the index or store.
    assert "push-only" in doc or "push only" in doc


# ===========================================================================
# Full-revert matrix — new extensions degrade cleanly when disabled
# ===========================================================================


def test_ide_observability_disabled_returns_403_for_sessions(monkeypatch):
    """With IDE_OBSERVABILITY_ENABLED=false, sessions endpoints 403
    just like tasks+plans."""
    from aiohttp.test_utils import make_mocked_request
    import asyncio as _asyncio
    import json as _json

    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    req = make_mocked_request("GET", "/observability/sessions")
    req._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    router = IDEObservabilityRouter()
    resp = _asyncio.new_event_loop().run_until_complete(
        router._handle_session_list(req)
    )
    body = _json.loads(resp.text or "{}")
    assert resp.status == 403
    assert body["reason_code"] == "ide_observability.disabled"


# ===========================================================================
# Determinism — same inputs yield the same SessionDiff value
# ===========================================================================


def test_diff_is_deterministic_across_calls():
    from backend.core.ouroboros.governance.session_diff import (
        diff_records,
    )
    from backend.core.ouroboros.governance.session_record import (
        SessionRecord,
    )
    a = SessionRecord(session_id="a", ops_total=3, ops_applied=2)
    b = SessionRecord(session_id="b", ops_total=7, ops_applied=6)
    d1 = diff_records(a, b)
    d2 = diff_records(a, b)
    assert d1 == d2
