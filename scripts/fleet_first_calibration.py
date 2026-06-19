"""First live FleetEvaluator calibration loop (operator mandate #4).

Runs the ACTUAL subsystem code (not a manual probe) against the live DW API in
advisory mode over the accessible roster, then prints per-model QualityScores,
the would-be re-rank for the STANDARD code route, and the graduation verdict.

Run: DOUBLEWORD_API_KEY=... PYTHONPATH=. python3 scripts/fleet_first_calibration.py
"""
from __future__ import annotations

import asyncio
import os

from backend.core.ouroboros.governance.fleet_evaluator import (
    FleetEvaluator,
    default_model_caller,
    _grad_margin,
)
from backend.core.ouroboros.governance import fleet_calibration_store as s

ROSTER = [
    "deepseek-ai/DeepSeek-V4-Flash",
    "deepseek-ai/DeepSeek-V4-Pro",
    "openai/gpt-oss-120b",
    "zai-org/GLM-5.2",
    "google/gemma-4-31B-it",
    "Qwen/Qwen3.5-397B-A17B-FP8",   # the incumbent default we want to demote
]
DEFAULT_MODEL = "Qwen/Qwen3.5-397B-A17B-FP8"


async def main() -> None:
    os.environ["JARVIS_FLEET_EVALUATOR_ENABLED"] = "true"
    os.environ.setdefault("JARVIS_FLEET_CALIBRATION_PATH", "/tmp/fleet_first_calibration.json")
    os.environ.setdefault("JARVIS_FLEET_PROBE_TIMEOUT_S", "90")
    # Leave JARVIS_FLEET_PROBE_MAX_TOKENS unset so the subsystem default
    # (2048) applies — 512 truncates the two-function codegen battery.

    store = s.FleetCalibrationStore()
    ev = FleetEvaluator(
        model_caller=default_model_caller,
        store=store,
        idle_check=lambda: True,
        default_model=DEFAULT_MODEL,
    )
    from backend.core.ouroboros.governance.fleet_evaluator import _probe_max_tokens
    print(
        f"probing {len(ROSTER)} models "
        f"(codegen + classify each, max_tokens={_probe_max_tokens()})...\n"
    )
    await ev.calibrate_models(ROSTER)

    scores = store.all_scores()
    print(f"{'model':42} {'ast':>5} {'label':>6} {'tok/s':>7} {'vtps':>7} {'n':>2}")
    print("-" * 76)
    for m in ROSTER:
        sc = scores.get(m)
        if sc is None:
            print(f"{m:42} {'--- no result (probe failed) ---':>30}")
            continue
        print(
            f"{m:42} {sc.ast_pass_rate:5.2f} {sc.label_adherence:6.2f} "
            f"{sc.tok_per_s:7.1f} {s.valid_tok_per_s(sc):7.1f} {sc.sample_count:2d}"
        )

    print("\n--- STANDARD (code) route re-rank by valid_tok_per_s ---")
    reranked = s.fleet_rerank(
        "standard", tuple(ROSTER), scores, route_kind="code"
    )
    for i, m in enumerate(reranked, 1):
        print(f"  {i}. {m}")

    print("\n--- triage route re-rank by triage_fitness ---")
    reranked_t = s.fleet_rerank(
        "semantic_triage", tuple(ROSTER), scores, route_kind="triage"
    )
    for i, m in enumerate(reranked_t, 1):
        print(f"  {i}. {m}")

    winner = s.graduation_ready(
        scores,
        default_model=DEFAULT_MODEL,
        min_samples=1,             # one calibration cycle for this demo
        min_margin=_grad_margin(),
    )
    print(
        f"\ngraduation verdict (vs default {DEFAULT_MODEL}): "
        f"{'GRADUATE -> ' + winner if winner else 'no graduation yet'}"
    )
    print(f"daily probe spend so far: ${store.spend_today(__import__('time').time()):.5f}")


if __name__ == "__main__":
    asyncio.run(main())
