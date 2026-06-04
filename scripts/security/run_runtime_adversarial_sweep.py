#!/usr/bin/env python3
"""Slice 92 CLI — thin entrypoint for the runtime containment sweep. NO logic
lives here; it composes governance.graduation.runtime_adversarial_sweep."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.getcwd())

from backend.core.ouroboros.governance.graduation import (  # noqa: E402
    runtime_adversarial_sweep as R,
)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="O+V runtime container-containment sweep")
    p.add_argument("--image", default="python:3-slim")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--json-out", default=None)
    p.add_argument("--fail-on-escape", action="store_true",
                   help="exit 1 if any escape attempt succeeds")
    args = p.parse_args(argv)

    report = asyncio.run(
        R.run_runtime_sweep(image=args.image, timeout_s=args.timeout))
    print(R.render_console_report(report))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2)
        print(f"[wrote] {args.json_out}")
    if args.fail_on_escape and report.escaped_count > 0:
        print(f"[gate] {report.escaped_count} escape(s) succeeded — FAIL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
