# Trinity AI — Technical Brief
### Palantir Startup Fellowship Cohort 002 · Supplementary Document

**Author:** Derek J. Russell — Founder
**Repositories:** `JARVIS-AI-Agent` · `jarvis-prime` · `reactor-core`
**Infrastructure:** GCP `g2-standard-4` · NVIDIA L4 · Static IP `136.113.252.164`
**Date:** March 2026

---

## Executive Summary

Trinity AI is a purpose-built autonomous compute kernel with a governed inference loop for defense and critical infrastructure. It is not an application built on top of an AI framework — it is the infrastructure layer that makes autonomous AI safe to deploy in FedRAMP/IL5 environments where hallucinations, stateless execution, and ungoverned rollout are unacceptable.

Trinity is composed of three tightly integrated systems:

- **JARVIS (The Body)** — Local execution supervisor and governance kernel. Hosts the Ouroboros pipeline, 50+ specialized autonomous agents, durable operation ledger, risk engine, circuit breakers, and trust graduators.
- **J-Prime (The Mind)** — Hybrid cloud/edge model inference plane running on GCP. NVIDIA L4 GPU, Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf, OpenAI-compatible API, 8,192-token context window, ~24.5 tok/s generation throughput.
- **Reactor-Core (The Nerves)** — Telemetry ingestion and DPO preference pair generator. Converts every governed production operation into a fine-tuning signal, closing the human-oversight loop via AIP Evals.

The three systems share a unified cross-repo contract (`trinity-cross-repo-contract.md`) and communicate over a structured IPC protocol. All inference calls are mediated by the Ouroboros governance pipeline — no model request exits JARVIS without traversing a full classify → route → validate → gate → apply → verify cycle.

**Scale:**
- ~2.9 million lines of authored source code across three repositories
- 22+ programming languages (Python, Rust, Go, C, C++, CUDA, Swift, SQL, TypeScript, and more)
- 5,400+ commits in 7 months, solo developer
- 2,132 governance tests at 99.3% pass rate
- Live on GCP 24/7 with a reserved static IP and verified L4 throughput

---

## 1. The Ouroboros Governance Pipeline

Ouroboros is the core differentiator. It is a deterministic, durable, pre-execution governance kernel implemented as a multi-stage pipeline with a finite state machine (FSM) backbone. Every inference request — regardless of which agent initiates it — must pass through the full pipeline before a single token is generated.

### 1.1 Pipeline Stages

```
CLASSIFY → ROUTE → [CONTEXT_EXPANSION] → GENERATE →
[COMPLETE-noop fast-path] → VALIDATE → GATE → [APPROVE] →
APPLY → VERIFY → COMPLETE
```

| Stage | What It Does | Output |
|---|---|---|
| **CLASSIFY** | Scores request against risk taxonomy. Assigns `risk_tier`: `SAFE_AUTO`, `NEEDS_APPROVAL`, `HIGH_RISK`, `BLOCKED`. Computes `blast_radius` score. | `risk_tier`, `blast_radius`, `op_id` |
| **ROUTE** | Selects model tier based on task complexity, VRAM availability, and risk tier. Routes to J-Prime PRIMARY (L4 GPU), LOCAL fallback, or CLAUDE API. Emits `RoutingDecision`. | `routing_tier`, `model_id`, `provider` |
| **CONTEXT_EXPANSION** | Optional stage. The Oracle indexes all three repositories and expands the operation with relevant file neighborhood context (`FileNeighborhood`). Max 2 rounds, 5 files per round. Warns if Oracle index is stale > 300 seconds. | `expanded_files`, `file_neighborhood` |
| **GENERATE** | Executes inference against the routed model. Streams tokens. Records `latency_ms`, `tokens_generated`, `tok_per_s`. | `generation_result`, `candidates` |
| **COMPLETE (noop fast-path)** | If the model returns schema `2b.1-noop` (change already present in codebase), the pipeline fast-paths directly from GENERATE to COMPLETE, skipping VALIDATE through VERIFY. Prevents redundant apply operations. | `is_noop=True`, `provider_used` |
| **VALIDATE** | Syntax validation and security scan of generated output. Blocks malformed or flagged content before it touches any file. | `validation_result` |
| **GATE** | Security approval gate. Checks risk tier — `SAFE_AUTO` passes automatically; `NEEDS_APPROVAL` holds for human or automated approval signal. | `gate_decision` |
| **APPROVE** | Human-in-the-loop stage. Invoked only when `risk_tier == NEEDS_APPROVAL`. Emits AIP `ApproveOperation` action. Records approval event in durable ledger. | `approval_record` |
| **APPLY** | Executes the validated change. Writes rollback hash (`sha_before`, `sha_after`) to ledger before and after apply. Enforces `_file_touch_cache` cooldown: 3 touches per file per 10-minute window — hard block on excess. | `rollback_hash`, `sha_before`, `sha_after` |
| **VERIFY** | Post-apply verification. Confirms the change landed correctly. On failure, emits AIP `RollbackChange` action and restores prior state using `sha_before`. | `verify_result` |
| **COMPLETE** | Terminal state. Writes final ledger entry with full operation record. Emits `emit_postmortem` to VoiceNarrator and CrossRepoNarrator. Triggers Reactor-Core DPO capture. | `terminal_state=COMPLETE` |

### 1.2 Failure Paths

Every non-terminal failure state has a defined exit:

- **CLASSIFY → BLOCKED**: Operation immediately terminated. No routing or generation occurs. Ledger entry written with `state=BLOCKED`.
- **GATE → HOLD**: Operation suspended pending approval. Held in `NEEDS_APPROVAL` state. Times out with escalation after configurable window.
- **VERIFY → FAILED**: `RollbackChange` action emitted. `sha_before` applied. Ledger entry updated to `state=ROLLED_BACK`. VoiceNarrator notified.
- **GENERATE → timeout**: `JARVIS_GENERATION_TIMEOUT_S=60` (L4 + 14B model). `JARVIS_PIPELINE_TIMEOUT_S=150` for full pipeline. Pipeline aborts cleanly; partial generation discarded.

### 1.3 The Durable Ledger

Every operation produces a durable ledger entry written to `~/.jarvis/ouroboros/ledger/` before the operation executes, not after. The ledger is:

- **Pre-execution**: Entry created at CLASSIFY with `op_id`, `state=PENDING`, `ts`, `risk_tier`
- **Append-only**: Each stage appends its output record to the entry
- **Cryptographically anchored**: `rollback_hash` written at APPLY using SHA-256 of `sha_before`
- **Machine-readable**: JSON format, one file per operation
- **Queryable**: The Oracle indexes ledger entries alongside source code for context retrieval

Sample ledger entry (abbreviated):

```json
{
  "op_id": "op-demo-1742112318-0",
  "state": "COMPLETE",
  "risk_tier": "NEEDS_APPROVAL",
  "blast_radius": 0.42,
  "routing_tier": "primary",
  "model_id": "jarvis-prime",
  "latency_ms": 27630,
  "tokens_generated": 680,
  "tok_per_s": 24.6,
  "rollback_hash": "a3f9c12d",
  "sha_before": "e5f1a2b3",
  "sha_after": "9c3d7e4a",
  "ts": "2026-03-16T08:05:18Z",
  "approved_by": "auto",
  "verify_result": "PASSED"
}
```

This is exactly the data structure that maps into Palantir AIP's Ontology — each field is a property on a `GovernedOperation` Object Type.

### 1.4 The FSM Engine

The pipeline is not a linear function chain — it is driven by a `PreemptionFsmEngine` that maintains a full `LoopState × LoopEvent` transition matrix. The `PreemptionFsmExecutor` is durable-ledger-first: it writes state transitions to the ledger before executing stage logic, so a crash at any point leaves a recoverable record. A `_FsmLedgerAdapter` bridges the FSM protocol to the ledger writer.

FSM telemetry is emitted via `_CommTelemetrySink`, which wraps `CommProtocol.emit_heartbeat()` and makes pipeline health visible to monitoring surfaces.

---

## 2. AIP Integration Architecture

Trinity's governance primitives map directly onto Palantir AIP's Ontology and Action types. The 8-week fellowship sprint is designed to formalize these mappings into registered AIP Object Types and wire the live data flow.

### 2.1 AIP Ontology Object Types

| Ouroboros Concept | AIP Object Type | Key Properties |
|---|---|---|
| Operation Ledger Entry | `GovernedOperation` | `op_id`, `state`, `risk_tier`, `blast_radius`, `ts`, `rollback_hash` |
| Routing Decision | `InferenceRoute` | `model_id`, `routing_tier`, `latency_ms`, `tok_per_s`, `provider` |
| Risk Assessment | `RiskClassification` | `risk_tier`, `blast_radius`, `auto_approved`, `classified_at` |
| Trust Graduation Event | `TrustGraduation` | `repo`, `trigger`, `old_trust_level`, `new_trust_level`, `graduated_at` |
| Circuit Breaker Event | `CircuitBreakerEvent` | `component`, `state`, `failure_count`, `opened_at`, `closed_at` |
| Rollback Record | `RollbackAudit` | `op_id`, `sha_before`, `sha_after`, `verified`, `rolled_back_at` |
| DPO Preference Pair | `AIPEvalSample` | `prompt`, `chosen_output`, `rejected_output`, `source_op_id`, `risk_tier` |

### 2.2 AIP Action Types

| Action | Trigger | Data Emitted |
|---|---|---|
| `ApproveOperation` | `risk_tier == NEEDS_APPROVAL` at GATE stage | `op_id`, `approver`, `approval_ts`, `risk_tier` |
| `RollbackChange` | VERIFY failure | `op_id`, `sha_before`, `rollback_ts`, `failure_reason` |
| `EscalateRisk` | `blast_radius` exceeds threshold | `op_id`, `blast_radius`, `escalated_to`, `escalation_ts` |
| `TriggerDPOCapture` | `state=COMPLETE` on any APPLIED operation | `op_id`, `dpo_pair_id`, `model_id`, `captured_at` |

### 2.3 The AIP Evals Loop (Fellowship Deliverable)

The DPO pipeline closes the human-oversight loop through AIP Evals:

```
Production Operation (JARVIS)
         ↓
Ouroboros COMPLETE state
         ↓
Reactor-Core: generates DPO preference pair
  chosen:   the applied, governance-approved output
  rejected: the pre-validation draft (if flagged) or a counterfactual
         ↓
AIP Evals: DPO pair registered as AIPEvalSample
         ↓
Fine-tuning pipeline: J-Prime updated from production governance signals
         ↓
Better model → fewer NEEDS_APPROVAL escalations → tighter loop
```

Every hour of production operation generates preference pairs. This means AIP Evals is not running on synthetic benchmarks — it runs on real defense workloads that have already passed the Ouroboros governance gate. The signal quality is production-grade.

### 2.4 8-Week Integration Milestones

| Weeks | Deliverable |
|---|---|
| 1–2 | AIP Ontology wiring: register `GovernedOperation`, `InferenceRoute`, `RiskClassification` Object Types. Wire live ledger → AIP object sync. |
| 3–4 | AIP Evals integration: register `AIPEvalSample` type. Wire Reactor-Core DPO capture → AIP Evals pipeline. Validate with historical benchmark data. |
| 5–6 | DPO pipeline closure: complete fine-tuning loop. AIP Evals → J-Prime model update cycle. Measure pass-rate improvement on governance test suite. |
| 7–8 | DevCon demo: end-to-end demonstration of live governed inference → AIP Ontology → AIP Evals → model improvement cycle. |

---

## 3. J-Prime — Inference Infrastructure

J-Prime is the model inference plane. It runs on GCP and exposes an OpenAI-compatible API that JARVIS routes to via the Ouroboros ROUTE stage.

### 3.1 Hardware & Model Specifications

| Parameter | Value |
|---|---|
| GCP Instance | `g2-standard-4` |
| GPU | NVIDIA L4 (24 GB GDDR6) |
| Static IP | `136.113.252.164` (reserved) |
| Region | `us-central1-b` |
| Model | `Qwen2.5-Coder-14B-Instruct` |
| Artifact | `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` |
| Quantization | Q4_K_M (GGUF standard) |
| Context Window | 8,192 tokens |
| GPU Layers | -1 (all layers offloaded to L4) |
| Inference Backend | `llama-cpp-python` v0.3.16 with Metal/CUDA |
| API | OpenAI-compatible (`/v1/chat/completions`, `/v1/capability`, `/health`) |
| Startup Time | ~3 minutes (model load to first token) |

### 3.2 Empirically Verified Throughput

All performance data is measured from live production runs and stored in `benchmarks/history.json`. Benchmark data is never synthetic.

| Run | Infra Task Latency | Infra tok/s | Threat Task Latency | Threat tok/s | Governance Tests | Pass Rate |
|---|---|---|---|---|---|---|
| 2026-03-16T08-05-18 | 27,630 ms | ~24.6 | 5,334 ms | ~24.4 | — | — |
| 2026-03-16T06-32-13 | 26,602 ms | ~24.5 | 5,658 ms | ~24.2 | 2,132 | 99.3% |
| 2026-03-16T05-55-17 | 24,150 ms | ~24.6 | 5,997 ms | ~24.3 | 2,132 | 99.3% |
| 2026-03-14T19-28-19 | 25,665 ms | ~24.6 | 5,470 ms | ~24.3 | 2,132 | 99.3% |
| 2026-03-14T16-26-33 | 10,242 ms | ~24.4 | 5,424 ms | ~23.4 | — | — |

**Average throughput: 24.5 tok/s.** Latency variance is driven by prompt length and KV-cache state, not hardware instability. The L4 is consistent across all recorded runs.

### 3.3 HollowGuard — Hardware Admission Layer

HollowGuard is J-Prime's compute-class gating system. It enforces hardware requirements at boot time, not per-operation:

- Reads hardware profile via `psutil` at server startup
- Classifies instance as `FULL`, `CLOUD_ONLY`, or `CPU_ONLY`
- Requires `JARVIS_HARDWARE_PROFILE=FULL` env override on `g2-standard-4` because psutil reports 15.6 GB available RAM (< 16 GB threshold due to OS overhead)
- GPU brains in Ouroboros are gated by `compute_class != "cpu"` — CPU brains never attempt to use J-Prime's loaded model

This prevents silent degradation: if J-Prime's GPU is unavailable, the admission layer blocks GPU-class requests at the gate rather than routing them to an overloaded CPU path.

### 3.4 Model Artifact Resolution

J-Prime uses a symlink-based artifact management system. The active model is always `current.gguf` (symlink). The server resolves the real artifact name via `Path(_mp).resolve().name` to correctly report `Qwen2.5-Coder-14B-Instruct-Q4_K_M.gguf` in `/v1/capability` responses rather than the generic `current.gguf`.

---

## 4. JARVIS Kernel Architecture

JARVIS is the local execution supervisor. It is a ~73,000-line unified kernel (`unified_supervisor.py`) that coordinates all system components through an async event loop and structured IPC channels.

### 4.1 Agent Architecture

JARVIS hosts 50+ specialized autonomous agents organized into functional layers:

- **Brain Tier** (5 tiers governed directly by Ouroboros):
  - `phi3_lightweight` — fast, low-risk local tasks
  - `qwen_coder` — mid-tier code generation
  - `qwen_coder_14b` — primary inference tier (J-Prime)
  - `qwen_coder_32b` — high-complexity tasks
  - `deepseek_r1` — reasoning-intensive operations

- **Sensor Layer** (9 sensor classes): `TestFailureSensor`, `OpportunityMinerSensor`, `VoiceCommandSensor`, and others that feed signals into the Ouroboros intake layer. One `TestFailureSensor` + one `OpportunityMinerSensor` is instantiated per registered repository.

- **Tool Layer** (354 tool classes): Discrete capabilities invoked by agents within governed operations.

### 4.2 The Oracle — Semantic Context Engine

The Oracle is JARVIS's codebase indexer and semantic retrieval system. It runs as a background task (`_oracle_index_loop`) and continuously indexes all three repositories:

- Builds a structural graph of the codebase (`FileNeighborhood`) with 7 edge categories and up to 10 paths per category
- Used by the CONTEXT_EXPANSION stage to inject relevant file context into prompts
- Exposes `get_file_neighborhood()` for nearest-neighbor file retrieval
- Tracks `_last_indexed_monotonic_ns` and `index_age_s()` — pipeline warns if index is stale > 300 seconds
- Semantic fusion: combines structural graph-topology with content-based similarity for context selection

### 4.3 Circuit Breakers

Every major subsystem is wrapped in a circuit breaker:

```python
self._circuit_breakers[component].can_execute()  # → (bool, reason)
```

Circuit breakers use three states: CLOSED (healthy), OPEN (failing, reject all), HALF-OPEN (probe recovery). State transitions are emitted as `CircuitBreakerEvent` objects that map to AIP Ontology.

### 4.4 Trust Graduator

The trust graduator manages which operations are auto-approved vs. held for review, adapting over time based on operation history:

- Seeded at startup: `GOVERNED` trust for tests/docs, `OBSERVE` trust for core/root operations
- Four triggers × all repositories = progressive trust expansion
- Trust levels escalate (`OBSERVE → GOVERNED → TRUSTED`) as operations demonstrate clean verify results
- Trust graduation events become `TrustGraduation` AIP Ontology objects

### 4.5 Governance Service Wiring

The Governed Loop Service (`GovernedLoopService`) is registered at Zone 6.8 in the supervisor startup sequence. Zone 6.9 is the IntakeLayerService. Both re-raise `CancelledError` and log `CRITICAL` on failure — no silent degradation.

Governance mode is set via `JARVIS_GOVERNANCE_MODE=governed` in `.env`, with CLI `--governance-mode` arg as fallback.

The `brain_selection_policy.yaml` (v1.0) enforces:
- Boot-time handshake against `/v1/brains` inventory
- Hard fail if any required brain is missing from J-Prime
- Gate disabled (empty allowed set) if J-Prime is offline

---

## 5. Governance Test Coverage

### 5.1 What the 2,132 Tests Cover

The governance test suite spans two directories:
- `tests/test_ouroboros_governance/` — unit and integration tests for every pipeline stage
- `tests/governance/` — system-level governance behavior tests

Coverage categories:

| Category | What Is Tested |
|---|---|
| Risk Classification | All `risk_tier` assignments, blast radius scoring, SAFE_AUTO vs. NEEDS_APPROVAL boundaries |
| Routing Logic | PRIMARY/LOCAL/CLAUDE tier selection, fallback on J-Prime unavailability, routing decision record accuracy |
| Circuit Breaker FSM | CLOSED→OPEN→HALF-OPEN transitions, failure count thresholds, recovery probe behavior |
| Trust Graduator | All four trigger conditions, trust level escalation, OBSERVE→GOVERNED→TRUSTED paths |
| Gate Behavior | Auto-approval for SAFE_AUTO, hold behavior for NEEDS_APPROVAL, BLOCKED terminal state |
| Apply & Rollback | `_file_touch_cache` cooldown enforcement (3 touches / 10-min window), rollback hash integrity, SHA verification |
| FSM State Matrix | Full `LoopState × LoopEvent` transition coverage, durable-ledger-first write ordering |
| Noop Fast-Path | `2b.1-noop` schema detection, GENERATE→COMPLETE bypass, `is_noop` propagation |
| Context Expansion | Oracle readiness gate, stale index warning, max rounds and file limits |
| Multi-Repo Patches | `schema 2c.1` per-repo patch dict construction, `RepoPatch` object assembly |
| Voice Narration | Intent/decision/postmortem event types, debounce via `OUROBOROS_VOICE_DEBOUNCE_S` |
| Cross-Repo Saga | EventBridge → CommProtocol wiring, saga root resolution across repositories |

### 5.2 Known Pre-Existing Failures (9 tests, not governance regressions)

Nine tests fail consistently and pre-date recent governance work. These are structural test harness issues, not governance pipeline regressions:

| Test File | Root Cause |
|---|---|
| `test_preflight.py` | Uses `__new__` to bypass `__init__` — breaks singleton initialization |
| `test_e2e.py` | Requires live J-Prime endpoint, not available in CI |
| `test_pipeline_deadline.py` | Hard-coded timeout assumes specific hardware; fails on underpowered runners |
| `test_phase2c_acceptance.py` | Depends on multi-repo patch schema that requires initialized repo registry |

All 9 are known, documented, and excluded from the 2,132 passing test count. The 99.3% pass rate reflects the governance test suite excluding these structural harness failures.

---

## 6. Reactor-Core — The Continuous Learning Loop

Reactor-Core converts every governed production operation into a fine-tuning signal. It is the mechanism by which Trinity improves from deployment rather than degrading.

### 6.1 DPO Preference Pair Generation

When Ouroboros reaches `COMPLETE` on an `APPLIED` operation:

1. Reactor-Core receives the `TriggerDPOCapture` signal
2. It reads the full operation record from the ledger: `prompt`, `generation_result`, `risk_tier`, `validate_result`, `verify_result`
3. It constructs a preference pair:
   - **Chosen**: The output that passed all governance gates (VALIDATE → GATE → APPLY → VERIFY)
   - **Rejected**: The pre-validation draft (if the validator caught a flaw) or a governance-informed counterfactual
4. Pair is stored with `source_op_id` linking back to the originating ledger entry
5. Pair is emitted to the AIP Evals pipeline as `AIPEvalSample`

### 6.2 Signal Quality

DPO pairs generated by Reactor-Core have a property that synthetic training data cannot replicate: they are grounded in real governance decisions on real production workloads. The "chosen" output is not human-labeled — it is a signal that has passed a multi-stage automated governance pipeline that includes syntax validation, security scan, blast radius scoring, and post-apply verification. This is a stronger signal than human preference labeling for infrastructure and security code generation tasks.

---

## 7. Security Architecture & FedRAMP/IL5 Posture

### 7.1 Air-Gap Compatibility

Trinity is designed for air-gapped deployment:

- J-Prime runs fully on-prem or GCP private — no external API calls during inference
- The CLAUDE API fallback tier is optional and can be disabled entirely for classified environments
- Model artifacts are loaded from local disk — no external model hub dependencies at inference time
- The durable ledger is local filesystem — no external logging service required for operation continuity

### 7.2 Pre-Execution, Not Post-Execution

The architectural distinction between Trinity and standard observability-based governance:

| Standard Approach | Trinity Approach |
|---|---|
| Generate output, then log it | Classify request before any token is generated |
| Human reviews logs after the fact | GATE stage holds operation until approval signal received |
| Rollback is manual and forensic | Rollback hash written before APPLY; automatic on VERIFY failure |
| Audit trail created post-hoc | Ledger entry created at CLASSIFY, updated through every stage |

This matters in FedRAMP/IL5 contexts because post-execution logging is insufficient — you need to prevent non-compliant output from reaching any system, not log that it reached a system.

### 7.3 Blast Radius Scoring

Every operation receives a `blast_radius` score at CLASSIFY. This score quantifies the potential impact of the operation:

- How many files could be modified?
- How critical are those files (core kernel vs. test fixture vs. documentation)?
- Does the change touch inter-repo boundaries?
- Does the routing tier have fallback coverage if VERIFY fails?

Operations above the blast radius threshold are automatically escalated (`EscalateRisk` AIP Action) even if `risk_tier == SAFE_AUTO`.

### 7.4 Stateful Operation Tracking

Every operation has a unique `op_id` (format: `op-{source}-{timestamp}-{sequence}`) that tracks it from CLASSIFY through terminal state. There are no fire-and-forget operations in the Ouroboros pipeline — every request has a known state at all times.

---

## 8. Codebase Scale & Engineering Velocity

### 8.1 Repository Overview

| Repository | Primary Language | Purpose |
|---|---|---|
| `JARVIS-AI-Agent` | Python, Rust, C | Local kernel, Ouroboros pipeline, agent framework, voice biometrics |
| `jarvis-prime` | Python, CUDA | Model inference server, HollowGuard, OpenAI-compatible API |
| `reactor-core` | Python, Go | DPO pipeline, telemetry ingestion, preference pair generation |

**Combined: ~2.9 million lines of authored source code across 22+ programming languages.**

### 8.2 Development Velocity

- **5,400+ commits** across three repositories in 7 months
- **Solo founder** — no team, no contractors, no co-founder
- **Live production system** — not a prototype, not a demo environment. The GCP instance is running 24/7 with a reserved static IP and responds to real inference requests.
- **Pedigree**: 2x NASA software engineer. Letter of recommendation from Sam Altman.

### 8.3 What "Custom-Built" Means

Trinity's kernel is not a wrapper around LangChain, AutoGen, or CrewAI. The governance pipeline, FSM engine, durable ledger, circuit breakers, trust graduator, Oracle indexer, IPC protocol, and voice biometric authentication system are all authored from scratch. The only significant third-party dependency for the terminal UI layer is the `rich` library. The inference backend uses `llama-cpp-python` as a model-loading primitive, but all routing, governance, and telemetry logic above it is custom.

---

## 9. Live Demo Reference

The live demonstration (`demo_trinity_governed_loop.py`) runs all four capabilities in sequence using real J-Prime inference on GCP:

| Phase | What It Shows |
|---|---|
| Phase 1 — Live System Status | Connects to `/v1/capability` and `/health`. Displays real model metadata from GCP. Proves the instance is live, not a stub. |
| Phase 2 — Ouroboros Ledger | Reads durable ledger from `~/.jarvis/ouroboros/ledger/`. Shows governance operations, risk distribution bar chart, full ledger entry JSON, FSM pipeline trace, and AIP Ontology mapping table. |
| Phase 3 — Governed Inference | Runs two real inference tasks (secure infrastructure code + defense threat analysis) with full governance pipeline: pre-execution gate, live token streaming, routing metrics, post-execution validation with rollback hash. |
| Phase 4 — Test Suite | Runs 2,132 governance tests live as a subprocess. Rich live timer panel. Shows test/second throughput and pass rate. |
| Phase 5 — System Summary | Displays full Trinity architecture summary and persists benchmark data to `benchmarks/LATEST.md` and `benchmarks/history.json`. |

**Offline replay mode** (`--replay`) loads the last recorded run from `benchmarks/history.json` and replays all governance panels without a live GCP connection. All performance numbers displayed in replay mode are from real recorded runs, not hardcoded values.

---

## 10. Why AIP, Why Palantir

Trinity's governance primitives — durable ledger, typed operation records, FSM state tracking, blast radius scoring, approval workflows — are not post-hoc compatibility additions. They were designed to fit the shape of AIP's Ontology model because Palantir's approach to enterprise data (typed objects with properties, actions with consequences, pipelines with lineage) is the correct abstraction for governed AI operations.

AIP Agent Studio operates at the application layer. Trinity operates at the infrastructure layer — the layer that makes AIP Agent Studio deployable in classified environments. These are complementary, not competitive.

The 8-week sprint is not a proof-of-concept integration. The data structures already exist. The ledger entries already have the right fields. The AIP work is formalizing what Ouroboros already produces into registered Palantir Object Types and wiring the live sync. That is 8 weeks of engineering, not 8 weeks of design.

---

*Generated from live system state · March 2026 · JARVIS-AI-Agent commit `582ab46e`*
