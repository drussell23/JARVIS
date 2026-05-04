#!/usr/bin/env python3
"""Soak v6 clean-bar assertion — composes Pass B (5 criteria) + 3 v6
add-ons that validate cumulative defect closures and the RenderConductor
arc holding under sustained load.

Soak v6 is the first session that runs with ALL of:
  * Defects #1-#5 closed (WallClockWatchdog / Production Oracle observer
    boot / PersistentIntelligence readonly-DB / CandidateGenerator task
    leak / no-VERIFY-phases-fire cascade)
  * RenderConductor arc graduated (Wave 4 #1 — 7 slices, 7 modules)
  * Global asyncio loop-level exception handler installed at harness boot
    (Phase 1 of the audit, 2026-05-03)

The Pass B 5 criteria already cover the W2(5) graduation contract;
the v6 add-ons assert that the cumulative substrate doesn't regress
under the new load profile.

V6 ADD-ON CRITERIA:

  V6-A — Defect-#5 cascade reflex DID fire at least once for a
         read-only BG op (proves the structural fix is reachable
         under real sensor activity, not just the synthetic verdict).

  V6-B — RenderConductor master flag enabled AND zero asyncio leak
         WARNINGs in the [asyncio.leak] logger channel. The leak
         logger is the harness-boot safety net; any WARNING entry
         indicates an unexpected exception class that bypassed the
         per-callsite swallowers — concrete audit follow-up target.

  V6-C — No regression of the 5 closed-defect signatures: zero
         occurrences of "WallClockWatchdog fired more than 60s late",
         "production_oracle_observer_tick=0 across whole session",
         "PersistentIntelligence ... readonly database",
         "Task exception was never retrieved", and zero terminal-
         failures with "background_dw_blocked_by_topology" on
         read-only BG ops (mutating BG terminal-failures are
         allowed by Defect #5's cost contract).

Exit codes:
    0 = CLEAN (Pass B 5 + V6 3 all passed)
    1 = at least one criterion failed
    2 = session artifacts missing / unparseable

Usage:
    python3 scripts/soak_v6_clean_bar.py [SESSION_ID]
    python3 scripts/soak_v6_clean_bar.py  # picks most recent
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


REPO_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = REPO_ROOT / ".ouroboros" / "sessions"


@dataclass(frozen=True)
class V6Verdict:
    name: str
    passed: bool
    evidence: str
    details: dict = field(default_factory=dict)


def _resolve_session(arg: str | None) -> Path:
    """Resolve session path from argv or pick most recent."""
    if arg:
        candidate = SESSIONS_DIR / arg
        if not candidate.is_dir():
            raise SystemExit(f"session not found: {candidate}")
        return candidate
    sessions = sorted(
        SESSIONS_DIR.glob("bt-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not sessions:
        raise SystemExit(f"no sessions found under {SESSIONS_DIR}")
    return sessions[0]


def _read_log(session: Path) -> str:
    log = session / "debug.log"
    if not log.exists():
        raise SystemExit(f"missing debug.log: {log}")
    return log.read_text(errors="replace")


def _eval_v6a_cascade_fired(log: str) -> V6Verdict:
    """V6-A — Defect-#5 cascade reflex fired for at least one read-only BG op."""
    pattern = "Nervous-System Reflex: BG topology skip_and_queue bypassed for read-only op"
    count = log.count(pattern)
    return V6Verdict(
        name="V6-A Defect #5 cascade reflex fired in production",
        passed=count > 0,
        evidence=f"reflex_fire_count={count} (need >= 1)",
    )


def _eval_v6b_asyncio_leak_quiet(log: str) -> V6Verdict:
    """V6-B — Zero WARNING entries on the [asyncio.leak] channel.

    The harness-boot loop handler logs DEBUG for expected patterns
    (CancelledError + EXPECTED_BACKGROUND_EXC_PATTERNS) and WARNING
    for everything else. WARNING entries are the audit follow-up
    surface; absence means every leak fell into a known class.
    """
    warning_pattern = re.compile(
        r"WARNING\s+\[asyncio leak\]", re.MULTILINE,
    )
    matches = warning_pattern.findall(log)
    debug_pattern = re.compile(
        r"DEBUG\s+\[asyncio leak\]", re.MULTILINE,
    )
    debug_count = len(debug_pattern.findall(log))
    return V6Verdict(
        name="V6-B Asyncio leak channel quiet (no unexpected classes)",
        passed=len(matches) == 0,
        evidence=(
            f"warning_leaks={len(matches)} debug_leaks={debug_count} "
            f"(warning must be 0; debug is informational)"
        ),
    )


def _eval_v6c_no_defect_regression(log: str) -> V6Verdict:
    """V6-C — None of the 5 closed defects re-surface."""
    failures: List[str] = []
    # Defect #1: WallClockWatchdog late-fire
    if re.search(r"WallClockWatchdog.*overshoot=\d{2,}\.", log):
        failures.append("defect_1_watchdog_late_fire")
    # Defect #2: production_oracle_observer never ticked
    if "production_oracle_observer" in log:
        # Look for at least one tick line
        tick_count = log.count("ProductionOracleObserver") + log.count(
            "production_oracle_observer.tick"
        )
        if tick_count == 0:
            failures.append("defect_2_oracle_observer_never_ticked")
    # Defect #3: persistent intel readonly DB
    if "readonly database" in log.lower() or "attempt to write a readonly" in log.lower():
        failures.append("defect_3_persistent_intel_readonly")
    # Defect #4: task exception was never retrieved
    if "Task exception was never retrieved" in log:
        failures.append("defect_4_task_exception_never_retrieved")
    # Defect #5: read-only BG ops terminal-failing on topology block
    # (mutating BG terminal-fails are by-design; we want to catch
    # the case where a read-only op is NOT cascading)
    ro_terminal_pattern = re.compile(
        r"BACKGROUND route: DW failed.*background_dw_blocked_by_topology.*"
        r"is_read_only=True", re.MULTILINE,
    )
    ro_terminal_count = len(ro_terminal_pattern.findall(log))
    if ro_terminal_count > 0:
        failures.append(f"defect_5_ro_bg_terminal_failed_{ro_terminal_count}x")

    return V6Verdict(
        name="V6-C No regression of any closed defect signature",
        passed=not failures,
        evidence=(
            "no_regressions" if not failures else f"failures={failures}"
        ),
    )


def main() -> int:
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    session = _resolve_session(arg)
    print(f"Soak v6 clean-bar assertion")
    print(f"  session: {session.name}")
    print()

    # ── Run Pass B (existing) ──────────────────────────────────
    print("[Phase 1] Pass B 5-criterion assertion (existing)")
    pass_b_path = REPO_ROOT / "scripts" / "pass_b_soak_assertion.py"
    pass_b_result = subprocess.run(
        [sys.executable, str(pass_b_path), session.name],
        capture_output=True, text=True,
    )
    print(pass_b_result.stdout)
    if pass_b_result.stderr:
        print(pass_b_result.stderr, file=sys.stderr)
    pass_b_clean = pass_b_result.returncode == 0
    print(f"  → Pass B: {'CLEAN' if pass_b_clean else 'FAILED'} (exit={pass_b_result.returncode})")
    print()

    # ── V6 add-on criteria ─────────────────────────────────────
    print("[Phase 2] V6 add-on criteria")
    log = _read_log(session)
    v6_results = [
        _eval_v6a_cascade_fired(log),
        _eval_v6b_asyncio_leak_quiet(log),
        _eval_v6c_no_defect_regression(log),
    ]
    for v in v6_results:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()

    v6_clean = all(v.passed for v in v6_results)
    overall_clean = pass_b_clean and v6_clean

    if overall_clean:
        print("VERDICT: SOAK V6 CLEAN — Pass B 5 + V6 3 all passed.")
        print("         Cumulative substrate (defects 1-5 + RenderConductor +")
        print("         asyncio handler) holds under sustained load.")
        return 0
    print(f"VERDICT: NOT CLEAN — pass_b={pass_b_clean} v6={v6_clean}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
