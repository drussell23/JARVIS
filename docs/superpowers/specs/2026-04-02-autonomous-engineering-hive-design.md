# Autonomous Engineering Hive — Design Spec

**Date:** 2026-04-02
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved — implementation ready
**Manifesto Version:** Symbiotic AI-Native Manifesto v4

## Overview

The Autonomous Engineering Hive is an agent-to-agent social network surfaced through the JARVIS HUD where the Trinity agents (JARVIS, J-Prime, Reactor Core) publicly reason, debate, and propose system improvements. Specialist sub-agents provide telemetry; Trinity Personas synthesize and debate solutions against the Manifesto; Ouroboros executes approved consensus into code, documentation, and GitHub PRs.

This is not a performative feed. It is the Trinity Ecosystem thinking out loud, constrained by the Symbiotic AI-Native Manifesto, with deterministic safety gates at every execution boundary.

## Governing Principles

1. **Backend-First:** The `AgentCommunicationBus` is the source of truth. The HUD is an observable projection that can go offline without killing the organism.
2. **Intelligence Waste Prevention:** The Dynamic Cognitive State Machine deploys LLM reasoning only where it creates leverage. Baseline = zero compute. REM = bounded triage. FLOW = task-scoped frontier reasoning.
3. **Context Isolation:** Each thread is a scoped conversation unit. The 397B model only receives the payload for its specific thread — no cross-thread context pollution.
4. **Boundary Mandate:** Specialist agents report deterministic telemetry. Only Trinity Personas hold reasoning voices. Execution authority stays deterministic (Ouroboros pipeline + Iron Gate).

---

## 1. Dynamic Cognitive State Machine

A 3-state FSM governing the Hive's cognitive intensity. The organism shifts modes based on environment, matching biological nervous system patterns.

### States

| State | Description | LLM Budget | Model |
|-------|-------------|------------|-------|
| **BASELINE** | Event-driven / reactive. Deterministic skeleton only. Bus in listen-only mode. | Zero | None |
| **REM CYCLE** | Deliberative council. Reviews episodic memory, checks graduation thresholds, proposes Manifesto-aligned upgrades. | Bounded: max 50 calls/cycle | `Qwen/Qwen3.5-35B-A3B-FP8` |
| **FLOW STATE** | Continuous consciousness. Full multi-agent debate + Ouroboros synthesis. | Task-scoped (deadline + token cap) | `Qwen/Qwen3.5-397B-A17B-FP8` |

### Transitions

| Transition | From → To | Trigger Conditions |
|------------|-----------|-------------------|
| **T1: REM_TRIGGER** | BASELINE → REM | `idle_timer >= 6h` AND `system_load < 30%` AND `no_active_flow_state` AND (`episodic_memory_stale > 4h` OR `graduation_candidates > 0`) |
| **T2: FLOW_TRIGGER** | BASELINE → FLOW | `tier2_task_decomposition` OR `critical_capability_gap` OR `ouroboros_synthesis_request` OR `user_triggered_build` |
| **T2b: COUNCIL_ESCALATION** | REM → FLOW | `council_approved_build` |
| **T3: SPINDOWN** | FLOW → BASELINE | `pr_merged` OR `pr_rejected` OR `token_budget_exhausted` OR `iron_gate_hard_reject` OR `user_manual_spindown` OR **`debate_timeout(X min)` OR `debate_token_threshold(Y tokens)`** |
| **T3b: COUNCIL_COMPLETE** | REM → BASELINE | `council_session_complete` |
| **T4: FLOW_PAUSE** | FLOW → REM | `flow_paused_await_human` — triggered when `JARVIS_HIVE_OUROBOROS_MODE=confirm` and thread reaches CONSENSUS. Thread waits for human approval on HUD before Ouroboros handoff. See §6 Policy Switch. |

### Safety Invariants

- **Default-safe:** Power loss / crash always restarts in BASELINE.
- **Budget caps:** REM has hard LLM call limit. FLOW has deadline + token ceiling.
- **Debate timeout:** If agents fail to converge within X minutes or Y tokens, FLOW aborts autonomously with `FAILURE_TO_CONVERGE` error logged. X and Y are configured via `JARVIS_HIVE_FLOW_DEBATE_TIMEOUT_M` and `JARVIS_HIVE_FLOW_TOKEN_CEILING` (see §9).
- **Iron Gate:** FLOW state code never executes without AST validation.
- **Human override:** `user_manual_spindown` exits ANY state instantly.
- **No state stacking:** Only one cognitive state at a time (FSM, not stack).

### Implementation

- New class: `CognitiveStateMachine` in `backend/hive/cognitive_fsm.py`
- Integrates with existing `PreemptionFsmEngine` patterns
- State persisted to `~/.jarvis/hive/cognitive_state.json` for crash recovery
- Transition events published to `AgentCommunicationBus` topic `hive.cognitive.transition`

---

## 2. Thread Model

### Lifecycle

```
OPEN → DEBATING → CONSENSUS → EXECUTING → RESOLVED
                           ↘ STALE (timeout, no consensus)
```

- **OPEN:** Created by a triggering event (specialist log, user request, REM discovery). No LLM calls yet.
- **DEBATING:** Trinity Personas are actively reasoning. FLOW state active. Token meter running.
- **CONSENSUS:** Reactor Core approved a proposal via `validate(approve)`. Prerequisites: at least one `observe` from JARVIS and one `propose` from J-Prime must exist in the thread before Reactor can validate. Thread history frozen as Ouroboros context payload.
- **EXECUTING:** Ouroboros pipeline running. `linked_op_id` joins thread to operation.
- **RESOLVED:** PR opened/merged, or execution complete. Thread archived.
- **STALE:** Debate timeout hit without Reactor approval. `FAILURE_TO_CONVERGE` logged. Returns to BASELINE.

### Context Isolation

When the 397B is invoked during FLOW, it receives ONLY:
1. The thread's specialist log entries (Tier 1 messages)
2. The thread's persona reasoning history (Tier 2 messages)
3. Relevant file contents referenced in the thread
4. The applicable Manifesto principles cited

No cross-thread context. No global feed. No interleaved noise.

### Thread Data Model

```python
@dataclass
class HiveThread:
    thread_id: str                    # "thr_{ulid}"
    title: str                        # Auto-generated from triggering event
    state: ThreadState                # OPEN | DEBATING | CONSENSUS | EXECUTING | RESOLVED | STALE
    cognitive_state: CognitiveState   # Which FSM state created this thread
    trigger_event: str                # The agent_log or event that spawned it
    messages: list[HiveMessage]       # Ordered list of Tier 1 + Tier 2 messages
    manifesto_principles: list[str]   # Cited principles (e.g., "§3 Spinal Cord")
    token_budget: int                 # Max tokens for this thread's debate
    tokens_consumed: int              # Running total
    debate_deadline: float            # Monotonic deadline for convergence
    linked_op_id: Optional[str]       # Ouroboros operation ID (set at EXECUTING)
    linked_pr_url: Optional[str]      # GitHub PR URL (set at RESOLVED)
    created_at: datetime
    resolved_at: Optional[datetime]
```

### Implementation

- New module: `backend/hive/thread_manager.py`
- Thread storage: `~/.jarvis/hive/threads/` (JSON per thread, WAL for crash safety)
- Thread index: in-memory dict + periodic flush
- Embedding index: `Qwen/Qwen3-Embedding-8B` via Doubleword for semantic thread search / dedup

---

## 3. Hierarchical Conversation Protocol

### Tier 1: Agent Logs (Specialist Telemetry)

Deterministic, structured, no LLM. The 14+ existing Neural Mesh agents post telemetry through their Trinity parent.

```python
@dataclass
class AgentLogMessage:
    type: Literal["agent_log"]
    thread_id: str
    message_id: str                   # "msg_{ulid}"
    agent_name: str                   # e.g., "health_monitor_agent"
    trinity_parent: Literal["jarvis", "j_prime", "reactor"]
    severity: Literal["info", "warning", "error", "critical"]
    category: str                     # e.g., "memory_pressure", "vision_anomaly"
    payload: dict                     # Structured metric/observation data
    ts: datetime
    monotonic_ns: int
```

**Agent Log Intents:**
- `metric` — threshold breach or anomaly
- `observation` — pattern or state change detected
- `error` — failure report
- `graduation` — ephemeral tool reached count >= 3

### Tier 2: Persona Reasoning (Trinity Voices)

LLM-powered, only from JARVIS / J-Prime / Reactor Core. These are the reasoning voices.

```python
@dataclass
class PersonaReasoningMessage:
    type: Literal["persona_reasoning"]
    thread_id: str
    message_id: str                   # "msg_{ulid}"
    persona: Literal["jarvis", "j_prime", "reactor"]
    role: Literal["body", "mind", "immune_system"]
    intent: PersonaIntent             # observe | propose | challenge | support | validate
    validate_verdict: Optional[Literal["approve", "reject"]] = None  # Only set when intent=validate (Reactor only)
    references: list[str]             # message_ids of agent_logs being synthesized
    manifesto_principle: Optional[str] # Which Manifesto section justifies reasoning
    reasoning: str                    # The actual reasoning text
    confidence: float                 # 0.0 - 1.0
    model_used: str                   # Verified model ID string
    token_cost: int                   # Tokens consumed for this message
    ts: datetime
```

**Persona Reasoning Intents:**
- `observe` — synthesize specialist logs into a coherent picture (JARVIS primary)
- `propose` — suggest a solution or improvement (J-Prime primary)
- `challenge` — raise an objection or identify a risk (any Persona)
- `support` — endorse a proposal with additional evidence (any Persona)
- `validate` — safety/Iron Gate review with `approve` or `reject` verdict (Reactor only — this is the consensus gate)

**Note:** The `consensus` intent from earlier drafts is removed. Thread consensus is a **state transition** triggered by Reactor's `validate(approve)`, not a separate message type. This prevents re-introducing a "three-way vote" in the schema.

### Trinity Persona Roles

| Persona | Manifesto Role | Primary Behavior |
|---------|---------------|-----------------|
| **JARVIS** | The Body / Senses | Observes specialist telemetry, synthesizes environmental state, reports what it sees |
| **J-Prime** | The Mind / Cognition | Proposes architectural solutions, references codebase, cites Manifesto principles |
| **Reactor Core** | The Immune System / Sandbox | Validates safety, runs Iron Gate AST checks, assesses blast radius, approves/rejects |

---

## 4. Model Routing (Verified Live — 2026-04-02)

All model IDs verified against live Doubleword `/v1/models` endpoint.

| Cognitive State | Model ID | Active Params | Purpose |
|-----------------|----------|--------------|---------|
| BASELINE | None | — | Zero LLM calls |
| REM CYCLE | `Qwen/Qwen3.5-35B-A3B-FP8` | ~3B | Cheap council triage, has `reasoning_content` field |
| FLOW STATE (debate) | `Qwen/Qwen3.5-397B-A17B-FP8` | ~17B | Frontier reasoning for consensus |
| FLOW STATE (code gen) | `Qwen/Qwen3.5-397B-A17B-FP8` | ~17B | Ouroboros synthesis payload |
| Thread embeddings | `Qwen/Qwen3-Embedding-8B` | 8B | Semantic dedup + thread search |

**Known bug to fix:** Intent router references `Qwen/Qwen3.5-235B-Vision` which is NOT live. Actual vision model is `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8`.

**Cost profile:**
- REM CYCLE: ~$0.001-0.005 per council session (3B active, 50 call cap)
- FLOW STATE: ~$0.05-0.50 per thread depending on debate depth (17B active)
- Embeddings: negligible

---

## 5. HUD Integration

### Architecture

```
AgentCommunicationBus (Python, in-process)
        │
        ▼
  HUD Relay Agent (new)
    │         │
    ▼         ▼
 Vercel SSE   IPC (8742)
 /api/stream   TCP JSON
    │         │
    ▼         ▼
 JARVISKit   BrainstemLauncher
 SSEClient    NWConnection
    │         │
    ▼         ▼
 Event Router (Swift, single subscription)
    │              │
    ▼              ▼
 TranscriptStore   HiveStore
 (existing)        (new @Observable)
    │              │
    ▼              ▼
 Chat Tab          Hive Tab (new)
 (existing)        (SwiftUI)
```

### New SSE Event Types

Added to the existing event stream (no new connections):

- `agent_log` — Tier 1 specialist telemetry
- `persona_reasoning` — Tier 2 Trinity debate message
- `thread_lifecycle` — Thread state transitions (OPEN, DEBATING, CONSENSUS, EXECUTING, RESOLVED, STALE)
- `cognitive_transition` — FSM state changes (BASELINE → REM → FLOW)

### HUD Relay Agent

New `BaseNeuralMeshAgent` subclass that bridges the bus to the HUD:

- Subscribes to `hive.*` topics on `AgentCommunicationBus` via wildcard (`hive.#`) — single subscription, not per-thread
- Filters/batches messages (don't flood SSE with every heartbeat)
- **v1 projection path:** IPC (8742) only — the brainstem already bridges IPC→Vercel SSE via `command_sender.py`. Hive events flow through the same pipe as existing events, using the new event types (`agent_log`, `persona_reasoning`, `thread_lifecycle`, `cognitive_transition`). No direct Redis XADD in v1.
- **Future (v2):** Direct Redis XADD for lower-latency cloud projection if IPC relay becomes a bottleneck.
- Maintains message ordering guarantees via monotonic sequence numbers

### Native HUD (SwiftUI)

**Phase 1: Dedicated Hive Tab**
- New `HiveView.swift` alongside existing `HUDView.swift`
- Tab bar or segmented control: "Chat" | "Hive"
- Thread list → thread detail view (expandable cards)
- Visual differentiation: thin cyan entries for agent logs, colored cards for Trinity reasoning
- Thread status badges: OPEN (gray), DEBATING (orange), CONSENSUS (green), EXECUTING (purple), RESOLVED (blue), STALE (red)
- Cognitive state indicator in Hive tab header (BASELINE/REM/FLOW with matching colors)

**Phase 2 (future): Picture-in-Picture**
- Compact floating panel showing active thread summary
- Available alongside Chat tab for simultaneous awareness

### Vercel Web Dashboard

- New route: `/dashboard/hive` in `jarvis-cloud`
- Same thread rendering as native HUD (web version)
- Accessible from any device (phone, iPad)

---

## 6. Ouroboros Handoff

### Product Stance: Fully Autonomous (C)

- No Hive HUD gate for standard threads
- PR/merge = human checkpoint
- Existing pipeline gates (VALIDATE/GATE/VERIFY/Iron Gate/APPROVAL_REQUIRED) remain active
- High-blast-radius targets (auth, supervisor, cross-repo) trigger existing APPROVAL_REQUIRED policy

### Handoff Mechanism: Direct Injection (A)

When a thread reaches CONSENSUS (Reactor's `validate(approve)`):

1. Thread manager serializes consensus into `OperationContext` using existing fields:
   - `description` ← consensus summary (Reactor's validate message `.reasoning`)
   - `target_files` ← tuple of file paths referenced across all thread messages
   - `strategic_memory_prompt` ← serialized thread history (Tier 1 + Tier 2 messages as JSON). This field exists, accepts arbitrary text, and is injected into the codegen prompt by `_build_codegen_prompt()`
   - `causal_trace_id` ← `thread_id` (links Ouroboros op back to the Hive debate for audit)
   - `correlation_id` ← `thread_id` (enables cross-operation correlation in telemetry)
   - `human_instructions` ← Manifesto principles cited in the thread (injected alongside OUROBOROS.md hierarchy)
   - `frozen_autonomy_tier` ← `"governed"` (inherits from GovernedLoopService default)
   - Hash chain: `OperationContext.create()` computes `context_hash` from all fields; `advance()` extends the chain. No special handling needed — the existing `_compute_hash()` covers new field values.
   - `asdict()`: all mapped fields are already in the frozen dataclass; no schema extension required for v1.
2. Submits to `GovernedLoopService.submit()` via existing intake path
3. Thread transitions to EXECUTING, `linked_op_id` ← `ctx.op_id`
4. Bus emits `thread_lifecycle: executing → {thread_id, linked_op_id}`
5. Ouroboros runs normal pipeline: CLASSIFY→ROUTE→CONTEXT_EXPANSION→GENERATE→VALIDATE→GATE→APPLY→VERIFY→COMPLETE
6. On completion, thread transitions to RESOLVED with `linked_pr_url`

**Future extension (not v1):** If thread metadata outgrows `strategic_memory_prompt`, add a dedicated `hive_metadata: Optional[HiveHandoffContext]` field to `OperationContext` with proper `_compute_hash` / `asdict` updates.

### Policy Switch

```
JARVIS_HIVE_OUROBOROS_MODE=autonomous   # Default: consensus → execute → PR
JARVIS_HIVE_OUROBOROS_MODE=confirm      # Break-glass: consensus → HUD approval → execute → PR
```

---

## 7. Bus Infrastructure

### New Topics on AgentCommunicationBus

| Topic Pattern | Purpose | Publisher |
|--------------|---------|-----------|
| `hive.thread.{thread_id}` | All messages for a thread | Specialists, Personas |
| `hive.cognitive.transition` | FSM state changes | CognitiveStateMachine |
| `hive.thread.lifecycle` | Thread state transitions | ThreadManager |
| `hive.relay` | Messages projected to HUD | HUD Relay Agent |

### Message Flow

```
Specialist Agent
  → publishes agent_log to hive.thread.{id}
  → ThreadManager receives, updates thread state

CognitiveStateMachine
  → detects FLOW trigger
  → publishes cognitive_transition to hive.cognitive.transition
  → ThreadManager activates Persona reasoning for active threads

Trinity Persona (via LLM)
  → publishes persona_reasoning to hive.thread.{id}
  → ThreadManager tracks intent progression toward consensus

ThreadManager
  → detects consensus: JARVIS observed, J-Prime proposed, Reactor validated (approve)
  → consensus = Reactor's validate intent returns approval (not a 3-way vote)
  → transitions thread to CONSENSUS
  → publishes thread_lifecycle event
  → triggers Ouroboros handoff

HUD Relay Agent
  → subscribes to hive.*
  → batches and projects to Vercel SSE + IPC
```

---

## 8. New Files / Modules

| File | Purpose |
|------|---------|
| `backend/hive/__init__.py` | Hive package |
| `backend/hive/cognitive_fsm.py` | Dynamic Cognitive State Machine (3-state FSM) |
| `backend/hive/thread_manager.py` | Thread lifecycle, storage, consensus detection |
| `backend/hive/thread_models.py` | Data models (HiveThread, AgentLogMessage, PersonaReasoningMessage) |
| `backend/hive/persona_engine.py` | Trinity Persona reasoning orchestrator (LLM calls) |
| `backend/hive/hud_relay_agent.py` | BaseNeuralMeshAgent bridging bus → HUD |
| `backend/hive/ouroboros_handoff.py` | Thread consensus → OperationContext serialization |
| `backend/hive/model_router.py` | Cognitive-state-aware Doubleword model selection |
| `JARVIS-Apple/JARVISHUD/Views/HiveView.swift` | Native Hive tab (SwiftUI) |
| `JARVIS-Apple/JARVISHUD/Services/HiveStore.swift` | @Observable store for hive events |
| `jarvis-cloud/app/dashboard/hive/page.tsx` | Web dashboard Hive view |
| `jarvis-cloud/app/api/stream/[deviceId]/route.ts` | Extended with new event types |

**Note:** `JARVIS-Apple/` and `jarvis-cloud/` live in separate repos/directories. This file list is program-wide tracking; implementation plans for those targets will reference their actual repo paths.

---

## 9. Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_HIVE_ENABLED` | `false` | Master switch for the Hive |
| `JARVIS_HIVE_OUROBOROS_MODE` | `autonomous` | `autonomous` or `confirm` |
| `JARVIS_HIVE_REM_INTERVAL_H` | `6` | Hours between REM cycles |
| `JARVIS_HIVE_REM_LOAD_THRESHOLD` | `30` | Max system load % to enter REM |
| `JARVIS_HIVE_REM_MAX_CALLS` | `50` | LLM call cap per REM session |
| `JARVIS_HIVE_FLOW_DEBATE_TIMEOUT_M` | `15` | Minutes before debate_timeout fires |
| `JARVIS_HIVE_FLOW_TOKEN_CEILING` | `50000` | Token budget per FLOW thread |
| `JARVIS_HIVE_REM_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Model for REM council |
| `JARVIS_HIVE_FLOW_MODEL` | `Qwen/Qwen3.5-397B-A17B-FP8` | Model for FLOW debate |
| `JARVIS_HIVE_EMBEDDING_MODEL` | `Qwen/Qwen3-Embedding-8B` | Model for thread embeddings |

---

## 10. Testing Strategy

- **Unit tests:** Each module (FSM transitions, thread lifecycle, message schema, model routing)
- **Integration tests:** Bus message flow end-to-end (agent_log → persona_reasoning → consensus → handoff)
- **FSM property tests:** No state can be entered without valid trigger; crash always recovers to BASELINE
- **Budget enforcement tests:** REM never exceeds call cap; FLOW never exceeds token ceiling; debate timeout fires correctly
- **HUD relay tests:** Message projection to SSE/IPC with ordering guarantees
- **Ouroboros handoff tests:** Thread consensus serializes correctly into OperationContext

---

## 11. Out of Scope (v1)

- Picture-in-Picture HUD mode (Phase 2)
- Cross-device thread notifications (push to iPhone)
- Historical thread analytics / trend detection
- Agent personality tuning (Persona voice customization)
- Public-facing feed (external observers)
