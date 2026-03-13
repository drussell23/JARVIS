# J-Prime Infrastructure: Architecture, Gaps & Roadmap

**Generated:** 2026-03-12
**Author:** Derek J. Russell (Solo Architect)
**Status:** Living Document — updated as infrastructure evolves

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Current Architecture (The Trinity)](#2-current-architecture-the-trinity)
3. [J-Prime Server & Golden Image](#3-j-prime-server--golden-image)
4. [The Multi-Model Arsenal](#4-the-multi-model-arsenal)
5. [The 3-Layer BrainSelector](#5-the-3-layer-brainselector)
6. [Unified Model Serving (3-Tier Fallback)](#6-unified-model-serving-3-tier-fallback)
7. [GCP Infrastructure & VM Management](#7-gcp-infrastructure--vm-management)
8. [Multi-Modal Capabilities](#8-multi-modal-capabilities)
9. [L2 Iterative Self-Repair Loop](#9-l2-iterative-self-repair-loop)
10. [Current Gaps & Limitations](#10-current-gaps--limitations)
11. [The 200B+ Model Gap](#11-the-200b-model-gap)
12. [Nebius Token Factory — Tier 0 Integration Plan](#12-nebius-token-factory--tier-0-integration-plan)
13. [GCP Scaling Options & Cost Analysis](#13-gcp-scaling-options--cost-analysis)
14. [Spot VM Strategy for Solo R&D](#14-spot-vm-strategy-for-solo-rd)
15. [Implementation Roadmap](#15-implementation-roadmap)
16. [Key File Reference](#16-key-file-reference)

---

## 1. Executive Summary

J-Prime is the inference engine of the JARVIS autonomous agent ecosystem — a multi-model, multi-tier intelligence hub that routes code generation tasks across a deterministic brain selection policy. It currently serves 1B–32B quantized models on an NVIDIA L4 GPU (24GB VRAM) hosted on GCP, with fallback to local Mac inference and Claude API.

**The core infrastructure gap:** The architecture supports compute class tiers up to `gpu_a100` and references 70B–236B models in the golden image, but the current L4's 24GB VRAM physically prevents serving them. This document maps the complete infrastructure, identifies all gaps, and proposes a roadmap to unlock the 200B+ tier via Nebius Token Factory (managed API) and/or GCP GPU scaling.

---

## 2. Current Architecture (The Trinity)

```
┌─────────────────────────────────────────────────────────────────────┐
│                    JARVIS Autonomous Agent Ecosystem                │
├─────────────────┬──────────────────────┬────────────────────────────┤
│  JARVIS (Body)  │   J-Prime (Mind)     │  Reactor-Core (Nerves)     │
│  macOS Monolith │   GCP Inference      │  Training Service          │
│                 │                      │                            │
│  • 73K+ line    │  • llama-cpp-python  │  • FastAPI training API    │
│    kernel       │  • NVIDIA L4 GPU     │  • Experience replay       │
│  • Ouroboros    │  • Multi-model       │  • Dataset pipelines       │
│    governance   │    arsenal           │  • SafeScout scraper       │
│  • Voice bio-   │  • BrainSelector     │  • Port 8090               │
│    metrics      │  • Port 8000         │                            │
│  • TUI dash-   │  • Static IP         │                            │
│    board        │                      │                            │
│  • Trinity      │                      │                            │
│    Knowledge    │                      │                            │
│    Indexer      │                      │                            │
│  • Embedding    │                      │                            │
│    pipeline     │                      │                            │
│    (CPU, sent-  │                      │                            │
│    transformers)│                      │                            │
├─────────────────┴──────────────────────┴────────────────────────────┤
│  16GB M-series     g2-standard-4 +        Separate repo              │
│  Mac (local)       NVIDIA L4 (GCP)        (FastAPI service)          │
└─────────────────────────────────────────────────────────────────────┘
```

**Ownership clarity:**
- **JARVIS Main** owns: ingestion, semantic chunking, embedding generation, governance FSM, voice biometrics, TUI
- **J-Prime** owns: LLM inference, model loading, GPU management, brain inventory endpoint
- **Reactor-Core** owns: training data reception, fine-tuning job orchestration (stub implementation)

---

## 3. J-Prime Server & Golden Image

### Server Configuration

```
Instance:       jarvis-prime-stable (static, not ephemeral)
Machine Type:   g2-standard-4 (4 vCPUs, 16GB RAM)
GPU:            NVIDIA L4 (24GB VRAM)
Zone:           us-central1-b
Static IP:      136.113.252.164 (reserved)
OS:             Ubuntu + CUDA toolkit
Runtime:        llama-cpp-python with full GPU offloading (--gpu-layers -1)
Context:        8192 tokens (--ctx-size 8192)
Port:           8000
```

### Golden Image Contents

The golden image is a pre-built GCP disk image containing:
- Ubuntu base + CUDA toolkit
- J-Prime server (`run_server.py`) + llama-cpp-python
- All GGUF model files pre-downloaded on disk
- Systemd service for auto-restart
- Health check script (APARS metadata)
- Startup script version: v238.0

### Server Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/completions` | POST | OpenAI-compatible text completion |
| `/v1/chat/completions` | POST | Chat-style inference (primary) |
| `/health` | GET | Health check |
| `/v1/brains` | GET | List available brain models (boot handshake) |
| `/v1/vision/completions` | POST | Vision (LLaVA) inference (port 8001) |

### Boot Handshake

At JARVIS supervisor startup (Zone 6.8), the system validates J-Prime's brain inventory:

```
GET http://136.113.252.164:8000/v1/brains
→ Returns: contract_version, list of brain_id + model_artifact + compute_class + status
→ Supervisor validates against brain_selection_policy.yaml
→ Hard fail if required brain missing
```

---

## 4. The Multi-Model Arsenal

### Currently Active (in brain_selection_policy.yaml)

| Brain ID | Model | Size | GGUF File | Compute Class | Task Tiers | Schema |
|----------|-------|------|-----------|---------------|------------|--------|
| `phi3_lightweight` | Llama-3.2-1B-Instruct | 1B | `Llama-3.2-1B-Instruct-Q4_K_M.gguf` | CPU | tier0, tier1 | full_content_only |
| `qwen_coder` | Qwen-2.5-Coder-7B | 7B | `qwen2.5-coder-7b-instruct-q4_k_m.gguf` | GPU (T4 min) | tier1, tier2 | full_content_only |
| `qwen_coder_14b` | Qwen-2.5-Coder-14B | 14B | `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` | GPU (L4 min) | tier2, tier3 | full_content_only |
| `qwen_coder_32b` | Qwen-2.5-Coder-32B | 32B | `Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf` | GPU (L4 min) | tier2, tier3 | full_content_and_diff |
| `deepseek_r1` | DeepSeek-R1-Distill-Qwen-7B | 7B | `DeepSeek-R1-Distill-Qwen-7B-Q4_K_M.gguf` | GPU (T4 min) | tier3 | full_content_only |
| `mistral_7b_fallback` | Mistral-7B-Instruct-v0.2 | 7B | `mistral-7b-instruct-v0.2.Q4_K_M.gguf` | GPU (T4 min) | tier1, tier2 | — (optional) |

### On Golden Image Disk (NOT Active in Routing)

| Model | Size | GGUF Size (Q4_K_M) | Required VRAM | Status |
|-------|------|---------------------|---------------|--------|
| Llama-3.3-70B | 70B | ~40GB | 40GB+ | On disk, cannot load on L4 (24GB) |
| DeepSeek-Coder-V2 | 236B | ~130GB+ | 130GB+ | On disk, cannot load on L4. **Legacy model — superseded by DeepSeek-V3/R1** |

### Task-to-Brain Routing Map

```
tier0 (trivial):     phi3_lightweight
tier1 (light):       phi3_lightweight → qwen_coder → mistral_7b_fallback
tier2 (heavy):       qwen_coder_14b → qwen_coder → mistral_7b_fallback
tier3 (complex):     qwen_coder_32b → qwen_coder_14b → deepseek_r1
```

### Fallback Chains

```
phi3_lightweight  → qwen_coder → mistral_7b_fallback
qwen_coder        → qwen_coder_14b → mistral_7b_fallback
qwen_coder_14b    → qwen_coder → mistral_7b_fallback
qwen_coder_32b    → qwen_coder_14b → qwen_coder → mistral_7b_fallback
deepseek_r1       → qwen_coder_32b → qwen_coder → mistral_7b_fallback
```

### Performance Benchmarks (Current L4)

| Model | Throughput | Load Time | VRAM Usage |
|-------|-----------|-----------|------------|
| 7B Q4_K_M | 43–47 tok/s | ~30s | ~4GB |
| 14B Q4_K_M | ~20–25 tok/s (est.) | ~1 min | ~8GB |
| 32B Q4_K_M | ~8–15 tok/s (est.) | ~3 min | ~18GB |

---

## 5. The 3-Layer BrainSelector

**File:** `backend/core/ouroboros/governance/brain_selector.py` (~500 lines)

### Layer 1: Task Gate (Complexity Classification)

Deterministic regex pattern matching — zero LLM calls to route.

```
Description + target files → complexity tier

TRIVIAL:  Single file, "append line", "add comment", "add marker"
LIGHT:    Single-file fix, config change, bug fix
HEAVY:    "refactor", "implement", "redesign", 3+ files
COMPLEX:  "architecture", "cross-repo", "migrate", 5+ files
```

Patterns defined in `brain_selection_policy.yaml` → hot-reloadable.

### Layer 2: Resource Gate (REMOVED — Phase 1 P0)

Removed because local memory pressure must NOT influence remote GCP routing decisions. The host-binding invariant was violated when local RAM pressure downgraded tasks that should run on the remote GPU.

### Layer 3: Cost Gate

```
Daily budget: $0.50/day (OUROBOROS_GCP_DAILY_BUDGET)
Persistence: ~/.jarvis/ouroboros/cost_state.json
Reset: Daily at UTC 00:00

If budget exceeded:
  - HEAVY/COMPLEX tasks → QUEUED (deferred)
  - LIGHT tasks → downgraded to phi3_lightweight
```

### Output: BrainSelectionResult

```python
BrainSelectionResult(
    brain_id="qwen_coder_32b",
    model_name="qwen-2.5-coder-32b",
    fallback_model="qwen-2.5-coder-14b",
    routing_reason="task_gate_heavy_code",
    task_complexity="heavy_code",
    provider_tier="gcp_prime",
    schema_capability="full_content_and_diff",
)
```

---

## 6. Unified Model Serving (3-Tier Fallback)

**File:** `backend/intelligence/unified_model_serving.py` (~2700 lines)

```
Request
  ↓
[Tier 1: PRIME_API — J-Prime on GCP L4]
  ├─ Endpoint:    POST http://136.113.252.164:8000/v1/chat/completions
  ├─ Models:      1B–32B (brain_selection_policy.yaml)
  ├─ Cost:        ~$0.70/hr amortized (self-hosted)
  ├─ Throughput:  43–47 tok/s (7B), ~8–15 tok/s (32B)
  ├─ Timeout:     60s generation, 150s pipeline
  ├─ Circuit:     5 failures → OPEN, 30s recovery → HALF_OPEN
  └─ Status:      PRIMARY
  ↓ [FAILS or TIMEOUT]
[Tier 2: PRIME_LOCAL — Mac llama-cpp-python]
  ├─ Models:      Phi3 1B only (memory-aware selection)
  ├─ Cost:        Free
  ├─ Throughput:  10–20 tok/s (CPU)
  └─ Status:      LOW CAPABILITY FALLBACK
  ↓ [FAILS]
[Tier 3: CLAUDE — Anthropic API]
  ├─ Model:       claude-sonnet-4-20250514
  ├─ Cost:        $3/$15 per 1M tokens (input/output)
  ├─ Throughput:  100–200 tok/s
  ├─ Timeout:     120s
  └─ Status:      EXPENSIVE EMERGENCY FALLBACK
```

### Task-Based Routing Preferences

```
CODE:        PRIME_API → PRIME_LOCAL → CLAUDE
REASONING:   PRIME_API → PRIME_LOCAL → CLAUDE
VISION:      PRIME_API → CLAUDE (LLaVA → Claude Vision)
TOOL_USE:    CLAUDE → PRIME_API → PRIME_LOCAL (Claude preferred)
EMBEDDING:   PRIME_API → PRIME_LOCAL (never Claude)
```

### Provider Cost Tracking

| Provider | Cost Efficiency | Per-Request Cost |
|----------|----------------|-----------------|
| PRIME_LOCAL | 1.0 (free) | $0.00 |
| PRIME_API | 0.9 (amortized) | ~$0.02/hr |
| CLAUDE | 0.3 (expensive) | ~$0.01–0.05/request |

---

## 7. GCP Infrastructure & VM Management

**File:** `backend/core/gcp_vm_manager.py` (~7400 lines)

### VM Lifecycle (Invincible Node Pattern)

```
PROVISIONING (3–5 min)
  ↓ [Startup script runs]
STAGING
  ↓ [J-Prime service starts]
RUNNING → READY (health checks pass, APARS validated)
  ↓ [If preempted]
STOPPED (disk preserved, not deleted)
  ↓ [Auto-restart on demand]
RUNNING → READY
```

### Health Polling (Supervisor Zone 5.6)

```
Health Check Interval: 10s
Timeout: 15s

Verdicts:
  READY            → Online, tests passing
  ALIVE_NOT_READY  → Process running, not ready yet
  UNREACHABLE      → No response
  UNHEALTHY        → Error or test failure
```

### Pressure-Driven Provisioning (Memory Defense)

```
60% RAM  → Fast polling (1s interval)
70% RAM  → Begin VM provisioning (3 consecutive readings)
85% RAM  → Emergency model unload (12s grace)
90% RAM  → Background GCP VM provisioning
95% RAM  → SIGSTOP local LLM processes (120s cooldown)

Recovery: RAM < 75% for 30–60s stable → reload model
Hysteresis: 15% gap to prevent flapping
```

### Prime Client Configuration

```bash
# Endpoint resolution (priority order):
JARVIS_PRIME_URL=http://136.113.252.164:8000      # 1st: Full URL
JARVIS_INVINCIBLE_NODE_IP=136.113.252.164          # 2nd: IP only
JARVIS_PRIME_HOST=localhost                         # 3rd: Host override
JARVIS_PRIME_PORT=8000                              # 4th: Port
JARVIS_PRIME_API_VERSION=v1                         # API prefix

# Timeouts:
PRIME_CONNECT_TIMEOUT=5.0
PRIME_READ_TIMEOUT=120.0
PRIME_TOTAL_TIMEOUT=180.0
JARVIS_GENERATION_TIMEOUT_S=60
JARVIS_PIPELINE_TIMEOUT_S=150
```

---

## 8. Multi-Modal Capabilities

### Text Generation (Primary)
- All brains in arsenal serve text completion / chat completion
- Schema versions: 2b.1 (full content), 2b.1-diff (unified diff), 2c.1 (multi-repo patches), 2b.1-noop (no-change detection)

### Vision (LLaVA — Port 8001)
- Separate inference endpoint from text models
- Used for screenshot analysis
- Fallback to Claude Vision API if LLaVA unavailable
- Integrated since v236.0

### Embeddings (Planned Phase 3)
- E5-large (encoder-only) for TheOracle semantic context expansion
- Currently: sentence-transformers (all-MiniLM-L6-v2) runs on CPU in JARVIS Main
- Future: dedicated embedding endpoint on J-Prime

### Voice Embeddings (Separate Service)
- ECAPA-TDNN speaker embeddings (192-dimensional)
- Cloud-only: `JARVIS_CLOUD_ML_ENDPOINT`
- Not integrated with J-Prime — standalone voice biometric service

---

## 9. L2 Iterative Self-Repair Loop

**File:** `backend/core/ouroboros/governance/repair_engine.py` (~1000 lines)

When a generated code patch fails validation, the L2 repair loop kicks in:

```
GENERATE → VALIDATE (fails)
  ↓
L2_CLASSIFY_FAILURE
  ├─ SYNTAX  → rebuild prompt with error context, retry (max 2)
  ├─ TEST    → rebuild prompt with test output, retry (max 3)
  ├─ FLAKE   → re-run test to confirm flakiness (max 2)
  └─ ENV     → non-retryable, stop
  ↓
L2_GENERATE_PATCH (with repair_context injected)
  ↓
L2_VALIDATE → L2_EVALUATE_PROGRESS
  ├─ PROGRESS  → continue repairing
  ├─ CONVERGED → success, exit L2
  └─ NO_PROGRESS (2 consecutive) → stop
```

**Budget constraints:**
```
Max iterations:        5  (JARVIS_L2_MAX_ITERS)
Timebox:              120s (JARVIS_L2_TIMEBOX_S)
Max diff lines:       150  (JARVIS_L2_MAX_DIFF_LINES)
Max files changed:      3  (JARVIS_L2_MAX_FILES_CHANGED)
No-progress kill:       2  consecutive stalls
```

---

## 10. Current Gaps & Limitations

### Critical Gaps

| Gap | Impact | Severity |
|-----|--------|----------|
| **24GB VRAM ceiling** | Cannot serve 70B+ models | **BLOCKING** |
| **DeepSeek-Coder-V2 is legacy** | 236B GGUF on disk is superseded by V3/R1 | **Stale asset** |
| **No Tier 0 provider** | Complex tasks fallback to Claude at $3/$15 per MTok | **Cost inefficient** |
| **Single-model loading** | J-Prime loads one model at a time, ~3 min swap for 32B | **Latency on tier switches** |
| **No dedicated embedding endpoint** | Embeddings run on JARVIS Mac CPU, not J-Prime GPU | **Underutilized GPU** |

### Architecture Gaps

| Gap | Location | Fix Complexity |
|-----|----------|---------------|
| **Cost estimation hardcoded** | `unified_model_serving.py:~400` — fixed multipliers for Claude rates | Low — move to config |
| **Failure regex hardcoded** | `failure_classifier.py` — SyntaxError patterns | Low — move to YAML |
| **Schema versions hardcoded** | `providers.py:~77` — string constants | Medium — capability negotiation |
| **No Nebius/external provider class** | `unified_model_serving.py` — only PRIME_API, PRIME_LOCAL, CLAUDE | Medium — new provider class |
| **Streaming format coupled to llama.cpp** | `prime_client.py` — `/generate/stream` with `data["content"]` format | Medium — abstract streaming parser |

### Operational Gaps

| Gap | Impact |
|-----|--------|
| **No model preloading strategy** | Cold start on tier switch costs 30s–3min |
| **Single GPU, single instance** | No horizontal scaling for concurrent requests |
| **No request queuing** | Second request during inference must wait or fail |
| **Health check is HTTP-only** | No GPU memory/temperature monitoring |
| **Reactor-Core fine-tuning is stubbed** | `TrainingJobManager` uses `asyncio.sleep()` placeholder |

---

## 11. The 200B+ Model Gap

### What's on the Golden Image vs What Can Actually Run

| Model | GGUF Size | Required VRAM | L4 (24GB) | A100-40GB | A100-80GB | 2×A100-80GB |
|-------|-----------|---------------|-----------|-----------|-----------|-------------|
| Llama-3.2-1B | ~0.7GB | ~1GB | YES | YES | YES | YES |
| Qwen-2.5-7B | ~4.5GB | ~5GB | YES | YES | YES | YES |
| Qwen-2.5-14B | ~8.5GB | ~10GB | YES | YES | YES | YES |
| Qwen-2.5-32B | ~18GB | ~20GB | YES (tight) | YES | YES | YES |
| Llama-3.3-70B | ~40GB | ~42GB | **NO** | **NO** | YES | YES |
| DeepSeek-Coder-V2 236B | ~130GB | ~135GB | **NO** | **NO** | **NO** | YES (tight) |
| DeepSeek-R1 (671B MoE) | API only | N/A | **NO** | **NO** | **NO** | API only |

### The Realistic 200B+ Strategy

**Option A: Managed API (Nebius Token Factory)** — Recommended for solo R&D
- No hardware provisioning needed
- Pay-per-token, scale to zero when idle
- See Section 12 for full integration plan

**Option B: Self-Hosted GCP GPU Upgrade** — For dedicated throughput
- Requires A100-80GB ($2–3/hr) minimum for 70B
- Requires 2×A100-80GB ($5–6/hr) for 236B
- See Section 13 for cost analysis

**Option C: Hybrid** — Best of both worlds
- Keep L4 for 7B–32B (always-on, ~$0.70/hr)
- Burst to Nebius API for 70B+ (pay-per-token, only when escalated)
- Replace DeepSeek-Coder-V2 (legacy) with DeepSeek-R1 via API

---

## 12. Nebius Token Factory — Tier 0 Integration Plan

### Why Nebius

| Factor | Nebius | Together AI | Fireworks | Claude API |
|--------|--------|-------------|-----------|------------|
| Llama-3.3-70B | $0.13/$0.40 | $0.88/$0.88 | $0.90/$0.90 | N/A |
| DeepSeek-R1 | $0.80/$2.40 | $3.00/$7.00 | N/A | N/A |
| 405B models | $1.00/$3.00 | N/A | N/A | N/A |
| OpenAI-compatible | YES | YES | YES | NO |
| Streaming (SSE) | YES | YES | YES | YES (different) |
| Tool calling | YES | YES | YES | YES |
| Batch (50% off) | YES | N/A | N/A | YES |
| SLA | 99.9% uptime | — | — | — |
| TTFT | Sub-second | — | — | — |
| DeepSeek-R1 tok/s | 248 tok/s | — | — | — |

### API Compatibility with J-Prime Client

**Current PrimeClient flow:**
```
PrimeClient.generate()
  → POST http://{host}:{port}/v1/chat/completions
  → Body: { model, messages, max_tokens, temperature, stop }
  → Response: { choices[0].message.content, usage }
```

**Nebius endpoint:**
```
POST https://api.tokenfactory.nebius.com/v1/chat/completions
  → Body: identical OpenAI format
  → Headers: Authorization: Bearer $NEBIUS_API_KEY
  → Response: identical OpenAI format
```

**Compatibility assessment:**

| Feature | Current J-Prime | Nebius | Compatible? |
|---------|----------------|--------|-------------|
| Endpoint format | `/v1/chat/completions` | `/v1/chat/completions` | YES — identical |
| Request body | OpenAI format | OpenAI format | YES — identical |
| Auth | None (local) | Bearer token | CHANGE — add header |
| Streaming | `/generate/stream` + `data["content"]` | `/v1/chat/completions` + `stream:true` + `choices[0].delta.content` | CHANGE — different stream format |
| Model ID | `"jarvis-prime"` default | `"meta-llama/Meta-Llama-3.3-70B-Instruct"` | CHANGE — Nebius model IDs |
| Protocol | HTTP (localhost) | HTTPS | CHANGE — TLS |
| Tool calling | Not currently used by governance | Supported | COMPATIBLE |
| `reasoning_content` | Not parsed | DeepSeek-R1 returns this field | ADD — parse reasoning chain |

### Code Changes Required

**1. New provider class in `unified_model_serving.py`:**
```python
class NebusAPIClient(ModelClient):
    """Tier 0: Nebius Token Factory for 70B+ models"""
    # OpenAI-compatible, aiohttp-based
    # Bearer token auth
    # Standard SSE streaming parser
```

**2. Update `brain_selection_policy.yaml`:**
```yaml
brains:
  required:
    # ... existing brains ...

    - brain_id: "nebius_llama_70b"
      provider: "nebius"
      model_name: "meta-llama/Meta-Llama-3.3-70B-Instruct"
      compute_class: "gpu_a100"  # served remotely
      required_capabilities: ["code_generation", "architecture_analysis", "complex_reasoning"]
      schema_capability: "full_content_and_diff"
      allowed_task_classes: ["tier3"]
      max_prompt_tokens: 128000
      max_output_tokens: 4096

    - brain_id: "nebius_deepseek_r1"
      provider: "nebius"
      model_name: "deepseek-ai/DeepSeek-R1-0528"
      compute_class: "gpu_a100"
      required_capabilities: ["complex_reasoning", "architecture_analysis"]
      schema_capability: "full_content_only"
      allowed_task_classes: ["tier3"]
      max_prompt_tokens: 164000
      max_output_tokens: 4096
```

**3. Update routing in `task_class_map`:**
```yaml
routing:
  task_class_map:
    tier0: ["phi3_lightweight"]
    tier1: ["phi3_lightweight", "qwen_coder", "mistral_7b_fallback"]
    tier2: ["qwen_coder_14b", "qwen_coder", "mistral_7b_fallback"]
    tier3: ["nebius_llama_70b", "nebius_deepseek_r1", "qwen_coder_32b", "qwen_coder_14b"]
```

**4. Update fallback chain:**
```yaml
  fallback_chain:
    nebius_llama_70b: ["nebius_deepseek_r1", "qwen_coder_32b", "qwen_coder_14b"]
    nebius_deepseek_r1: ["nebius_llama_70b", "qwen_coder_32b"]
    qwen_coder_32b: ["nebius_llama_70b", "qwen_coder_14b", "qwen_coder"]
```

**5. Environment variables:**
```bash
NEBIUS_ENABLED=true
NEBIUS_API_KEY=sk-...
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_TIMEOUT_S=60
NEBIUS_MAX_RETRIES=2
```

**6. Cost tracking update in `brain_selector.py`:**
```python
# Add Nebius spend tracking alongside GCP and Claude
daily_spend_nebius: float = 0.0
```

### Nebius Model Inventory (Available for Tier 0)

| Model ID | Params | Context | Input/MTok | Output/MTok | Notes |
|----------|--------|---------|------------|-------------|-------|
| `meta-llama/Meta-Llama-3.3-70B-Instruct` | 70B | 128K | $0.13 (base) | $0.40 (base) | Primary Tier 0 |
| `deepseek-ai/DeepSeek-R1-0528` | 671B MoE | 164K | $0.80 (base) | $2.40 (base) | Deep reasoning |
| `deepseek-ai/DeepSeek-V3-0324` | MoE | — | $0.50 (base) | $1.50 (base) | Code generation |
| `meta-llama/Meta-Llama-3.1-405B-Instruct` | 405B | 128K | $1.00 | $3.00 | Max capability |
| `NousResearch/Hermes-4-405B` | 405B | 128K | $1.00 | $3.00 | Fine-tuned 405B |
| `Qwen/Qwen3-235B-A22B-Thinking-2507` | 235B MoE | 262K | TBD | TBD | Qwen reasoning |
| `Qwen/Qwen3-Coder-480B-A35B-Instruct` | 480B MoE | 262K | TBD | TBD | Qwen code |

**Batch discount:** All models at 50% of base pricing for batch workloads.

---

## 13. GCP Scaling Options & Cost Analysis

### Current Setup Cost (Verified March 2026)

| Metric | On-Demand | Spot | 1-Year CUD | 3-Year CUD |
|--------|-----------|------|------------|------------|
| g2-standard-4 + L4 (24GB) | **$0.71/hr** | **$0.28/hr** | $0.45/hr (37% off) | $0.32/hr (55% off) |
| Monthly (24/7) | $516 | $205 | $325 | $232 |

### GPU Upgrade Options (All us-central1, Verified Pricing)

| Machine Type | GPU | VRAM | vCPU/RAM | On-Demand/hr | Spot/hr | Monthly Spot 24/7 | Max Model (Q4_K_M) |
|-------------|-----|------|----------|-------------|---------|-------------------|---------------------|
| g2-standard-4 | 1× L4 | 24GB | 4/16GB | $0.71 | $0.28 | $205 | 32B |
| a2-highgpu-1g | 1× A100-40GB | 40GB | 12/85GB | $3.67 | $1.80 | $1,314 | 70B (tight) |
| a2-ultragpu-1g | 1× A100-80GB | 80GB | 12/170GB | $5.07 | $2.53 | $1,845 | 70B (comfortable) |
| a2-ultragpu-2g | 2× A100-80GB | 160GB | 24/340GB | $10.14 | $5.05 | $3,687 | 236B (tight) |
| a2-ultragpu-4g | 4× A100-80GB | 320GB | 48/680GB | $20.28 | $10.10 | $7,373 | 405B Q4 |
| a3-highgpu-1g | 1× H100-80GB | 80GB | 26/234GB | $11.06 | $3.38 | $2,467 | 70B (2-3× faster) |
| a3-highgpu-2g | 2× H100-80GB | 160GB | 52/468GB | $22.12 | $6.75 | $4,928 | 236B |
| a3-highgpu-4g | 4× H100-80GB | 320GB | 104/936GB | $44.25 | $13.51 | $9,862 | 405B Q4 |

### Zone Availability Warning

Your current L4 is in **us-central1-b**. A100s are NOT available in us-central1-b:

| Zone | L4 | A100-40GB | A100-80GB | H100 |
|------|----|-----------|-----------| -----|
| us-central1-a | Yes | **Yes** | **Yes** | No |
| us-central1-b | **Yes (current)** | No | No | **Yes** (A3) |
| us-central1-c | Yes | **Yes** | **Yes** | No |
| us-central1-f | Yes | Limited | No | No |

An A100 upgrade would require moving to us-central1-a or us-central1-c. H100 can stay in us-central1-b.

### Hybrid Architecture: Always-On L4 + Burst A100 Spot

| Scenario | L4 Cost | A100 Burst (Spot) | Total/Month |
|----------|---------|-------------------|-------------|
| L4 on-demand + A100-40GB Spot 2h/day | $516 | $108 (2h × $1.80 × 30d) | **$624** |
| L4 on-demand + A100-80GB Spot 2h/day | $516 | $152 (2h × $2.53 × 30d) | **$668** |
| L4 1yr-CUD + A100-40GB Spot 2h/day | $325 | $108 | **$433** |
| L4 3yr-CUD + A100-80GB Spot 2h/day | $232 | $152 | **$384** |
| L4 3yr-CUD + A100-80GB Spot 4h/day | $232 | $304 | **$536** |
| L4 3yr-CUD + H100 Spot 2h/day | $232 | $203 (2h × $3.38 × 30d) | **$435** |

### Cost Comparison: Self-Hosted vs Nebius API

**Scenario: 100 complex tasks/day, avg 3K input + 2K output tokens each**

| Approach | Monthly Cost | Notes |
|----------|-------------|-------|
| **Current (Claude fallback)** | ~$45–90 Claude + $516 L4 | **~$561–606/mo** total |
| **L4 3yr-CUD + Nebius Llama-70B** | ~$5 Nebius + $232 L4 | **~$237/mo** total |
| **L4 3yr-CUD + Nebius DeepSeek-R1** | ~$29 Nebius + $232 L4 | **~$261/mo** total |
| **L4 3yr-CUD + A100-40GB Spot 2h/day** | $108 Spot + $232 L4 | **~$340/mo** (+ model load overhead) |
| **L4 3yr-CUD + A100-80GB Spot 4h/day** | $304 Spot + $232 L4 | **~$536/mo** (+ scheduling complexity) |
| **A100-80GB always-on Spot** | $1,845/mo | Over-provisioned, preemption risk |

**Verdict:** For solo R&D with ~100 complex escalations/day, **Nebius API at $237–261/mo** is 1.5–2.5× cheaper than self-hosting burst A100s ($340–536/mo) and requires zero infrastructure management, zero model load delays, and zero preemption risk.

### CUD Recommendations

| GPU | CUD Worth It? | Notes |
|-----|--------------|-------|
| L4 (g2-standard-4) | **YES** — 3yr CUD saves 55% | $232/mo vs $516 on-demand |
| A100-40GB | No — only 7–13% CUD discount | Spot at 51% off is better |
| A100-80GB | No — CUD not publicly listed | Contact sales; Spot preferred |
| H100 | Maybe — 3yr CUD = 56% off | $4.86/hr vs $11.06 on-demand |

---

## 14. Spot VM Strategy for Solo R&D

### Spot Preemption Reality (Verified March 2026)

| GPU Tier | Typical Interruption Rate | GCP Preemption Notice |
|----------|--------------------------|----------------------|
| L4 (budget tier) | 1–10% per day | **30 seconds** |
| A100 | 5–10% per day | **30 seconds** |
| H100 (high demand) | 10–20% per day | **30 seconds** |

Key facts:
- GCP gives only **30 seconds** preemption notice (AWS gives ~2 min)
- Preemption rates swing from 3% to >60% depending on time/region/demand
- Less frequent during nights and weekends
- A100 Spot availability has improved in 2025–2026 as newer GPUs came online
- **No SLA** on Spot VM uptime — GCP can reclaim at any time
- No sustained use discounts (SUDs) apply to GPU-accelerated machines

**Your current L4 is NOT Spot** — it's a static reserved instance (`jarvis-prime-stable`). This is correct for a primary workhorse.

### Recommended Strategy

```
ALWAYS-ON (Static, 3yr CUD recommended):
  └─ g2-standard-4 + L4 — $0.32/hr (3yr CUD) = $232/mo
     Serves: 7B–32B models, 90%+ of all tasks
     Zone: us-central1-b (current)

TIER 0 API (Nebius Token Factory):
  └─ Pay-per-token for 70B+ escalation
     Sub-second TTFT, 248 tok/s on DeepSeek-R1
     No provisioning, no preemption, no model load delay
     = correct abstraction for rare, complex tasks

BURST SPOT (Only for batch/training):
  └─ A100-80GB Spot ($2.53/hr) for Reactor-Core fine-tuning
     Training checkpoints survive preemption
     Spin up on-demand, tear down when done
```

### When Spot DOES make sense for solo R&D
- **Reactor-Core training jobs** — batch fine-tuning on Spot A100; checkpoints survive preemption
- **Offline batch analysis** — Doubleword Batch or Nebius Batch (50% off) for non-real-time work
- **Model evaluation runs** — benchmarking new models doesn't need uptime guarantees
- **One-off large inference** — spin up A100 for a specific analysis session, tear down after

### When Spot does NOT make sense
- **Real-time inference serving** — preemption during generation = lost work + pipeline timeout
- **J-Prime workhorse** — always-on L4 with CUD is the right call
- **70B+ model serving** — model load time (~3–5 min for 70B on A100) + preemption risk = unacceptable latency for governance pipeline with 60s generation timeout

### Why Nebius API Beats Self-Hosted Spot for Real-Time 70B+

| Factor | A100 Spot (Self-Hosted) | Nebius API |
|--------|------------------------|------------|
| First request latency | 3–5 min (cold model load) | Sub-second (always warm) |
| Preemption risk | 5–10% per day | None (99.9% SLA) |
| Idle cost | $2.53/hr even when not generating | $0 (pay-per-token) |
| Zone constraint | Must move from us-central1-b | No zone dependency |
| Infrastructure management | You manage VM lifecycle | Zero ops |
| 70B cost per request (3K in + 2K out) | ~$0.005 amortized + idle | ~$0.0012 (Nebius base) |

---

## 15. Implementation Roadmap

### Phase 1: Nebius Tier 0 Integration (Week 1-2)

```
[ ] Create NebusAPIClient provider class in unified_model_serving.py
    - OpenAI-compatible POST to /v1/chat/completions
    - Bearer token auth via NEBIUS_API_KEY
    - Standard SSE streaming parser (not llama.cpp format)
    - Parse reasoning_content field for DeepSeek-R1 responses
    - Circuit breaker + retry logic

[ ] Add Nebius brains to brain_selection_policy.yaml
    - nebius_llama_70b (Llama-3.3-70B)
    - nebius_deepseek_r1 (DeepSeek-R1-0528)
    - Update task_class_map tier3 to prioritize Nebius brains
    - Update fallback_chain

[ ] Update cost tracking
    - Add daily_spend_nebius counter
    - Track Nebius per-token costs from response usage field
    - Add Nebius budget cap (separate from GCP/Claude)

[ ] Wire into PrimeRouter
    - New RoutingDecision.NEBIUS_API variant
    - Endpoint-aware circuit breaker for Nebius

[ ] Environment configuration
    - NEBIUS_ENABLED, NEBIUS_API_KEY, NEBIUS_BASE_URL
    - NEBIUS_TIMEOUT_S (default 60)
```

### Phase 2: Golden Image Cleanup (Week 2-3)

```
[ ] Remove DeepSeek-Coder-V2 236B GGUF from golden image
    - Legacy model, superseded by DeepSeek-V3/R1
    - Recovers ~130GB disk space

[ ] Evaluate Llama-3.3-70B GGUF on golden image
    - If serving via Nebius API, no need to store locally
    - Keep if planning future A100 upgrade

[ ] Add newer model GGUFs if needed
    - Qwen3-Coder (if released as GGUF)
    - DeepSeek-R1-Distill-14B (fits on L4, better reasoning than 7B)

[ ] Update startup script version to reflect changes
```

### Phase 3: Streaming & Provider Abstraction (Week 3-4)

```
[ ] Abstract streaming parser
    - Current: coupled to llama.cpp /generate/stream format
    - Target: pluggable parser for OpenAI SSE vs llama.cpp SSE
    - Benefit: Nebius, Together, Fireworks all use OpenAI SSE

[ ] Move hardcoded cost rates to config
    - Currently in unified_model_serving.py as fixed multipliers
    - Move to brain_selection_policy.yaml or provider config

[ ] Move failure patterns to YAML
    - Currently hardcoded regex in failure_classifier.py
    - Hot-reloadable patterns like brain policy
```

### Phase 4: Advanced Capabilities (Week 5-8)

```
[ ] Nebius Batch integration for Reactor-Core
    - Synthetic training data generation at 50% batch pricing
    - Bulk model evaluation pipelines

[ ] Dedicated embedding endpoint on J-Prime
    - E5-large for TheOracle semantic search
    - Offload from Mac CPU to L4 GPU

[ ] Multi-model preloading strategy
    - Keep 7B always loaded + lazy-load 14B/32B
    - Reduce tier-switch latency

[ ] Request queuing for concurrent operations
    - Currently: second request blocks during inference
    - Target: async queue with priority by task tier

[ ] Nebius reasoning_content integration
    - Parse DeepSeek-R1 chain-of-thought
    - Feed reasoning trace into L2 repair context
    - Display reasoning in TUI dashboard
```

### Phase 5: Future GPU Scaling (When Justified)

```
[ ] Evaluate self-hosted 70B (if Nebius costs exceed $200/mo)
    - Provision A100-80GB Spot for Reactor training + 70B inference
    - Keep L4 as primary workhorse
    - Hybrid: self-hosted 70B + Nebius API for 405B

[ ] GPU fleet management
    - Multi-instance support in gcp_vm_manager.py
    - Brain-to-instance routing (7B–32B → L4, 70B → A100)
    - Auto-scaling based on task queue depth
```

---

## 16. Key File Reference

| File | Lines | Purpose |
|------|-------|---------|
| `backend/core/prime_client.py` | ~1600 | J-Prime HTTP client, generate(), health_check(), streaming |
| `backend/core/prime_router.py` | ~1200 | RoutingDecision enum, endpoint-aware circuit breaker |
| `backend/core/gcp_hybrid_prime_router.py` | ~4100 | Pressure-driven provisioning, memory defense FSM |
| `backend/core/gcp_vm_manager.py` | ~7400 | VM lifecycle, golden image, APARS validation |
| `backend/intelligence/unified_model_serving.py` | ~2700 | 3-tier fallback, provider classes, cost tracking |
| `backend/intelligence/model_selector.py` | ~800 | UAE/SAI/CAI scoring, intent classification |
| `backend/core/ouroboros/governance/brain_selector.py` | ~500 | 3-layer gate, complexity classification, cost gate |
| `backend/core/ouroboros/governance/brain_selection_policy.yaml` | ~316 | Model arsenal, routing maps, fallback chains, compute tiers |
| `backend/core/ouroboros/governance/providers.py` | ~1700 | PrimeProvider, ClaudeProvider, codegen prompt builder |
| `backend/core/ouroboros/governance/repair_engine.py` | ~1000 | L2 self-repair loop, failure classification |
| `backend/reactor/reactor_api_interface.py` | ~720 | Reactor-Core training API (stub fine-tuning) |

---

## Appendix A: Compute Class Rank (from policy YAML)

```yaml
compute_class_rank:
  cpu:      0
  gpu_t4:   1
  gpu_l4:   2
  gpu_v100: 3
  gpu_a100: 4
```

The admission gate enforces: `rank(vm_class) >= rank(brain.min_compute_class)`

When Nebius is added, remote brains with `compute_class: "gpu_a100"` bypass the local admission gate entirely (no local VRAM check needed — the provider handles hardware).

---

## Appendix B: Environment Variable Reference

```bash
# ─── J-Prime Endpoint ───
JARVIS_PRIME_URL=http://136.113.252.164:8000
JARVIS_PRIME_HOST=localhost
JARVIS_PRIME_PORT=8000
JARVIS_PRIME_API_VERSION=v1

# ─── Timeouts ───
JARVIS_GENERATION_TIMEOUT_S=60
JARVIS_PIPELINE_TIMEOUT_S=150
JARVIS_BACKEND_STARTUP_TIMEOUT=300
PRIME_CONNECT_TIMEOUT=5.0
PRIME_READ_TIMEOUT=120.0

# ─── Circuit Breaker ───
PRIME_CIRCUIT_FAILURE_THRESHOLD=5
PRIME_CIRCUIT_RESET_TIMEOUT=30.0

# ─── Cost Management ───
OUROBOROS_GCP_DAILY_BUDGET=0.50
OUROBOROS_COST_STATE_PATH=~/.jarvis/ouroboros/cost_state.json

# ─── L2 Repair ───
JARVIS_L2_ENABLED=false
JARVIS_L2_MAX_ITERS=5
JARVIS_L2_TIMEBOX_S=120.0

# ─── GCP VM ───
GCP_VM_INSTANCE_NAME=jarvis-prime-node
GCP_VM_STATIC_IP_NAME=jarvis-prime-ip
JARVIS_HARDWARE_PROFILE=FULL

# ─── Nebius (Tier 0 — to be added) ───
NEBIUS_ENABLED=false
NEBIUS_API_KEY=
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_TIMEOUT_S=60
```
