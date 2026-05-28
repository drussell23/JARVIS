#!/usr/bin/env python3
"""Slice 34 Phase 0 — DW capacity probe runner.

Operator-runnable diagnostic. Loads the configured DW provider,
runs the standard probe matrix (4 prompt sizes × N trials), records
into the DW capacity ledger, prints summary + hypothesis verdict.

Usage:

    # Default (probes 397B with 1/5/20/50KB × 10 trials, 60s timeout)
    python3 scripts/dw_capacity_probe.py

    # Custom model
    python3 scripts/dw_capacity_probe.py --model Qwen/Qwen3.5-35B-A3B-FP8

    # Tighter or wider trial count
    python3 scripts/dw_capacity_probe.py --trials 30

    # Custom prompt sizes (comma-sep KB values)
    python3 scripts/dw_capacity_probe.py --sizes 1,5,20,50,100

    # Custom per-call timeout (default 60s — set higher to MEASURE
    # actual response time vs production Slice 28 budget)
    python3 scripts/dw_capacity_probe.py --timeout 120

The script writes structured probe events to the standard DW
capacity ledger (``.jarvis/dw_capacity_ledger.jsonl`` by default,
override via ``JARVIS_DW_CAPACITY_LEDGER_PATH``) AND prints a
human-readable summary table + Phase 0 → Phase 1 hypothesis verdict
to stdout. Exit code: 0 on probe completion (regardless of DW
outcome); 1 only on script-level errors.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path


# Add project root to sys.path so we can import backend.* from any cwd
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _setup_logging(verbose: bool) -> None:
    """Lightweight log setup — probe shouldn't pull in the heavyweight
    harness logging config."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    # Suppress noisy aiohttp at INFO unless verbose
    if not verbose:
        logging.getLogger("aiohttp").setLevel(logging.WARNING)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="DW capacity probe — Slice 34 Phase 0",
    )
    p.add_argument(
        "--model",
        default="Qwen/Qwen3.5-397B-A17B-FP8",
        help="DW model_id to probe (default: %(default)s)",
    )
    p.add_argument(
        "--sizes",
        default="1,5,20,50",
        help="Comma-separated prompt sizes in KB (default: %(default)s)",
    )
    p.add_argument(
        "--trials",
        type=int,
        default=10,
        help="Trials per prompt size (default: %(default)s)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help=(
            "Per-call timeout in seconds (default: %(default)s — "
            "set higher to measure actual response time)"
        ),
    )
    p.add_argument(
        "--caller",
        default="dw_capacity_probe.cli",
        help="Ledger 'caller' field for filtering (default: %(default)s)",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging",
    )
    return p.parse_args(argv)


def _print_summary_table(results: list) -> None:
    """Human-readable summary table for stdout."""
    print()
    print("=" * 88)
    print(
        f"{'size (chars)':<14} {'trials':>8} {'ok':>4} "
        f"{'timeout':>8} {'other':>6} {'p50ms':>8} "
        f"{'p95ms':>8} {'p99ms':>8} {'max ms':>8} {'resp chars':>10}"
    )
    print("-" * 88)
    for r in results:
        print(
            f"{r.target_size:<14d} {r.trials_run:>8d} "
            f"{r.successes:>4d} {r.timeouts:>8d} "
            f"{r.other_failures:>6d} {r.p50_ms:>8.0f} "
            f"{r.p95_ms:>8.0f} {r.p99_ms:>8.0f} "
            f"{r.max_ms:>8.0f} {r.avg_response_chars:>10.0f}"
        )
    print("=" * 88)


def _print_verdict(verdict: dict) -> None:
    """Print the Phase 0 → Phase 1 hypothesis verdict."""
    print()
    print("=" * 88)
    print("§48.7 PHASE 0 → PHASE 1 HYPOTHESIS VERDICT")
    print("=" * 88)
    print(f"hypothesis         : {verdict['hypothesis']}")
    print(f"confidence         : {verdict['confidence']:.2f}")
    print()
    print(f"reasoning          :")
    for line in verdict["reasoning"].split(". "):
        if line.strip():
            print(f"  {line.strip()}")
    print()
    print(f"recommended_action :")
    for line in verdict["recommended_action"].split(". "):
        if line.strip():
            print(f"  {line.strip()}")
    print("=" * 88)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    _setup_logging(args.verbose)

    # Lazy imports — avoid pulling the full backend stack during arg parse
    from backend.core.ouroboros.governance.dw_capacity_ledger import (
        get_default_ledger,
        ledger_path,
        is_enabled as ledger_is_enabled,
    )
    from backend.core.ouroboros.governance.dw_capacity_probe import (
        build_capacity_probe_from_default_provider,
        classify_probe_results,
    )

    if not ledger_is_enabled():
        print(
            "WARNING: JARVIS_DW_CAPACITY_LEDGER_ENABLED=false — "
            "probe will run but no records will be persisted.",
            file=sys.stderr,
        )

    # Parse prompt sizes (KB → chars)
    try:
        sizes_kb = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --sizes value: {exc}", file=sys.stderr)
        return 1
    if not sizes_kb:
        print("ERROR: --sizes must list at least one value", file=sys.stderr)
        return 1
    sizes_chars = [kb * 1024 for kb in sizes_kb]

    ledger = get_default_ledger()
    print(f"Ledger path        : {ledger.path}")
    print(f"Model              : {args.model}")
    print(f"Prompt sizes (KB)  : {sizes_kb}")
    print(f"Trials per size    : {args.trials}")
    print(f"Per-call timeout   : {args.timeout}s")
    print(f"Caller             : {args.caller}")
    print()

    try:
        probe = build_capacity_probe_from_default_provider()
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: failed to build probe — {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    print(f"Running {len(sizes_chars) * args.trials} probe trials...")
    try:
        results = await probe.probe(
            model_id=args.model,
            prompt_sizes=sizes_chars,
            trials_per_size=args.trials,
            timeout_per_call_s=args.timeout,
            caller=args.caller,
        )
    except Exception as exc:  # noqa: BLE001
        print(
            f"ERROR: probe execution failed — {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    _print_summary_table(results)
    verdict = classify_probe_results(results)
    _print_verdict(verdict)

    # Also emit a machine-readable summary JSON line on stderr so
    # automation can capture it
    summary = {
        "schema": "dw_probe_summary.1",
        "model_id": args.model,
        "verdict": verdict,
        "results": [r.to_dict() for r in results],
    }
    print("\n--- machine-readable summary (stderr) ---", file=sys.stderr)
    print(json.dumps(summary, sort_keys=True), file=sys.stderr)
    return 0


def main() -> None:
    sys.exit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
