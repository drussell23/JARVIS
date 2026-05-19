#!/usr/bin/env python3
"""Predictive Provider Resilience — Slice 1 SHADOW proof harness.

READ-ONLY. Replays the durable Slice-0 dataset
(``.jarvis/provider_latency.jsonl``) prequentially through the
**actual production** :class:`TtftForecaster` — it imports and drives
the real forecaster, it does NOT re-implement the math (so the proof
cannot diverge from what ships).

For every row, in arrival order, it:
  1. asks the STANDING model for ``(predicted, sigma, threshold)``
     where ``threshold = predicted + k·sigma`` (the value a future
     Slice-2 dynamic HTTP timeout WOULD adopt);
  2. compares against the just-arrived actual TTFT;
  3. folds the row in (predict-then-update — honest out-of-sample).

It then reports:
  * the prequential running-MAE curve (is the EMA tracking reality);
  * point-forecast coverage vs k·σ-envelope coverage (does the
    variance margin envelope the spikes);
  * the worst latency spikes and whether the shadow threshold would
    have enveloped them WITHOUT an unbounded hang.

Enforces nothing. Usage:
    python3 scripts/predictive_resilience_proof.py \
        [--jsonl .jarvis/provider_latency.jsonl] [--k 3.0] [--min-n 15]
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
    ProviderLatencySample,
    TtftForecaster,
)


def _load(path: Path):
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
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
    args = ap.parse_args()

    rows = _load(Path(args.jsonl))
    succ = [
        r for r in rows
        if r.get("outcome") == "success"
        and int(r.get("input_tokens", 0) or 0) > 0
        and int(r.get("ttft_ms", -1)) >= 0
    ]
    print(f"=== Predictive Resilience SHADOW proof (k={args.k}) ===")
    print(f"dataset={args.jsonl}  total_rows={len(rows)}  "
          f"fittable_success={len(succ)}")
    if len(succ) < args.min_n:
        print(f"\nHOLD: need >= {args.min_n} fittable rows, have "
              f"{len(succ)}. Statistically insufficient — NOT "
              f"presenting a proof on noise.")
        return 2

    fc = TtftForecaster()
    running_abs = 0.0
    scored = 0
    pt_cov = 0
    env_cov = 0
    worst = []  # (actual, predicted, threshold, enveloped, tokens)

    print("\n  idx  tokens   actual_ms  pred_ms   sigma_ms  thresh_ms"
          "  env  run_MAE")
    print("  " + "-" * 70)
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
        res = fc.observe(s)
        if res.predicted_ms is None:
            continue
        scored += 1
        running_abs += (res.abs_err_ms or 0.0)
        mae = running_abs / scored
        if res.predicted_ms >= res.actual_ms:
            pt_cov += 1
        if res.enveloped:
            env_cov += 1
        worst.append(
            (res.actual_ms, res.predicted_ms, res.threshold_ms,
             res.enveloped, res.input_tokens)
        )
        if scored <= 40 or scored % 10 == 0:
            print(
                f"  {i:4d}  {res.input_tokens:6d}  "
                f"{res.actual_ms:9d}  "
                f"{res.predicted_ms:8.0f}  "
                f"{(res.sigma_ms or 0):8.0f}  "
                f"{(res.threshold_ms or 0):9.0f}  "
                f"{'Y' if res.enveloped else 'n':>3}  "
                f"{mae:8.0f}"
            )

    if scored == 0:
        print("\nNo estimable predictions yet (model still cold).")
        return 2

    final_mae = running_abs / scored
    pt_rate = pt_cov / scored
    env_rate = env_cov / scored
    worst.sort(reverse=True)

    print("\n=== PROOF SUMMARY ===")
    print(f"scored predictions (out-of-sample): {scored}")
    print(f"final prequential MAE:              {final_mae:,.0f} ms")
    print(f"point-forecast coverage:            {pt_rate:6.1%}  "
          f"(bare mean ≥ actual)")
    print(f"k·σ ENVELOPE coverage (k={args.k}):     {env_rate:6.1%}  "
          f"(predicted + k·σ ≥ actual)")
    print(f"envelope advantage:                 "
          f"{env_rate - pt_rate:+.1%}")
    print("\n  worst 8 latency spikes — would the shadow timeout "
          "have enveloped them?")
    print("   actual_ms  pred_ms   thresh_ms  enveloped  tokens")
    for a, p, t, e, tok in worst[:8]:
        print(f"   {a:9d}  {p:7.0f}  {(t or 0):9.0f}  "
              f"{'YES' if e else 'NO ':>9}  {tok}")
    print("\nSHADOW — no timeout was enforced; this is the "
          "counterfactual the dynamic HTTP timeout (Slice 2) WOULD "
          "have applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
