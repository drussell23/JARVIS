#!/usr/bin/env python3
"""Cadence preflight probe — Phase 9 Slice 2 (2026-05-06).

Thin Python script invoked by the cadence wrapper / cron line
BEFORE the heavy harness imports. Records ONE row to
``.jarvis/cadence_health.jsonl`` so the overdue detector
(Slice 3) can answer "did the schedule fire and die before
Python?" — closing the EPERM-before-harness silent-failure
mode that bit cron #1 on 2026-05-06.

Architectural locks (consumed via composition):

  * Imports ONLY ``cadence_health`` substrate + stdlib —
    intentionally lightweight so a sandboxed/TCC-restricted
    Python interpreter can still execute it (no battle-test
    machinery dependency, no heavy substrate walks).
  * Writes via §33.4 canonical flock primitive.
  * NEVER raises — uncaught exception → exit code 1, no
    stack trace to cron mail.

Invocation:
    python3 scripts/cadence_preflight.py [--cadence-kind KIND]

Exit codes:
    0 — preflight passed; harness can proceed
    1 — preflight failed; row recorded; caller should abort
        (the wrapper does NOT invoke the harness on non-zero)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cadence_preflight",
        description=(
            "Pre-invocation cadence capability probe; records "
            "one cadence_health row + exits."
        ),
    )
    parser.add_argument(
        "--cadence-kind",
        type=str,
        default="adhoc",
        choices=("cron", "launchd", "adhoc"),
        help=(
            "Cadence kind that triggered this preflight; "
            "stamped on the health row."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=str,
        default=None,
        help=(
            "Override repo root for testing; defaults to the "
            "script's parent-of-parent."
        ),
    )
    args = parser.parse_args()
    try:
        # Substrate host — always the script's own repo, NOT
        # the --repo-root being probed. Decoupled so a bogus
        # --repo-root can still record a failure row via the
        # canonical substrate.
        substrate_host = (
            Path(__file__).resolve().parent.parent
        )
        # Probe target — caller may override.
        repo_root = (
            Path(args.repo_root)
            if args.repo_root
            else substrate_host
        )
        jarvis_dir = repo_root / ".jarvis"
        log_dir = jarvis_dir / "live_fire_soak_logs"
    except Exception as exc:  # noqa: BLE001 — defensive
        sys.stderr.write(
            f"cadence_preflight: path resolution failed: "
            f"{type(exc).__name__}: {exc}\n",
        )
        return 1
    # Ensure ``backend.*`` imports resolve regardless of cwd —
    # cron and launchd may invoke us from anywhere. Prepend the
    # SUBSTRATE host (the repo the script lives in) to sys.path;
    # idempotent.
    repo_str = str(substrate_host)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
            KIND_PREFLIGHT_FAILURE,
            record_health_row,
            run_preflight,
        )
    except ImportError as exc:
        sys.stderr.write(
            f"cadence_preflight: substrate import failed: "
            f"{exc}\n",
        )
        # Substrate unimportable means we can't record a row.
        # Returning non-zero would block the harness; instead
        # log the issue and let the caller proceed (the
        # downstream harness will record its own evidence).
        return 0
    try:
        row = run_preflight(
            repo_root=repo_root,
            jarvis_dir=jarvis_dir,
            log_dir=log_dir,
            cadence_kind=args.cadence_kind,
        )
        ok, detail = record_health_row(row)
    except Exception as exc:  # noqa: BLE001 — defensive
        sys.stderr.write(
            f"cadence_preflight: probe raised: "
            f"{type(exc).__name__}: {exc}\n",
        )
        return 1
    # Stdout summary so cron mail / log file shows the outcome.
    print(
        f"cadence_preflight kind={row.kind} "
        f"failure_class={row.failure_class} "
        f"subject={row.subject!r} "
        f"errno_name={row.errno_name!r} "
        f"recorded={ok} ({detail})",
        flush=True,
    )
    if row.kind == KIND_PREFLIGHT_FAILURE:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
