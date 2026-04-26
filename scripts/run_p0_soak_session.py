#!/usr/bin/env python3
"""P0 PostmortemRecall graduation soak runner — paste-and-forget wrapper.

One command. Runs sanity gates, runs the headless soak with
JARVIS_POSTMORTEM_RECALL_ENABLED=true, parses post-soak artifacts, appends a
ledger row, and prints the verdict. Does NOT open a PR (operator commits +
opens after reviewing the verdict).

Usage::

    python3 scripts/run_p0_soak_session.py --session-num 1
    python3 scripts/run_p0_soak_session.py --session-num 2 --cost-cap 0.50

Per PRD §11 Layer 4 — three CLEAN sessions in a row before the graduation PR.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_LEDGER = _REPO / "memory" / "project_p0_postmortem_recall_soak_ledger.md"

_LEDGER_HEADER = """# P0 PostmortemRecall Graduation Soak Ledger

Tracks the 3-session graduation soak for `PostmortemRecall` (PRD §11 Layer 4).
Merged commit: `d708c5d425` — PR #20976.
Required: 3 consecutive CLEAN sessions before graduation PR may be opened.

## Columns

| session_id | session_num | start_utc | duration_s | stop_reason | session_outcome | postmortem_recall_markers | clean_verdict | notes |
|---|---|---|---|---|---|---|---|---|
"""

_LEDGER_FOOTER = """
---

> **Soak conductor:** operator (local).
> **HUMAN_REVIEW_WAIVED:** TTY-only affordances (Rich diff, REPL) not exercised in headless soak — accepted residual risk per feedback_agent_conducted_soak_delegation.md.
"""


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, **kw)


def _sanity_gate() -> bool:
    print("\n=== STEP 1 — SANITY GATE ===")
    r1 = _run(
        [sys.executable, "scripts/livefire_p0_postmortem_recall.py"],
        capture_output=True, text=True, cwd=_REPO,
    )
    print(r1.stdout[-800:] if r1.stdout else "")
    if "16/16 checks passed" not in (r1.stdout or ""):
        print(f"\n[FAIL] live-fire smoke did not report 16/16. Exit: {r1.returncode}")
        return False

    r2 = _run(
        [
            sys.executable, "-m", "pytest",
            "tests/governance/test_postmortem_recall_p0.py",
            "tests/governance/test_postmortem_recall_graduation_pins.py",
            "--no-header", "-q", "--timeout=60",
        ],
        capture_output=True, text=True, cwd=_REPO,
    )
    print(r2.stdout[-800:] if r2.stdout else "")
    if "57 passed" not in (r2.stdout or ""):
        print(f"\n[FAIL] pytest did not report 57 passed. Exit: {r2.returncode}")
        return False
    print("[OK] sanity gate clear (16/16 + 57/57)")
    return True


def _run_soak(cost_cap: float, idle_timeout: int, max_wall: int) -> tuple[int, float]:
    print("\n=== STEP 2 — SOAK ===")
    env = os.environ.copy()
    env["JARVIS_POSTMORTEM_RECALL_ENABLED"] = "true"
    cmd = [
        sys.executable, "scripts/ouroboros_battle_test.py",
        "--headless",
        "--cost-cap", str(cost_cap),
        "--idle-timeout", str(idle_timeout),
        "--max-wall-seconds", str(max_wall),
        "-v",
    ]
    print(f"\n$ JARVIS_POSTMORTEM_RECALL_ENABLED=true {' '.join(cmd)}\n")
    t0 = time.monotonic()
    rc = subprocess.call(cmd, cwd=_REPO, env=env)
    dur = time.monotonic() - t0
    return rc, dur


def _latest_session_dir() -> Path | None:
    sessions = _REPO / ".ouroboros" / "sessions"
    if not sessions.exists():
        return None
    candidates = sorted(
        (p for p in sessions.iterdir() if p.is_dir() and p.name.startswith("bt-")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


_RUNNER_ATTRIBUTED_PATTERNS = (
    re.compile(r"backend\.core\.ouroboros\.governance\.orchestrator"),
    re.compile(r"backend\.core\.ouroboros\.governance\.postmortem_recall"),
    re.compile(r"backend\.core\.ouroboros\.governance\.conversation_bridge"),
)
_INFRA_NOISE_PATTERNS = (
    re.compile(r"anthropic_transport"),
    re.compile(r"DoublewordProvider"),
    re.compile(r"Event loop is closed"),
    re.compile(r"asyncio.*shutdown"),
)


def _classify_verdict(debug_log: Path) -> tuple[str, dict]:
    text = debug_log.read_text(encoding="utf-8", errors="replace")
    pm_markers = len(re.findall(r"\[PostmortemRecall\] op=", text))
    skipped = text.count("[Orchestrator] PostmortemRecall injection skipped")

    runner_errors = []
    for line in text.splitlines():
        if "Traceback" in line or "ERROR" in line.upper():
            for pat in _RUNNER_ATTRIBUTED_PATTERNS:
                if pat.search(line):
                    if not any(p.search(line) for p in _INFRA_NOISE_PATTERNS):
                        runner_errors.append(line[:200])
                    break

    verdict = "CLEAN" if not runner_errors else "NOT_CLEAN"
    return verdict, {
        "pm_markers": pm_markers,
        "injection_skipped": skipped,
        "runner_errors": runner_errors[:5],
    }


def _ensure_ledger() -> None:
    if not _LEDGER.exists():
        _LEDGER.parent.mkdir(parents=True, exist_ok=True)
        _LEDGER.write_text(_LEDGER_HEADER + _LEDGER_FOOTER, encoding="utf-8")


def _append_ledger_row(row: str) -> None:
    _ensure_ledger()
    text = _LEDGER.read_text(encoding="utf-8")
    if "---\n" in text:
        idx = text.rfind("---\n")
        new = text[:idx] + row + "\n" + text[idx:]
    else:
        new = text + row + "\n"
    _LEDGER.write_text(new, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session-num", type=int, required=True, choices=[1, 2, 3])
    ap.add_argument("--cost-cap", type=float, default=0.50)
    ap.add_argument("--idle-timeout", type=int, default=1800)
    ap.add_argument("--max-wall-seconds", type=int, default=2400)
    ap.add_argument("--skip-sanity", action="store_true")
    args = ap.parse_args()

    print("=" * 64)
    print(f"P0 PostmortemRecall — soak session #{args.session_num} of 3")
    print(f"  cost_cap={args.cost_cap}  idle_timeout={args.idle_timeout}  max_wall={args.max_wall_seconds}")
    print("=" * 64)

    if not args.skip_sanity and not _sanity_gate():
        return 2

    rc, dur = _run_soak(args.cost_cap, args.idle_timeout, args.max_wall_seconds)
    print(f"\n[harness exit] rc={rc} duration={dur:.1f}s")

    print("\n=== STEP 3 — POST-SOAK ANALYSIS ===")
    session_dir = _latest_session_dir()
    if session_dir is None:
        print("[FAIL] No session dir found under .ouroboros/sessions/")
        return 3
    sid = session_dir.name
    print(f"session_id: {sid}")
    print(f"session_dir: {session_dir}")

    summary = session_dir / "summary.json"
    debug = session_dir / "debug.log"
    if not summary.exists() or not debug.exists():
        print(f"[FAIL] missing artifacts (summary.json={summary.exists()}, debug.log={debug.exists()})")
        return 4

    s = json.loads(summary.read_text(encoding="utf-8"))
    stop_reason = s.get("stop_reason", "?")
    outcome = s.get("session_outcome", "?")
    cost = float(s.get("total_cost", 0.0) or 0.0)
    ops_completed = s.get("ops_completed", "?")
    duration_s = int(s.get("duration_seconds", dur))

    verdict, meta = _classify_verdict(debug)
    pm = meta["pm_markers"]
    skipped = meta["injection_skipped"]

    notes_parts = [f"ops={ops_completed}", f"cost=${cost:.4f}"]
    if skipped:
        notes_parts.append(f"injection_skipped={skipped}")
    if meta["runner_errors"]:
        notes_parts.append(f"runner_errors={len(meta['runner_errors'])}")
    notes = "; ".join(notes_parts)

    row = (
        f"| `{sid}` | {args.session_num} | {s.get('start_utc', '?')} | {duration_s} "
        f"| {stop_reason} | {outcome} | {pm} | {verdict} | {notes} |"
    )
    _append_ledger_row(row)

    print("\n=== STEP 4 — VERDICT ===")
    print(f"Session ID:               {sid}")
    print(f"Verdict:                  {verdict}")
    print(f"PostmortemRecall fired:   {pm} times")
    print(f"Injection skipped:        {skipped} (best-effort fallback, non-blocking)")
    print(f"Cost:                     ${cost:.4f} / ${args.cost_cap:.2f}")
    print(f"Duration:                 {duration_s}s ({duration_s // 60}m)")
    print(f"Stop reason:              {stop_reason}")
    print(f"Outcome:                  {outcome}")
    if meta["runner_errors"]:
        print("\nFirst few runner-attributed errors:")
        for e in meta["runner_errors"]:
            print(f"  {e}")
    print(f"\nLedger row appended to:   {_LEDGER}")
    print("\nNext step:")
    if verdict == "CLEAN":
        next_n = args.session_num + 1
        if next_n <= 3:
            print(f"  - Commit + push the ledger row, open soak PR, then run session #{next_n} of 3.")
        else:
            print("  - 3/3 CLEAN. Open the graduation PR (flip JARVIS_POSTMORTEM_RECALL_ENABLED default).")
    else:
        print("  - Triage runner errors. Soak counter resets — do NOT advance to next session.")
    return 0 if verdict == "CLEAN" else 1


if __name__ == "__main__":
    sys.exit(main())
