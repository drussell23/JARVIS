#!/usr/bin/env python3
"""Per-provider spend report for a SWE-Bench-Pro soak session.

Reads a battle-test session's durable artifacts and renders the DoubleWord-vs-
Claude cost split — the cost-attribution the OUROBOROS_VENOM_PRD §50 program
tracks. The authoritative source is `summary.json["cost_breakdown"]` (keyed by
provider, written by the harness from `battle_test.cost_tracker`, which tags
every cost record with its provider). Falls back to summing the
`CostTracker: recorded $X for <provider>` lines in `debug.log` when the session
is still in flight (no final summary yet).

Usage:
  python3 scripts/swe_bench_pro_provider_spend.py                 # latest session
  python3 scripts/swe_bench_pro_provider_spend.py <session-dir>   # specific one
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_SESSIONS = Path(".ouroboros/sessions")


def _latest_session() -> Path | None:
    if not _SESSIONS.is_dir():
        return None
    dirs = [d for d in _SESSIONS.iterdir() if (d / "debug.log").exists()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _from_summary(session: Path):
    sj = session / "summary.json"
    if not sj.exists():
        return None
    try:
        d = json.loads(sj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None
    cb = d.get("cost_breakdown")
    if not isinstance(cb, dict) or not cb:
        return None
    outcome = d.get("session_outcome", "")
    label = ("summary.json (FINAL)" if outcome == "complete"
             else f"summary.json ({outcome or 'in_flight'})")
    return {k: float(v) for k, v in cb.items()}, label


def _from_debug_log(session: Path):
    """In-flight fallback: take the LAST running-total per provider from the
    CostTracker lines (`... for <provider> | total=$X`)."""
    dbg = session / "debug.log"
    if not dbg.exists():
        return None
    last_total: dict[str, float] = {}
    try:
        for line in dbg.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"for ([a-z0-9_-]+) \| total=\$([0-9.]+)", line)
            if m:
                last_total[m.group(1)] = float(m.group(2))
    except OSError:
        return None
    return (last_total, "debug.log running-totals (in-flight)") if last_total else None


def main() -> None:
    session = Path(sys.argv[1]) if len(sys.argv) > 1 else _latest_session()
    if session is None or not session.exists():
        print("no session found")
        sys.exit(1)
    result = _from_summary(session) or _from_debug_log(session)
    if result is None:
        print(f"no cost data in {session}")
        sys.exit(1)
    breakdown, source = result
    total = sum(breakdown.values())
    print(f"# O+V SWE-Bench-Pro — Per-Provider Spend")
    print(f"session: {session.name}   source: {source}\n")
    print("| Provider | Spend (USD) | Share |")
    print("|---|---:|---:|")
    for prov, usd in sorted(breakdown.items(), key=lambda kv: -kv[1]):
        share = (usd / total * 100.0) if total else 0.0
        print(f"| {prov} | ${usd:.4f} | {share:.1f}% |")
    print(f"| **total** | **${total:.4f}** | 100% |")
    # the asymmetry insight, computed (not asserted)
    dw = breakdown.get("doubleword", 0.0)
    cl = breakdown.get("claude", 0.0)
    if dw > 0 and cl > 0:
        print(f"\n_Claude per-dollar ratio vs DoubleWord: "
              f"~{cl / dw:.0f}× of total spend (the tiered-failback cost story: "
              f"DoubleWord serves cheap classify/probe work, Claude serves the "
              f"expensive multi-round GENERATE)._")


if __name__ == "__main__":
    main()
