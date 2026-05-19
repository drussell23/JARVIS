#!/usr/bin/env python3
"""Predictive Provider Resilience — E1 SHADOW envelope proof.

READ-ONLY. Replays the durable Slice-0 dataset
(``.jarvis/provider_latency.jsonl``) prequentially through the
**actual production** :class:`ProviderLatencyEnvelope` — it imports
and drives the shipped estimator, it does NOT re-implement the math
(the proof cannot diverge from what ships).

E1 maintains a token-INDEPENDENT robust ceiling (the token-slope was
falsified, r(tokens,TTFT)≈0.11). For every row, in arrival order:

  1. read the STANDING ``(baseline, band, ceiling)`` where
     ``ceiling = exp(log-median + k·MAD_const·log-MAD)`` — the
     dynamic timeout a future Slice-2 WOULD adopt;
  2. score whether that ceiling covered the just-arrived actual;
  3. fold the row into the robust EWMA-median + log-MAD state.

It reports the load-bearing facts:
  * **envelope coverage** — % of successes where ceiling ≥ actual
    (does the dynamic ceiling protect against the spikes);
  * **baseline stability** — the EWMA-median across the worst
    spike (it must barely move — no OLS-style divergence);
  * **ceiling sanity** — the ceiling stays physically bounded,
    never the 2.7e6-class divergence;
  * the worst spikes and whether the shadow ceiling enveloped them.

``--exclude-route sonar`` runs the D2 proof on GENUINE PASSIVE
telemetry only (synthetic active probes excluded). Enforces nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from backend.core.ouroboros.governance.dw_ttft_observer import (  # noqa: E402
    ProviderLatencyEnvelope,
    ProviderLatencySample,
)


def _load(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(
        encoding="utf-8", errors="replace",
    ).splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", default=".jarvis/provider_latency.jsonl")
    ap.add_argument("--k", type=float, default=3.0)
    ap.add_argument("--min-n", type=int, default=15)
    ap.add_argument(
        "--exclude-route", default="",
        help="Comma-separated route tags to drop (e.g. 'sonar') so "
             "the D2 proof runs on GENUINE passive telemetry only, "
             "not synthetic active probes.",
    )
    args = ap.parse_args()

    rows = _load(Path(args.jsonl))
    drop = {t.strip() for t in args.exclude_route.split(",") if t.strip()}
    if drop:
        before = len(rows)
        rows = [r for r in rows if r.get("route") not in drop]
        print(f"[filter] excluded routes {sorted(drop)}: "
              f"{before}→{len(rows)} rows (genuine-passive only)")

    succ = [
        r for r in rows
        if r.get("outcome") == "success"
        and int(r.get("ttft_ms", -1)) >= 0
    ]
    print(f"=== E1 SHADOW envelope proof (k={args.k}) ===")
    print(f"dataset={args.jsonl}  total_rows={len(rows)}  "
          f"success_rows={len(succ)}")
    if len(succ) < args.min_n:
        print(f"\nHOLD: need >= {args.min_n} success rows, have "
              f"{len(succ)}. Statistically insufficient — NOT "
              f"presenting a proof on noise.")
        return 2

    env = ProviderLatencyEnvelope()
    scored = 0
    covered = 0
    spikes = []          # (actual, baseline, ceiling, enveloped)
    baseline_trace = []   # (idx, actual, baseline) for stability

    print("\n  idx   actual_ms  baseline_ms  band_ms  ceiling_ms  env")
    print("  " + "-" * 58)
    for i, r in enumerate(rows):
        s = ProviderLatencySample(
            provider=str(r.get("provider", "")),
            route=str(r.get("route", "")),
            op_id=str(r.get("op_id", "")),
            input_tokens=int(r.get("input_tokens", 0) or 0),
            ttft_ms=int(r.get("ttft_ms", -1)),
            total_ms=int(r.get("total_ms", 0) or 0),
            outcome=str(r.get("outcome", "")),
            sample_unix=float(r.get("sample_unix", 0.0) or 0.0),
        )
        res = env.observe(s)
        if res.baseline_ms is None or res.enveloped is None:
            continue
        scored += 1
        if res.enveloped:
            covered += 1
        baseline_trace.append((i, res.actual_ms, res.baseline_ms))
        spikes.append(
            (res.actual_ms, res.baseline_ms, res.ceiling_ms,
             res.enveloped)
        )
        if scored <= 40 or scored % 5 == 0:
            print(
                f"  {i:4d}  {res.actual_ms:9d}  "
                f"{res.baseline_ms:11.0f}  "
                f"{(res.band_ms or 0):7.0f}  "
                f"{(res.ceiling_ms or 0):10.0f}  "
                f"{'Y' if res.enveloped else 'n':>3}"
            )

    if scored == 0:
        print("\nNo estimable envelope yet (state still cold).")
        return 2

    cov_rate = covered / scored
    spikes.sort(reverse=True)
    bmin = min(b for _, _, b in baseline_trace)
    bmax = max(b for _, _, b in baseline_trace)
    cmax = max(c for _, _, c, _ in spikes if c is not None)
    worst_actual = max(a for a, _, _, _ in spikes)

    print("\n=== PROOF SUMMARY (E1: robust EWMA-median + log-MAD) ===")
    print(f"scored (estimable, out-of-sample): {scored}")
    print(f"ENVELOPE COVERAGE (ceiling≥actual): {cov_rate:6.1%}  "
          f"← the real metric")
    print(f"baseline range across the run:     "
          f"{bmin:,.0f} … {bmax:,.0f} ms  "
          f"(stable = no OLS-style divergence)")
    print(f"max ceiling observed:              {cmax:,.0f} ms  "
          f"(physically bounded, not 2.7e6-class)")
    print(f"worst actual TTFT in data:         {worst_actual:,.0f} ms")
    print("\n  worst 8 spikes — did the shadow ceiling envelope them?")
    print("   actual_ms  baseline_ms  ceiling_ms  enveloped")
    for a, b, c, e in spikes[:8]:
        print(f"   {a:9d}  {b:11.0f}  {(c or 0):10.0f}  "
              f"{'YES' if e else 'NO ':>9}")
    print("\nSHADOW — no timeout was enforced; ``ceiling_ms`` is the "
          "counterfactual dynamic HTTP timeout Slice-2 WOULD adopt. "
          "It inflates under congestion and deflates when healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
