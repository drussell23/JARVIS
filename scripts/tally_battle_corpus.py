#!/usr/bin/env python3
"""Deterministic tally of the Ouroboros battle-test empirical corpus.

This script is the auditable artifact behind any resume / paper claim of the
form "N battle-test sessions, H hours cumulative soak, C clean completions,
$X total cost". It reads ONLY the on-disk session artifacts under
``.ouroboros/sessions/`` and recomputes the four headline metrics from
``summary.json`` files. No network, no mutation, stdlib only.

Methodology (fail-closed -- "only the harness counts"):

  total_sessions      Directories matching the battle-test session naming
                      pattern ``bt-YYYY-MM-DD-HHMMSS``. Reported two ways:
                        - all_dirs:      every matching directory on disk
                        - with_summary:  directories with a parseable
                                         summary.json (the auditable subset;
                                         this is the number a resume should
                                         cite, since unparseable/missing
                                         summaries are not "documented")

  cumulative_soak_h   Sum of ``duration_s`` across parseable summaries,
                      converted to hours. Missing/zero durations contribute 0.

  clean_completions   Count of summaries with ``session_outcome == "complete"``.
                      This is the value stamped only by the clean
                      ``_generate_report`` path; ``incomplete_kill`` and any
                      session lacking a parseable summary are NOT counted.

  total_cost_usd      Sum of ``cost_total`` across parseable summaries.

A directory with no summary.json, or an unparseable one, is counted toward
``all_dirs`` only -- it contributes 0 to soak, completions, and cost. This is
deliberate: an unverifiable run is not evidence.

Usage:
    python3 scripts/tally_battle_corpus.py
    python3 scripts/tally_battle_corpus.py --sessions-dir .ouroboros/sessions
    python3 scripts/tally_battle_corpus.py --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# bt-2026-04-06-205230  ->  battle-test session directory
_BT_DIR_RE = re.compile(r"^bt-\d{4}-\d{2}-\d{2}-\d{6}$")


def tally(sessions_dir: Path) -> dict:
    all_dirs: list[Path] = sorted(
        p for p in sessions_dir.iterdir()
        if p.is_dir() and _BT_DIR_RE.match(p.name)
    )

    with_summary = 0
    unparseable = 0
    missing_summary = 0
    cumulative_soak_s = 0.0
    clean_completions = 0
    total_cost_usd = 0.0
    outcome_dist: Counter[str] = Counter()
    stop_reason_dist: Counter[str] = Counter()

    for d in all_dirs:
        sj = d / "summary.json"
        if not sj.is_file():
            missing_summary += 1
            outcome_dist["<no summary.json>"] += 1
            continue
        try:
            data = json.loads(sj.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            unparseable += 1
            outcome_dist["<unparseable summary.json>"] += 1
            continue
        if not isinstance(data, dict):
            unparseable += 1
            outcome_dist["<non-dict summary.json>"] += 1
            continue

        with_summary += 1

        dur = data.get("duration_s", 0.0)
        if isinstance(dur, (int, float)) and dur > 0:
            cumulative_soak_s += float(dur)

        cost = data.get("cost_total", 0.0)
        if isinstance(cost, (int, float)) and cost > 0:
            total_cost_usd += float(cost)

        outcome = data.get("session_outcome", "<unset>")
        outcome_dist[str(outcome)] += 1
        if outcome == "complete":
            clean_completions += 1

        stop_reason_dist[str(data.get("stop_reason", "<unset>"))] += 1

    return {
        "sessions_dir": str(sessions_dir),
        "total_sessions_all_dirs": len(all_dirs),
        "total_sessions_with_summary": with_summary,
        "dirs_missing_summary": missing_summary,
        "dirs_unparseable_summary": unparseable,
        "cumulative_soak_seconds": round(cumulative_soak_s, 3),
        "cumulative_soak_hours": round(cumulative_soak_s / 3600.0, 2),
        "clean_completions": clean_completions,
        "total_cost_usd": round(total_cost_usd, 4),
        "session_outcome_distribution": dict(outcome_dist.most_common()),
        "stop_reason_distribution": dict(stop_reason_dist.most_common()),
    }


def _print_human(r: dict) -> None:
    print(f"  Battle-Test Empirical Corpus tally  ({r['sessions_dir']})")
    print("=" * 64)
    print(f"  Battle-test session dirs (bt-*) ......... {r['total_sessions_all_dirs']}")
    print(f"    with parseable summary.json ........... {r['total_sessions_with_summary']}  <- auditable subset")
    print(f"    missing summary.json .................. {r['dirs_missing_summary']}")
    print(f"    unparseable summary.json .............. {r['dirs_unparseable_summary']}")
    print(f"  Cumulative soak ......................... {r['cumulative_soak_hours']} h"
          f"  ({r['cumulative_soak_seconds']} s)")
    print(f"  Clean completions (session_outcome=complete) {r['clean_completions']}")
    print(f"  Total cost .............................. ${r['total_cost_usd']}")
    print("-" * 64)
    print("  session_outcome distribution:")
    for k, v in r["session_outcome_distribution"].items():
        print(f"    {k:<32} {v}")
    print("  stop_reason distribution:")
    for k, v in r["stop_reason_distribution"].items():
        print(f"    {k:<32} {v}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sessions-dir", default=".ouroboros/sessions",
                    help="Path to the battle-test sessions directory")
    ap.add_argument("--json", action="store_true",
                    help="Emit machine-readable JSON only")
    args = ap.parse_args(argv)

    sessions_dir = Path(args.sessions_dir)
    if not sessions_dir.is_dir():
        print(f"error: {sessions_dir} is not a directory", file=sys.stderr)
        return 2

    result = tally(sessions_dir)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        _print_human(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
