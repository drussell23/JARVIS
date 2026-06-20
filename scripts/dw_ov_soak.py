"""DW-primary O+V soak (2026-06-20) — thin CLI over the canonical battery.

Soaks the DoubleWord-driven O+V repair cycle over the SHARED defect battery,
multiple rounds, reporting per-op state=applied + aggregate success rate /
latency / cost. Pure DW autarky. Runs on a 16GB M1 (core repair cycle only, not
the starving sensor stack).

REUSES backend.core.ouroboros.governance.fleet_repair_battery (BATTERY +
repair_one) and fleet_evaluator.default_model_caller — NO logic duplication
(this is the SAME battery the Autonomic Repair Sentinel runs in the background).

Run: DOUBLEWORD_API_KEY=... python3 scripts/dw_ov_soak.py [rounds]
"""
from __future__ import annotations

import asyncio
import os
import sys

from backend.core.ouroboros.governance.fleet_repair_battery import (
    BATTERY,
    DEFAULT_MODELS,
    repair_one,
)
from backend.core.ouroboros.governance.fleet_evaluator import default_model_caller

OUT_PER_MTOK = 1.10  # DeepSeek-V4 output tier, cost estimate


async def main() -> int:
    if not os.environ.get("DOUBLEWORD_API_KEY", ""):
        print("ERROR: DOUBLEWORD_API_KEY not set"); return 2
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(f"=== DW-primary O+V soak: {len(BATTERY)} defects x {rounds} rounds "
          f"(pure DW autarky) ===\n")
    applied = total = toks = 0
    lat = []
    for rnd in range(1, rounds + 1):
        print(f"-- round {rnd}/{rounds} --")
        for defect in BATTERY:
            res = await repair_one(default_model_caller, defect, models=DEFAULT_MODELS)
            total += 1; toks += res.completion_tokens
            if res.applied:
                applied += 1; lat.append(res.seconds)
            mark = "OK " if res.applied else "XX "
            print(f"  {mark} {defect.name:16} {res.note:22} "
                  f"model={res.model.split('/')[-1]:22} {res.seconds:4.1f}s "
                  f"{res.completion_tokens:4}tok")
        print()
    rate = 100.0 * applied / total if total else 0.0
    mean_lat = sum(lat) / len(lat) if lat else 0.0
    print("=" * 59)
    print(f"  state=applied : {applied}/{total}  ({rate:.0f}%)")
    print(f"  mean latency  : {mean_lat:.1f}s (applied ops)")
    print(f"  total tokens  : {toks}  (~${toks/1_000_000*OUT_PER_MTOK:.5f})")
    print("=" * 59)
    return 0 if applied == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
