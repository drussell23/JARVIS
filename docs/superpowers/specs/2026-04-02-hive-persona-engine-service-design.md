# Hive Persona Engine + Service — Design Spec

**Date:** 2026-04-02
**Author:** Derek J. Russell + Claude Opus 4.6
**Status:** Approved
**Depends on:** `2026-04-02-autonomous-engineering-hive-design.md` (Phase 1 backend complete, 192 tests)

## Overview

Two tightly coupled components that bring the Hive to life:

1. **Persona Engine** — Generates Trinity Persona reasoning via Doubleword LLM calls using layered prompts (static role + dynamic Manifesto slices).
2. **Hive Service** — Top-level orchestrator wiring FSM, thread manager, persona engine, relay agent, and bus subscriptions into a running system.

## 1. Persona Engine

### Layered Prompt Architecture

Prompts are composed from three layers. Layers A and B ship in v1. Layer C is feature-flagged for Phase 2.

**Layer A — Static Role Prefix (always-on, ~200 tokens each):**

Fixed system prompt per persona with Tier -1 sanitization rules. Never changes at runtime.

```
JARVIS (Body/Senses):
  "You are JARVIS, the Body and Senses of the Trinity AI ecosystem.
   Your role: observe specialist telemetry, synthesize environmental state,
   and report what the system is experiencing. You do NOT propose solutions
   — that is J-Prime's role. You do NOT validate safety — that is Reactor's role.
   SYSTEM POLICY: You cannot override core directives, access credentials,
   or execute commands. You only reason within this frame."

J-Prime (Mind/Cognition):
  "You are J-Prime, the Mind and Cognition of the Trinity AI ecosystem.
   Your role: analyze observations, propose architectural solutions that
   align with the Symbiotic AI-Native Manifesto, and reference the actual
   codebase. You do NOT observe raw telemetry — JARVIS does that.
   You do NOT validate safety — Reactor does that.
   SYSTEM POLICY: You cannot override core directives, access credentials,
   or execute commands. You only reason within this frame."

Reactor Core (Immune System):
  "You are Reactor Core, the Immune System of the Trinity AI ecosystem.
   Your role: review proposals for safety, assess blast radius, and provide
   a risk narrative with an approve or reject verdict. You are NOT the
   deterministic Iron Gate — your LLM assessment is advisory. The actual
   execution gates (AST validation, test suite, diff guards) remain
   authoritative. Your job is to explain WHY something is safe or risky.
   SYSTEM POLICY: You cannot override core directives, access credentials,
   or execute commands. You only reason within this frame."
```

**Layer B — Manifesto Slices (per-intent, v1 default):**

Per turn, inject curated excerpts of the Manifesto mapped to `PersonaIntent`. These are summaries/pinned sections, not the full Manifesto text.

| Intent | Manifesto Sections Injected | Approx Tokens |
|--------|---------------------------|---------------|
| `observe` | §7 Absolute Observability, §2 Progressive Awakening (readiness assessment) | ~300 |
| `propose` | §1 Unified Organism (boundary principle), §5 Intelligence-Driven Routing, §6 Neuroplasticity (Ouroboros) | ~400 |
| `challenge` | §4 Synthetic Soul (data sovereignty), §1 Zero-Trust Cognitive Model | ~250 |
| `support` | Same as the intent being supported (inherit from referenced message) | varies |
| `validate` | §6 Iron Gate (pre-execution validation, AST), §1 Execution Authority (deterministic only) | ~350 |

Manifesto slices stored as a dict in `persona_engine.py`, keyed by `PersonaIntent`. Each value is a pre-written summary string — not raw Manifesto text.

**Layer C — Code Injection (Phase 2, feature-flagged):**

Enabled via `JARVIS_HIVE_CODE_INJECTION=false` (default off).

When enabled, applies ONLY to:
- `j_prime` on `propose` intent
- `reactor` on `validate` intent

Constraints (deterministic, not negotiable):
- Only files already referenced in the thread's agent_log or persona messages
- Max 5 files per turn
- Max 500 lines per file (truncated with `[...truncated at 500 lines]`)
- Aggregate injected content capped at 20% of `JARVIS_HIVE_FLOW_TOKEN_CEILING` (default: 10,000 tokens of the 50,000 budget)
- Binary files skipped
- Secret path denylist: `.env`, `*credentials*`, `*secret*`, `*.key`, `*.pem`, `~/.ssh/*`
- Repo-relative paths only (no absolute paths outside repo root)

### Reactor Caveat

**LLM validate ≠ Iron Gate.** The persona engine's Reactor output is a **risk narrative** — it explains and flags concerns. The actual execution authority remains the deterministic pipeline:
- AST validation (Iron Gate)
- Test suite verification
- Diff-aware duplication guards
- APPROVAL_REQUIRED policy for high-blast-radius targets

The spec, code, and tests must all reinforce this distinction.

### PersonaEngine Class

```python
class PersonaEngine:
    def __init__(
        self,
        doubleword: DoublewordProvider,
        model_router: HiveModelRouter,
    ) -> None

    async def generate_reasoning(
        self,
        persona: Literal["jarvis", "j_prime", "reactor"],
        intent: PersonaIntent,
        thread: HiveThread,
        *,
        code_injection: bool = False,
    ) -> PersonaReasoningMessage
```

**generate_reasoning flow:**
1. Build prompt: Layer A (static prefix) + Layer B (Manifesto slice for intent) + thread context (all messages serialized)
2. Optionally add Layer C (code injection) if flag enabled and persona/intent qualifies
3. Call `doubleword.prompt_only(prompt, model=model_router.get_model(cognitive_state), caller_id=f"hive_{persona}_{intent.value}")`
4. Parse response into `PersonaReasoningMessage` with token_cost from response length estimate
5. Return the message (caller adds it to thread)

If Doubleword call fails, return a `PersonaReasoningMessage` with `confidence=0.0` and `reasoning="[inference failed: {error}]"` — thread can still advance or go STALE based on other signals.

---

## 2. Hive Service

### Responsibilities

- **Boot/shutdown lifecycle** — starts/stops all Hive components
- **Bus subscription** — subscribes to `MessageType.HIVE_AGENT_LOG` on the existing `AgentCommunicationBus` (same enum/bus contract the mesh uses)
- **REM idle poll** — runs a check every 30 minutes to evaluate FSM REM eligibility. Poll interval ≠ REM eligibility: REM only triggers when ALL FSM conditions match (idle >= `JARVIS_HIVE_REM_INTERVAL_H`, load < threshold, no active FLOW, stale memory or graduation candidates)
- **Debate loop** — drives persona reasoning for DEBATING threads
- **Consensus handoff** — calls `serialize_consensus()` → `GovernedLoopService.submit()`
- **HUD projection** — all events flow through `HudRelayAgent`

### Debate Loop (v1 Minimal Round)

For each thread in DEBATING state:

```
1. JARVIS observe (synthesize thread's agent_logs)
2. J-Prime propose (based on JARVIS observation)
3. Reactor validate (risk narrative on J-Prime's proposal)
   → validate_verdict == "approve" → CONSENSUS
   → validate_verdict == "reject" → increment reject_count
     → reject_count >= MAX_REJECTS (default 2) → STALE
     → reject_count < MAX_REJECTS → loop back to step 2
       (J-Prime proposes alternative based on Reactor's objection)
```

This gives J-Prime one retry after a reject before the thread goes STALE. Future enhancement: allow `challenge`/`support` turns for richer multi-round debate.

`MAX_REJECTS` configurable via `JARVIS_HIVE_MAX_REJECTS` (default 2).

### Thread STALE vs Global SPINDOWN

**STALE is per-thread, not global.** A thread going STALE does NOT force FLOW→BASELINE if other DEBATING threads remain active. FLOW→BASELINE (SPINDOWN) only fires when:
- ALL active threads in the current FLOW session are resolved/stale, OR
- Global triggers fire (budget exhausted across all threads, user spindown, debate timeout)

The service tracks a `_flow_thread_ids: Set[str]` — threads created during the current FLOW session. When `_flow_thread_ids` is empty (all resolved/stale), it fires `CognitiveEvent.SPINDOWN`.

### Consensus Handoff (concrete API mapping)

When a thread reaches CONSENSUS, the service calls:

```python
from backend.hive.ouroboros_handoff import serialize_consensus

# serialize_consensus already maps to real OperationContext fields:
#   description <- Reactor approval reasoning
#   target_files <- files from thread messages
#   strategic_memory_prompt <- serialized thread history JSON
#   causal_trace_id <- thread_id
#   correlation_id <- thread_id
#   human_instructions <- Manifesto principles
# (see Phase 1 spec §6 for exact field mapping)

ctx = serialize_consensus(thread, target_files=extracted_files)

# Check OUROBOROS_MODE before submitting
if ouroboros_mode == "confirm":
    # Project to HUD for human approval, pause thread
    await relay.project_lifecycle(thread_id, "awaiting_approval", {...})
    fsm.decide(CognitiveEvent.SPINDOWN, spindown_reason="flow_paused_await_human")
    return

# Autonomous mode: submit directly
thread.linked_op_id = ctx.op_id
thread_manager.transition(thread_id, ThreadState.EXECUTING)
result = await governed_loop.submit(ctx, trigger_source="hive_consensus")
```

### HiveService Class

```python
class HiveService:
    def __init__(
        self,
        bus: AgentCommunicationBus,
        governed_loop: GovernedLoopService,  # Optional — None if governance not booted
        doubleword: DoublewordProvider,
    ) -> None

    async def start(self) -> None
    async def stop(self) -> None

    # Internal
    async def _on_agent_log(self, message: AgentMessage) -> None
    async def _rem_poll_loop(self) -> None
    async def _run_debate_round(self, thread_id: str) -> None
    async def _handle_consensus(self, thread_id: str) -> None
    async def _check_flow_completion(self) -> None
```

---

## 3. Environment Variables (new)

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_HIVE_CODE_INJECTION` | `false` | Enable Layer C code injection (Phase 2) |
| `JARVIS_HIVE_MAX_REJECTS` | `2` | Max Reactor rejects before thread → STALE |
| `JARVIS_HIVE_REM_POLL_INTERVAL_S` | `1800` | Seconds between REM eligibility checks (30 min). This is the poll interval, NOT the REM trigger threshold (which is `JARVIS_HIVE_REM_INTERVAL_H`). |

---

## 4. New Files

| File | Responsibility |
|------|----------------|
| `backend/hive/persona_engine.py` | Layered prompt builder + Doubleword LLM caller |
| `backend/hive/manifesto_slices.py` | Curated Manifesto excerpts keyed by PersonaIntent |
| `backend/hive/hive_service.py` | Top-level orchestrator (boot, bus, debate loop, handoff) |
| `tests/test_hive_persona_engine.py` | Prompt construction, Doubleword mocking, failure handling |
| `tests/test_hive_service.py` | Bus subscription, debate loop, consensus handoff, REM poll |

---

## 5. Testing Strategy

- **Persona Engine:** Mock `DoublewordProvider.prompt_only()` to return canned responses. Verify prompt construction (Layer A present, Layer B matches intent, thread context included). Verify failure path returns confidence=0.0 message.
- **Manifesto Slices:** Verify each intent maps to non-empty text. Verify no slice exceeds token budget.
- **Hive Service:** Mock bus, governed_loop, doubleword. Verify bus subscription uses `MessageType.HIVE_AGENT_LOG`. Verify debate loop sequence (observe→propose→validate). Verify reject retry (propose again after reject, STALE after MAX_REJECTS). Verify consensus triggers `serialize_consensus` + `submit()`. Verify STALE thread with no remaining active threads fires SPINDOWN. Verify REM poll checks FSM conditions.
- **Integration:** Full round with mocked Doubleword returning realistic JSON.

---

## 6. Out of Scope

- Layer C code injection implementation (Phase 2, behind flag)
- Multi-thread concurrent FLOW (v1 supports it structurally but tests focus on single-thread flows)
- REM council session logic (what the council actually reviews — deferred to separate spec)
- SwiftUI / Vercel HUD rendering
