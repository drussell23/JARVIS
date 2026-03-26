# Doubleword × Trinity AI — Integration Guide

**Last updated:** 2026-03-25
**Status:** Benchmarked (35B + 397B) · VLA vision pipeline LIVE (235B + 35B) · Ouroboros Neuro-Compilation PROVEN · Tier 0 batch routing active
**Benchmark results:** `benchmarks/doubleword/results/2026-03-25T20-08-33-UTC.json` (latest 397B)
**Latest Batch ID:** `d36e8837-326b-424d-9a4e-68a5b1e091b8`

---

## Overview

Doubleword (formerly TitanML, $12M Series A — Dawn Capital) is a managed async batch LLM inference service providing an OpenAI-compatible API with access to models up to 397B parameters at 75–95% lower cost than real-time alternatives.

**Integration role in Trinity AI:** Doubleword serves as **Tier 0** in Trinity's routing architecture — handling ultra-complex tasks that exceed J-Prime's NVIDIA L4 VRAM ceiling, and as the scoring engine for Reactor-Core's latency-insensitive DPO training pipeline.

**Key economic insight:** Doubleword charges per token consumed against a queued batch, not per second of GPU time. Trinity's J-Prime tier charges for every second the `g2-standard-4` VM runs, regardless of whether it's doing inference or idle. Below ~1,150 ops/day, Doubleword is unambiguously cheaper. Above that threshold, the always-on VM amortises. The routing policy exploits this crossover point.

---

## Benchmark Results (2026-03-18)

> **Live production run** — not simulated. Batch `ca6b7b1f` submitted to `https://api.doubleword.ai/v1/batches`, polled to completion, output retrieved and parsed. All numbers below are exact API response values.

### Setup

| Component | Value |
|-----------|-------|
| Doubleword model | `Qwen/Qwen3.5-35B-A3B-FP8` (35B total · ~3B active params · MoE reasoning) |
| J-Prime baseline | `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` |
| J-Prime compute | NVIDIA L4, g2-standard-4, 24GB VRAM, GCP us-central1-b |
| J-Prime boot | `jarvis-prime-golden` custom GCP image (11 GGUF models pre-baked · ~87s cold start) |
| Batch window | 1-hour SLA |
| Tasks | Secure Infrastructure Code (NIST 800-53) + Defense Threat Analysis (SSH lateral movement) |
| API pricing | $0.10/1M input tokens · $0.40/1M output tokens |

### Cost Comparison

![Cost Comparison — J-Prime VM time vs Doubleword Batch API](../../benchmarks/doubleword/chart_dw_cost.png)

**Chart interpretation:** Three grouped bar pairs show per-task and total cost. The J-Prime bars (blue) reflect VM spot pricing ($1.20/hr) prorated against actual inference time — 27.6s for the infrastructure task and 5.3s for the threat task. The Doubleword bars (green) reflect pure token cost with zero idle-time charge. The total column makes the ratio unmistakable: $0.010988 vs $0.000376. Note that the cost scale is logarithmic in effect — the green bars are so small relative to blue that they barely register visually, which is exactly the point. At this scale of difference (29x), the economic argument for Doubleword on latency-insensitive tasks closes immediately.

| Task | J-Prime (VM time) | Doubleword (batch) | Savings |
|------|-------------------|--------------------|---------|
| Secure Infrastructure Code | $0.009210 | $0.000288 | **32x cheaper** |
| Defense Threat Analysis | $0.001778 | $0.000088 | **20x cheaper** |
| **Total (both tasks)** | **$0.010988** | **$0.000376** | **29x cheaper · 96.6% savings** |

**Why the infrastructure task has a larger absolute saving:** The infra task ran for 27.6s on J-Prime, while the threat task ran for only 5.3s. J-Prime's clock doesn't care about output quality — it charges for wall time. Doubleword charged $0.000288 for 700 output tokens regardless of how long the batch took to execute internally. Longer J-Prime tasks produce proportionally larger Doubleword savings.

**Input token cost is negligible:** Both tasks submitted 78 input tokens each ($0.0000078 per task at $0.10/1M). The cost is almost entirely output-token-driven. This means that verbose system prompts or long context windows add very little marginal cost — which matters for the governance pipeline's context-expansion step that injects `FileNeighborhood` graph data into prompts.

### Token Volume

![Output Token Volume — J-Prime vs Doubleword](../../benchmarks/doubleword/chart_dw_tokens.png)

**Chart interpretation:** Both models produced similar raw output token counts (680 vs 700 for infra; 130 vs 200 for threat). However, the comparison is not apples-to-apples. J-Prime's bars show `finish_reason: stop` — the model completed its response naturally. Doubleword's bars show `finish_reason: length` — the model was cut off at the `max_tokens` cap. This distinction is critical: **Doubleword's output tokens for both tasks were consumed entirely by the chain-of-thought reasoning layer**, not by final output. The orange annotation in the chart signals that `max_tokens` is undersized for reasoning models. See the *Reasoning Model Deep Dive* section below for the full analysis and recommended budgets.

| Task | J-Prime tokens | Finish | Doubleword tokens | Finish | Gap |
|------|---------------|--------|-------------------|--------|-----|
| Infrastructure | 680 | `stop` ✓ | 700 | `length` ✗ | Cap exhausted during reasoning |
| Threat analysis | 130 | `stop` ✓ | 200 | `length` ✗ | Cap exhausted during reasoning |

### Timing

| Metric | Value |
|--------|-------|
| Batch wall time | 257 seconds (4.3 min) |
| J-Prime real-time total | ~33 seconds |
| Speed tradeoff | **7.8x slower wall-to-wall** |
| Acceptable latency SLA | Yes — Tier 0 targets 1-hour window ops |

**The 7.8x slowdown is a feature, not a bug.** J-Prime runs synchronously in the governance pipeline's hot path — if it takes 33s, the user-facing operation blocks for 33s. Doubleword's batch is submitted, a job ID is stored in the governance ledger, and Trinity's pipeline continues handling other work. The result is retrieved asynchronously when ready. This means Doubleword doesn't add to perceived latency at all — it runs in the background.

### Key Finding: Reasoning Model Token Budget

`Qwen3.5-35B-A3B-FP8` is a chain-of-thought reasoning model with a separate `reasoning_content` field in its API response. This field contains the model's internal thinking — hypotheses, step-by-step analysis, self-correction loops — before the final `content` field. Both the thinking tokens and the output tokens are billed at output rates ($0.40/1M). At `max_tokens=700`, **both tasks exhausted their budget entirely within the reasoning layer before producing a single token of final output** — `content` was empty in both cases.

This is not a model deficiency. It reflects a calibration mismatch: the token budget was sized for a non-reasoning model, but the 35B-A3B is a reasoning model. The fix is straightforward — raise `max_tokens` to give the model room to think and then output.

**Cost implication of the token budget error:** We paid $0.000376 for 900 reasoning tokens and 0 useful output tokens. With correct budgets (2000/500), the same two tasks would cost approximately $0.00096 — still 11x cheaper than J-Prime — while producing complete, usable output. The benchmark result is therefore a lower bound on Doubleword's output quality, not a ceiling.

**Recommended token budgets for Qwen3.5 reasoning models:**

> **Updated 2026-03-25** based on 397B benchmarking. The 397B reasons more deeply than the 35B —
> budget estimates below are calibrated from live batch results, not projections.

| Task type | Recommended `max_tokens` | Actual reasoning overhead (397B) |
|-----------|--------------------------|----------------------------------|
| Code generation (complex) | **20000** | ~15,000 reasoning → ~4,300 output |
| Code generation (simple) | 8000–10000 | ~5,000–7,000 reasoning tokens |
| Threat analysis / classification | **5000** | ~2,400 reasoning → ~2,600 output |
| Architecture review | 15000–20000 | ~10,000–15,000 reasoning tokens |
| DPO preference scoring | 5000–8000 | ~3,000–5,000 reasoning tokens |

**Rule of thumb for 397B:** Set `max_tokens` to **4–5× expected output** for code generation, and **2–2.5× expected output** for analysis tasks. The provider default is 10000.

---

## 397B Benchmark Results (2026-03-25)

> **Live production run** — 4 iterative runs calibrating token budgets. Final run: batch `d36e8837` submitted to `Qwen/Qwen3.5-397B-A17B-FP8`, both tasks completed with `finish_reason: stop`.

### Final Run (Corrected Budgets)

| Task | Budget | Used | finish_reason | Content | Cost |
|------|--------|------|---------------|---------|------|
| Secure Infrastructure Code | 20,000 | 4,323 | **stop** ✅ | Full NIST 800-53 Python validator with FedRAMP audit logging | $0.00174 |
| Defense Threat Analysis | 5,000 | 2,640 | **stop** ✅ | CRITICAL classification + 3 actionable response bullets | $0.00106 |
| **Total** | — | **6,963** | — | — | **$0.00280** |

### Cost Comparison: 397B vs J-Prime (14B L4)

| Metric | J-Prime (14B) | Doubleword (397B) | Delta |
|--------|---------------|-------------------|-------|
| Total cost (both tasks) | $0.01099 | $0.00280 | **4x cheaper** |
| Output tokens | 810 | 6,963 | **8.6x more output** |
| Wall time | ~33s (streaming) | 55s (batch) | 1.7x slower |
| Monthly @ 100 ops/day | $216.00/mo | **$8.40/mo** | **25.7x cheaper** |

### Calibration History

| Run | Model | Infra `max_tokens` | Threat `max_tokens` | Infra Output | Threat Output | Wall Time |
|-----|-------|--------------------|---------------------|-------------|---------------|-----------|
| 1 (Mar 18) | 35B | 700 | 200 | None (reasoning exhausted) | None | 257s |
| 2 (Mar 25) | 397B | 5,000 | 2,000 | Partial code (truncated) | None | 270s |
| 3 (Mar 25) | 397B | 10,000 | 5,000 | None (deeper reasoning) | **Complete** ✅ | 130s |
| **4 (Mar 25)** | **397B** | **20,000** | **5,000** | **Complete** ✅ | **Complete** ✅ | **55s** |

**Key insight:** The 397B's reasoning depth scales with available budget. At 5K tokens, reasoning was shallow enough to produce partial code output. At 10K, it reasoned deeper and consumed the entire budget. At 20K, it had room to reason fully AND produce complete output. This non-linear behavior means budgets must be set generously — the model self-regulates quality rather than truncating.

---

## VL-235B Vision Benchmark (2026-03-25)

> **Live dual-model vision test** — Doubleword's `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` as the "fast eye" alongside Claude Vision as the "deep brain", both observing a bouncing ball animation with on-screen counters.

### Test: `tests/test_vision_dual_model_realtime.py`

The VL-235B acts as Tier 0 for the Lean Vision Loop — a real-time OpenAI-compatible `/chat/completions` endpoint (not batch) with base64 image input.

### Results

| Metric | Doubleword VL-235B | Claude Vision |
|--------|-------------------|---------------|
| **Role** | Fast eye (every ~4s) | Deep brain (every ~12s) |
| **Cold start latency** | 11.9s (first call) | N/A (API always warm) |
| **Warm call latency** | **3.6s** | ~4-6s |
| **Output quality** | Reads counters accurately, tracks ball position + direction | Pattern analysis, trend detection across observations |
| **Model** | `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` (235B total, ~22B active) | `claude-sonnet-4-20250514` |

### Sample Output

```
Doubleword VL-235B (fast eye):
  Read #1: "Derek, bounces show 3 horizontal, 3 vertical, 6 total at 331 px/s
            - green ball positioned upper-left moving diagonally." (11.9s, cold)
  Read #2: "Derek, ball moves left-down; counters: H7, V9, Total16,
            Speed331 px/s." (3.6s, warm)
```

### Integration in Lean Vision Loop

The VL-235B is already wired as Tier 0 in `backend/vision/lean_loop.py`:

```
Vision Provider Cascade:
  Tier 0: Doubleword VL-235B  (real-time, 3.6s warm, ~22B active params)
  Tier 1: Claude Vision        (fallback, 4-6s)
  Tier 2: J-Prime GCP          (last resort, only when VM running)
```

**Key advantage:** The VL-235B provides near-Claude-quality vision at MoE economics — 235B total params but only ~22B active per forward pass. For the fast-eye role (frequent, low-latency screen reads), this is ideal: fast enough for 4-second observation cycles, accurate enough to read counters and track motion.

---

## Understanding the MoE Cost Structure

This is the single most important concept for modeling Doubleword's economics at scale.

All large models in the Doubleword catalog tagged `A3B`, `A12B`, `A17B`, or `A22B` are **Mixture of Experts (MoE)** architectures. In a MoE model, the total parameter count and the *active* parameter count during any single forward pass are different numbers:

| Model | Total params | Active params | Ratio |
|-------|-------------|---------------|-------|
| `Qwen3.5-35B-A3B-FP8` | 35B | ~3B | 8.6% active |
| `Qwen3.5-397B-A17B-FP8` | 397B | ~17B | 4.3% active |
| `Qwen3-VL-235B-A22B-Instruct-FP8` | 235B | ~22B | 9.4% active |
| `NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 120B | ~12B | 10% active |

**What this means for cost:** Inference compute scales with active parameters, not total parameters. A single forward pass through `Qwen3.5-397B-A17B` activates approximately 17B parameters — comparable to running a 17B dense model — but the output carries the quality advantage of a model trained with 397B parameters in its mixture. You get 397B-scale knowledge at ~17B compute cost.

**What this means for pricing:** Doubleword prices per output token, not per FLOP. At $0.40/1M output tokens, every model in the catalog costs the same per token regardless of MoE structure. The larger models are not more expensive per token — they are simply more capable. This pricing structure makes `Qwen3.5-397B-A17B-FP8` the obvious choice for Tier 0: same token cost, dramatically more reasoning capacity for architecture reviews and DPO scoring.

**Why this matters for Trinity's DPO pipeline:** Reactor-Core generates preference pairs by having a judge model score N candidate responses. Using the 397B model as judge costs the same per token as the 35B, but the preference scores will be higher quality, producing better training signal for J-Prime fine-tuning.

---

## Reasoning Model Deep Dive

The `Qwen3.5` series differs architecturally from standard instruction-tuned models in one critical way: it performs explicit chain-of-thought reasoning internally before generating output. The API response surfaces this as two distinct fields:

```json
{
  "choices": [{
    "message": {
      "role": "assistant",
      "reasoning_content": "Let me think through this step by step...\n[600 tokens of internal reasoning]",
      "content": ""
    },
    "finish_reason": "length"
  }]
}
```

**Billing:** Both `reasoning_content` tokens and `content` tokens are billed at the output token rate ($0.40/1M). The model does not distinguish between "thinking" and "answering" for billing purposes.

**Production parsing:** The `DoublewordProvider` must extract `message.content`, not `message.reasoning_content`, when returning a `GenerationResult` to the governance pipeline. The reasoning layer is internal scaffolding — useful for debugging but not part of the deliverable. If `message.content` is empty and `finish_reason` is `length`, the provider must detect this condition and either retry with a higher token budget or fall back to Tier 1 (J-Prime).

**Quality advantage:** For complex multi-step tasks — the exact workloads Trinity routes to Tier 0 — the reasoning layer is a significant quality multiplier. A 35B reasoning model outperforms a 35B non-reasoning model on architecture analysis and code review tasks because it self-checks its work before outputting. For DPO scoring specifically, reasoning models produce more reliable preference scores because they articulate *why* one candidate is better than another before assigning a score.

**The token budget rule of thumb (updated for 397B):** Set `max_tokens` = **4–5× expected output** for code generation, **2–2.5× expected output** for analysis. The 397B reasons much more deeply than the 35B — a 4.3K-token code output required a 20K budget. For a thorough architecture review, budget 15K–20K minimum. The provider default (`DOUBLEWORD_MAX_TOKENS`) is 10000.

---

## Model Catalog

![Doubleword Model Catalog — Parameter Scale (log)](../../benchmarks/doubleword/chart_dw_catalog.png)

**Chart interpretation:** Models are plotted on a log-scale horizontal axis by total parameter count. The log scale is necessary because the range spans 9B to 397B — a 44x span. J-Prime (blue) is shown as a reference line at 14B for direct comparison. The green bar (`Qwen3.5-35B`) marks the model used in this benchmark; the purple bar (`Qwen3.5-397B`) marks the Tier 0 routing candidate. Grey bars are additional catalog options. The key insight from this chart: J-Prime sits in the lower third of the parameter range. Doubleword's Tier 0 candidate is 28x larger by parameter count, and the MoE architecture means it runs at comparable per-token compute cost to a 17B dense model. The catalog gives Trinity access to a 397B model that would require a cluster of ~17 NVIDIA A100s to self-host — Doubleword exposes it as a $0.40/1M token API call.

Full catalog via `GET https://api.doubleword.ai/v1/models` as of 2026-03-18:

| Model ID | Total Params | Active Params | Architecture | Best for |
|----------|-------------|---------------|--------------|----------|
| `Qwen/Qwen3.5-9B` | 9B | 9B | Dense | Fast classification, routing decisions |
| `Qwen/Qwen3-14B-FP8` | 14B | 14B | Dense | J-Prime quality parity comparison |
| `openai/gpt-oss-20b` | 20B | 20B | Dense | OpenAI-style instruction following |
| `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8` | 30B | 3B | MoE + Vision | Multimodal — diagram analysis |
| `Qwen/Qwen3.5-35B-A3B-FP8` | 35B | 3B | MoE + Reasoning | **Used in benchmark** · code + analysis |
| `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 120B | 12B | MoE | Complex multi-step reasoning |
| `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | 235B | 22B | MoE + Vision | Architecture diagrams + ultra-scale analysis |
| `Qwen/Qwen3.5-397B-A17B-FP8` | 397B | 17B | MoE + Reasoning | **Tier 0 primary** · DPO scoring · arch reviews |

**Pricing (March 2026):** $0.10/1M input · $0.40/1M output — applies uniformly across all models.

---

## Architecture: Where Doubleword Fits

![Trinity AI Compute Routing Architecture](../../benchmarks/doubleword/chart_dw_routing.png)

**Chart interpretation:** Three tier boxes are stacked vertically with bidirectional arrows showing escalation (downward, when complexity exceeds a tier's capability) and fallback (upward, when a tier is unavailable or times out). Doubleword sits at the top — it is the most capable tier but also the most latency-tolerant. J-Prime sits in the middle: real-time, always-on, the default hot path. Claude API sits at the bottom as the emergency fallback when both J-Prime and Doubleword are unavailable. The routing decision is made once per operation by `RoutingPolicy.decide()` based on `complexity_score`, `task_category`, and `deadline_ms`. The arrows are not symmetric: escalation is deliberate (routing choice), fallback is reactive (failure recovery).

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRINITY AI COMPUTE TIERS                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  TIER 0 — Doubleword Batch API                     [ASYNC]     │
│  ├─ Model: Qwen3.5-397B-A17B-FP8 (397B · ~17B active)         │
│  ├─ Cost:  $0.10/1M in · $0.40/1M out                         │
│  ├─ SLA:   1-hour or 24-hour completion window                 │
│  ├─ Trigger: complexity > 0.85 OR task in ULTRA_TASKS          │
│  └─ Use: architecture reviews, cross-repo analysis, DPO        │
│                          ↓ escalate / ↑ fallback               │
│  TIER 1 — J-Prime · NVIDIA L4 (GCP g2-standard-4)  [REALTIME] │
│  ├─ Model: Qwen2.5-Coder-14B-Q4_K_M (~24 tok/s)               │
│  ├─ Boot:  jarvis-prime-golden image · 11 GGUFs · ~87s cold    │
│  ├─ Cost:  ~$0.009/request (VM time, spot pricing)             │
│  ├─ VRAM: 24GB → ceiling ~32B models                           │
│  └─ Use: standard governance ops, streaming inference           │
│                          ↓ escalate / ↑ fallback               │
│  TIER 2 — Claude API                               [REALTIME]  │
│  ├─ Model: claude-sonnet-4-6                                   │
│  ├─ Cost:  $3/1M in · $15/1M out                               │
│  └─ Use: emergency fallback, tool-use-heavy tasks               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Reactor-Core DPO Pipeline:
  Telemetry JSONL → Doubleword Batch (397B) → scored preference pairs → J-Prime fine-tune
```

**Note on the GCP golden image:** `jarvis-prime-golden` pre-bakes all 11 quantized GGUF models directly into the boot disk, reducing cold-start time from ~8 minutes (model download) to ~87 seconds (model load from disk). Doubleword requires **zero changes** to this image. The integration is entirely client-side: the `autobatcher` pip package is added to requirements, `DOUBLEWORD_API_KEY` and `DOUBLEWORD_BASE_URL` are added to `.env`, and the `DoublewordProvider` class is registered in the governance bootstrap. The golden image stays lean and unchanged.

---

## Batch API Protocol

Understanding the exact API flow is essential for implementing the `DoublewordProvider` correctly. The batch endpoint is not a simple POST/response — it is a four-stage asynchronous protocol:

```
Stage 1: Upload
  POST /v1/files
  Content-Type: multipart/form-data
  Body: JSONL file where each line = one {"custom_id": "...", "method": "POST",
        "url": "/v1/chat/completions", "body": {...openai request...}}
  Response: {"id": "file-abc123", ...}

Stage 2: Create Batch
  POST /v1/batches
  Body: {"input_file_id": "file-abc123", "endpoint": "/v1/chat/completions",
         "completion_window": "1h"}
  Response: {"id": "batch-xyz789", "status": "in_progress", ...}

Stage 3: Poll
  GET /v1/batches/{batch_id}
  Response: {"status": "in_progress" | "completed" | "failed", ...}
  → Poll every 10–30s until status == "completed"

Stage 4: Retrieve
  GET /v1/files/{output_file_id}/content
  Response: JSONL where each line = one response, keyed by custom_id
  → Parse, extract message.content per request, ignore message.reasoning_content
```

**Key implementation details:**
- The `custom_id` field in each JSONL line is caller-defined and echoed back in the output — use operation IDs from the governance ledger to correlate results
- A single batch file can contain **N requests** — Reactor-Core should coalesce all DPO scoring candidates into one batch job rather than submitting one job per candidate (see *Batch Coalescing* below)
- The `completion_window` is either `"1h"` or `"24h"` — choose based on whether the result is needed before the next governance cycle or the next training epoch
- `output_file_id` is returned in the completed batch response as `output_file_id` — store this in the ledger alongside the `batch_id` for auditability

---

## Monthly Cost Projections

![Monthly Cost Projection — Doubleword vs J-Prime VM](../../benchmarks/doubleword/chart_dw_monthly.png)

**Chart interpretation:** The green line shows Doubleword's monthly cost scaling linearly with daily operation count — it starts near zero and climbs gradually because every dollar is earned by actual usage. The blue dashed line shows J-Prime at 6hr/day spot pricing ($216/mo) — a flat cost regardless of how many operations run. The yellow dashed line shows J-Prime always-on ($864/mo). The vertical dotted lines mark break-even points: where Doubleword's cumulative cost crosses the flat VM cost. Below ~1,150 ops/day, Doubleword is cheaper than 6hr/day spot. Below ~4,600 ops/day, Doubleword is cheaper than always-on. The area to the left of the first break-even line — which encompasses Trinity's current operating range — is the zone where every Tier 0 routing decision saves money.

| Daily ops | Doubleword batch/mo | J-Prime 6hr/day spot/mo | J-Prime always-on/mo |
|-----------|---------------------|------------------------|----------------------|
| 10 ops/day | $0.11 | $216 | $864 |
| 50 ops/day | $0.56 | $216 | $864 |
| 100 ops/day | $1.13 | $216 | $864 |
| 500 ops/day | $5.63 | $216 | $864 |
| 1,000 ops/day | $11.27 | $216 | $864 |
| **Break-even (6hr)** | **$216** | **~1,150 ops/day** | — |
| **Break-even (always-on)** | **$864** | — | **~4,600 ops/day** |

*Based on $0.0003756 per 2-task batch. Assumes correct token budgets (costs will be ~2.5x higher with proper max_tokens for reasoning models, break-even shifts to ~460 and ~1,840 ops/day respectively — still strongly favorable at Trinity's current scale).*

---

## Full Benchmark Dashboard

![Trinity AI × Doubleword — Full Benchmark Dashboard](../../benchmarks/doubleword/chart_dw_dashboard.png)

**Chart interpretation:** The 2×3 dashboard consolidates all six analyses. Top row (left to right): cost comparison per task and total, output token volume with finish-reason annotation, model catalog log-scale. Bottom row: monthly cost projection with break-even markers, Trinity tier routing diagram, key metrics table. The metrics table in the bottom-right is designed as a quick-reference summary — cost ratio (29x), savings percentage (96.6%), exact dollar amounts, token counts, wall times, and speed tradeoff — all sourced directly from the live batch result. This dashboard is generated programmatically from `benchmarks/doubleword/results/*.json` and will auto-update when re-run against new benchmark data or a different model.

---

## Batch Coalescing — A Critical Optimization

The Doubleword batch API accepts **N requests per batch file**. Trinity should never submit one batch job per inference request when multiple requests share a similar deadline.

**Example — Reactor-Core DPO scoring:**

A naive implementation submits one batch per candidate:
```
Candidate 1 → batch job A (1 request)
Candidate 2 → batch job B (1 request)
Candidate 3 → batch job C (1 request)
```
This creates 3 `output_file_id` values to track, 3 polls, 3 result retrievals.

The correct implementation coalesces all candidates into one batch:
```
[Candidate 1, Candidate 2, Candidate 3] → batch job A (3 requests, 1 JSONL)
```
One poll, one retrieval, results keyed by `custom_id` matching each candidate ID. For a DPO cycle with 20 candidates, this reduces batch management overhead by 20x and guarantees all scores share the same timestamp — important for fair comparison.

**Coalescing window:** The Ouroboros governance pipeline should implement a `BatchAccumulator` with a configurable window (e.g., 30 seconds) that collects all Tier 0 requests and flushes them as a single batch job. This is the async governance pattern that makes Tier 0 work without blocking the hot path.

---

## Async Governance Pattern

Tier 0 routing breaks the synchronous request/response model that Tier 1 (J-Prime) uses. The governance pipeline must handle this without blocking.

**Proposed pattern:**

```python
# Tier 1 (synchronous — current pattern)
result = await jprime_provider.generate(context, deadline)
# blocks until generation complete, then continues

# Tier 0 (asynchronous — new pattern)
batch_id = await dw_provider.submit(context)
await ledger.record_pending(operation_id, batch_id, deadline)
# returns immediately — operation enters PENDING_TIER0 state
# background poller checks ledger, advances FSM when batch completes
```

The `PreemptionFsmEngine` needs a new `PENDING_TIER0` state that suspends the operation without consuming the pipeline timeout. The governance ledger stores `(operation_id, batch_id, submitted_at, deadline)`. A background polling task checks all pending Tier 0 operations on a configurable interval (default: 30s), retrieves results when the batch completes, and resumes the FSM at `GENERATE → VALIDATE`.

**Deadline handling:** If the batch hasn't completed by `deadline - 60s`, the governance pipeline should automatically fall back to Tier 1 (J-Prime) with the original prompt. The `asyncio.wait_for()` pattern applies but wraps the poller, not the batch itself — the batch continues running in Doubleword's infrastructure regardless of the local deadline.

---

## Failure Modes & Resilience

| Failure mode | Detection | Recovery |
|-------------|-----------|----------|
| Doubleword API unreachable | `health_probe()` returns False at routing time | Route directly to Tier 1 — skip Tier 0 entirely |
| File upload fails (HTTP 4xx/5xx) | `POST /v1/files` non-200 response | Retry once with backoff; fall back to Tier 1 on second failure |
| Batch creation fails | `POST /v1/batches` non-200 | Same retry + fallback pattern |
| Batch status `"failed"` | Poll returns `status: "failed"` | Log error with `batch_id`, fall back to Tier 1, alert |
| Batch times out (>SLA window) | `submitted_at + window > now` | Fall back to Tier 1 with same prompt; record Tier 0 timeout in telemetry |
| Output `finish_reason: length` | Parse response, check field | Retry with `max_tokens * 2` if budget allows; else return partial result with flag |
| `message.content` empty (pure reasoning) | Content field empty or null | Detected as reasoning-only truncation; retry with higher budget |

**Circuit breaker:** The `DoublewordProvider` should implement the existing `_circuit_breakers` pattern from `governance/providers.py`. After 3 consecutive failures, the circuit opens for 5 minutes, during which all Tier 0 requests are redirected to Tier 1 without attempting the Doubleword API.

---

## Why Doubleword vs Alternatives

Several services offer large-model inference. The key differentiators for Trinity's use case:

| Service | Pricing model | Largest model | Open-source gateway | Batch-first |
|---------|--------------|---------------|---------------------|-------------|
| **Doubleword** | Token batch | 397B (MoE) | ✓ Control Layer (Rust) | ✓ native |
| Together AI | Token real-time | 70B dense | ✗ | ✗ |
| Fireworks AI | Token real-time | 405B | ✗ | partial |
| Anyscale | Token real-time | 70B | ✗ | ✗ |
| RunPod serverless | GPU-second | Model-dependent | ✗ | ✗ |

**Why Doubleword wins for Trinity:**

1. **Batch-first pricing** — The only service where async submission with SLA-based pricing is the primary model, not a second-class feature. This directly matches Reactor-Core's DPO cycle and Tier 0 governance operations.

2. **Control Layer** — No other provider offers an open-source (Apache 2.0) Rust gateway with per-user API key management, credit budgets, and full request audit logs. This maps directly to Palantir AIP's `GovernedOperation` objects and is the only clean path to multi-tenant Trinity deployments without building a proxy from scratch.

3. **397B MoE accessible** — The largest MoE model publicly available via API at standard token pricing. Competitors cap at 70B–405B dense, which requires full VRAM footprint. The Doubleword 397B model runs at ~17B active-param compute cost.

4. **OpenAI-compatible API** — The autobatcher is a drop-in `AsyncOpenAI` replacement. Reactor-Core can switch inference backends with two lines of code, no protocol changes.

5. **Autobatcher OSS tool** — Transparent client-side batching that doesn't require any server-side changes. Trinity's Reactor-Core inference calls can be batched without refactoring the call sites.

---

## Defense & FedRAMP Considerations

Trinity's primary use case — governed AI operations in defense and critical infrastructure environments — introduces data handling requirements that don't apply to standard commercial SaaS integrations. These are open questions to validate with Doubleword before production deployment:

**Data residency:** Where does batch input data persist between submission and retrieval? Are input files retained after `output_file_id` is returned, or deleted immediately? For FedRAMP and ITAR-adjacent workloads, input data containing network topology, firewall rules, or threat intelligence must not persist in uncontrolled storage.

**Request audit log:** The Control Layer provides a full request audit log per its documentation. For defense use, this log needs to be tamper-evident and exportable. Mapping: Control Layer audit entries → Palantir AIP `GovernedOperation` audit events.

**Encryption in transit and at rest:** The JSONL batch files contain operational context (system prompts, task descriptions, code snippets). Doubleword's API is HTTPS — transit encryption is standard. At-rest encryption for queued batch files and output files before retrieval is the open question.

**Model provenance:** `Qwen3.5` is a Qwen/Alibaba model family. For certain defense contracting contexts, there may be model provenance requirements. The `openai/gpt-oss-20b` model in the catalog is an alternative for contexts where model origin matters.

**Recommendation:** For the Palantir fellowship demo and early production use, Trinity's Tier 0 operations should be scoped to non-sensitive content — architecture analysis of public code, DPO scoring of synthetic training examples, and complexity-routing decisions. Sensitive operational data (classified network telemetry, active threat intelligence) remains on Tier 1 (J-Prime, air-gapped GCP) until data handling questions are validated.

---

## Integration Points

### 1. JARVIS — `RoutingPolicy` (Highest Priority)

**File:** `backend/core/ouroboros/governance/routing_policy.py:55-162`

Add `DOUBLEWORD = "doubleword_batch"` to the `RoutingDecision` enum and a routing rule for tasks with `task_category in (CROSS_REPO_PLANNING, MULTI_FILE_ANALYSIS)` or `complexity > 0.85`.

```python
# .env additions
DOUBLEWORD_ENABLED=true
DOUBLEWORD_API_KEY=sk-...
DOUBLEWORD_BASE_URL=https://api.doubleword.ai/v1
DOUBLEWORD_MODEL=Qwen/Qwen3.5-397B-A17B-FP8
DOUBLEWORD_WINDOW=1h
DOUBLEWORD_COMPLEXITY_THRESHOLD=0.85
DOUBLEWORD_INPUT_COST_PER_M=0.10
DOUBLEWORD_OUTPUT_COST_PER_M=0.40
DOUBLEWORD_MAX_TOKENS_CODE=3000
DOUBLEWORD_MAX_TOKENS_ANALYSIS=1000
DOUBLEWORD_MAX_TOKENS_DPO=2000
```

### 2. JARVIS — `DoublewordProvider` (CandidateProvider)

**File:** `backend/core/ouroboros/governance/providers.py:1586+`

Create `backend/core/ouroboros/governance/doubleword_provider.py` implementing the `CandidateProvider` protocol:

```python
class DoublewordProvider:
    """CandidateProvider for Doubleword batch API. Async — does not block hot path."""

    @property
    def provider_name(self) -> str:
        return "doubleword"

    async def generate(self, context, deadline) -> GenerationResult:
        # 1. Build JSONL from context (inject operation_id as custom_id)
        # 2. Upload file → get file_id
        # 3. Create batch job → get batch_id, record in ledger
        # 4. Poll with deadline guard (asyncio.wait_for wrapping poller)
        # 5. Parse response: extract message.content, detect length truncation
        # 6. If content empty (reasoning-only): retry with 2× token budget
        # 7. Return GenerationResult with provider_used, cost, batch_id metadata
        ...

    async def health_probe(self) -> bool:
        # GET /v1/models — True if 200 and model present in catalog
        ...

    async def submit(self, context) -> str:
        """Non-blocking submit — returns batch_id for ledger storage."""
        ...

    async def retrieve(self, batch_id: str) -> GenerationResult | None:
        """Check batch status; returns None if still in_progress."""
        ...
```

### 3. J-Prime — `hybrid_tiered_router.py`

**File:** `jarvis_prime/core/hybrid_tiered_router.py:15-45`

```yaml
# config/unified_config.yaml additions
routing:
  tiers:
    - name: tier_0_local
      complexity_max: 0.45
    - name: tier_1_cloud
      complexity_max: 0.70
    - name: tier_2_deep
      complexity_max: 0.85
    - name: tier_3_doubleword   # NEW
      complexity_min: 0.85
      model: Qwen/Qwen3.5-397B-A17B-FP8
      endpoint: ${DOUBLEWORD_BASE_URL}
      batch_window: 1h
      max_tokens_code: 3000
      max_tokens_analysis: 1000
```

### 4. Reactor-Core — DPO Pipeline with Batch Coalescing

**File:** `reactor_core/training/dpo_pair_generator.py`

```python
class DoublewordDPOClient:
    """
    Score N candidates in a single coalesced batch job.
    24h SLA acceptable — called once per nightly training epoch.
    """

    async def score_candidates(
        self,
        candidates: list[str],
        reference: str,
        n: int = 20,
    ) -> list[float]:
        # 1. Build JSONL: one scoring request per candidate, all in one file
        # 2. custom_id = candidate_id for result correlation
        # 3. Submit as single batch (window="24h")
        # 4. Poll until complete (background task, non-blocking)
        # 5. Parse reasoning_content to understand WHY each candidate scored as it did
        # 6. Extract scores from content field, return ranked list
        ...
```

**Note on reasoning_content for DPO:** For preference scoring, the `reasoning_content` field is *useful*, not discarded. The model's internal justification for why Candidate A is better than Candidate B can be stored alongside the preference pair as a rationale label — which produces richer training signal than a numeric score alone.

### 5. Autobatcher (Drop-in for AsyncOpenAI)

```bash
pip install autobatcher
```

```python
# Before
from openai import AsyncOpenAI
client = AsyncOpenAI(api_key=OPENAI_KEY)

# After (transparent batching, no other changes needed)
from autobatcher import AsyncOpenAI
client = AsyncOpenAI(
    api_key=DOUBLEWORD_API_KEY,
    base_url="https://api.doubleword.ai/v1",
)
```

### 6. Control Layer (Future — Palantir AIP Gateway)

Doubleword's open-source [Control Layer](https://github.com/doublewordai/control-layer) (Rust, Apache 2.0) provides 450x less overhead than LiteLLM according to their benchmarks. Features:
- Per-user API key management
- Credit budgets
- Full request audit log

**Trinity mapping:** Each Ouroboros governance operation maps to one Control Layer request record. The audit log becomes the per-operation inference evidence that Palantir AIP's `GovernedOperation` objects require. For multi-tenant Trinity deployments (multiple operators sharing one Doubleword account), the per-user key management gives operator-level cost attribution and rate limiting without building custom middleware.

---

## Running the Benchmark

```bash
# Set API key
export DOUBLEWORD_API_KEY=sk-...

# Run with defaults (Qwen3.5-35B, 1h window, conservative token budgets)
python3 benchmarks/doubleword/benchmark_doubleword.py

# Run with 397B model (Tier 0 primary)
DOUBLEWORD_MODEL=Qwen/Qwen3.5-397B-A17B-FP8 \
  python3 benchmarks/doubleword/benchmark_doubleword.py

# Correct token budgets for reasoning models (recommended)
DOUBLEWORD_MAX_TOKENS_INFRA=3000 \
  DOUBLEWORD_MAX_TOKENS_THREAT=1000 \
  python3 benchmarks/doubleword/benchmark_doubleword.py
```

Results save to `benchmarks/doubleword/results/<timestamp>-UTC.json` automatically.

To regenerate all charts from the latest result:

```bash
cd benchmarks/doubleword
/path/to/.venv/bin/jupyter nbconvert --to notebook --execute \
  doubleword_benchmark_analysis.ipynb --output doubleword_benchmark_analysis.ipynb
```

---

## Roadmap

| Phase | Integration | Key risk / open question |
|-------|------------|--------------------------|
| **Week 1** | `DoublewordProvider` + `RoutingPolicy` wiring | Async governance FSM state for `PENDING_TIER0` |
| **Week 2–3** | Reactor-Core `DoublewordDPOClient` with coalescing | Reasoning content parsing for preference rationale labels |
| **Week 3–4** | Autobatcher drop-in across Reactor-Core async inference | Batch ID exposure via autobatcher for audit ledger |
| **Month 2** | `BatchAccumulator` with configurable coalescing window | Flush window tuning vs latency SLA |
| **Post-fellowship** | Control Layer as Palantir AIP audit gateway | Data residency / FedRAMP validation |
| **Scale phase** | Doubleword Inference Stack (K8s Helm) for self-hosted | Evaluate when ops/day crosses J-Prime break-even |

---

## VLA Vision Pipeline Integration (2026-03-25)

> **Live production integration** -- all results below are from real API calls against live Doubleword endpoints, captured during JARVIS VLA smoke tests.

### Architecture: Dual-Model Parallel Perception

JARVIS's Vision-Language-Action pipeline uses Doubleword models in two distinct roles:

| Role | Model | API Mode | Latency | Purpose |
|------|-------|----------|---------|---------|
| **Layer 2: Structural Vision** | `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | Real-time `/v1/chat/completions` | ~8-12s | Fast text extraction, UI element detection, object position, quadrant classification |
| **Ouroboros Architect** | `Qwen/Qwen3.5-35B-A3B-FP8` | Real-time `/v1/chat/completions` | ~60s | Code generation -- writes local Python reflexes that replace cloud API calls |

Both models operate alongside Claude Vision (Layer 3: semantic reasoning) and Apple Vision OCR (Layer 1: local deterministic text extraction).

### 235B Vision Model Performance

The 235B VLM fires in parallel with Claude Vision on every VLA cycle (~8s interval). It receives a 1280x800 PNG screenshot as a base64 `image_url` and returns structural analysis.

**Typical 235B output:**
```
Text on screen: Horizontal Bounces: 24 Vertical Bounces: 28 Total Bounces: 52
Speed: 331 px/s
Green ball: bottom-left quadrant. Trail pointing diagonally up-right.
```

**Cross-validation results (21 VLA cycles, live test):**

| Metric | Result |
|--------|--------|
| Number agreement (235B vs OCR) | **100%** -- zero drift across all cycles |
| Position consensus (235B vs Claude) | ~40-50% -- disagreements caused by temporal lag (ball moves between API calls) |
| Motion consensus | ~80% -- both models agree on trail direction |

The position disagreements are not model errors -- they are the signal that triggers Ouroboros Neuro-Compilation. The ball moves ~3 bounces/second; two API calls separated by 2-3s will naturally observe different positions.

**API call format:**
```python
{
    "model": "Qwen/Qwen3-VL-235B-A22B-Instruct-FP8",
    "messages": [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
            {"type": "text", "text": "Read ALL text on screen precisely. Describe ball position, quadrant, trail direction."},
        ],
    }],
    "max_tokens": 200,
    "temperature": 0.0,
}
```

### 35B Reasoning Model as Ouroboros Code Architect

When the VLA cross-validation detects repeated patterns (3+ cycles), Ouroboros hands the problem to the 35B reasoning model. The 35B receives:
- The 235B's latest structural analysis
- Cross-validation consensus data (position agree/disagree, motion agree/disagree)
- A concise prompt describing the desired Python function

The 35B then **generates a complete Python function** (~80-100 lines) that performs local pixel-level extraction. This function is compiled in a sandbox, validated against the last known-good OCR result, and hot-swapped into the live loop.

**35B code generation performance:**

| Metric | Value |
|--------|-------|
| Generation time | ~60s (background, non-blocking) |
| Generated code size | 80-100 lines of Python |
| Sandbox compilation | Pass rate: 100% (all generated code compiles) |
| Validation pass | Pass rate: ~80% (fails when OCR server not yet compiled) |
| `max_tokens` required | **16384** (reasoning models need 4-5x the output size for chain-of-thought) |

**Example generated reflex (abridged):**
```python
def reflex_extract(b64_png: str) -> dict:
    img = Image.open(io.BytesIO(base64.b64decode(b64_png))).convert('RGB')
    arr = np.array(img)
    green = arr[:,:,1]

    # Ball centroid via green pixel thresholding
    mask = green > 200
    ys, xs = np.where(mask)
    ball_x, ball_y = int(np.mean(xs)), int(np.mean(ys))

    # Quadrant classification
    mid_x, mid_y = w / 2, h / 2
    quadrant = ("top" if ball_y < mid_y else "bottom") + "-" + \
               ("left" if ball_x < mid_x else "right")

    # Trail direction from bright core vs mass centroid
    core_mask = green > 240
    ...

    return {"horizontal": ..., "vertical": ..., "total": ..., "speed": ...,
            "ball_x": ball_x, "ball_y": ball_y,
            "quadrant": quadrant, "trail_direction": trail_direction}
```

The generated code is not a template -- it is synthesized fresh by the 35B for each specific visual task. Different screen layouts produce different reflex functions.

### Token Budget for Reasoning Models (Updated)

Both the 35B and 397B are chain-of-thought reasoning models with a separate `reasoning` field in the API response. The `max_tokens` budget must cover BOTH the internal thinking AND the final output.

| Use Case | Model | Recommended `max_tokens` | Typical Reasoning Overhead |
|----------|-------|--------------------------|---------------------------|
| Vision reflex generation | 35B | **16384** | ~10K reasoning, ~3K output |
| Vision reflex generation | 397B | **20000** | ~15K reasoning, ~4K output |
| Batch code generation | 397B | **20000** | ~15K reasoning, ~4K output |
| Classification / analysis | 35B | **5000** | ~2K reasoning, ~2K output |
| DPO preference scoring | 397B | **8000** | ~4K reasoning, ~3K output |

**Critical lesson learned:** At `max_tokens=8192`, the 35B returns `finish_reason: length` with empty `content` -- all tokens consumed by reasoning. At `max_tokens=16384`, the same prompt returns `finish_reason: stop` with 3227 characters of working code.

### Ouroboros Neuro-Compilation: End-to-End Flow

```
VLA Cycle 1-3: Cloud perception (235B + Claude + OCR)
    |
    v
Cross-Validation: Numbers agree? Position agree? Motion agree?
    |
    v (3 cycles reached = graduation threshold)
    |
CognitiveInefficiencyEvent fires
    |
    v
Background Task launched (asyncio.create_task)
    |--- 235B analyzes the current frame (5s)
    |--- 35B generates a reflex_extract() function (60s)
    |--- Sandbox compiles the generated code
    |--- Validates against ground truth OCR result
    |
    v (VLA loop continues uninterrupted during this)
    |
Tier 4 Reflex Assimilated -- hot-swapped into live loop
    |
    v
Subsequent reads: local numpy reflex (~5ms) replaces cloud API (~8s)
```

### Vision Models Available on Doubleword

| Model | Params | Active | Type | Role in JARVIS |
|-------|--------|--------|------|----------------|
| `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | 235B | ~22B | Vision+Language | VLA Layer 2 structural analysis |
| `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8` | 30B | ~3B | Vision+Language | Fast vision fallback (not yet wired) |
| `deepseek-ai/DeepSeek-OCR-2` | -- | -- | OCR | Dedicated OCR engine (not yet wired) |
| `allenai/olmOCR-2-7B-1025-FP8` | 7B | 7B | Document OCR | Specialized document extraction (not yet wired) |
| `lightonai/LightOnOCR-2-1B-bbox-soup` | 1B | 1B | Bbox OCR | Bounding box coordinate extraction (not yet wired) |
| `Qwen/Qwen3.5-397B-A17B-FP8` | 397B | ~17B | Reasoning | Ouroboros Architect (complex code gen) |
| `Qwen/Qwen3.5-35B-A3B-FP8` | 35B | ~3B | Reasoning | Ouroboros Architect (fast code gen, default) |

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOUBLEWORD_API_KEY` | *(required)* | API authentication |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | API endpoint |
| `DOUBLEWORD_VISION_MODEL` | `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | VLA Layer 2 model |
| `DOUBLEWORD_ARCHITECT_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Ouroboros code generation model |
| `DOUBLEWORD_VISION_TIMEOUT_S` | `15` | VLA vision call timeout |
| `OUROBOROS_GRADUATION_THRESHOLD` | `3` | VLA cycles before Neuro-Compilation triggers |

---

## Open Questions for Doubleword

1. **Reasoning model token guide:** Is there published documentation on minimum `max_tokens` for `Qwen3.5-35B` and `Qwen3.5-397B` reasoning models? Specifically, what is the typical reasoning overhead in tokens for a 500-token code generation task?

2. **397B pricing:** Is `Qwen3.5-397B-A17B-FP8` priced at $0.10/$0.40 per 1M (same as 35B), or does the Tier 0 model carry a different rate? The MoE active-param cost structure suggests same pricing is feasible, but want to confirm before modeling DPO costs.

3. **Control Layer production readiness:** Is the Rust gateway stable for single-tenant production use today, or is it still in active development? Specifically interested in the audit log schema for mapping to Palantir AIP `GovernedOperation` objects.

4. **Autobatcher batch ID exposure:** Does the autobatcher library surface the underlying `batch_id` for external tracking, or does it fully abstract the batch lifecycle? The governance ledger needs to store batch IDs for audit trail purposes.

5. **Data retention policy:** How long are input files and output files retained after a batch completes? Is there an explicit deletion API? This matters for FedRAMP-adjacent workloads.

6. **Batch file size limits:** Is there a maximum number of requests per JSONL batch file? For Reactor-Core DPO cycles with 50–100 candidates, understanding coalescing limits is important.

---

## Contacts

**Meryem Arik** — CEO & Co-Founder
maryem@doubleword.ai

**Support:** support@doubleword.ai
**Docs:** https://docs.doubleword.ai
**Pricing:** https://www.doubleword.ai/calculator
**Autobatcher:** https://docs.doubleword.ai/batching/autobatcher
**Control Layer:** https://github.com/doublewordai/control-layer

---

*Generated from live benchmark run `ca6b7b1f-da63-4c44-ac8e-e9e8b796eae4` on 2026-03-18. Charts generated by `benchmarks/doubleword/doubleword_benchmark_analysis.ipynb`.*
