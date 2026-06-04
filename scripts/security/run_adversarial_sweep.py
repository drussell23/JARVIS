#!/usr/bin/env python3
"""Slice 84 CLI — thin entrypoint. NO evaluation logic lives here."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from backend.core.ouroboros.governance.graduation import adversarial_sweep as S


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="O+V adversarial cage sweep")
    p.add_argument("--mutations", choices=["on", "off"], default="on")
    p.add_argument("--json-out", default=None)
    p.add_argument("--fail-on-regression", action="store_true")
    p.add_argument("--baseline-escape-rate", type=float, default=21.9)
    args = p.parse_args(argv)

    report = asyncio.run(
        S.run_sweep(include_mutations=(args.mutations == "on")))
    print(S.render_console_report(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"[wrote] {args.json_out}")
    if args.fail_on_regression:
        ok, msg = S.evaluate_regression(
            report, baseline_escape_rate_raw=args.baseline_escape_rate)
        print(f"[regression-gate] {msg}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
