#!/usr/bin/env python3
"""Live-fire battle test — Session History Browser arc.

Resolves the gap quote:
  'No session history browser — you have session replay HTML but no
   /session list to navigate between runs.'

Scenarios
---------
 1. Parser handles valid summary.json on disk.
 2. Parser handles corrupt summary.json fail-closed.
 3. Index scans a root tree and registers every valid session.
 4. Index evicts removed sessions on re-scan.
 5. Index filter (ok_outcome / max_cost / prefix / has_replay / parse_error).
 6. Bookmark add/remove persists across BookmarkStore instances.
 7. Browser list / show / recent / bookmark / replay round-trip.
 8. /session REPL: default / list flags / show / recent / bookmark /
    unbookmark / bookmarks / replay / rescan / help.
 9. Index listener hooks fire new-record + rescan-complete events.
10. Authority + §1 invariants grep-enforced.

Run::
    python3 scripts/livefire_session_browser.py
"""
from __future__ import annotations

import asyncio
import json
import re as _re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.session_browser import (  # noqa: E402
    Bookmark,
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
    dispatch_session_command,
    reset_default_session_singletons,
    set_default_session_browser,
)
from backend.core.ouroboros.governance.session_record import (  # noqa: E402
    SessionRecord,
    parse_session_dir,
)


C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(t: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {t}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, d: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(d)
        (_pass if ok else _fail)(d)

    @property
    def ok(self) -> bool:
        return not self.failed


def _make_session(
    root: Path, session_id: str, *,
    ops_total: int = 3, ops_applied: int = 1,
    stop_reason: str = "idle_timeout",
    cost_spent_usd: float = 0.012,
    verify_pass: int = 1, verify_total: int = 1,
    with_replay: bool = False,
    corrupt: bool = False,
) -> Path:
    session_dir = root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    if corrupt:
        (session_dir / "summary.json").write_text("{not valid json")
    else:
        (session_dir / "summary.json").write_text(json.dumps({
            "schema_version": "summary.v2",
            "stop_reason": stop_reason,
            "started_at": "2026-04-21T10:00:00+00:00",
            "ended_at": "2026-04-21T10:01:00+00:00",
            "duration_s": 60.0,
            "stats": {
                "ops_total": ops_total,
                "ops_applied": ops_applied,
                "verify": {"pass": verify_pass, "total": verify_total},
                "cost": {"spent_usd": cost_spent_usd, "budget_usd": 0.50},
            },
        }))
    (session_dir / "debug.log").write_text("log line 1\nlog line 2\n")
    if with_replay:
        (session_dir / "replay.html").write_text("<html/>")
    return session_dir


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_parse_valid() -> Scenario:
    """Parser handles valid summary.json on disk."""
    s = Scenario("Parser: valid summary.json → full record")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-valid", ops_total=7)
        rec = parse_session_dir(root / "bt-valid")
        s.check("summary_found", rec.summary_found)
        s.check("parse_error is False", rec.parse_error is False)
        s.check(f"ops_total=7 (got {rec.ops_total})", rec.ops_total == 7)
        s.check("ok_outcome", rec.ok_outcome)
    return s


async def scenario_parse_corrupt() -> Scenario:
    """Parser fail-closed on corrupt summary.json."""
    s = Scenario("Parser: corrupt summary → record with parse_error")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-corrupt", corrupt=True)
        rec = parse_session_dir(root / "bt-corrupt")
        s.check("summary_found (file exists)", rec.summary_found)
        s.check("parse_error", rec.parse_error)
        s.check("ok_outcome is False", rec.ok_outcome is False)
        s.check("parse_error_reason mentions json_decode",
                "json_decode" in rec.parse_error_reason)
    return s


async def scenario_index_scan() -> Scenario:
    """Index scans root and registers every valid session."""
    s = Scenario("Index: scan registers every valid session")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-a")
        _make_session(root, "bt-b")
        _make_session(root, "bt-c-with-replay", with_replay=True)
        (root / "weird name").mkdir()  # invalid name → skipped
        (root / "non-dir").write_text("junk")  # non-dir → skipped
        idx = SessionIndex(root=root)
        records = idx.scan()
        ids = {r.session_id for r in records}
        s.check(f"3 valid records (got {len(records)})", len(records) == 3)
        s.check("bt-a in index", "bt-a" in ids)
        s.check("bt-c-with-replay in index", "bt-c-with-replay" in ids)
        s.check(
            "replay detected",
            any(r.has_replay_html for r in records),
        )
    return s


async def scenario_index_evicts_removed() -> Scenario:
    """Index evicts removed sessions on re-scan."""
    s = Scenario("Index: evicts sessions whose dir disappears")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-gone")
        _make_session(root, "bt-stays")
        idx = SessionIndex(root=root)
        idx.scan()
        shutil.rmtree(root / "bt-gone")
        records = idx.scan()
        ids = {r.session_id for r in records}
        s.check("bt-gone evicted", "bt-gone" not in ids)
        s.check("bt-stays retained", "bt-stays" in ids)
    return s


async def scenario_index_filters() -> Scenario:
    """Index filter (ok_outcome / max_cost / prefix / has_replay / parse_error)."""
    s = Scenario("Index: multi-predicate filtering")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-cheap-ok",
                      cost_spent_usd=0.010, stop_reason="complete",
                      ops_total=1, ops_applied=1,
                      verify_pass=1, verify_total=1)
        _make_session(root, "bt-expensive",
                      cost_spent_usd=0.500, stop_reason="cost_cap",
                      ops_total=5, ops_applied=3)
        _make_session(root, "bt-crashed",
                      stop_reason="crashed", ops_total=1)
        _make_session(root, "bt-replay-ok",
                      stop_reason="complete", ops_total=1, ops_applied=1,
                      verify_pass=1, verify_total=1, with_replay=True)
        _make_session(root, "bt-corrupt", corrupt=True)
        idx = SessionIndex(root=root)
        idx.scan()
        ok = idx.filter(ok_outcome=True)
        s.check(
            "ok_outcome filter excludes crash + corrupt",
            "bt-crashed" not in {r.session_id for r in ok}
            and "bt-corrupt" not in {r.session_id for r in ok},
        )
        cheap = idx.filter(max_cost_usd=0.050)
        s.check(
            "max_cost_usd=0.05 → excludes expensive",
            "bt-expensive" not in {r.session_id for r in cheap},
        )
        prefix = idx.filter(session_id_prefix="bt-replay-")
        s.check(
            "prefix filter returns just replay-ok",
            {r.session_id for r in prefix} == {"bt-replay-ok"},
        )
        with_replay = idx.filter(has_replay=True)
        s.check(
            "has_replay filter catches replay-ok",
            {r.session_id for r in with_replay} == {"bt-replay-ok"},
        )
        bad = idx.filter(parse_error=True)
        s.check(
            "parse_error filter returns corrupt",
            {r.session_id for r in bad} == {"bt-corrupt"},
        )
    return s


async def scenario_bookmark_persist() -> Scenario:
    """Bookmark add/remove persists across BookmarkStore instances."""
    s = Scenario("BookmarkStore: persistence round-trip")
    with tempfile.TemporaryDirectory() as td:
        bmk_root = Path(td)
        s1 = BookmarkStore(bookmark_root=bmk_root)
        s1.add("bt-persist-1", note="revisit")
        s1.add("bt-persist-2")
        s.check("1 added", s1.has("bt-persist-1"))
        s.check("2 added", s1.has("bt-persist-2"))
        # Fresh instance reads from disk
        s2 = BookmarkStore(bookmark_root=bmk_root)
        s.check("persistence: bt-persist-1", s2.has("bt-persist-1"))
        s.check("persistence: bt-persist-2", s2.has("bt-persist-2"))
        s.check(
            "note preserved",
            any(bm.note == "revisit" for bm in s2.list_all()),
        )
        s2.remove("bt-persist-1")
        s3 = BookmarkStore(bookmark_root=bmk_root)
        s.check("remove persisted", not s3.has("bt-persist-1"))
    return s


async def scenario_browser_round_trip() -> Scenario:
    """Browser list / show / recent / bookmark / replay."""
    s = Scenario("Browser: list + show + recent + bookmark + replay")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        sessions = root / "sessions"
        bookmarks = root / "bookmarks"
        for i in range(3):
            _make_session(sessions, f"bt-br-{i}")
        _make_session(sessions, "bt-replay", with_replay=True)
        browser = SessionBrowser(
            index=SessionIndex(root=sessions),
            bookmarks=BookmarkStore(bookmark_root=bookmarks),
        )
        records = browser.list_records()
        s.check(
            f"list returned 4 records (got {len(records)})",
            len(records) == 4,
        )
        detail = browser.show("bt-br-0")
        s.check("show returned record", detail is not None)
        recent = browser.recent(limit=2)
        s.check("recent respects limit", len(recent) == 2)
        bm = browser.bookmark("bt-br-0", note="worth revisiting")
        s.check("bookmark added", bm.note == "worth revisiting")
        pairs = browser.list_bookmarks_with_records()
        s.check("bookmark appears with record", len(pairs) == 1)
        replay = browser.replay_html_path("bt-replay")
        s.check("replay HTML resolved", replay is not None)
    return s


async def scenario_repl_round_trip() -> Scenario:
    """/session REPL: default / list / show / recent / bookmark / replay / rescan / help."""
    s = Scenario("/session REPL: full round trip")
    with tempfile.TemporaryDirectory() as td:
        reset_default_session_singletons()
        root = Path(td)
        sessions = root / "sessions"
        bookmarks = root / "bookmarks"
        _make_session(sessions, "bt-repl-a", ops_total=3,
                       stop_reason="complete", ops_applied=3,
                       verify_pass=3, verify_total=3)
        _make_session(sessions, "bt-repl-b", with_replay=True,
                       stop_reason="crashed")
        browser = SessionBrowser(
            index=SessionIndex(root=sessions),
            bookmarks=BookmarkStore(bookmark_root=bookmarks),
        )
        set_default_session_browser(browser)

        r_default = dispatch_session_command("/session")
        s.check("/session default lists recent",
                "bt-repl-" in r_default.text)
        r_list_ok = dispatch_session_command("/session list --ok")
        s.check("/session list --ok filters crashed",
                "bt-repl-a" in r_list_ok.text
                and "bt-repl-b" not in r_list_ok.text)
        r_show = dispatch_session_command("/session show bt-repl-a")
        s.check("/session show bt-repl-a", r_show.ok
                and "ops_total" in r_show.text)
        r_recent = dispatch_session_command("/session recent 1")
        body = [l for l in r_recent.text.splitlines() if "bt-" in l]
        s.check("/session recent limits to 1", len(body) == 1)
        r_bm = dispatch_session_command(
            "/session bookmark bt-repl-a known-good run",
        )
        s.check("/session bookmark ok", r_bm.ok)
        r_bms = dispatch_session_command("/session bookmarks")
        s.check(
            "/session bookmarks includes the entry",
            "bt-repl-a" in r_bms.text,
        )
        r_replay = dispatch_session_command("/session replay bt-repl-b")
        s.check("/session replay resolves html", r_replay.ok
                and "replay.html" in r_replay.text)
        r_rescan = dispatch_session_command("/session rescan")
        s.check("/session rescan ok", r_rescan.ok)
        r_help = dispatch_session_command("/session help")
        s.check("/session help mentions bookmark", "bookmark" in r_help.text)
        reset_default_session_singletons()
    return s


async def scenario_listener_hooks() -> Scenario:
    """Index listener hooks fire new-record + rescan-complete events."""
    s = Scenario("Listener hooks: new-record + rescan-complete")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _make_session(root, "bt-hook")
        idx = SessionIndex(root=root)
        events: List[Dict[str, Any]] = []
        idx.on_change(events.append)
        idx.scan()
        kinds = {e["event_type"] for e in events}
        s.check("rescan_complete event fired",
                "session_rescan_complete" in kinds)
        s.check("record_added event fired",
                "session_record_added" in kinds)
    return s


async def scenario_authority_invariant() -> Scenario:
    """Arc modules import no gate/execution code."""
    s = Scenario("Authority invariant grep")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/session_record.py",
        "backend/core/ouroboros/governance/session_browser.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_parse_valid,
    scenario_parse_corrupt,
    scenario_index_scan,
    scenario_index_evicts_removed,
    scenario_index_filters,
    scenario_bookmark_persist,
    scenario_browser_round_trip,
    scenario_repl_round_trip,
    scenario_listener_hooks,
    scenario_authority_invariant,
]


async def main() -> int:
    print(f"{C_BOLD}Session History Browser — live-fire{C_END}")
    print(f"{C_DIM}Record + Index + BookmarkStore + Browser + /session REPL{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for sc in results:
        status = f"{C_PASS}PASS{C_END}" if sc.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {sc.title}  ({len(sc.passed)} ✓, {len(sc.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"SESSION HISTORY BROWSER GAP: CLOSED"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
