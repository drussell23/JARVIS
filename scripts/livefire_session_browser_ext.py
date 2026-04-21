#!/usr/bin/env python3
"""End-to-end live-fire battle test for the Session History Browser
extension arc — cross-session diff + pinned bookmarks + SSE live-append
+ GET /observability/sessions.

Boots real modules (no mocks), walks a sequence of scenarios, and
prints a PASS/FAIL summary. Intended as the Slice 5 graduation proof —
run after the arc lands to confirm every extension carries weight
end-to-end.

Exits 0 on success, 1 on any scenario failure.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.ouroboros.governance.session_browser import (  # noqa: E402
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
    dispatch_session_command,
    reset_default_session_singletons,
    set_default_session_browser,
)
from backend.core.ouroboros.governance.session_diff import (  # noqa: E402
    diff_records,
    render_session_diff,
)
from backend.core.ouroboros.governance.session_stream_bridge import (  # noqa: E402
    bridge_session_browser_to_broker,
)
from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E402
    EVENT_TYPE_SESSION_ADDED,
    EVENT_TYPE_SESSION_PINNED,
    EVENT_TYPE_SESSION_RESCAN,
    EVENT_TYPE_SESSION_UNPINNED,
    StreamEventBroker,
)
from backend.core.ouroboros.governance.ide_observability import (  # noqa: E402
    IDEObservabilityRouter,
)


# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------


BOLD = "\x1b[1m"
GREEN = "\x1b[92m"
RED = "\x1b[91m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _hr() -> None:
    print(f"{BOLD}{'─' * 72}{RESET}")


def _header(text: str) -> None:
    _hr()
    print(f"{BOLD}▶ {text}{RESET}")
    _hr()


def _ok(text: str) -> None:
    print(f"  {GREEN}✓ {text}{RESET}")


def _fail(text: str) -> None:
    print(f"  {RED}✘ {text}{RESET}")


# ---------------------------------------------------------------------------
# Scenario framework
# ---------------------------------------------------------------------------


@dataclass
class Scenario:
    name: str
    fn: Callable[[], None]
    passed: int = 0
    failed: int = 0


SCENARIOS: List[Scenario] = []


def scenario(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        SCENARIOS.append(Scenario(name=name, fn=fn))
        return fn
    return decorator


_current: Optional[Scenario] = None


def expect(cond: bool, label: str) -> None:
    assert _current is not None
    if cond:
        _current.passed += 1
        _ok(label)
    else:
        _current.failed += 1
        _fail(label)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _mk_session(
    root: Path, session_id: str,
    *,
    ops_total: int = 3, ops_applied: int = 2,
    stop_reason: str = "complete", cost: float = 0.10,
    replay: bool = False,
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
    if replay:
        (d / "replay.html").write_text("<html>ok</html>")
    return d


def _fresh_browser(base: Path) -> SessionBrowser:
    sessions = base / "sessions"
    bmroot = base / "bmroot"
    sessions.mkdir(parents=True, exist_ok=True)
    bmroot.mkdir(parents=True, exist_ok=True)
    b = SessionBrowser(
        index=SessionIndex(root=sessions),
        bookmarks=BookmarkStore(bookmark_root=bmroot),
    )
    return b


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


@scenario("Cross-session diff: structural delta visible end-to-end.")
def s01_diff() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        browser = _fresh_browser(base)
        sessions = base / "sessions"
        _mk_session(
            sessions, "bt-baseline",
            ops_total=3, ops_applied=2, cost=0.10,
        )
        _mk_session(
            sessions, "bt-regressed",
            ops_total=3, ops_applied=1, cost=0.40,
            stop_reason="cost_cap",
        )
        diff = browser.diff("bt-baseline", "bt-regressed")
        expect(diff is not None, "diff returns a SessionDiff")
        assert diff is not None
        expect(
            "cost_spent_usd" in diff.regressed_fields,
            "cost_spent_usd flagged regressed",
        )
        expect(
            diff.stop_reason_pair == ("complete", "cost_cap"),
            "stop_reason pair is (complete, cost_cap)",
        )
        rendered = render_session_diff(diff)
        expect("Session diff" in rendered, "renderer emits header")
        expect("bt-baseline" in rendered, "left id in output")
        expect("bt-regressed" in rendered, "right id in output")
        # REPL path too.
        res = dispatch_session_command(
            "/session diff bt-baseline bt-regressed", browser=browser,
        )
        expect(res.ok, "/session diff REPL ok")
        expect("Session diff" in res.text, "REPL output has header")


@scenario("Pinned bookmarks: pinned surfaces at default-entry top + persists.")
def s02_pinned() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        browser = _fresh_browser(base)
        sessions = base / "sessions"
        _mk_session(sessions, "bt-recent-1")
        _mk_session(sessions, "bt-recent-2")
        _mk_session(sessions, "bt-keepsafe")
        browser.index.scan()
        # Pin a session via REPL
        res = dispatch_session_command(
            "/session pin bt-keepsafe important milestone",
            browser=browser,
        )
        expect(res.ok, "/session pin ok")
        expect(browser.is_pinned("bt-keepsafe"), "browser.is_pinned matches")
        # Default entry shows pinned first
        res = dispatch_session_command("/session", browser=browser)
        expect(res.ok, "/session default ok")
        idx_p = res.text.find("Pinned session")
        idx_r = res.text.find("recent session")
        expect(
            idx_p != -1 and idx_r != -1 and idx_p < idx_r,
            "pinned block surfaces before recent block",
        )
        # Persist across fresh BookmarkStore
        browser2 = SessionBrowser(
            index=browser.index, bookmarks=BookmarkStore(
                bookmark_root=base / "bmroot",
            ),
        )
        expect(
            browser2.is_pinned("bt-keepsafe"),
            "pinned survives fresh BookmarkStore reload",
        )
        # Unpin
        res = dispatch_session_command(
            "/session unpin bt-keepsafe", browser=browser,
        )
        expect(res.ok, "/session unpin ok")
        expect(
            not browser.is_pinned("bt-keepsafe"),
            "pinned flag cleared after unpin",
        )


@scenario("SSE bridge: new session dir landing fires session_added event.")
def s03_sse_bridge() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        browser = _fresh_browser(base)
        sessions = base / "sessions"
        broker = StreamEventBroker()
        unsub = bridge_session_browser_to_broker(
            browser=browser, broker=broker,
        )
        _mk_session(sessions, "bt-live-1")
        browser.index.scan()
        # Peek at the history
        types = {ev.event_type for ev in list(broker._history)}
        expect(
            EVENT_TYPE_SESSION_ADDED in types,
            f"session_added in broker history (got {sorted(types)})",
        )
        expect(
            EVENT_TYPE_SESSION_RESCAN in types,
            "session_rescan in broker history",
        )
        # Pin via browser — expect session_pinned event
        browser.pin("bt-live-1")
        types = {ev.event_type for ev in list(broker._history)}
        expect(
            EVENT_TYPE_SESSION_PINNED in types,
            "session_pinned in broker history after pin",
        )
        # Unpin — session_unpinned
        browser.unpin("bt-live-1")
        types = {ev.event_type for ev in list(broker._history)}
        expect(
            EVENT_TYPE_SESSION_UNPINNED in types,
            "session_unpinned in broker history after unpin",
        )
        unsub()


@scenario("SSE subscriber: raw subscribe + queue drain sees session_added.")
def s04_sse_subscriber() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            browser = _fresh_browser(base)
            sessions = base / "sessions"
            broker = StreamEventBroker()
            bridge_session_browser_to_broker(
                browser=browser, broker=broker,
            )
            sub = broker.subscribe()
            assert sub is not None
            # Trigger
            _mk_session(sessions, "bt-sub-1")
            browser.index.scan()
            types = []
            for _ in range(6):
                try:
                    ev = await asyncio.wait_for(sub.queue.get(), timeout=0.3)
                except asyncio.TimeoutError:
                    break
                types.append(ev.event_type)
            expect(
                EVENT_TYPE_SESSION_ADDED in types,
                f"subscriber received session_added (got {types})",
            )
            broker.unsubscribe(sub)

    asyncio.new_event_loop().run_until_complete(_run())


@scenario("GET /observability/sessions: list returns projection with overlay.")
def s05_get_sessions() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            browser = _fresh_browser(base)
            set_default_session_browser(browser)
            sessions = base / "sessions"
            _mk_session(sessions, "bt-http-1", ops_total=7, cost=0.12)
            _mk_session(sessions, "bt-http-2", ops_total=4)
            browser.index.scan()
            browser.pin("bt-http-1", note="keeper")
            from aiohttp.test_utils import make_mocked_request
            req = make_mocked_request("GET", "/observability/sessions")
            req._transport_peername = ("127.0.0.1", 0)
            router = IDEObservabilityRouter()
            resp = await router._handle_session_list(req)
            expect(resp.status == 200, "status 200")
            body = json.loads(resp.text or "{}")
            expect(body["count"] == 2, f"count=2 (got {body['count']})")
            ids = {s["session_id"] for s in body["sessions"]}
            expect(ids == {"bt-http-1", "bt-http-2"}, "ids match")
            by_id = {s["session_id"]: s for s in body["sessions"]}
            expect(
                by_id["bt-http-1"]["pinned"] is True,
                "pinned overlay reflects bookmark state",
            )
            expect(
                by_id["bt-http-1"]["bookmark_note"] == "keeper",
                "note echoes through",
            )
            reset_default_session_singletons()

    asyncio.new_event_loop().run_until_complete(_run())


@scenario("GET /observability/sessions/<id>: full projection + pin overlay.")
def s06_get_session_detail() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            browser = _fresh_browser(base)
            set_default_session_browser(browser)
            sessions = base / "sessions"
            _mk_session(
                sessions, "bt-det-1",
                ops_total=5, cost=0.20, replay=True,
            )
            browser.index.scan()
            browser.pin("bt-det-1", note="persistable")
            from aiohttp.test_utils import make_mocked_request
            req = make_mocked_request(
                "GET", "/observability/sessions/bt-det-1",
            )
            req._transport_peername = ("127.0.0.1", 0)
            req.match_info.update({"session_id": "bt-det-1"})
            router = IDEObservabilityRouter()
            resp = await router._handle_session_detail(req)
            expect(resp.status == 200, "status 200")
            body = json.loads(resp.text or "{}")
            expect(body["session_id"] == "bt-det-1", "session_id matches")
            expect(body["ops_total"] == 5, "ops_total 5")
            expect(body["has_replay_html"] is True, "has_replay_html true")
            expect(body["pinned"] is True, "pinned true")
            expect(
                body["bookmark_note"] == "persistable",
                "note echoes in detail",
            )
            reset_default_session_singletons()

    asyncio.new_event_loop().run_until_complete(_run())


@scenario("Kill switch: disabled flag → 403 on session endpoints.")
def s07_kill_switch() -> None:
    import os

    async def _run() -> None:
        os.environ["JARVIS_IDE_OBSERVABILITY_ENABLED"] = "false"
        try:
            from aiohttp.test_utils import make_mocked_request
            req = make_mocked_request("GET", "/observability/sessions")
            req._transport_peername = ("127.0.0.1", 0)
            router = IDEObservabilityRouter()
            resp = await router._handle_session_list(req)
            expect(resp.status == 403, "list 403 when disabled")
            req2 = make_mocked_request(
                "GET", "/observability/sessions/bt-any",
            )
            req2._transport_peername = ("127.0.0.1", 0)
            req2.match_info.update({"session_id": "bt-any"})
            resp2 = await router._handle_session_detail(req2)
            expect(resp2.status == 403, "detail 403 when disabled")
        finally:
            os.environ.pop("JARVIS_IDE_OBSERVABILITY_ENABLED", None)

    asyncio.new_event_loop().run_until_complete(_run())


@scenario("CORS + filters: allowlisted origin + ?bookmarked=true narrows.")
def s08_cors_and_filters() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            browser = _fresh_browser(base)
            set_default_session_browser(browser)
            sessions = base / "sessions"
            _mk_session(sessions, "bt-fa")
            _mk_session(sessions, "bt-fb")
            browser.index.scan()
            browser.bookmark("bt-fa")
            from aiohttp.test_utils import make_mocked_request
            from urllib.parse import urlencode
            q = urlencode({"bookmarked": "true"})
            req = make_mocked_request(
                "GET", f"/observability/sessions?{q}",
                headers={"Origin": "http://localhost:3000"},
            )
            req._transport_peername = ("127.0.0.1", 0)
            router = IDEObservabilityRouter()
            resp = await router._handle_session_list(req)
            expect(resp.status == 200, "status 200 with cors origin")
            expect(
                resp.headers.get("Access-Control-Allow-Origin")
                == "http://localhost:3000",
                "CORS echoes localhost",
            )
            body = json.loads(resp.text or "{}")
            ids = {s["session_id"] for s in body["sessions"]}
            expect(
                ids == {"bt-fa"},
                f"bookmarked=true narrows to {{bt-fa}} (got {ids})",
            )
            reset_default_session_singletons()

    asyncio.new_event_loop().run_until_complete(_run())


@scenario("Authority invariant: extension modules import no gate code.")
def s09_authority_grep() -> None:
    forbidden = (
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    )
    modules = [
        "backend/core/ouroboros/governance/session_diff.py",
        "backend/core/ouroboros/governance/session_stream_bridge.py",
    ]
    for mod in modules:
        src = (ROOT / mod).read_text()
        violations = [
            f for f in forbidden
            if re.search(
                rf"^\s*(from|import)\s+[^#\n]*{re.escape(f)}",
                src, re.MULTILINE,
            )
        ]
        expect(violations == [], f"{mod}: zero forbidden imports")


@scenario(
    "Backward-compat: legacy bookmark JSON (no pinned key) still loads.")
def s10_backward_compat() -> None:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        # Pre-extension shape: no `pinned` field
        legacy = [{
            "session_id": "bt-legacy-001",
            "note": "legacy-shape",
            "created_at_iso": "2026-04-15T12:00:00+00:00",
        }]
        (base / "session_bookmarks.json").write_text(json.dumps(legacy))
        store = BookmarkStore(bookmark_root=base)
        expect(store.has("bt-legacy-001"), "legacy bookmark loaded")
        expect(
            not store.is_pinned("bt-legacy-001"),
            "legacy bookmark defaults pinned=False",
        )
        # Mutate, re-persist, re-load
        store.pin("bt-legacy-001")
        store2 = BookmarkStore(bookmark_root=base)
        expect(
            store2.is_pinned("bt-legacy-001"),
            "pin survives re-load post-upgrade",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    global _current
    started = time.monotonic()
    print()
    _hr()
    print(
        f"{BOLD}SESSION HISTORY BROWSER — EXTENSION ARC LIVE-FIRE{RESET}"
    )
    _hr()
    print()
    for sc in SCENARIOS:
        _current = sc
        _header(sc.name)
        try:
            sc.fn()
        except Exception as exc:  # noqa: BLE001
            sc.failed += 1
            _fail(f"SCENARIO RAISED: {type(exc).__name__}: {exc}")
        print()
    _header("SUMMARY")
    total_p = sum(s.passed for s in SCENARIOS)
    total_f = sum(s.failed for s in SCENARIOS)
    ok_n = sum(1 for s in SCENARIOS if s.failed == 0)
    for sc in SCENARIOS:
        state = (
            f"{GREEN}PASS{RESET}" if sc.failed == 0
            else f"{RED}FAIL{RESET}"
        )
        print(
            f"  {state} {sc.name}  ({sc.passed} ✓, {sc.failed} ✘)"
        )
    elapsed = time.monotonic() - started
    print()
    print(
        f"  {BOLD}Total:{RESET} {total_p} checks passed, {total_f} failed"
        f" — {ok_n}/{len(SCENARIOS)} scenarios OK"
    )
    print(f"  {DIM}elapsed: {elapsed:.2f}s{RESET}")
    print()
    if total_f == 0:
        print(
            f"  {GREEN}{BOLD}SESSION HISTORY BROWSER EXTENSION ARC: CLOSED{RESET}"
        )
    else:
        print(
            f"  {RED}{BOLD}SESSION HISTORY BROWSER EXTENSION ARC: FAILING{RESET}"
        )
    print()
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
