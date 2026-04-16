# DoubleWord × Ouroboros + Venom — Battle-Test Benchmarks

**Prepared by:** Derek J. Russell (JARVIS Trinity / Ouroboros + Venom)
**Prepared for:** Meryem Arik, Co-founder/CEO, DoubleWord
**Report date:** 2026-04-16
**Coverage:** 2026-04-06 → 2026-04-16 (11 days, 160+ battle-test sessions)
**Repo:** `github.com/drussell23/JARVIS-AI-Agent`
**Models exercised:** Qwen 3.5 397B (`Qwen/Qwen3.5-397B-A17B-FP8`), Gemma 4 31B (`google/gemma-4-31B-it`), Qwen 3.5 35B (`Qwen/Qwen3.5-35B-A3B-FP8`, retired caller)

---

## TL;DR

1. **DoubleWord 397B reasoning quality is excellent when generation completes.** Multi-kilobyte structured JSON, correct tool-call shape, sensible plan reasoning — the intelligence is there.
2. **The blocker is infrastructure-level, not model-level.** Two dated isolation tests on 2026-04-14 — one on Gemma 4 31B (BACKGROUND), one on Qwen 3.5 397B (STANDARD) — produced the identical `SSE stream stalled (no data for 30s)` failure signature on right-sized payloads (2,836 tokens Gemma, 1,080–5,247 tokens Qwen). **Same streaming endpoint, different models, same stall.**
3. **As of `brain_selection_policy.yaml` revision 2026-04-14, DW is topology-sealed from every agent-generation route** (IMMEDIATE / STANDARD / COMPLEX / BACKGROUND / SPECULATIVE). It runs only on short, structured-JSON, non-streaming `callers` (semantic_triage, ouroboros_plan, Phase-0 compaction in shadow).
4. **Production cost impact: DW currently carries ~1.7% of inference spend** across 160 sessions ($0.31 DW / $18.63 total). The other 98.3% went to Claude — not by design, by forced fallback after the streaming endpoint proved unstable for agent-shape workloads.
5. **Session W (2026-04-15, `bt-2026-04-15-230849`)** achieved the first end-to-end autonomous multi-file APPLY in the repo's history — 4 Python test modules generated, validated, repaired, written to disk, autonomously committed, 20/20 pytest green. **It ran on Claude because DW was topology-sealed from COMPLEX.** I want to re-route that work to DW, which is exactly what this benchmark report is meant to unblock.
6. **The ask is not "make DW better at agents."** The ask is: **can the non-streaming `/v1/chat/completions` endpoint sustain heavy-payload agent workloads?** If yes, the architectural reframe in `docs/architecture/OUROBOROS.md` §"Functions, Not Agents" reseats 397B as a heavy analyst and Gemma 31B as a structured-function caller — and a lot of that 98.3/1.7 cost ratio starts moving back.

This document is a full-transparency engineering partner report, not a complaint thread. Where DW is strong, that's said. Where it fails, the session IDs, timestamps, payload sizes, and debug.log line numbers are cited verbatim so your infrastructure team can reproduce every finding.

---

## 1. How Ouroboros + Venom Uses DoubleWord

### 1.1 The provider chain (design intent)

O+V is a 6-layer autonomous development engine built on top of the JARVIS Trinity ecosystem. It runs an 11-phase governance pipeline:

```
CLASSIFY → ROUTE → CONTEXT_EXPANSION → PLAN → GENERATE →
VALIDATE → GATE → APPROVE → APPLY → VERIFY → COMPLETE
```

Every signal carries a deterministic `ProviderRoute` stamped at the ROUTE phase by `UrgencyRouter` (pure code, <1ms, zero LLM calls). Each route defines a contract for which provider tier is used and how cascade works:

| Route | Design-intent strategy | Intended cost | When |
|---|---|---|---|
| IMMEDIATE | Claude direct (skip DW) | ~$0.03/op | Test failures, voice, runtime health — latency-critical |
| STANDARD | **DW 397B primary → Claude fallback** | ~$0.005/op | Default cascade for moderate ops |
| COMPLEX | **Claude plans → DW executes** | ~$0.015/op | Heavy code, multi-file architectural |
| BACKGROUND | **DW only, no Claude fallback** | ~$0.002/op | OpportunityMiner, DocStaleness, TODOs |
| SPECULATIVE | **DW batch fire-and-forget** | ~$0.001/op | IntentDiscovery, DreamEngine pre-compute |

The routing table is built around DW as the cost-optimization backbone for 80% of operations. Claude is positioned as the fast-reflex tier and the safety-net fallback. **This is what we were trying to do.**

Source: `backend/core/ouroboros/governance/urgency_router.py:20-26`, `CLAUDE.md:34-52`.

### 1.2 What actually runs DW today

Per `backend/core/ouroboros/governance/brain_selection_policy.yaml` lines 342–376 (verbatim quoted in §4), DW is currently blocked from every one of those five generation routes. DW runs only on three short, structured-JSON, non-streaming `callers`:

- `semantic_triage` — pre-generation file intelligence (Gemma 4 31B)
- `ouroboros_plan` — plan-phase structured JSON output (Gemma 4 31B)
- `compaction` — Phase 0 Functions-Not-Agents (Gemma 4 31B, shadow mode, `JARVIS_COMPACTION_CALLER_ENABLED` default false)

Everything else now cascades to Claude.

### 1.3 DW configuration (as exercised)

From `backend/core/ouroboros/governance/doubleword_provider.py:38-90`:

| Setting | Value | Source |
|---|---|---|
| Default model | `Qwen/Qwen3.5-397B-A17B-FP8` | `DOUBLEWORD_MODEL` env |
| Base URL | `https://api.doubleword.ai/v1` | `DOUBLEWORD_BASE_URL` env |
| Input pricing | $0.10 / M tokens | `DOUBLEWORD_INPUT_COST_PER_M` env |
| Output pricing | $0.40 / M tokens | `DOUBLEWORD_OUTPUT_COST_PER_M` env |
| Max output tokens | trivial/moderate 8192, standard 12288, complex/heavy 16384 | `_DW_COMPLEXITY_MAX_TOKENS` |
| Poll interval | 5s | `DOUBLEWORD_POLL_INTERVAL_S` env |
| Request timeout | 120s | `DOUBLEWORD_REQUEST_TIMEOUT_S` env |
| Per-op cost cap | $0.10 | `DOUBLEWORD_MAX_COST_PER_OP` env |
| Daily budget | $5.00 | `DOUBLEWORD_DAILY_BUDGET` env |
| Temperature | 0.2 | `DOUBLEWORD_TEMPERATURE` env |

O+V talks to DW via a 3-tier pattern (per `CLAUDE.md:20`):

1. **RT SSE (default)** — real-time streaming for interactive generation
2. **Webhook batch** — async batch with webhook delivery
3. **Adaptive poll fallback** — polling when webhook isn't available

The failures described below occurred on Tier 0 (RT SSE streaming). The non-streaming `complete_sync()` path (Tier 3 adaptive poll variant, or `stream=false` HTTP) has not been stressed by O+V under agent-shape workloads. This is the architectural gap the roadmap section addresses.

---

## 2. Observed DoubleWord Strengths (what we're still trying to use DW for)

These are observations from sessions when DW *did* complete a generation. Cited verbatim so you can pull the same debug lines from the same session IDs.

### 2.1 397B reasoning quality is first-tier when generation completes

On Qwen 3.5 397B STANDARD isolation test `bt-2026-04-14-203740` (debug.log:457):

```
2026-04-14T13:41:36 [DoublewordProvider] RT: 1 candidates in 83.2s
                    ($0.0021, 0 tool calls, 5037+4018 tokens)
```

One candidate, 5,037 input tokens, 4,018 output tokens, $0.0021 — cost per completed op under $0.003 on a structured-JSON GENERATE with zero tool-loop rounds. Quality of the candidate itself was correct shape (JSON parsed, schema-valid, rationale present). The issue was latency: 83.2s exceeded the 71.5s Tier 0 RT budget, so the FSM had already cascaded by the time the candidate arrived.

### 2.2 Cost economics are exactly what DW promises — when throughput is met

On an earlier session `bt-2026-04-12-073546`, a hybrid run where DW was still permitted on structured JSON:

| Provider | Spend |
|---|---|
| DW | $0.002106 |
| Claude | $0.502881 |
| **Total** | **$0.504987** |

DW delivered ~0.4% of the inference spend for its share of work — the cost model was working as designed. When generations completed, the unit economics held.

### 2.3 Structured-JSON callers hit Gemma 31B reliably

`semantic_triage` and `ouroboros_plan` invocations against `google/gemma-4-31B-it` via the prompt_only batch protocol complete routinely — these calls are bounded (<512 token output), single-turn, and structurally simple. From `bt-2026-04-14-203740/debug.log:253-254`:

```
2026-04-14T13:38:24 [DoublewordProvider] prompt_only batch ff05400d-...
                    submitted (model=google/gemma-4-31B-it, caller=triage_op-019d8db7-)
2026-04-14T13:38:26 [DoublewordProvider] prompt_only batch ab6c4a97-...
                    submitted (model=google/gemma-4-31B-it, caller=triage_op-019d8db7-)
```

These calls work. They are the reason DW is still wired into the pipeline at all.

### 2.4 Model availability / version health check

Model verification on session boot confirms the endpoint serves the claimed model inventory (`bt-2026-04-14-182446/debug.log:11`):

```
2026-04-14T11:25:01 [SemanticTriage] Model verified:
                    google/gemma-4-31B-it is available (14 models on endpoint)
```

Gemma 4 31B shipped and was live the day Meryem announced it. That part of the platform works as advertised.

---

## 3. The Reproducible Blocker — SSE Stream Stall Under Agent-Shape Load

This is the single most valuable piece of feedback in the report. Two independent dated isolation tests, two different models, **one identical failure signature.**

### 3.1 Gemma 4 31B — BACKGROUND route isolation (`bt-2026-04-14-182446`)

**What ran:** 13 BACKGROUND-route operations forced through `google/gemma-4-31B-it` via topology override, with Claude fallback severed. Right-sized payload envelopes (2,836 tokens typical, down from ~11K pre-patch).

**Configuration:**
- Model: `google/gemma-4-31B-it`
- Route: `background` (topology override)
- Stream mode: Tier 0 RT SSE
- Prompt size: 5,233 → 11,916 chars (~1,308 → 2,979 tokens)
- Fallback: disabled (`fallback_disabled_by_env:background`)

**Result: 0 / 13 successes.**

- 12× 180s timeouts with SSE stream stall
- 1× schema_invalid (`doubleword_schema_invalid:candidate_0_missing_rationale`)

**Failure signature** (`bt-2026-04-14-182446/debug.log:445-451`):

```
2026-04-14T11:31:12 [DoublewordProvider] WARNING SSE stream stalled (no data for 30s)
2026-04-14T11:31:12 [Orchestrator] BACKGROUND route: DW failed
                    (background_dw_error:RuntimeError:doubleword_schema_invalid:candidate_0_missing_rationale)
```

**Session summary:**

| Field | Value |
|---|---|
| Session ID | `bt-2026-04-14-182446` |
| Start | 2026-04-14T11:25:00 UTC |
| Duration | 1,334s (22min) |
| Stop reason | `idle_timeout` |
| strategic_drift | 14 ops tracked, 1 drifted (7.1%, ok) |
| DW spend | $0.00 (no successful completions) |
| Claude spend | $0.078012 (fallback traffic on other ops) |

### 3.2 Qwen 3.5 397B — STANDARD route isolation (`bt-2026-04-14-203740`)

**Intent:** Prove the Gemma stall wasn't model-specific by repeating the isolation protocol on 397B with Claude fallback severed.

**Configuration:**
- Model: `Qwen/Qwen3.5-397B-A17B-FP8`
- Route: `standard`
- Stream mode: Tier 0 RT SSE
- Prompt size: 4,321 → 20,991 chars (~1,080 → 5,247 tokens)
- Fallback: disabled (`fallback_disabled_by_env:standard`)

**Result: 0 valid candidates across the run. Exactly the same failure signature.**

**Representative trace** (`bt-2026-04-14-203740/debug.log:421-425, 544-551`):

```
2026-04-14T13:41:36 [CandidateGenerator] Tier 0 RT: budget exhausted (44.3s).
                    Cascading to Tier 1 (136.9s remaining)
2026-04-14T13:41:36 [FailbackFSM] Primary failure (mode=TIMEOUT, consecutive=1,
                    recovery_eta=+45s) -> FALLBACK_ACTIVE
2026-04-14T13:41:36 [CandidateGenerator] EXHAUSTION event_n=5
                    cause=fallback_disabled_by_env:standard
                    fsm_state=FALLBACK_ACTIVE fsm_failure_mode=TIMEOUT
                    primary_name=doubleword-397b op_id=op-019d8db8-...
...
2026-04-14T13:42:48 [DoublewordProvider] WARNING SSE stream stalled (no data for 30s)
2026-04-14T13:42:48 [CandidateGenerator] Tier 0 RT: budget exhausted (71.5s).
                    Cascading to Tier 1 (98.1s remaining)
2026-04-14T13:42:48 [FailbackFSM] Primary failure (mode=TIMEOUT, consecutive=2,
                    recovery_eta=+90s) -> FALLBACK_ACTIVE
```

Per-op elapsed times from `bt-2026-04-14-203740/summary.json:40-85`: 0.0s (stream never opened), 100.5s, 267.7s. The stream either never starts or hangs for minutes after opening.

**Session summary:**

| Field | Value |
|---|---|
| Session ID | `bt-2026-04-14-203740` |
| Start | 2026-04-14T13:37:48 UTC |
| Duration | 330s (5.5 min) |
| Stop reason | `shutdown_signal` |
| Ops attempted | 3 (summary counter worked this run) |
| Ops failed | 3 (100% failure rate) |
| DW spend | $0.004208 |
| Claude spend | $0.00 (fallback severed) |

### 3.3 Shared failure signature across both tests

| Signal | Gemma 31B BG | Qwen 397B STANDARD |
|---|---|---|
| Stream opens | Yes | Yes |
| `SSE stream stalled (no data for 30s)` emitted | Yes (12×) | Yes (every op) |
| Payload size inside documented envelope | Yes (<3K tokens) | Yes (<6K tokens) |
| Tier 0 budget exceeded | Yes (180s) | Yes (44.3s, 71.5s) |
| FSM failure mode | TIMEOUT | TIMEOUT |
| Valid candidates produced | 0/13 | 0/3 |
| Candidate latency when one did arrive | n/a | 83.2s (past budget) |

**Conclusion encoded in `brain_selection_policy.yaml` line 331-341** (quoted verbatim in §4): the streaming endpoint cannot sustain agent-shape workloads across any tested model. The symptom is not prompt size, model weights, or the tool-loop shape — it is SSE stream behavior on sustained generation.

---

## 4. Current Topology — Verbatim Governance Config

This is the single source of truth for how DW is currently wired into O+V. Quoted verbatim from `backend/core/ouroboros/governance/brain_selection_policy.yaml` lines 299–376 so your gateway team can map 1:1 to endpoints and routes:

```yaml
# ---------------------------------------------------------------------------
# Provider Topology — Hard-segmented DW model mapping (Manifesto §5)
# ---------------------------------------------------------------------------
#
# Strict Cognitive Segmentation. Enforced by `provider_topology.py` and
# consumed by `candidate_generator.py`, `doubleword_provider.py`, and
# `semantic_triage.py`. Do not mutate at runtime — any change here is a
# deliberate architectural decision.
#
# Calibration: bbpst3ebf live-fire (2026-04-14) proved DW models time out
# on 120s Tier 0 RT for COMPLEX architectural GENERATE, regardless of
# whether the backing model is Qwen 3.5 397B or Gemma 4 31B. The response
# is not to extend timeouts but to harden the topology: DW is excluded
# from the Prefrontal Cortex entirely.
#
# Prefrontal Cortex (Claude only):
#   - IMMEDIATE  — fast reflex, deep context, strict deadlines
#   - COMPLEX    — architectural generation, Venom tool loops
#   - STANDARD   — midbrain sealed, cascades to Claude (see below)
# Basal Ganglia (Gemma 4 31B, strictly confined to structured-JSON callers):
#   - semantic_triage — pre-generation file intelligence
#   - ouroboros_plan  — plan-phase structured JSON output
#
# BACKGROUND + SPECULATIVE are SEALED. Alignment test bt-2026-04-14-182446
# produced 0/13 Gemma BG successes (12× 180s timeout, 1× schema_invalid)
# even with a right-sized 2,836-token envelope — the root cause is
# provider-side SSE stream stalling, not prompt size. We will NOT cascade
# these routes to Claude: routing continuous background daemons to a
# $3/$15 per M-token provider violates the unit economics of scalable
# autonomy. Until a viable, cost-effective inference endpoint is secured,
# BG/SPEC generation is paused via skip-and-queue.
#
# STANDARD is also SEALED as of bt-2026-04-14-203740 (Qwen 397B isolation
# benchmark). With the Claude fallback severed, Qwen 3.5 397B produced
# ZERO valid candidates across the run: Tier 0 RT streams started, then
# stalled mid-generation (``SSE stream stalled (no data for 30s)``), budget
# exhausted at 71.5s, and the FSM hit ``TIMEOUT`` on every attempt. Same
# failure signature as Gemma on BACKGROUND — provider-side stream stall,
# not prompt size. Unlike BG/SPEC, STANDARD DOES cascade to Claude: it
# carries bounded-volume mid-tier ops where Claude's unit economics are
# acceptable and speed matters. DW is confined to tiny-payload named
# callers (semantic_triage, ouroboros_plan) until DoubleWord repairs
# their SSE generation endpoint.
doubleword_topology:
  schema_version: "topology.1"
  enabled: true
  routes:
    immediate:
      dw_allowed: false
      block_mode: "cascade_to_claude"
      reason: "Prefrontal cortex — Claude only. 120s DW RT budget is insufficient for fast-reflex ops with Venom tool loops."
    complex:
      dw_allowed: false
      block_mode: "cascade_to_claude"
      reason: "Prefrontal cortex — Claude only. Live-fire bbpst3ebf proved DW (both 397B and Gemma 4 31B) times out on architectural COMPLEX GENERATE within 120s Tier 0 RT."
    standard:
      dw_allowed: false
      block_mode: "cascade_to_claude"
      reason: "Qwen 397B verified stream-stalling without candidate generation in bt-2026-04-14-203740. DW Tier 0 sealed to prevent latency debt."
    background:
      dw_allowed: false
      block_mode: "skip_and_queue"
      reason: "Gemma 4 31B stream-stalls on DW endpoint even at <2K tokens. Background generation paused to preserve Claude compute runway."
    speculative:
      dw_allowed: false
      block_mode: "skip_and_queue"
      reason: "Gemma 4 31B stream-stalls on DW endpoint even at <2K tokens. Background generation paused to preserve Claude compute runway."
  callers:
    semantic_triage:
      dw_model: "google/gemma-4-31B-it"
      reason: "Basal ganglia — native function-calling for triage JSON. Replaces Qwen 3.5 35B."
    ouroboros_plan:
      dw_model: "google/gemma-4-31B-it"
      reason: "Basal ganglia — structured JSON plan generation during PLAN phase."
    compaction:
      dw_model: "google/gemma-4-31B-it"
      reason: "Functions-not-Agents Phase 0. Structured summary over ToolLoop memory entries — fully-bounded input, short output (<1KB), trivial anti-hallucination check (subset of entry-keys). First non-streaming complete_sync() caller. SHADOW mode by default (JARVIS_COMPACTION_CALLER_ENABLED)."
```

**Derek's own design directive on why BG/SPEC don't cascade to Claude**, recorded at the time of the 2026-04-14 seal decision:

> Routing continuous background daemons to Claude violates the fundamental unit economics of scalable autonomy.

Claude is ~$0.03/op on IMMEDIATE, DW BACKGROUND targets ~$0.002/op — a ~15× delta that makes a continuous background loop unsustainable on the Claude tier. Skipping is the honest decision; the work queues dormant until a viable endpoint is secured.

---

## 5. Session-by-Session Evidence

### 5.1 Verified session ladder (direct anchors)

These sessions have session IDs explicitly named in `docs/architecture/OUROBOROS.md` or session memory. Every entry cites a verifiable artifact on disk.

| Session | ID | Date (UTC) | Duration | Stop | Cost (Claude) | Cost (DW) | Outcome |
|---|---|---|---|---|---|---|---|
| **A** | `bt-2026-04-15-040118` | 2026-04-15 04:01 | 316s | `budget_exhausted` | $0.2694 | $0 | ExplorationLedger shadow-mode: caught shallow 4× read_file at score=3.00 categories=comprehension. Enforcement not yet active. 0 files landed. |
| **B** | `bt-2026-04-15-041413` | 2026-04-15 04:14 | ~420s | `SIGKILL` (cost gov) | $0.2955 | $0 | Flipped `JARVIS_EXPLORATION_LEDGER_ENABLED=true` → hard Iron Gate rejection. First production proof the toggle enforces. 0 files. |
| **C** | `bt-2026-04-15-044627` | 2026-04-15 04:46 | see OUROBOROS.md:2134 | varies | — | — | Instrumentation proof (Track 1 + Track 2 diagnostics from commit `614009ec05`). |
| **O** | `bt-2026-04-15-175547` | 2026-04-15 17:56 | 4217s | `idle_timeout` | $0.3515 | $0 | **First end-to-end autonomous APPLY to disk.** `tests/.../test_test_failure_sensor_dedup.py` (4,986 B) written by ChangeEngine. 1 of 4 target files landed (winning candidate didn't populate `files:[...]`). Full CLASSIFY→PLAN→GENERATE→VALIDATE→L2(conv iter 1)→GATE→APPLY→VERIFY→POSTMORTEM. |
| **Q** | `bt-2026-04-15-201035` | 2026-04-15 20:10 | 876s | `idle_timeout` | ~$0.138 | $0 | `schema_invalid:candidate_0_missing_file_path` on multi-file candidates. Per-op dedup reset on interleaved IMMEDIATE success. |
| **R** | `bt-2026-04-15-203724` | 2026-04-15 20:37 | — | — | — | — | Parser fix verified. 4-file candidate passed parser at $0.1642 / 117.8s. Died at Iron Gate 1 (exploration) on 0 fresh tool calls. |
| **S** | `bt-2026-04-15-204901` | 2026-04-15 20:49 | 1234s | `idle_timeout` | ~$0.160 | $0 | Iron Gate 5 (multi-file coverage) passed silently. 4 paths covered, 3/4 had LSP type errors, routed to L2 which didn't converge before idle timeout. 0/4 landed. |
| **T** | `bt-2026-04-15-211616` | 2026-04-15 21:16 | 1356s | `idle_timeout` | $0.510 | $0 | Follow-up A hypothesis falsified. L2 never got a chance to run; upstream stall. |
| **U** | `bt-2026-04-15-215858` | 2026-04-15 21:58 | — | — | — | — | FSM instrumentation kills the "silent exit" ghost. Re-validation non-determinism exposed: iter=0 returns `failure_class='test'`, iter=1 returns `'infra'` on same candidate. |
| **V** | `bt-2026-04-15-223631` | 2026-04-15 22:36 | — | — | — | — | `JARVIS_MAX_VALIDATE_RETRIES=0` env-factory works. First `l2_dispatch_pre` log on multi-file op. Exposes L2 budget contract bug (120s vs 600s configured). |
| **W** | `bt-2026-04-15-230849` | 2026-04-15 23:08 | 3645s | `idle_timeout` | $0.3201 | $0 | **First end-to-end autonomous multi-file APPLY.** 4 Python test modules to disk. L2 converged iter 1/5 in 50s. APPLY mode=multi. AutoCommitter commit `0890a7b6f0`. Post-hoc pytest: **20/20 passed in 2.28s**. Ran on Claude (DW topology-sealed from COMPLEX). |

### 5.2 Inferred Apr 14 arc (letters not explicitly recorded)

Sessions labeled H–N in the breakthrough-log narrative correspond to an 8-session unmasking ladder on 2026-04-15 (between Sessions C and O), where each session surfaced a distinct failure mode:

| Inferred label | Failure mode unmasked | Resolution |
|---|---|---|
| H | `prompt_async [Errno 22]` headless TTY crash | commit `d8c1cb4d30` |
| I | `too_many_files` risk escalation | env override |
| J | L3 `READ_ONLY_PLANNING` mode switch on ambient probe failures | commit `5d169266d6` (env-tunable thresholds) |
| K | `RollbackArtifact [Errno 2]` new-file path | commit `28d52274ec` |
| L | 900s pool ceiling crossed by ambient load | env `JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S=1800` |
| M | 180s fallback cap clipped 5-tool-round retry | env `JARVIS_FALLBACK_MAX_TIMEOUT_COMPLEX_S=360` |
| N | Intake WAL cross-session coalescing merged stale envelopes | state cleanup: `rm .jarvis/intake_wal.jsonl` + `.jarvis/intake_router.lock` |

These sessions are documented in `docs/architecture/OUROBOROS.md` lines 2194–2266 as a group narrative. Concrete session IDs exist in `.ouroboros/sessions/` but the letter-tag mapping was not recorded at the time of each run. Listed here with the caveat that the letters are a post-hoc narrative convenience, not a hard ID → letter mapping.

### 5.3 DW-specific isolation tests (Apr 14)

These are the two dated benchmarks that drove the topology seal decisions. **These are the single most important data points in this report for DoubleWord's gateway team.**

| Session | Model | Route | Result | DW spend | Failure signature |
|---|---|---|---|---|---|
| `bt-2026-04-14-182446` | Gemma 4 31B | BACKGROUND (override) | 0/13 successes | $0.00 | 12× 180s SSE stream stall, 1× schema_invalid |
| `bt-2026-04-14-203740` | Qwen 3.5 397B | STANDARD (fallback severed) | 0/3 successes | $0.004208 | SSE stream stall at 30s no-data, FSM TIMEOUT, 1 candidate arrived at 83.2s past budget |

### 5.4 Today's sessions (Apr 16, incremental work)

Three sessions ran on 2026-04-16 before this benchmark was drafted:

| Session | Duration | Stop | Cost | Notable |
|---|---|---|---|---|
| `bt-2026-04-16-100328` | 396s | `idle_timeout` | $0.0825 Claude | Zero ops attempted; schema_version=2 confirmed |
| `bt-2026-04-16-102525` | 639s | `budget_exhausted` | $0.5943 Claude | $0.50 cost cap triggered |
| `bt-2026-04-16-105356` | 797s | `budget_exhausted` | $0.6511 Claude | **ops_digest v1.1a populated**: `last_apply_mode=single`, `last_apply_files=1`, `last_apply_op_id=op-019d95ef-80aa-7dfd-87cd-0c7b0d73afda-cau`. One op applied. |

All three ran on Claude; DW continues to be sealed from generation routes.

---

## 6. Cost Telemetry (11-day aggregate)

| Metric | Value |
|---|---|
| Total sessions | 160+ battle tests (Apr 6 – Apr 16) |
| Total inference spend | ~$18.63 |
| Claude spend | ~$18.32 (98.3%) |
| **DoubleWord spend** | **~$0.31 (1.7%)** |
| Sessions with any DW usage | 33 (20.6%) — mostly structured-JSON callers |
| Sessions with Claude only | 84 (52.5%) |
| Sessions with neither (idle / zero-cost) | 43 (26.9%) |
| Cost per session (mean) | ~$0.116 |
| Cost per session (median) | ~$0.06 |

**What I was hoping the split would look like by now:** DW carrying STANDARD + BACKGROUND + SPECULATIVE at ~80% of ops. Based on the urgency_router.py cost model, that would put DW at ~50–60% of spend with Claude at ~40–50%. Instead DW is at 1.7%. The gap is the SSE stall. Closing the SSE gap moves the numbers back toward the design target.

**Known data hygiene note:** `summary.json` has a known counter bug where `stats.attempted` / `completed` / `failed` stay at zero even when ops fire (documented in `CLAUDE.md` §"Battle Test Milestones"). The canonical source for ops counts is the session `debug.log`. The one session where the counter worked — `bt-2026-04-14-203740` — reported `attempted=3, completed=0, failed=3`, consistent with the isolation-test failure signature. ops_digest (schema v2, landed 2026-04-15) provides typed APPLY/VERIFY/commit telemetry going forward and is visible in today's Apr 16 sessions.

---

## 7. The Architectural Reframe — "Functions, Not Agents"

From `docs/architecture/OUROBOROS.md` §"Functions, Not Agents: DoubleWord Reseating Roadmap (2026-04-14)", lines 2685–2718:

> DoubleWord is not a broken agent provider. It is an **untested function provider.** The streaming endpoint fails; the non-streaming `/v1/chat/completions` endpoint with `stream=false` has never been exercised by O+V. The reseating matrix below moves DW out of the agent cascade entirely and into a fixed set of short, structured, non-streaming function calls.

This is the forward path. It is already being built.

### 7.1 Phase 0 (SHIPPING, shadow mode) — Gemma Compaction Caller

- Model: `google/gemma-4-31B-it`
- Call site: `ContextCompactor._build_summary()` in `context_compaction.py`
- Contract: non-streaming `complete_sync()`, bounded input, <1KB structured output, trivial anti-hallucination check (subset of entry-keys)
- Deterministic fallback: existing type-counting + phase-histogram summarizer
- Master switch: `JARVIS_COMPACTION_CALLER_ENABLED` (default `false`) — explicitly promoted per battle test during shadow validation

**This is the first O+V caller to exercise the non-streaming DW endpoint at all.** Its shadow telemetry will directly test whether the stall is streaming-specific.

### 7.2 Phase 3 (planned) — Qwen 397B heavy analyst

Three structured-JSON, non-streaming callers designed to sit Qwen 397B into the governance pipeline without ever touching the streaming endpoint:

1. **BlastRadius scoring** (`OperationAdvisor`) — semantic impact score with 10s timeout, invariant-subset anti-hallucination gate, score-floor contract (397B can only *escalate* risk tier, never de-escalate).
2. **Episodic failure clustering** — cross-session pattern recognition over UserPreferenceMemory + ProphecyEngine substrate.
3. **(Third caller in design, name redacted pending architecture review.)**

### 7.3 Design invariants for every DW caller (architectural contract)

Every DW caller ships with these guarantees (source: `OUROBOROS.md:2700-2707`):

1. **Non-streaming only.** `stream=false`, single JSON response, no SSE.
2. **Short structured output.** ≤512 output tokens typical, JSON-schema bounded.
3. **Caller-supplied timeout.** Per-call site, enforced by `asyncio.wait_for()`.
4. **Anti-hallucination gate.** Output cross-referenced against provided context; hallucinated refs → autonomous rejection + deterministic fallback.
5. **Circuit breaker.** Per-call, per-op, and global-rolling breakers.
6. **Shadow mode first.** Disabled-by-default, runs parallel to deterministic baseline, writes telemetry to `.ouroboros/sessions/<id>/<caller>_shadow.jsonl`. Promoted to LIVE only after offline shadow-JSONL analysis.

**If Phase 0 shadow telemetry shows the non-streaming endpoint is stable, the reseating roadmap unlocks a path for DW to carry a substantial share of O+V's structured inference load without going anywhere near the streaming stall.**

---

## 8. Engineering Asks for the DoubleWord Gateway Team

Framed as concrete, falsifiable items — not wishlist items.

### 8.1 Primary: diagnose the SSE stream stall

Two dated isolation tests with matching failure signatures across two different models on the same streaming endpoint. We have the debug logs, timestamps, payload sizes, and per-op latencies. **What is happening at the DoubleWord gateway between `stream opens successfully` and `no data for 30s`?**

Candidate hypotheses (ordered by our prior likelihood):

1. **Gateway keepalive / buffering timeout.** Intermediate proxy drops idle connections before the model finishes reasoning on longer-latency ops (tool-loop rounds, multi-file planning).
2. **Backend worker exhaustion.** Sustained agent-shape workloads queue at the model tier, gateway holds the stream open with no data until queue clears, client hits `no data for 30s` first.
3. **SSE framing mismatch.** The client (`DoublewordProvider._consume_sse`) expects periodic keepalive frames during long generation; the gateway emits none, so the 30s no-data timer fires even when the backend is still producing.

I will provide the two debug.logs attached to this email as evidence. If you can get a gateway engineer to run a capture on one rerun, the root cause should be observable end-to-end.

### 8.2 Secondary: non-streaming `/v1/chat/completions` stability contract

O+V is building Phase 0 of the "Functions, Not Agents" reseating specifically around the non-streaming endpoint. Before we commit the reseating roadmap to production traffic, I want to know:

1. Is the non-streaming path on the same inference backend as streaming, or does it have a separate SLA?
2. What is the practical upper bound on output tokens before the non-streaming endpoint itself exhibits degraded latency?
3. Is there a documented rate-limit or queue behavior under sustained low-QPS agent load (our target is ~1 QPS sustained, ~5 QPS peak)?

### 8.3 Tertiary: a function-calling-optimized Gemma tier

Gemma 4 31B with "stronger reasoning, function calling, and multimodal support" (your 2026-04-13 announcement) is exactly what the `semantic_triage` and `ouroboros_plan` callers want. Today those callers run reliably at small payload sizes. If the model is capable of consistent native function-calling, we want to mount it at:

- `blast_radius` (10s timeout, structured JSON, invariant-subset gate)
- `episodic_failure_clustering` (30s timeout, structured JSON)
- `deep_analysis_sensor` (planned sensor — autonomous codebase comprehension)

An explicit SLA on function-calling consistency (e.g., "returns valid JSON per provided schema in >99% of <2K-token requests") would let us promote these callers from shadow to live with a documented risk profile.

### 8.4 Optional wishlist

- **Per-route SSE idle timeout configuration.** The 30s no-data default is reasonable for chat-style UX but incompatible with agent ops that spend 60–120s reasoning before emitting the first output token. If the provider could accept a `X-Stream-Idle-Timeout-MS` header and honor it, that alone might unseal STANDARD.
- **Streaming heartbeat frames.** Alternative to #1: the provider emits SSE `:heartbeat` comments every 10s during model computation. Client side we already discard comment frames; this would keep the stream formally alive without polluting output.
- **Observable streaming state.** A separate status endpoint keyed to `batch_id` or `request_id` that reports `queued | generating | stalled | complete` would let clients make informed cascade decisions before the 30s timer fires.

---

## 9. What I'd like from the next meeting

1. **10-min pairing on the two debug.logs.** I'll share the full logs; your gateway engineer reads the stall timestamps live.
2. **Go / no-go on Phase 0 non-streaming stability.** If yes, I'll promote the compaction caller from shadow → live in the next battle test (target: 2026-04-22) and share the shadow JSONL analysis with you.
3. **Alignment on the Gemma function-calling SLA** — what's the best-case target, and what would move the current 0/13 BG result on a non-streaming reroute?
4. **Optional**: If Phase 0 + Phase 3 planning goes well, a timeline sketch for when streaming stability could be closed, so I can re-enable the topology seals on BG/SPEC with a dated expectation.

---

## 10. Appendix

### 10.1 Reproduction commands

All benchmarks reproduce on the main branch of `github.com/drussell23/JARVIS-AI-Agent` at commit `5d98d418bc` (tip of 2026-04-16).

**Standard battle test (Claude-default, DW topology-sealed):**
```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

**Gemma BG isolation repro (requires topology override):**
```bash
export JARVIS_DOUBLEWORD_TOPOLOGY_OVERRIDE_ROUTE=background
export DOUBLEWORD_MODEL=google/gemma-4-31B-it
export JARVIS_FALLBACK_DISABLED_ROUTES=background
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 1800 -v
```

**Qwen 397B STANDARD isolation repro (Claude fallback severed):**
```bash
export JARVIS_FALLBACK_DISABLED_ROUTES=standard
export DOUBLEWORD_MODEL=Qwen/Qwen3.5-397B-A17B-FP8
python3 scripts/ouroboros_battle_test.py --cost-cap 0.10 --idle-timeout 600 -v
```

### 10.2 Session artifact layout

Every battle-test session writes to `.ouroboros/sessions/bt-YYYY-MM-DD-HHMMSS/`:

- `summary.json` — session-level stats (note: `stats.attempted` counter bug; use `debug.log` for ops counts)
- `debug.log` — full session log (every provider call, every FSM transition, every Iron Gate verdict)
- `cost_tracker.json` — per-provider spend breakdown
- (new) `ops_digest` inside `summary.json` — schema v2, typed APPLY/VERIFY/commit telemetry

### 10.3 Key file references (full paths)

- Provider topology: `backend/core/ouroboros/governance/brain_selection_policy.yaml` (lines 299–376)
- DW provider implementation: `backend/core/ouroboros/governance/doubleword_provider.py`
- Urgency router (route definitions): `backend/core/ouroboros/governance/urgency_router.py`
- Candidate generator (cascade logic): `backend/core/ouroboros/governance/candidate_generator.py`
- Reseating roadmap: `docs/architecture/OUROBOROS.md` §"Functions, Not Agents" (lines 2685–2900)
- Battle-test breakthrough log: `docs/architecture/OUROBOROS.md` §"Battle Test Breakthrough Log" (lines 1741–2558)

### 10.4 Environment variables (key knobs)

Configured on the O+V side. All default values shown.

| Env var | Default | Purpose |
|---|---|---|
| `DOUBLEWORD_API_KEY` | *(required)* | DW API credential |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | DW endpoint |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-397B-A17B-FP8` | Default model |
| `DOUBLEWORD_MAX_TOKENS` | `16384` | Output ceiling |
| `DOUBLEWORD_TEMPERATURE` | `0.2` | Sampling |
| `DOUBLEWORD_POLL_INTERVAL_S` | `5` | Poll cadence |
| `DOUBLEWORD_REQUEST_TIMEOUT_S` | `120` | Per-request timeout |
| `DOUBLEWORD_MAX_COST_PER_OP` | `0.10` | Per-op cost cap |
| `DOUBLEWORD_DAILY_BUDGET` | `5.00` | Daily DW spend limit |
| `DOUBLEWORD_INPUT_COST_PER_M` | `0.10` | Input pricing |
| `DOUBLEWORD_OUTPUT_COST_PER_M` | `0.40` | Output pricing |
| `JARVIS_COMPACTION_CALLER_ENABLED` | `false` | Phase 0 Gemma compaction caller gate |
| `JARVIS_MAX_VALIDATE_RETRIES` | *(env-tunable)* | Workaround for iter=1 infra flake (Session U/V) |

---

**Artifacts attached to the delivery email:**

1. This markdown file, also available at `docs/benchmarks/DW_BENCHMARKS_2026-04-16.md` in the repo
2. PDF export (see §10.5 for the generation command)
3. (On request) Full debug.logs for `bt-2026-04-14-182446` and `bt-2026-04-14-203740` — too large for inline quoting, available via secure share

### 10.5 PDF / HTML generation commands (reproducibility)

Generated on macOS 15 (Darwin 25.3.0) with pandoc 3.9 + Chrome headless (no LaTeX engine required):

```bash
# Styled HTML (intermediate, can be opened directly in any browser)
pandoc DW_BENCHMARKS_2026-04-16.md \
  -o DW_BENCHMARKS_2026-04-16.html \
  --from=gfm --to=html5 --standalone \
  --metadata title="DoubleWord × Ouroboros + Venom — Battle-Test Benchmarks" \
  --css=print.css

# PDF via Chrome headless (isolated user-data-dir to avoid profile conflicts)
TMPDIR_CHROME="${TMPDIR:-/tmp}/chrome-headless-$$"
mkdir -p "$TMPDIR_CHROME"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu \
  --user-data-dir="$TMPDIR_CHROME" \
  --no-pdf-header-footer \
  --print-to-pdf=DW_BENCHMARKS_2026-04-16.pdf \
  "file://$(pwd)/DW_BENCHMARKS_2026-04-16.html"
rm -rf "$TMPDIR_CHROME"
```

If a LaTeX engine is available (MacTeX / BasicTeX), the equivalent pandoc-native path is:

```bash
pandoc DW_BENCHMARKS_2026-04-16.md \
  -o DW_BENCHMARKS_2026-04-16.pdf \
  --from=gfm --pdf-engine=xelatex \
  -V geometry:margin=1in \
  -V mainfont="Helvetica Neue"
```

*Generated 2026-04-16. Prepared as an engineering-partner deliverable; feedback and corrections welcome.*
