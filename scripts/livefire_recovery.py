#!/usr/bin/env python3
"""Live-fire — Recovery Guidance + Voice Loop Closure arc.

Simulates every known failure mode end-to-end:
  1. FailureContext → RecoveryAdvisor → RecoveryPlan
  2. Plan routes to RecoveryFormatter (text + voice + JSON)
  3. Plan lands in RecoveryPlanStore for live REPL query
  4. /recover REPL renders live + historical drill-downs
  5. RecoveryAnnouncer queues Karen voice output (capture speaker)
  6. End-to-end hands-free loop: /recover <op> speak → Karen speaks

Exits 0 on success, 1 on any scenario failure.
"""
from __future__ import annotations

import asyncio
import json
import os
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

from backend.core.ouroboros.governance.recovery_advisor import (  # noqa: E402
    FailureContext,
    STOP_APPROVAL_REQUIRED,
    STOP_ASCII_GATE,
    STOP_COST_CAP,
    STOP_IRON_GATE_REJECT,
    STOP_L2_EXHAUSTED,
    STOP_PROVIDER_EXHAUSTED,
    STOP_UNHANDLED_EXCEPTION,
    STOP_VALIDATION_EXHAUSTED,
    advise,
    known_stop_reasons,
    rule_count,
)
from backend.core.ouroboros.governance.recovery_announcer import (  # noqa: E402
    RecoveryAnnouncer,
    is_voice_live,
    reset_default_announcer,
)
from backend.core.ouroboros.governance.recovery_formatter import (  # noqa: E402
    render_json,
    render_text,
    render_voice,
)
from backend.core.ouroboros.governance.recovery_repl import (  # noqa: E402
    dispatch_recovery_command,
    reset_default_plan_provider,
)
from backend.core.ouroboros.governance.recovery_store import (  # noqa: E402
    RecoveryPlanStore,
    reset_default_plan_store,
)
from backend.core.ouroboros.governance.session_browser import (  # noqa: E402
    BookmarkStore,
    SessionBrowser,
    SessionIndex,
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


# Spoken capture ---------------------------------------------------------


class _SpokenCapture:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def __call__(self, text: str, voice: str = "Karen") -> bool:
        self.calls.append((text, voice))
        return True


def _reset_env() -> None:
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
        "JARVIS_RECOVERY_VOICE_MIN_GAP_S",
    ):
        os.environ.pop(key, None)
    reset_default_announcer()
    reset_default_plan_provider()
    reset_default_plan_store()


# ===========================================================================
# Scenarios
# ===========================================================================


@scenario("Rule coverage: every known failure_class resolves to a plan.")
def s01_rule_coverage() -> None:
    expect(rule_count() >= 14, f"rule_count >= 14 (got {rule_count()})")
    for reason in known_stop_reasons():
        plan = advise(FailureContext(op_id="op-cov", stop_reason=reason))
        expect(
            plan.matched_rule != "generic",
            f"{reason} -> {plan.matched_rule}",
        )


@scenario("Cost cap failure → plan names the hot-phase drill-down.")
def s02_cost_cap_plan() -> None:
    plan = advise(FailureContext(
        op_id="op-cost", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    ))
    expect(plan.matched_rule == "cost_cap", f"rule={plan.matched_rule}")
    top = plan.top_suggestion()
    expect(top is not None, "top suggestion present")
    expect(
        "/cost" in top.command if top else False,
        f"top suggestion points at /cost (got {top.command if top else ''})",
    )


@scenario("Validation exhausted → plan points at debug.log grep.")
def s03_validation_plan() -> None:
    plan = advise(FailureContext(
        op_id="op-val", stop_reason=STOP_VALIDATION_EXHAUSTED,
        session_id="bt-2026-04-21",
    ))
    expect(plan.matched_rule == "validation_exhausted", "rule match")
    commands = " ".join(s.command for s in plan.suggestions)
    expect(
        "bt-2026-04-21" in commands,
        "session_id threaded into command",
    )
    expect(
        "debug.log" in commands.lower() or "FAIL" in commands,
        "debug.log / FAIL grep referenced",
    )


@scenario("Exception without stop_reason routes to unhandled-exception rule.")
def s04_exception_fallback() -> None:
    plan = advise(FailureContext(
        op_id="op-exc",
        exception_type="KeyError",
        exception_message="missing key",
    ))
    expect(plan.matched_rule == "unhandled_exception", "rule match")
    expect("KeyError" in plan.failure_summary, "exc type in summary")


@scenario("Text render: Try-next header + priority tags + command lines.")
def s05_render_text() -> None:
    plan = advise(FailureContext(
        op_id="op-render", stop_reason=STOP_IRON_GATE_REJECT,
        session_id="bt-iron",
    ))
    out = render_text(plan)
    expect("Try next:" in out, "Try-next header present")
    expect("[high]" in out or "[medium]" in out, "priority tag present")
    expect("$" in out, "$ command prefix present")


@scenario("Voice render: ordinals + count-phrase + TTS-safe token stripping.")
def s06_render_voice() -> None:
    plan = advise(FailureContext(
        op_id="op-voice", stop_reason=STOP_ASCII_GATE,
    ))
    out = render_voice(plan)
    expect("Here are three things to try" in out, "count phrase")
    expect("First," in out and "Second," in out, "ordinals used")
    # No raw env-var / flag / backtick in the spoken form
    expect("JARVIS_" not in out, "env vars redacted")
    expect("--" not in out, "flags redacted")
    expect("`" not in out, "backticks redacted")


@scenario("JSON render: schema version + has_plan flag + suggestions list.")
def s07_render_json() -> None:
    plan = advise(FailureContext(
        op_id="op-json", stop_reason=STOP_APPROVAL_REQUIRED,
    ))
    obj = render_json(plan)
    expect(obj["schema_version"] == "recovery_plan.v1", "schema version")
    expect(obj["has_plan"] is True, "has_plan true")
    expect(len(obj["suggestions"]) >= 1, "suggestions populated")
    # Must be JSON-safe
    try:
        blob = json.dumps(obj)
        parsed = json.loads(blob)
        expect(
            parsed["op_id"] == "op-json",
            "round-trips through json.dumps",
        )
    except (TypeError, ValueError):
        expect(False, "JSON round-trip")


@scenario("/recover REPL: record → list → drill-down flow.")
def s08_repl_flow() -> None:
    _reset_env()
    store = RecoveryPlanStore()
    plan = advise(FailureContext(
        op_id="op-repl", stop_reason=STOP_L2_EXHAUSTED,
    ))
    store.record(plan)
    # Bare list
    r = dispatch_recovery_command("/recover", plan_provider=store)
    expect(r.ok and "op-repl" in r.text, "/recover lists op-repl")
    # Detail
    r = dispatch_recovery_command(
        "/recover op-repl", plan_provider=store,
    )
    expect(r.ok, "/recover op-repl ok")
    expect("Try next:" in r.text, "renders plan in detail")
    expect("l2" in r.text.lower(), "names the L2 rule")


@scenario("/recover session <sid>: historical path from summary.json.")
def s09_historical_path() -> None:
    _reset_env()
    with tempfile.TemporaryDirectory() as td:
        sessions = Path(td) / "sessions"
        sessions.mkdir()
        (sessions / "bt-hist").mkdir()
        (sessions / "bt-hist" / "summary.json").write_text(json.dumps({
            "stop_reason": STOP_PROVIDER_EXHAUSTED,
        }))
        bm = Path(td) / "bm"
        bm.mkdir()
        browser = SessionBrowser(
            index=SessionIndex(root=sessions),
            bookmarks=BookmarkStore(bookmark_root=bm),
        )
        browser.index.scan()
        r = dispatch_recovery_command(
            "/recover session bt-hist", session_browser=browser,
        )
        expect(r.ok, "/recover session ok")
        expect("bt-hist" in r.text, "session id rendered")
        expect(
            "provider" in r.text.lower(),
            "provider-exhausted rule fired",
        )


@scenario("Hands-free loop: /recover op speak → Karen voice queue drains.")
def s10_voice_loop() -> None:
    _reset_env()
    os.environ["OUROBOROS_NARRATOR_ENABLED"] = "true"
    os.environ["JARVIS_RECOVERY_VOICE_ENABLED"] = "true"
    expect(is_voice_live() is True, "voice flags flipped to live")

    captured = _SpokenCapture()
    announcer = RecoveryAnnouncer(speaker=captured)

    store = RecoveryPlanStore()
    plan = advise(FailureContext(
        op_id="op-voice-e2e", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    ))
    store.record(plan)

    r = dispatch_recovery_command(
        "/recover op-voice-e2e speak",
        plan_provider=store,
        announcer=announcer,
    )
    expect(r.ok, "/recover speak ok")
    expect("queued" in r.text.lower(), "response confirms queued")
    expect(
        announcer.stats()["queued"] == 1,
        f"queue has 1 item (got {announcer.stats()['queued']})",
    )

    # Drain — Karen "speaks" into the capture
    asyncio.new_event_loop().run_until_complete(
        announcer.drain_once_for_test(),
    )
    expect(
        len(captured.calls) == 1,
        f"speaker called once (got {len(captured.calls)})",
    )
    spoken_text, spoken_voice = captured.calls[0]
    expect(spoken_voice == "Karen", f"voice=Karen (got {spoken_voice})")
    expect(
        "three things" in spoken_text.lower(),
        "Karen says the count phrase",
    )
    expect(
        "First," in spoken_text,
        "Karen uses ordinals (First, Second, Third)",
    )
    _reset_env()


# ===========================================================================
# Main
# ===========================================================================


def main() -> int:
    global _current
    started = time.monotonic()
    print()
    _hr()
    print(f"{BOLD}RECOVERY GUIDANCE + VOICE LOOP CLOSURE — LIVE-FIRE{RESET}")
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
            f"  {GREEN}{BOLD}RECOVERY GUIDANCE + VOICE LOOP ARC: CLOSED{RESET}"
        )
    else:
        print(
            f"  {RED}{BOLD}RECOVERY GUIDANCE + VOICE LOOP ARC: FAILING{RESET}"
        )
    print()
    return 0 if total_f == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
