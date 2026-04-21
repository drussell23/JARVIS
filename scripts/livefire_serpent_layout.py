#!/usr/bin/env python3
"""End-to-end live-fire for the SerpentFlow Opt-in Split Layout arc.

Exercises mode transitions, event routing, CLI arg parsing, and the
zero-change flow-mode invariant against real modules — no mocks.

Exits 0 on success, 1 on any scenario failure.
"""
from __future__ import annotations

import io
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.core.ouroboros.battle_test.layout_controller import (  # noqa: E402
    LayoutController,
    MODE_FLOW,
    MODE_SPLIT,
    REGION_DASHBOARD,
    REGION_DIFF,
    REGION_STREAM,
    layout_default_from_env,
    parse_cli_layout_arg,
    reset_default_layout_controller,
    valid_regions,
)
from backend.core.ouroboros.battle_test.layout_repl import (  # noqa: E402
    dispatch_layout_command,
)
from backend.core.ouroboros.battle_test.serpent_flow_app import (  # noqa: E402
    SerpentFlowApp,
    resolve_initial_mode,
)
from backend.core.ouroboros.battle_test.split_layout import (  # noqa: E402
    SplitLayout,
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


@scenario("Default mode is flow when no env / CLI override is set.")
def s01_default_flow() -> None:
    os.environ.pop("JARVIS_SERPENT_LAYOUT_DEFAULT", None)
    reset_default_layout_controller()
    expect(layout_default_from_env() == "flow", "env default resolves 'flow'")
    app = SerpentFlowApp.from_argv([], output_stream=io.StringIO())
    expect(app.controller.is_flow, "boot with no argv -> flow")


@scenario("CLI --split flag activates split mode at boot.")
def s02_cli_split() -> None:
    os.environ.pop("JARVIS_SERPENT_LAYOUT_DEFAULT", None)
    expect(parse_cli_layout_arg(["--split"]) == "split", "parser maps --split")
    app = SerpentFlowApp.from_argv(
        ["--split"], output_stream=io.StringIO(),
    )
    expect(app.controller.is_split, "app boots in split")


@scenario(
    "Env JARVIS_SERPENT_LAYOUT_DEFAULT honored; invalid values ignored.")
def s03_env_default() -> None:
    os.environ["JARVIS_SERPENT_LAYOUT_DEFAULT"] = "split"
    try:
        expect(layout_default_from_env() == "split", "env='split' maps through")
    finally:
        os.environ["JARVIS_SERPENT_LAYOUT_DEFAULT"] = "focus:diff"
    try:
        expect(
            layout_default_from_env() == "focus:diff",
            "env='focus:diff' parsed as focus mode",
        )
    finally:
        os.environ["JARVIS_SERPENT_LAYOUT_DEFAULT"] = "evil"
    try:
        expect(
            layout_default_from_env() == "flow",
            "env='evil' falls back to flow",
        )
    finally:
        os.environ.pop("JARVIS_SERPENT_LAYOUT_DEFAULT", None)


@scenario("Flow mode: emit routes to stream writer, NOT to split buffers.")
def s04_flow_emit_routing() -> None:
    captured: List[str] = []
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_FLOW),
        stream_writer=captured.append,
        output_stream=io.StringIO(),
    )
    app.emit_stream("s-line")
    app.emit_dashboard("d-line")
    app.emit_diff("diff-line")
    expect(
        captured == ["s-line", "d-line", "diff-line"],
        "flow writer received 3 emits in order",
    )
    snap = app.split_layout.snapshot()
    for region in (REGION_STREAM, REGION_DASHBOARD, REGION_DIFF):
        expect(
            snap[region] == (),
            f"split buffer {region!r} stayed empty in flow mode",
        )


@scenario("Split mode: emit routes into region buffers by name.")
def s05_split_emit_routing() -> None:
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    app.emit_stream("pipeline phase=CLASSIFY")
    app.emit_dashboard("cost=$0.04 ops=2")
    app.emit_diff("--- a/x\n+++ b/x\n@@ -1 +1 @@")
    snap = app.split_layout.snapshot()
    expect(
        snap[REGION_STREAM] == ("pipeline phase=CLASSIFY",),
        "stream buffer got stream content",
    )
    expect(
        snap[REGION_DASHBOARD] == ("cost=$0.04 ops=2",),
        "dashboard buffer got dashboard content",
    )
    expect(
        snap[REGION_DIFF] == ("--- a/x\n+++ b/x\n@@ -1 +1 @@",),
        "diff buffer got diff content",
    )


@scenario("Focus mode: all regions buffered; visible_regions = [focused].")
def s06_focus_mode() -> None:
    app = SerpentFlowApp(
        controller=LayoutController(initial_mode="focus:diff"),
        stream_writer=lambda _: None,
        output_stream=io.StringIO(),
    )
    app.emit_stream("hidden-stream")
    app.emit_dashboard("hidden-dashboard")
    app.emit_diff("focused-diff")
    snap = app.split_layout.snapshot()
    # All three regions still buffer.
    expect(snap[REGION_STREAM] == ("hidden-stream",), "stream buffered")
    expect(
        snap[REGION_DASHBOARD] == ("hidden-dashboard",),
        "dashboard buffered",
    )
    expect(snap[REGION_DIFF] == ("focused-diff",), "diff buffered")
    # But only the focused region is visible.
    expect(
        app.split_layout.visible_regions() == [REGION_DIFF],
        "visible_regions = [diff] in focus:diff",
    )


@scenario("/layout REPL: flow -> split -> focus -> flow traversal.")
def s07_repl_traversal() -> None:
    c = LayoutController(initial_mode=MODE_FLOW)
    r = dispatch_layout_command("/layout", controller=c)
    expect(r.ok and "current mode" in r.text, "/layout (bare) prints status")
    r = dispatch_layout_command("/layout split", controller=c)
    expect(r.ok and c.is_split, "/layout split -> split")
    r = dispatch_layout_command(
        "/layout focus dashboard", controller=c,
    )
    expect(
        r.ok and c.mode == "focus:dashboard",
        "/layout focus dashboard -> focus:dashboard",
    )
    r = dispatch_layout_command("/layout flow", controller=c)
    expect(r.ok and c.is_flow, "/layout flow -> flow (escape)")


@scenario("/layout focus rejects unknown regions with a stable error.")
def s08_repl_bad_region() -> None:
    c = LayoutController(initial_mode=MODE_FLOW)
    r = dispatch_layout_command("/layout focus pwn", controller=c)
    expect(not r.ok, "unknown region rejected")
    expect("pwn" in r.text, "error names the bad region")
    expect(c.is_flow, "controller state unchanged on rejection")


@scenario(
    "SplitLayout stays inert in headless env (no Rich Live activation).")
def s09_headless_inert() -> None:
    split = SplitLayout(
        controller=LayoutController(initial_mode=MODE_SPLIT),
        output_stream=io.StringIO(),  # not a TTY
    )
    expect(split.start() is False, "start() returns False on non-TTY")
    expect(split.active is False, "renderer stays inactive headless")
    # Push still works — buffer is valid even without a renderer.
    split.push(REGION_STREAM, "x")
    expect(
        split.snapshot()[REGION_STREAM] == ("x",),
        "push works even when inert",
    )


@scenario("Authority invariant: layout modules import no gate/execution code.")
def s10_authority() -> None:
    forbidden = (
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    )
    modules = [
        "backend/core/ouroboros/battle_test/layout_controller.py",
        "backend/core/ouroboros/battle_test/split_layout.py",
        "backend/core/ouroboros/battle_test/layout_repl.py",
        "backend/core/ouroboros/battle_test/serpent_flow_app.py",
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
        expect(
            violations == [],
            f"{mod}: zero forbidden imports",
        )


# ===========================================================================
# Main
# ===========================================================================


def main() -> int:
    global _current
    started = time.monotonic()
    print()
    _hr()
    print(f"{BOLD}SERPENTFLOW OPT-IN SPLIT LAYOUT — LIVE-FIRE{RESET}")
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
            f"  {GREEN}{BOLD}SERPENTFLOW OPT-IN SPLIT LAYOUT ARC: CLOSED{RESET}"
        )
    else:
        print(
            f"  {RED}{BOLD}SERPENTFLOW OPT-IN SPLIT LAYOUT ARC: FAILING{RESET}"
        )
    print()
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
