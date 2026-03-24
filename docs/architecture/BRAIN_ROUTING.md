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

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOUBLEWORD_API_KEY` | (none) | Enables Tier 0 Doubleword routing |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | Doubleword API endpoint |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Default Doubleword model |
| `ANTHROPIC_API_KEY` | (required) | Enables Tier 1 Claude API |
| `CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Default Claude model |
| `OUROBOROS_GCP_DAILY_BUDGET` | `0.50` | Daily cost gate budget (USD) |
| `OUROBOROS_COST_STATE_PATH` | `~/.jarvis/ouroboros/cost_state.json` | Cost tracker file |
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
| `backend/core/interactive_brain_router.py` | Interactive task brain routing |
| `backend/core/ouroboros/governance/brain_selection_policy.yaml` | Hot-reloadable routing policy |
| `backend/core/ouroboros/governance/providers.py` | PrimeProvider + ClaudeProvider adapters |
| `backend/core/ouroboros/governance/boot_handshake.py` | Startup brain inventory validation |
| `benchmarks/doubleword/benchmark_doubleword.py` | Doubleword vs J-Prime benchmark suite |
