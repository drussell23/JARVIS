# Brain Routing: 3-Tier Model Cascade

## Overview

All AI inference in JARVIS flows through a tiered routing cascade that
selects the optimal model provider based on task complexity, system health,
cost constraints, and backend availability.  The system is designed so that
no single provider failure causes a total outage -- requests gracefully
degrade through the tiers.

```
User Request
     |
     v
+-----------+     +-----------+     +------------+
| Tier 0    |---->| Tier 1    |---->| Tier 2     |
| Doubleword|     | Claude API|     | GCP J-Prime|
| (PRIMARY) |     | (SECONDARY|     | (LAST      |
|           |     |           |     |  RESORT)   |
+-----------+     +-----------+     +------------+
```

---

## Tier 0: Doubleword API (PRIMARY)

**When available**: Activated automatically when `DOUBLEWORD_API_KEY` is set
in the environment.

Doubleword provides access to large open-weight models at dramatically lower
cost than proprietary APIs.  The API is OpenAI-compatible, enabling drop-in
use with existing client code.

### Available Models

| Model | Parameters | Use Case |
|-------|-----------|----------|
| `Qwen/Qwen3.5-397B-A17B-FP8` | 397B (17B active MoE) | Complex reasoning, coding, Ouroboros |
| `Qwen/Qwen3-VL-235B-A22B` | 235B (22B active MoE) | Vision tasks |
| `nvidia/Nemotron-3-Super` | 120B | Heavy general tasks |
| `Qwen/Qwen3.5-35B-A3B-FP8` | 35B (3B active MoE) | Fast/light tasks (default benchmark model) |

### API Details

```
Base URL:    https://api.doubleword.ai/v1
Auth:        Bearer token (DOUBLEWORD_API_KEY)
Protocol:    OpenAI-compatible (chat/completions)
Modes:       Real-time AND batch (1h or 24h SLA)
```

### Pricing (March 2026)

| Metric | Cost |
|--------|------|
| Input tokens | $0.10 per 1M tokens |
| Output tokens | $0.40 per 1M tokens |

For comparison, Claude Sonnet 4 costs $3.00/$15.00 per 1M tokens --
Doubleword is 30-37x cheaper.

### Benchmark Reference

The benchmark suite at `benchmarks/doubleword/benchmark_doubleword.py`
compares Doubleword against the J-Prime L4 baseline on standard Trinity
tasks (infrastructure code generation, threat analysis).

---

## Tier 1: Claude API (SECONDARY)

**When available**: Always available when `ANTHROPIC_API_KEY` is set.  This
is the primary fallback when Doubleword is unavailable or when tasks require
Claude-specific capabilities (tool use, extended thinking).

### Available Models

| Model | Use Case |
|-------|----------|
| `claude-sonnet-4-20250514` | Most tasks (coding, reasoning, generation) |
| `claude-haiku-4-5-20251001` | Trivial tasks (classification, short answers) |
| Claude Vision | Screen analysis in the Lean Vision Loop |

### ClaudeProvider (Ouroboros)

**Source**: `backend/core/ouroboros/governance/providers.py` (line 1863)

The `ClaudeProvider` wraps the Anthropic SDK for use in the governed
pipeline.  Features:

- **Cost gate**: Checks accumulated daily spend against `daily_budget` before
  each call.  Budget resets at midnight UTC.
- **Cost estimation**: Tracks input/output tokens and applies Sonnet pricing
  ($3.00/$15.00 per 1M tokens).
- **Structured output**: Enforces JSON schema compliance (2b.1, 2c.1, 2d.1)
  via system prompt engineering.

### PrimeRouter Integration

**Source**: `backend/core/prime_router.py`

The `PrimeRouter` singleton manages the top-level routing decision for
general inference (non-Ouroboros).  Its `RoutingDecision` enum:

| Decision | Condition |
|----------|-----------|
| `GCP_PRIME` | J-Prime VM healthy, model loaded |
| `LOCAL_PRIME` | Local llama.cpp instance running |
| `CLOUD_CLAUDE` | GCP unavailable, fallback to Anthropic |
| `HYBRID` | Try local first, then cloud |
| `CACHED` | Response served from cache |
| `DEGRADED` | All backends down |

Timeouts per backend:

| Backend | Env Var | Default |
|---------|---------|---------|
| Local | `PRIME_LOCAL_TIMEOUT` | 30s |
| GCP | `PRIME_GCP_TIMEOUT` | 120s |
| Cloud Claude | `PRIME_CLOUD_TIMEOUT` | 60s |

---

## Tier 2: GCP J-Prime (LAST RESORT)

**When available**: When the GCP VM (`jarvis-prime-stable`) is running and
the model is loaded.  Used as last resort for cost reasons (VM compute cost)
and as the primary backend for Ouroboros code generation.

### Infrastructure

```
VM:          jarvis-prime-stable (g2-standard-4)
GPU:         NVIDIA L4 (24 GB VRAM)
Static IP:   136.113.252.164:8000
Cost:        ~$1.20/hour when running
Lifecycle:   On-demand (golden image preserved, auto-start/stop)
```

### Available Models

| Model | File | Use Case |
|-------|------|----------|
| Qwen2.5-Coder-7B | `Qwen2.5-Coder-7B-Instruct-Q4_K_M.gguf` | Light tasks, fast |
| Qwen2.5-Coder-14B | `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` | Medium tasks |
| Qwen2.5-Coder-32B | `Qwen2.5-Coder-32B-Instruct-Q4_K_M.gguf` | Heavy code, architecture |
| LLaVA v1.5-32B | (multimodal) | Vision tasks |

Performance: ~24-47 tok/s on L4, model load ~3 minutes.

### PrimeProvider (Ouroboros)

**Source**: `backend/core/ouroboros/governance/providers.py` (line 1598)

The `PrimeProvider` wraps `PrimeClient.generate()` for Ouroboros code
generation.  Temperature is fixed at 0.2 for deterministic output.

---

## Brain Selector: 3-Layer Gate

**Source**: `backend/core/ouroboros/governance/brain_selector.py`

The `BrainSelector` is a deterministic classifier -- zero LLM calls, zero
latency.  It selects which brain tier handles a given operation.

### Gate Architecture

```
Operation Description + Target Files
          |
          v
   +------+------+
   | Layer 1      |  Task Gate: classify complexity from description
   | (Intent)     |  keywords + file count + blast radius
   +------+------+
          |
          v
   +------+------+
   | Layer 2      |  Resource Gate (delegated to TelemetryContextualizer)
   | (Resource)   |  Remote host pressure, not local Mac pressure
   +------+------+
          |
          v
   +------+------+
   | Layer 3      |  Cost Gate: daily budget enforcement
   | (Cost)       |  File-backed persistence across restarts
   +------+------+
          |
          v
   BrainSelectionResult
```

### Task Complexity Classification

| Complexity | Brain ID | Example Tasks |
|------------|----------|---------------|
| `TRIVIAL` | `phi3_lightweight` | Single-line change, comment append, docs edit |
| `LIGHT` | `qwen_coder` (7B) | Bug fix, code explanation |
| `HEAVY_CODE` | `qwen_coder_32b` | Refactor, implement, multi-file |
| `COMPLEX` | `qwen_coder_32b` | Architecture, cross-repo, deep reasoning |

### Cost Gate

The cost gate tracks daily spend per provider in a JSON file
(`~/.jarvis/ouroboros/cost_state.json`).  When the daily budget is exceeded:

- **Heavy/Complex tasks**: Queued until midnight budget reset
- **Light tasks**: Downgraded to `phi3_lightweight` (cheapest brain)

Default daily budget: `$0.50` (configurable via `OUROBOROS_GCP_DAILY_BUDGET`).

### Policy Hot-Reload

Brain selection policy is defined in YAML
(`backend/core/ouroboros/governance/brain_selection_policy.yaml`).  The file
is hot-reloaded when its mtime changes -- no restart required to adjust
routing rules.

---

## RouteDecisionService: Intelligence-Aware Routing

**Source**: `backend/core/ouroboros/governance/route_decision_service.py`

Extends BrainSelector with intelligence-driven routing:

### Intelligence Layers

| Intelligence | Module | Role |
|--------------|--------|------|
| **CAI** (Context Awareness) | `IntelligentModelSelector` | Intent classification -> brain_id |
| **SAI** (Self-Aware) | `SelfAwareIntelligence` | System health -> downgrade under pressure |
| **UAE** (Unified Awareness) | Fusion engine | Tiebreaker for borderline CAI confidence |

### SAI Health-Aware Downgrade

When SAI reports backpressure above the threshold (default 0.6), models
are downgraded to reduce VM load:

```
qwen_coder_32b  -->  qwen_coder_14b
qwen_coder_14b  -->  qwen_coder (7B)
deepseek_r1     -->  qwen_coder (7B)
```

### CAI Intent Mapping

| Intent | Complexity | Brain |
|--------|-----------|-------|
| `single_line_change` | TRIVIAL | phi3_lightweight |
| `docs_edit` | TRIVIAL | phi3_lightweight |
| `code_generation` | HEAVY_CODE | qwen_coder_32b |
| `bug_fix` | LIGHT | qwen_coder (7B) |
| `heavy_refactor` | HEAVY_CODE | qwen_coder_32b |
| `architecture_design` | COMPLEX | qwen_coder_32b |
| `segfault_analysis` | COMPLEX | qwen_coder_32b |

---

## Interactive Brain Router

**Source**: `backend/core/interactive_brain_router.py`

Extends BrainSelector to non-code tasks (voice commands, app control, vision).
Maps interactive task types to brain selections without LLM calls.

### Interactive Task Types

| Task Type | Complexity | Brain |
|-----------|-----------|-------|
| `workspace_fastpath` | trivial | (no LLM needed) |
| `system_command` | trivial | (local execution) |
| `classification` | light | qwen_coder (7B) |
| `step_decomposition` | light | qwen_coder (7B) |
| `vision_action` | heavy | qwen_coder + vision model |
| `vision_verification` | heavy | qwen_coder + vision model |
| `multi_step_planning` | complex | qwen_coder_32b |

The `InteractiveBrainSelection` dataclass carries:
- `jprime_model`: Model name to send to J-Prime (None if using Claude)
- `claude_model`: Claude model for paid fallback
- `vision_model`: Vision model name (None if not a vision task)
- `fallback_chain`: Ordered list of fallback brain IDs

---

## Boot Handshake

At supervisor startup (Zone 6.8), `run_boot_handshake()` validates the
J-Prime brain inventory against `brain_selection_policy.yaml`:

1. Calls `GET /v1/brains` on J-Prime
2. Checks all required brains from the policy are present
3. **Hard fail** if a required brain is missing
4. Populates the admitted brain set for runtime gating

---

## Adaptive Failback & Predictive Recovery

**Source**: `backend/core/ouroboros/governance/candidate_generator.py`

The `CandidateGenerator` uses a `FailbackStateMachine` enhanced with
**failure-mode classification** and **exponential backoff recovery prediction**.

### FailbackStateMachine States

```
PRIMARY_READY ----[failure]----> FALLBACK_ACTIVE
     ^                               |    |
     |                               |    +--[permanent failure]--> QUEUE_ONLY
     |                       [probe success]                           |
     |                               |                        [probe success]
     |                               v                                 |
     +---[N probes + dwell]--- PRIMARY_DEGRADED  <---------------------+
```

### Failure Mode Classification

Exceptions are classified by `FailbackStateMachine.classify_exception()`:

| Mode | Recovery Base | Max | Triggers |
|------|-------------|-----|----------|
| `RATE_LIMITED` | 15s | 120s | HTTP 429, `CircuitBreakerOpen` |
| `TIMEOUT` | 45s | 300s | `asyncio.TimeoutError`, connection timeout |
| `SERVER_ERROR` | 60s | 600s | HTTP 500/502/503 |
| `CONNECTION_ERROR` | 120s | 900s | Host unreachable, DNS failure |
| `CONTENT_FAILURE` | 0s | 0s | Bad model output (no infra penalty) |

### Recovery Prediction

```
recovery_eta = last_failure_at + base_s * 2^(consecutive_failures - 1)
```

Capped at `max_s`. The `should_attempt_primary()` method returns `True`
when the recovery window has elapsed, enabling the system to eagerly
return to the cheap provider.

### Self-Healing: QUEUE_ONLY Auto-Recovery

- **Transient failures** (TIMEOUT, RATE_LIMITED, SERVER_ERROR) stay in
  `FALLBACK_ACTIVE` — the next operation retries both providers
- **Permanent failures** (CONNECTION_ERROR on both) → `QUEUE_ONLY`
- `QUEUE_ONLY` **auto-recovers** when a health probe succeeds

### DoublewordProvider Resilience

| Feature | Implementation |
|---------|---------------|
| Per-request timeouts | `ClientTimeout(total=120s, connect=30s)` on every HTTP call |
| Connector recovery | Detects poisoned aiohttp connector (`_closed=True`), creates fresh session |
| Session re-acquire | `_poll_batch()` gets fresh session each iteration |
| Concurrent poll cap | Max 3 background poll tasks to prevent connector saturation |
| RateLimitService | Circuit breaker + token bucket + predictive throttle (wired at boot) |
| Cost gating | `DOUBLEWORD_MAX_COST_PER_OP=$0.10`, `DOUBLEWORD_DAILY_BUDGET=$5.00` |

---

## Environment Variables

### Provider Keys

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOUBLEWORD_API_KEY` | (none) | Enables Tier 0 Doubleword routing |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | Doubleword API endpoint |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-397B-A17B-FP8` | Default Doubleword model |
| `DOUBLEWORD_MAX_COST_PER_OP` | `0.10` | Per-operation cost cap (USD) |
| `DOUBLEWORD_DAILY_BUDGET` | `5.00` | Daily budget (USD) |
| `DOUBLEWORD_CONNECT_TIMEOUT_S` | `30` | TCP connect timeout (seconds) |
| `DOUBLEWORD_REQUEST_TIMEOUT_S` | `120` | Total request timeout (seconds) |
| `ANTHROPIC_API_KEY` | (required) | Enables Tier 1 Claude API |
| `JARVIS_GOVERNED_CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `JARVIS_GOVERNED_CLAUDE_MAX_COST_PER_OP` | `0.50` | Claude per-op cost cap |
| `JARVIS_GOVERNED_CLAUDE_DAILY_BUDGET` | `10.00` | Claude daily budget |

### Routing & Recovery

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_TIER0_BUDGET_FRACTION` | `0.50` | Fraction of deadline for Tier 0 |
| `OUROBOROS_TIER0_MAX_WAIT_S` | `90` | Absolute max Tier 0 wait |
| `OUROBOROS_TIER1_MIN_RESERVE_S` | `45` | Minimum reserved for Tier 1 |
| `OUROBOROS_PRIMARY_BUDGET_FRACTION` | `0.65` | Primary's share within Tier 1 |
| `OUROBOROS_FALLBACK_MIN_RESERVE_S` | `20` | Minimum reserved for fallback |
| `OUROBOROS_GCP_DAILY_BUDGET` | `0.50` | GCP daily cost gate budget (USD) |
| `OUROBOROS_SAI_DOWNGRADE_THRESHOLD` | `0.6` | SAI backpressure downgrade trigger |
| `PRIME_LOCAL_TIMEOUT` | `30` | Local inference timeout (seconds) |
| `PRIME_GCP_TIMEOUT` | `120` | GCP inference timeout (seconds) |
| `PRIME_CLOUD_TIMEOUT` | `60` | Claude API timeout (seconds) |

---

## File Reference

| File | Purpose |
|------|---------|
| `backend/core/prime_router.py` | Top-level routing (GCP/Local/Claude/Hybrid) |
| `backend/core/prime_client.py` | HTTP client to J-Prime with circuit breakers |
| `backend/core/ouroboros/governance/brain_selector.py` | 3-layer deterministic brain gate |
| `backend/core/ouroboros/governance/route_decision_service.py` | CAI/SAI intelligence-aware routing |
| `backend/core/ouroboros/governance/candidate_generator.py` | Adaptive failback FSM, FailureMode, recovery prediction |
| `backend/core/ouroboros/governance/doubleword_provider.py` | DoublewordProvider (Tier 0 batch API) |
| `backend/core/ouroboros/governance/rate_limiter.py` | RateLimitService, circuit breaker, token bucket |
| `backend/core/interactive_brain_router.py` | Interactive task brain routing |
| `backend/core/ouroboros/governance/brain_selection_policy.yaml` | Hot-reloadable routing policy |
| `backend/core/ouroboros/governance/providers.py` | PrimeProvider + ClaudeProvider adapters |
| `backend/core/ouroboros/governance/boot_handshake.py` | Startup brain inventory validation |
| `benchmarks/doubleword/benchmark_doubleword.py` | Doubleword vs J-Prime benchmark suite |
