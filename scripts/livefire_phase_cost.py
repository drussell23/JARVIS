#!/usr/bin/env python3
"""Live-fire — Per-Phase Cost Drill-Down arc.

Simulates a full op traversing the Ouroboros pipeline with per-phase
provider charges, then exercises every drill-down surface:

  1. Charge events produce per-phase accounting in CostGovernor.
  2. get_phase_breakdown() projects live state.
  3. SessionRecorder observes finalize events and aggregates.
  4. save_summary() persists cost_by_phase + cost_by_op_phase to disk.
  5. SessionRecord parses the summary cleanly.
  6. /cost REPL renders live + historical drill-downs.
  7. GET /observability/sessions/<id> surface returns the cost overlay.

Exits 0 on success, 1 on any scenario failure.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.ouroboros.battle_test.session_recorder import (  # noqa: E402
    SessionRecorder,
)
from backend.core.ouroboros.governance.cost_governor import (  # noqa: E402
    CostGovernor,
    CostGovernorConfig,
    reset_finalize_observers,
)
from backend.core.ouroboros.governance.cost_repl import (  # noqa: E402
    dispatch_cost_command,
    reset_default_governor,
    set_default_governor,
)
from backend.core.ouroboros.governance.phase_cost import (  # noqa: E402
    render_phase_cost_breakdown,
)
from backend.core.ouroboros.governance.session_browser import (  # noqa: E402
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
    reset_default_session_singletons,
    set_default_session_browser,
)
from backend.core.ouroboros.governance.session_record import (  # noqa: E402
    parse_session_dir,
)


# ANSI --------------------------------------------------------------------

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


# Scenario harness --------------------------------------------------------

@dataclass
class Scenario:
    name: str
    fn: Callable[[], None]
    passed: int = 0
    failed: int = 0


SCENARIOS: List[Scenario] = []
_current: Optional[Scenario] = None


def scenario(name: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def decorator(fn: Callable[[], None]) -> Callable[[], None]:
        SCENARIOS.append(Scenario(name=name, fn=fn))
        return fn
    return decorator


def expect(cond: bool, label: str) -> None:
    assert _current is not None
    if cond:
        _current.passed += 1
        _ok(label)
    else:
        _current.failed += 1
        _fail(label)


# ===========================================================================
# Scenarios
# ===========================================================================


@scenario("CostGovernor tags charges with phase, keeps cumulative unchanged.")
def s01_charge_tagging() -> None:
    g = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g.start("op-a", route="standard", complexity="light")
    g.charge("op-a", 0.12, "claude", phase="CLASSIFY")
    g.charge("op-a", 0.50, "claude", phase="GENERATE")
    g.charge("op-a", 0.05, "doubleword", phase="GENERATE")
    g.charge("op-a", 0.18, "claude", phase="VERIFY")
    summary = g.summary("op-a")
    expect(
        summary["cumulative_usd"] == 0.85,
        f"cumulative=0.85 (got {summary['cumulative_usd']})",
    )
    expect(
        summary["phase_totals"]["GENERATE"] == 0.55,
        f"GENERATE={summary['phase_totals']['GENERATE']}",
    )
    expect(
        summary["phase_totals"]["VERIFY"] == 0.18,
        f"VERIFY={summary['phase_totals']['VERIFY']}",
    )
    expect(
        summary["phase_by_provider"]["GENERATE"]["claude"] == 0.50,
        "GENERATE claude 0.50",
    )
    expect(
        summary["phase_by_provider"]["GENERATE"]["doubleword"] == 0.05,
        "GENERATE doubleword 0.05",
    )


@scenario("get_phase_breakdown returns PhaseCostBreakdown projection.")
def s02_breakdown_projection() -> None:
    g = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g.start("op-b", route="complex", complexity="heavy_code")
    g.charge("op-b", 0.40, "claude", phase="GENERATE")
    g.charge("op-b", 0.25, "claude", phase="VERIFY")
    b = g.get_phase_breakdown("op-b")
    expect(b is not None, "breakdown not None")
    expect(b.total_usd == 0.65, f"total_usd=0.65 (got {b.total_usd})")
    expect(
        b.top_phase() == ("GENERATE", 0.40),
        f"top_phase=GENERATE (got {b.top_phase()})",
    )
    rendered = render_phase_cost_breakdown(b)
    expect("op-b" in rendered, "rendered includes op id")
    expect("GENERATE" in rendered, "rendered includes GENERATE")
    expect("top phase" in rendered, "rendered includes top-phase footer")


@scenario(
    "Backward compat: charges without phase still enforce the budget cap.")
def s03_backward_compat_budget() -> None:
    cfg = CostGovernorConfig(
        enabled=True, baseline_usd=0.05,
        max_cap_usd=0.05, min_cap_usd=0.05,
    )
    g1 = CostGovernor(cfg)
    g2 = CostGovernor(cfg)
    g1.start("op-1", route="standard", complexity="light")
    g2.start("op-1", route="standard", complexity="light")
    g1.charge("op-1", 0.08, "claude")  # no phase
    g2.charge("op-1", 0.08, "claude", phase="GENERATE")
    expect(
        g1.is_exceeded("op-1") == g2.is_exceeded("op-1") is True,
        "both governors report exceeded",
    )
    expect(
        g1.remaining("op-1") == g2.remaining("op-1"),
        "remaining identical across phased/unphased",
    )


@scenario("SessionRecorder observes finalize, save_summary emits new keys.")
def s04_persistence_round_trip() -> None:
    reset_finalize_observers()
    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td)
        recorder = SessionRecorder(session_id="bt-livefire-01")
        g = CostGovernor(CostGovernorConfig(
            enabled=True, baseline_usd=1.0,
            max_cap_usd=100.0, min_cap_usd=0.01,
        ))
        g.start("op-1", route="standard", complexity="light")
        g.charge("op-1", 0.10, "claude", phase="CLASSIFY")
        g.charge("op-1", 0.60, "claude", phase="GENERATE")
        g.charge("op-1", 0.20, "claude", phase="VERIFY")
        g.finish("op-1")
        expect(
            "op-1" in recorder.cost_by_op_phase,
            "recorder captured op-1 finalize event",
        )
        recorder.save_summary(
            output_dir=session_dir,
            stop_reason="complete",
            duration_s=5.0,
            cost_total=0.90,
            cost_breakdown={"claude": 0.90},
            branch_stats={},
            convergence_state="IMPROVING",
            convergence_slope=0.0,
            convergence_r2=0.0,
        )
        raw = json.loads((session_dir / "summary.json").read_text())
        expect(
            "cost_by_phase" in raw,
            "summary.json includes cost_by_phase key",
        )
        expect(
            raw["cost_by_phase"]["GENERATE"] == 0.60,
            f"GENERATE rollup = 0.60 (got {raw['cost_by_phase'].get('GENERATE')})",
        )
        expect(
            "cost_by_op_phase" in raw,
            "summary.json includes cost_by_op_phase",
        )
        expect(
            raw["cost_by_op_phase"]["op-1"]["CLASSIFY"] == 0.10,
            "cost_by_op_phase[op-1][CLASSIFY] = 0.10",
        )
        recorder.detach_cost_finalize_observer()


@scenario(
    "Empty-session save_summary omits cost keys (legacy-compat).")
def s05_empty_session_omits_keys() -> None:
    reset_finalize_observers()
    with tempfile.TemporaryDirectory() as td:
        recorder = SessionRecorder(session_id="bt-empty")
        recorder.save_summary(
            output_dir=Path(td),
            stop_reason="complete",
            duration_s=0.5,
            cost_total=0.0,
            cost_breakdown={},
            branch_stats={},
            convergence_state="IMPROVING",
            convergence_slope=0.0,
            convergence_r2=0.0,
        )
        raw = json.loads((Path(td) / "summary.json").read_text())
        for forbidden in (
            "cost_by_phase", "cost_by_op_phase",
            "cost_by_op_phase_provider", "cost_unknown_phase_by_op",
        ):
            expect(
                forbidden not in raw,
                f"no {forbidden!r} key leaked for empty session",
            )
        recorder.detach_cost_finalize_observer()


@scenario("SessionRecord parses cost fields into projection.")
def s06_record_parse() -> None:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td) / "bt-parse"
        d.mkdir()
        (d / "summary.json").write_text(json.dumps({
            "stop_reason": "complete",
            "cost_by_phase": {"GENERATE": 0.40, "VERIFY": 0.30},
            "cost_by_op_phase": {
                "op-z": {"GENERATE": 0.40, "VERIFY": 0.30},
            },
        }))
        rec = parse_session_dir(d)
        expect(
            rec.cost_by_phase["GENERATE"] == 0.40,
            "parsed GENERATE 0.40",
        )
        expect(
            rec.cost_by_op_phase["op-z"]["VERIFY"] == 0.30,
            "parsed op-z VERIFY 0.30",
        )
        expect(
            rec.project()["has_phase_cost_data"] is True,
            "projection flags has_phase_cost_data",
        )


@scenario("/cost <op-id> renders live drill-down via REPL.")
def s07_cost_live_repl() -> None:
    reset_default_governor()
    reset_finalize_observers()
    g = CostGovernor(CostGovernorConfig(
        enabled=True, baseline_usd=1.0,
        max_cap_usd=100.0, min_cap_usd=0.01,
    ))
    g.start("op-live", route="standard", complexity="light")
    g.charge("op-live", 0.42, "claude", phase="GENERATE")
    g.charge("op-live", 0.18, "claude", phase="VERIFY")
    res = dispatch_cost_command("/cost op-live", governor=g)
    expect(res.ok, "/cost op-live ok")
    expect("op-live" in res.text, "response mentions op-live")
    expect("GENERATE" in res.text, "response mentions GENERATE")
    expect("$0.4200" in res.text, "rendered amount present")


@scenario("/cost session <sid> renders historical drill-down via browser.")
def s08_cost_historical_repl() -> None:
    with tempfile.TemporaryDirectory() as td:
        sessions = Path(td) / "sessions"
        sessions.mkdir()
        d = sessions / "bt-hist"
        d.mkdir()
        (d / "summary.json").write_text(json.dumps({
            "stop_reason": "complete",
            "cost_by_phase": {"GENERATE": 0.55},
            "cost_by_op_phase": {"op-h": {"GENERATE": 0.55}},
        }))
        bm_root = Path(td) / "bm"
        bm_root.mkdir()
        browser = SessionBrowser(
            index=SessionIndex(root=sessions),
            bookmarks=BookmarkStore(bookmark_root=bm_root),
        )
        browser.index.scan()
        res = dispatch_cost_command(
            "/cost session bt-hist", session_browser=browser,
        )
        expect(res.ok, "/cost session bt-hist ok")
        expect("bt-hist" in res.text, "response mentions session id")
        expect("GENERATE" in res.text, "response mentions GENERATE")
        expect("$0.5500" in res.text, "rendered amount present")


@scenario("/cost session unknown id returns a graceful error.")
def s09_cost_unknown_session() -> None:
    with tempfile.TemporaryDirectory() as td:
        sessions = Path(td) / "sessions"
        sessions.mkdir()
        bm_root = Path(td) / "bm"
        bm_root.mkdir()
        browser = SessionBrowser(
            index=SessionIndex(root=sessions),
            bookmarks=BookmarkStore(bookmark_root=bm_root),
        )
        res = dispatch_cost_command(
            "/cost session bt-not-exist", session_browser=browser,
        )
        expect(not res.ok, "unknown session rejected")
        expect(
            "unknown" in res.text.lower(),
            "response text says 'unknown'",
        )


@scenario(
    "IDE observability GET /observability/sessions/<id> returns cost overlay.")
def s10_ide_overlay() -> None:
    async def _run() -> None:
        with tempfile.TemporaryDirectory() as td:
            sessions = Path(td) / "sessions"
            sessions.mkdir()
            d = sessions / "bt-ide"
            d.mkdir()
            (d / "summary.json").write_text(json.dumps({
                "stop_reason": "complete",
                "stats": {"ops_total": 1, "ops_applied": 1},
                "cost_by_phase": {"GENERATE": 0.45, "VERIFY": 0.20},
                "cost_by_op_phase": {
                    "op-ide": {"GENERATE": 0.45, "VERIFY": 0.20},
                },
            }))
            bm_root = Path(td) / "bm"
            bm_root.mkdir()
            browser = SessionBrowser(
                index=SessionIndex(root=sessions),
                bookmarks=BookmarkStore(bookmark_root=bm_root),
            )
            reset_default_session_singletons()
            set_default_session_browser(browser)
            from aiohttp.test_utils import make_mocked_request
            from backend.core.ouroboros.governance.ide_observability import (
                IDEObservabilityRouter,
            )
            req = make_mocked_request(
                "GET", "/observability/sessions/bt-ide",
            )
            req._transport_peername = ("127.0.0.1", 0)
            req.match_info.update({"session_id": "bt-ide"})
            router = IDEObservabilityRouter()
            resp = await router._handle_session_detail(req)
            expect(resp.status == 200, "status 200")
            body = json.loads(resp.text or "{}")
            expect(
                body["has_phase_cost_data"] is True,
                "projection flags has_phase_cost_data",
            )
            expect(
                body["cost_by_phase"]["GENERATE"] == 0.45,
                f"GENERATE overlay 0.45 (got {body['cost_by_phase'].get('GENERATE')})",
            )
            expect(
                body["cost_by_op_phase"]["op-ide"]["VERIFY"] == 0.20,
                "per-op VERIFY overlay 0.20",
            )
            reset_default_session_singletons()

    asyncio.new_event_loop().run_until_complete(_run())


# ===========================================================================
# Main
# ===========================================================================


def main() -> int:
    global _current
    started = time.monotonic()
    print()
    _hr()
    print(f"{BOLD}PER-PHASE COST DRILL-DOWN — LIVE-FIRE{RESET}")
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
            f"  {GREEN}{BOLD}PER-PHASE COST DRILL-DOWN ARC: CLOSED{RESET}"
        )
    else:
        print(
            f"  {RED}{BOLD}PER-PHASE COST DRILL-DOWN ARC: FAILING{RESET}"
        )
    print()
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
