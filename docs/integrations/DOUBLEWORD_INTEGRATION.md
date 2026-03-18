# Doubleword × Trinity AI — Integration Guide

**Last updated:** 2026-03-18
**Status:** Benchmarked · Ready for implementation
**Benchmark results:** `benchmarks/doubleword/results/2026-03-18T00-56-02-UTC.json`

---

## Overview

Doubleword (formerly TitanML, $12M Series A — Dawn Capital) is a managed async batch LLM inference service. It provides an OpenAI-compatible API with access to models up to 397B parameters at 75–95% lower cost than real-time alternatives.

**Integration role in Trinity AI:** Doubleword serves as **Tier 0** in the routing architecture — handling ultra-complex tasks that exceed J-Prime's NVIDIA L4 VRAM ceiling, and as the compute engine for Reactor-Core's latency-insensitive DPO pipeline.

---

## Benchmark Results (2026-03-18)

> **Live benchmark run** — Batch ID `ca6b7b1f-da63-4c44-ac8e-e9e8b796eae4` · Wall time 257s · $0.000376 total

### Setup

| Component | Value |
|-----------|-------|
| Doubleword model | `Qwen/Qwen3.5-35B-A3B-FP8` |
| J-Prime baseline | `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` |
| J-Prime compute | NVIDIA L4, g2-standard-4, 24GB VRAM |
| Batch window | 1-hour SLA |
| Tasks | Secure Infrastructure Code + Defense Threat Analysis |

### Cost Comparison

![Cost Comparison](../../benchmarks/doubleword/chart_dw_cost.png)

| Task | J-Prime (VM time) | Doubleword (batch) | Savings |
|------|-------------------|--------------------|---------|
| Secure Infrastructure Code | $0.009210 | $0.000288 | 32x cheaper |
| Defense Threat Analysis | $0.001778 | $0.000088 | 20x cheaper |
| **Total (both tasks)** | **$0.010988** | **$0.000376** | **29x cheaper** |

### Token Volume

![Token Volume](../../benchmarks/doubleword/chart_dw_tokens.png)

| Task | J-Prime tokens | Doubleword tokens | Note |
|------|---------------|-------------------|------|
| Infrastructure | 680 (stop) | 700 (length) | Doubleword hit max_tokens cap |
| Threat analysis | 130 (stop) | 200 (length) | Increase to 500+ for full output |

### Timing

| Metric | Value |
|--------|-------|
| Batch wall time | 257 seconds (4.3 min) |
| J-Prime real-time total | ~33 seconds |
| Trade-off | 7.8x slower, 29x cheaper — optimal for latency-insensitive ops |

### Key Finding: Reasoning Model Token Budget

`Qwen/Qwen3.5-35B-A3B-FP8` is a chain-of-thought reasoning model. It uses tokens for internal reasoning before producing final output. At `max_tokens=700`, both tasks were cut off during the thinking phase before generating code/analysis.

**Recommended token budgets for Qwen3.5 reasoning models:**

| Task type | Recommended `max_tokens` |
|-----------|--------------------------|
| Code generation (complex) | 3000–5000 |
| Code generation (simple) | 1500–2000 |
| Threat analysis / classification | 500–1000 |
| Architecture review | 4000–8000 |
| DPO preference scoring | 1000–2000 |

---

## Model Catalog

![Model Catalog — Parameter Scale](../../benchmarks/doubleword/chart_dw_catalog.png)

Full catalog available via `GET https://api.doubleword.ai/v1/models` as of 2026-03-18:

| Model ID | Params | Active Params | Best for |
|----------|--------|---------------|----------|
| `Qwen/Qwen3.5-9B` | 9B | 9B | Fast, cheap classification |
| `Qwen/Qwen3-14B-FP8` | 14B | 14B | Same tier as J-Prime — quality comparison |
| `openai/gpt-oss-20b` | 20B | 20B | OpenAI-style outputs |
| `Qwen/Qwen3-VL-30B-A3B-Instruct-FP8` | 30B | 3B | Vision + language (MoE) |
| `Qwen/Qwen3.5-35B-A3B-FP8` | 35B | 3B | **Used in benchmark** — reasoning + code |
| `nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4` | 120B | 12B | Complex reasoning (NVIDIA) |
| `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | 235B | 22B | Vision + language ultra-scale |
| `Qwen/Qwen3.5-397B-A17B-FP8` | 397B | 17B | **Tier 0 primary** — largest available |

**Pricing (March 2026):** $0.10/1M input tokens · $0.40/1M output tokens (Qwen3.5-35B)
Full pricing: https://www.doubleword.ai/calculator

---

## Architecture: Where Doubleword Fits

![Routing Architecture](../../benchmarks/doubleword/chart_dw_routing.png)

```
┌─────────────────────────────────────────────────────────────────┐
│                    TRINITY AI COMPUTE TIERS                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  TIER 0 — Doubleword Batch API (NEW)               [ASYNC]     │
│  ├─ Model: Qwen3.5-397B-A17B-FP8 (397B)                       │
│  ├─ Cost:  $0.10/1M in · $0.40/1M out                         │
│  ├─ SLA:   1-hour or 24-hour completion window                 │
│  ├─ Trigger: complexity > 0.85 OR task in ULTRA_TASKS          │
│  └─ Use: architecture reviews, cross-repo analysis, DPO        │
│                          ↓ fallback                             │
│  TIER 1 — J-Prime · NVIDIA L4 (GCP g2-standard-4)  [REALTIME] │
│  ├─ Model: Qwen2.5-Coder-14B-Q4_K_M (~24 tok/s)               │
│  ├─ Cost:  ~$0.009/request (VM time, spot pricing)             │
│  ├─ VRAM: 24GB → ceiling ~32B models                           │
│  └─ Use: standard governance ops, streaming inference           │
│                          ↓ fallback                             │
│  TIER 2 — Claude API                               [REALTIME]  │
│  ├─ Model: claude-sonnet-4-6                                   │
│  ├─ Cost:  $3/1M in · $15/1M out                               │
│  └─ Use: emergency fallback, tool-use-heavy tasks               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

Reactor-Core DPO Pipeline:
  Telemetry JSONL → Doubleword Batch → 397B scoring → preference pairs → J-Prime fine-tune
```

---

## Monthly Cost Projections

![Monthly Cost Projection](../../benchmarks/doubleword/chart_dw_monthly.png)

| Daily ops | Doubleword batch | J-Prime (6hr/day spot) | J-Prime (always-on) |
|-----------|-----------------|------------------------|---------------------|
| 10 ops/day | $0.11/mo | $216/mo | $864/mo |
| 50 ops/day | $0.56/mo | $216/mo | $864/mo |
| 100 ops/day | $1.13/mo | $216/mo | $864/mo |
| 500 ops/day | $5.63/mo | $216/mo | $864/mo |
| 1,000 ops/day | $11.27/mo | $216/mo | $864/mo |

*Based on $0.0003756 per 2-task batch. Doubleword cost scales linearly with usage; J-Prime VM cost is flat regardless of ops.*

**Break-even:** At ~1,150 ops/day, J-Prime 6hr/day spot becomes cheaper than Doubleword batch. Below that threshold, Doubleword is the correct economic choice.

---

## Full Benchmark Dashboard

![Trinity AI × Doubleword — Full Benchmark Dashboard](../../benchmarks/doubleword/chart_dw_dashboard.png)

*Dashboard: cost comparison · token volume · model catalog · monthly projection · routing architecture · key metrics*

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
```

### 2. JARVIS — `DoublewordProvider` (CandidateProvider)

**File:** `backend/core/ouroboros/governance/providers.py:1586+`

Create `backend/core/ouroboros/governance/doubleword_provider.py` implementing the `CandidateProvider` protocol:

```python
class DoublewordProvider:
    """CandidateProvider implementation for Doubleword batch API."""

    @property
    def provider_name(self) -> str:
        return "doubleword"

    async def generate(self, context, deadline) -> GenerationResult:
        # 1. Build JSONL from context
        # 2. Upload file → get file_id
        # 3. Create batch job → get batch_id
        # 4. Poll with deadline guard (asyncio.wait_for)
        # 5. Retrieve results → parse → return GenerationResult
        ...

    async def health_probe(self) -> bool:
        # GET /v1/models — returns True if 200
        ...
```

### 3. J-Prime — `hybrid_tiered_router.py`

**File:** `jarvis_prime/core/hybrid_tiered_router.py:15-45`

Extend the existing 3-tier system with Tier 3 (Doubleword) for complexity > 0.85:

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
```

### 4. Reactor-Core — DPO Pipeline

**File:** `reactor_core/training/dpo_pair_generator.py`

Use Doubleword's batch API to score candidate solutions at 35B/397B quality:

```python
# New: DoublewordDPOClient
class DoublewordDPOClient:
    async def score_candidates(
        self,
        candidates: list[str],
        reference: str,
    ) -> list[float]:
        """
        Submit N candidates to Doubleword batch,
        score against reference, return preference scores.
        One batch job per DPO cycle. 24h SLA acceptable.
        """
```

### 5. Autobatcher (Drop-in for AsyncOpenAI)

Install Doubleword's open-source autobatcher for any async inference call in Reactor-Core:

```bash
pip install autobatcher
```

```python
# Before
from openai import AsyncOpenAI
client = AsyncOpenAI(api_key=OPENAI_KEY)

# After (transparent batching, no other changes)
from autobatcher import AsyncOpenAI
client = AsyncOpenAI(
    api_key=DOUBLEWORD_API_KEY,
    base_url="https://api.doubleword.ai/v1",
)
```

### 6. Control Layer (Future — Palantir AIP Integration)

Doubleword's open-source [Control Layer](https://github.com/doublewordai/control-layer) (Rust, Apache 2.0) is a gateway with:
- Per-user API key management
- Credit budgets
- Full request audit log

This maps directly to Palantir AIP `GovernedOperation` objects and should be evaluated as the gateway layer for multi-tenant Trinity deployments.

---

## Running the Benchmark

```bash
# Set API key
export DOUBLEWORD_API_KEY=sk-...

# Run with defaults (Qwen3.5-35B, 1h window)
python3 benchmarks/doubleword/benchmark_doubleword.py

# Run with 397B model (Tier 0 candidate)
DOUBLEWORD_MODEL=Qwen/Qwen3.5-397B-A17B-FP8 \
  python3 benchmarks/doubleword/benchmark_doubleword.py

# Increase token budget for reasoning models
DOUBLEWORD_MAX_TOKENS_INFRA=3000 \
  DOUBLEWORD_MAX_TOKENS_THREAT=1000 \
  python3 benchmarks/doubleword/benchmark_doubleword.py
```

Results are saved to `benchmarks/doubleword/results/<timestamp>-UTC.json` automatically.

To re-execute the notebook and regenerate all charts:

```bash
cd benchmarks/doubleword
/path/to/.venv/bin/jupyter nbconvert --to notebook --execute \
  doubleword_benchmark_analysis.ipynb --output doubleword_benchmark_analysis.ipynb
```

---

## Roadmap

| Phase | Integration | Timeline |
|-------|------------|----------|
| **Now** | Tier 0 routing in `RoutingPolicy` + `DoublewordProvider` | Week 1 |
| **Soon** | Reactor-Core DPO client using 397B scoring | Week 2–3 |
| **Medium** | Autobatcher across all async inference calls | Week 3–4 |
| **Future** | Control Layer as Palantir AIP gateway | Post-fellowship |
| **Future** | Inference Stack (K8s) for self-hosted deployment | Scale phase |

---

## Contacts

**Meryem Arik** — CEO & Co-Founder
maryem@doubleword.ai

**Support:** support@doubleword.ai
**Docs:** https://docs.doubleword.ai
**Pricing:** https://www.doubleword.ai/calculator
**Autobatcher:** https://docs.doubleword.ai/batching/autobatcher

---

*Generated from benchmark run `ca6b7b1f-da63-4c44-ac8e-e9e8b796eae4` on 2026-03-18.*
