"""Session W real-world calibration run for the mutation tester.

Runs MutationTester against the SUT that the Session W autonomous loop
wrote tests for (``test_failure_sensor.py``), using the four
Session-W-generated test files (20 tests total). The mutation score is
recorded as a calibration datapoint so we know what "good" looks like
for future operator-triggered mutation-test runs.

This is a standalone script — not a pytest test — because the run is
expensive (~28 mutants × pytest subprocess) and should be triggered
deliberately, not as part of the regression spine.

Usage:
    python3 scripts/mutation_calibration_session_w.py
    python3 scripts/mutation_calibration_session_w.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from backend.core.ouroboros.governance.mutation_tester import (  # noqa: E402
    render_console_report,
    render_json_report,
    run_mutation_test,
)

SUT = REPO_ROOT / (
    "backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py"
)
TESTS = [
    REPO_ROOT / f"tests/governance/intake/sensors/test_test_failure_sensor_{part}.py"
    for part in ("dedup", "ttl", "isolation", "marker_refresh")
]


def _progress(idx: int, total: int, outcome) -> None:
    mark = "caught" if outcome.caught else "SURVIVED"
    print(
        f"  [{idx:>2}/{total}] {outcome.mutant.op:<14} "
        f"line={outcome.mutant.line:<4} "
        f"{outcome.mutant.original[:12]:<14} -> "
        f"{outcome.mutant.mutated[:12]:<14} "
        f"{mark:<9} ({outcome.duration_s:.1f}s)",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", help="Write full JSON report to this path")
    parser.add_argument(
        "--max", type=int, default=40,
        help="Cap on mutants (default 40 — exceeds the 28 sites so none skipped)",
    )
    parser.add_argument(
        "--timeout", type=int, default=60,
        help="Per-mutant pytest timeout (seconds, default 60)",
    )
    args = parser.parse_args()

    if not SUT.is_file():
        print(f"SUT not found: {SUT}", file=sys.stderr)
        return 1
    missing = [str(t) for t in TESTS if not t.is_file()]
    if missing:
        print(f"Missing test file(s): {missing}", file=sys.stderr)
        return 1

    print("=" * 78)
    print("Session W Calibration — MutationTester real-world run")
    print("=" * 78)
    print(f"SUT:   {SUT.relative_to(REPO_ROOT)}")
    print("Tests:")
    for t in TESTS:
        print(f"  - {t.relative_to(REPO_ROOT)}")
    print(f"Max mutants: {args.max}  Per-mutant timeout: {args.timeout}s")
    print()

    result = run_mutation_test(
        SUT,
        test_files=TESTS,
        max_mutants_override=args.max,
        timeout_s_override=float(args.timeout),
        global_timeout_s_override=3600.0,
        seed_override=0,
        cwd=REPO_ROOT,
        progress_cb=_progress,
    )
    print()
    print(render_console_report(result))
    if args.json:
        Path(args.json).write_text(
            render_json_report(result), encoding="utf-8"
        )
        print(f"JSON written to {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
