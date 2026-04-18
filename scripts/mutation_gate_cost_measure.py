"""Cost-measurement harness for the MutationGate rollout decision.

Runs the gate against one or more allowlisted files twice — once with
a cold cache (invalidated first) and once with a warm cache — so the
operator can see exactly what turning on ``JARVIS_MUTATION_GATE_MODE=enforce``
costs at the APPLY hook.

Reports (human + JSON):
  * per-file mutant count
  * cold-cache wall-clock (catalog enumeration + every mutant via pytest)
  * warm-cache wall-clock (catalog cached + outcome cache hit)
  * amortization ratio (warm / cold)
  * p50 / p95 per-mutant cost on the cold run
  * verdict on each file (allow / upgrade / block)

Usage (two modes):

  # Use the configured allowlist (env + YAML):
  python3 scripts/mutation_gate_cost_measure.py

  # Override — pass one or more (src, tests_dir) pairs:
  python3 scripts/mutation_gate_cost_measure.py \\
      backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py

  # Export the raw JSON for trend tracking:
  python3 scripts/mutation_gate_cost_measure.py --json /tmp/cost.json

This script is the evidence the operator needs to flip
JARVIS_MUTATION_GATE_MODE from ``shadow`` to ``enforce``. Re-running it
periodically (weekly?) catches regressions in suite performance before
they become a pipeline cost surprise.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance import mutation_cache as MC  # noqa: E402
from backend.core.ouroboros.governance import mutation_gate as MG  # noqa: E402


def _discover_tests_for(sut: Path) -> List[Path]:
    tests = REPO_ROOT / "tests"
    if not tests.is_dir():
        return []
    return sorted(
        p for p in tests.rglob(f"test_{sut.stem}*.py") if p.is_file()
    )


def _measure_one(sut: Path, tests: List[Path]) -> Dict[str, Any]:
    # Force a cold cache for the SUT.
    MC.invalidate_catalog(sut)
    MC.invalidate_outcomes()

    print(f"--- {sut.relative_to(REPO_ROOT)}")
    if not tests:
        print("  no tests discovered; skipping")
        return {"sut": str(sut), "skipped": "no_tests"}

    # Cold run.
    print(f"  cold run ({len(tests)} test file(s)) …", flush=True)
    t0 = time.time()
    cold_verdict = MG.evaluate_file(sut, tests, force=True)
    cold_total = time.time() - t0
    cold_per_mutant = [o.duration_s for o in cold_verdict.survivors]
    # survivors only carries non-caught outcomes; we want ALL durations,
    # so we re-derive via the cache — each surviving + caught mutant
    # wrote to the outcome cache, but durations aren't stored there.
    # For V1 we just report survivor durations + total.

    # Warm run — same file, same tests, should hit outcome cache.
    print("  warm run …", flush=True)
    t1 = time.time()
    warm_verdict = MG.evaluate_file(sut, tests, force=True)
    warm_total = time.time() - t1

    amortization = warm_total / cold_total if cold_total > 0 else 0.0
    entry: Dict[str, Any] = {
        "sut": str(sut.relative_to(REPO_ROOT)),
        "test_files": [str(t.relative_to(REPO_ROOT)) for t in tests],
        "total_mutants": cold_verdict.total_mutants,
        "score": round(cold_verdict.score, 4),
        "grade": cold_verdict.grade,
        "decision_cold": cold_verdict.decision,
        "decision_warm": warm_verdict.decision,
        "cold_wall_clock_s": round(cold_total, 2),
        "warm_wall_clock_s": round(warm_total, 2),
        "amortization_ratio": round(amortization, 4),
        "cold_cache_hits": cold_verdict.cache_hits,
        "cold_cache_misses": cold_verdict.cache_misses,
        "warm_cache_hits": warm_verdict.cache_hits,
        "warm_cache_misses": warm_verdict.cache_misses,
        "survivor_count": len(cold_verdict.survivors),
    }
    if cold_per_mutant:
        entry["p50_survivor_duration_s"] = round(
            statistics.median(cold_per_mutant), 3,
        )
        entry["p95_survivor_duration_s"] = round(
            sorted(cold_per_mutant)[int(len(cold_per_mutant) * 0.95)]
            if len(cold_per_mutant) > 1 else cold_per_mutant[0],
            3,
        )
    print(
        f"  decision={cold_verdict.decision} score={cold_verdict.score:.1%} "
        f"grade={cold_verdict.grade}  "
        f"cold={cold_total:.1f}s  warm={warm_total:.1f}s  "
        f"ratio={amortization:.3f}"
    )
    return entry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cost-measurement harness for the MutationGate rollout."
    )
    parser.add_argument(
        "paths", nargs="*",
        help="Specific SUT paths (relative to repo root). "
             "If empty, uses the configured allowlist.",
    )
    parser.add_argument("--json", help="Write full JSON report to this path")
    parser.add_argument(
        "--max", type=int, default=40,
        help="Cap on mutants per file (default 40)",
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="Per-mutant pytest timeout (seconds, default 60)",
    )
    args = parser.parse_args()

    import os
    os.environ.setdefault("JARVIS_MUTATION_GATE_MAX_MUTANTS", str(args.max))
    os.environ.setdefault("JARVIS_MUTATION_GATE_PER_TIMEOUT_S", str(args.timeout))

    if args.paths:
        sut_paths = [REPO_ROOT / p for p in args.paths]
    else:
        allowlist = MG.load_allowlist()
        if not allowlist:
            print(
                "No allowlist configured. Set JARVIS_MUTATION_GATE_CRITICAL_PATHS "
                "or populate config/mutation_critical_paths.yml.",
                file=sys.stderr,
            )
            return 2
        sut_paths = []
        for entry in allowlist:
            p = REPO_ROOT / entry
            if p.is_file():
                sut_paths.append(p)
            elif p.is_dir():
                sut_paths.extend(x for x in p.rglob("*.py") if x.is_file())

    print("=" * 78)
    print("MutationGate Cost-Measurement Harness")
    print("=" * 78)
    print(f"Files under test: {len(sut_paths)}")
    print(f"Max mutants/file: {args.max}  Per-mutant timeout: {args.timeout}s")
    print()

    started = time.time()
    entries: List[Dict[str, Any]] = []
    for sp in sut_paths:
        tests = _discover_tests_for(sp)
        entries.append(_measure_one(sp, tests))

    total_duration = time.time() - started
    print()
    print("=" * 78)
    print("Aggregate")
    print("=" * 78)
    n_scored = [e for e in entries if "total_mutants" in e]
    if n_scored:
        cold_sum = sum(e["cold_wall_clock_s"] for e in n_scored)
        warm_sum = sum(e["warm_wall_clock_s"] for e in n_scored)
        mutants_sum = sum(e["total_mutants"] for e in n_scored)
        print(f"  files scored:       {len(n_scored)}")
        print(f"  mutants total:      {mutants_sum}")
        print(f"  cold wall-clock:    {cold_sum:.1f}s "
              f"(per-op worst-case cost with no cache)")
        print(f"  warm wall-clock:    {warm_sum:.1f}s "
              f"(per-op steady-state cost once cache warms)")
        print(f"  amortization:       "
              f"{(warm_sum / cold_sum):.3f} "
              f"(warm/cold; lower = more value from cache)")
    else:
        print("  no files scored")
    print(f"  harness duration:   {total_duration:.1f}s")
    print()

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "harness_duration_s": round(total_duration, 2),
                    "files": entries,
                },
                indent=2, sort_keys=True,
            ),
            encoding="utf-8",
        )
        print(f"JSON written to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
