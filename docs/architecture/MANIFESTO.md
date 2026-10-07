# Symbiotic AI-Native Manifesto (full text)

> Preserved verbatim from the README as of bc4c338dd8. The README carries the condensed version that `StrategicDirectionService` injects at boot; this is the complete original, including the zero-shortcut mandate and the five execution contexts.

## Symbiotic AI-Native Manifesto: Trinity Ecosystem Unification

This initiative is **not a software refactor**. It is the genesis of an autonomous, self-evolving **Artificial Intelligence Operating System**. The project explicitly rejects the rigid paradigms of traditional software engineering in favor of a symbiotic design: **the models and agents carry intelligence; the codebase is the nervous system** that connects senses, cognition, and safe execution.

### What we abandon

- Hardcoded business logic expressed as long `if` / `elif` routing trees
- Static heuristics that break the first time a user goal does not match a pre-enumerated case
- “Agent sprawl” — dozens or hundreds of narrow specialist classes that duplicate similar behaviors with slightly different code paths
- Shortcuts: retry spam, brute-force patches, and symptom-level fixes that leave structural disease in place

### What we build instead

- **Deep asynchrony** — true parallelism across senses, cognition, and UI; no accidental blocking of the organism
- **Mathematical and semantic governance** — thresholds, entropy-aware signals, and model-driven planning instead of string-matching routers
- **Cross-repo contract integrity** — JARVIS (Body), J-Prime (Mind), and Reactor Core (Immune System) behave as one organism, not three glued scripts

### The tri-partite organism

| Layer | Repository | Role | Analogy |
|---|---|---|---|
| **Senses / Body** | **JARVIS** (this repo) | Screen, audio, input automation, APIs, supervisor | Peripheral and motor nervous system |
| **Mind / Cognition** | **J-Prime** | Model serving, reasoning, brain policy, LangGraph | Executive and associative cortex |
| **Immune system / Sandbox** | **Reactor Core** | Isolated validation of generated code, training lineage, probation | Adaptive immunity — test before merge |

The Mind is not “imported” into the Body as a Python library. The Body talks to the Mind over **HTTP and WebSocket** with explicit contracts (`MindClient`, `PrimeRouter`), so each subsystem can evolve, scale, and fail independently without corrupting the whole.

### The seven principles

**1. The unified organism (tri-partite microkernel)**  
The ecosystem awakens through a **single authoritative entry point**: `python3 unified_supervisor.py`. The supervisor is not a dumb process launcher; it is the **kernel** that coordinates local edge (senses), cloud cognition (J-Prime), and sandboxed execution (Reactor / Docker) with consistent lifecycle and health semantics.

**2. Progressive awakening (adaptive lifecycle)**  
Boot is **not** a single long blocking chain. Local senses (UI, voice, capture) should reach **ACTIVE_LOCAL** quickly so the host can interact while heavy resources (e.g. GCP J-Prime) spin up **asynchronously**. UI readiness must track **real async resolution** — never a cosmetic “ready” that lies about backend state.

**3. Asynchronous tendrils (disciplined concurrency)**  
Foreground work, background exploration, and telemetry must use **structured concurrency** (`asyncio` task groups, gathering, cancellation discipline). Background **tendrils** must not starve the event loop, leak context, or corrupt shared memory assumptions.

**4. The synthetic soul (Trinity consciousness)**  
The system maintains **episodic awareness** of what worked and what failed. Components such as the **ConsciousnessBridge** (and related memory / telemetry paths) observe outcomes so the organism can detect **gaps in its own competence**. When statistical signals (e.g. entropy / uncertainty) show persistent ignorance, that drives **exploration and evolution** — not silent failure.

**5. Intelligence-driven routing (the cognitive forge)**  
Routing, planning, and decomposition are **agentic and semantic**, not regex catalogs. **Intent classification** and **brain selection** evaluate requests against policy and capability, then route to the right **model tier** and tools. Complex work is expressed as **DAG-shaped plans** (directed acyclic graphs) rather than linear scripts.

**6. Threshold-triggered neuroplasticity (Ouroboros)**  
When the organism hits a **capability gap**, it must synthesize a **just-in-time** response:

- **Ephemeral tools** — one-off generated code or scripts run in the **sandbox** (Reactor Core), then discarded.
- **Persistent assimilation** — if the same ephemeral capability is exercised successfully and **repeated** (e.g. graduation threshold such as **count ≥ 3**), **GraduationOrchestrator** proposes **permanent** integration: tests, validation, and a **secure Git PR** into the OS DNA.
- **Unified Event Spine** — a `FileWatchGuard` (watchdog) watches the repo root and publishes file change events to `TrinityEventBus`. Intake sensors react to code changes, test results, and git commits in **sub-second time** instead of polling. The event spine bridges `GapSignalBus`, `EventEmitter`, and `EventChannelServer` into a single topic-based pub-sub system (MQTT-style wildcards, priority queues, WAL persistence, cross-repo transport).
- **Adaptive provider routing + 6-layer cost optimization** — DoubleWord 397B now supports both batch and **real-time** (`/v1/chat/completions`) with full Venom tool loop at $0.10/$0.40/M — 30-37x cheaper than Claude. Claude fallback uses **prompt caching** (90% input savings). Smart `max_tokens` during tool rounds (1024 vs 8192). Prompt compression (20KB max per file vs 65KB). Complexity routing skips Venom for trivial tasks. **50-150+ operations per $0.50 budget** (vs 5-15 before optimization). Hard timeout enforcement at every layer: `asyncio.wait_for` on generation (180s + 5s grace), fallback provider cap (60s max), Tier 1 reserve rebalanced (25s minimum), DW poll interval tuned (5s). Python 3.9+ compatible — no `asyncio.timeout` (3.11+) anywhere.
- **Venom: Agentic Execution** — The `ToolLoopCoordinator` transforms Ouroboros from a one-shot patch generator into a multi-turn agentic loop. During generation, the provider can call `read_file`, `search_code`, `run_tests`, and `get_callers` — reading the codebase, running tests, and revising its output across multiple turns. When validation fails, the **L2 Repair Engine** takes over with an iterative `generate → test → classify failure → revise` loop (up to 5 iterations, 120s timebox). Policy enforcement via `GoverningToolPolicy` ensures all tool calls stay within repo boundaries.
- **SemanticTriage: Pre-Generation Intelligence** — The `SemanticTriageEngine` runs a lightweight 35B model analysis **before** expensive generation to classify whether the target change is already implemented (`NO_OP`), targets the wrong file (`REDIRECT`), needs additional context (`ENRICH`), or can proceed (`GENERATE`). This eliminates wasted generation cycles on no-op changes and redirects misrouted signals, reducing pipeline cost by filtering before the most expensive phase.
- **5-Phase Communication Protocol** — The `CommProtocol` enforces structured observability across every operation: **INTENT** (what we're about to do) → **PLAN** (how we'll do it) → **HEARTBEAT** (progress with phase, percentage, and subsystem metadata) → **DECISION** (outcome with rationale) → **POSTMORTEM** (full diagnostic on failure). Fault-isolated transports, idempotency keys, global sequence numbers, and correlation IDs provide runbook-grade cross-operation tracing. Transports are pluggable; the battle-test harness routes messages to the SerpentFlow CLI surface.
- **Trinity Consciousness: Metacognition** — The self-awareness layer (Zone 6.11) with 4 core engines (HealthCortex, MemoryEngine, DreamEngine, ProphecyEngine) and 3 fusion engines (CAI, SAI, UAE). **MemoryEngine** records operation outcomes and builds per-file reputation (success rate, fragility score). **ProphecyEngine** predicts regression risk. **DreamEngine** waits for 5 readiness gates (J-Prime health, user idle, VM warmth, resource governor, daily budget) before speculatively generating improvement blueprints during idle GPU time — with preemption, flap damping, and persistent state. **ConsciousnessBridge** injects this intelligence into the pipeline: regression assessment at CLASSIFY, fragile file context at GENERATE RETRY, outcome recording at POST-APPLY. The organism **learns from its own history**.
- **Strategic Direction Awareness** — The `StrategicDirectionService` reads the Manifesto (README.md) and architecture docs on boot, extracts the 7 core principles, and injects them into every operation's generation prompt. The organism understands the developer's architectural vision and generates Manifesto-compliant code — async patterns, cross-repo integrity, observability, structural repair over shortcuts.
- **IntentDiscoverySensor: Purpose-Driven Exploration** — The 16th sensor combines StrategicDirection principles, DreamEngine blueprints, and Oracle semantic search to synthesize concrete code improvement intents aligned with the developer's architectural vision. Unlike reactive sensors that wait for failures, IntentDiscoverySensor **proactively discovers** what the organism should improve next. Human acknowledgment is required (AC2 safety gate) before any discovered intent enters the pipeline.
- **23 Autonomous Sensors** — The intake layer now deploys 23 specialized sensors: `TestFailureSensor`, `VoiceCommandSensor`, `OpportunityMinerSensor`, `CapabilityGapSensor`, `ScheduledTriggerSensor`, `BacklogSensor`, `RuntimeHealthSensor`, `WebIntelligenceSensor`, `PerformanceRegressionSensor`, `DocStalenessSensor`, `GitHubIssueSensor` (polls Trinity repos for bugs/features), `CrossRepoDriftSensor` (detects API/contract drift between JARVIS/J-Prime/Reactor), `ProactiveExplorationSensor` (curiosity-driven entropy-based exploration), `TodoScannerSensor`, `CUExecutionSensor`, and `IntentDiscoverySensor`. All signals flow through the `UnifiedIntakeRouter` with priority queuing, deduplication, and WAL persistence.
- **Streaming output** — Both DW and Claude stream tokens character-by-character to the Rich TUI as code is being generated — like Claude Code shows its output appearing in real-time. SSE parsing for DW, `client.messages.stream()` for Claude.
- **Terminal surface: SerpentFlow + a composable TUI layer** — the operator-facing renderer is `battle_test/serpent_flow.py`, a flowing Claude-Code-style transcript rather than a pinned table. Around it sit single-purpose modules: `stream_renderer.py` (GENERATE token streaming via Rich `Live`, 16ms batched), `diff_preview.py` (Yellow-tier `NOTIFY_APPLY` overlay), `status_line.py` + `live_status_line.py` (phase/cost/idle aggregation into a persistent bottom toolbar), `layout_controller.py` + `split_layout.py` (flow / split / focus modes), `ouroboros_tui.py` (Panel-based op lifecycle), and `op_block_buffer.py` (collapsible op blocks with `o-N` refs). Three-channel terminal muting runs throughout, preventing Rich cursor corruption from third-party output.

  > An earlier `live_dashboard.py` provided a pinned Rich `Live` table. It was **deleted** in `481b430745` (UI Slice 2) in favour of the flowing surface above; this README described it for three months afterwards, which is the kind of drift `scripts/readme_stats.py` and the verification pass below exist to catch.
- **Battle Test Runner** — `scripts/ouroboros_battle_test.py` boots the full 6-layer stack (GovernedLoopService, IntakeLayer, Consciousness, StrategicDirection, CommProtocol, SerpentFlow). SerpentFlow renders a flowing transcript with provider badges, tool-call counts, colored diffs, streaming code output, and real-time phase progression. Every commit signed `Generated-By: Ouroboros + Venom + Consciousness`. Hard timeout enforcement at every pipeline layer prevents generation hangs. The battle test is the empirical validator for every capability on this page — no claim is considered true until the harness reproduces it end-to-end.

- **Battle Test Breakthrough — 2026-04-11: First Sustained Full-Pipeline Completion** — Session `bt-2026-04-11-154947` marked the first autonomous op to traverse the governance pipeline end to end (CLASSIFY → ROUTE → GENERATE → VALIDATE → GATE → APPLY → VERIFY, then L2 repair and POSTMORTEM on the failure path) since the Iron Gate regime was tightened in the Apr 9-10 push. Op `op-019d7d3e` (Python dependency upgrade in `requirements.txt`, SAFE_AUTO, critical urgency, runtime_health sensor) was autonomously generated, rejected on attempt 1 by the `dependency_file_integrity` Iron Gate catching a hallucinated `anthropic → anthropichttp` rename, regenerated with episodic-failure feedback injected, applied to disk (DECISION `applied/safe_auto_passed`), then post-APPLY-verified by the InfrastructureApplicator triggering a real `pip install` (SUCCESS in 4.7s). Verification correctly rerouted a 0/0-test result to L2 repair, which escaped to POSTMORTEM rather than brute-forcing a false pass. Three workers completed their assigned operations in the same session (402s, 365s, 11s); zero `client has been closed` errors across 119KB of debug log — the captured-client race that had blocked every prior session was empirically resolved. Root cause was closure-capture of `self._client` in `_do_stream`/`_create_with_prefill_fallback`/`_legacy_create`/`_plan_create`: when a hard-pool signal (`RemoteProtocolError`, `ReadError` unwrapped from `APIConnectionError`) triggered `_recycle_client()` mid-backoff, the `_call_with_backoff` retry re-ran the inner function but reused the closed client. Fix: re-acquire `self._client` at the top of each attempt so every retry sees the current generation. Iron Gate's `dependency_file_integrity` check (added to guard against hallucinated package renames) fired on its first production exposure, vindicating the Manifesto §6 Iron Discipline principle — the exploration-first + ASCII-strictness + dependency-integrity stack is working as a layered defence against model failure modes. Full postmortem in `docs/architecture/OUROBOROS.md#battle-test-breakthrough-log`.

**7. Absolute observability (systemic transparency)**  
Autonomous decisions must be **visible**. A **TelemetryBus** (and related emitters) broadcasts decisions, state transitions, and errors into **live dashboards** and, where appropriate, **voice narration** — the circulatory and reporting layer of the symbiote.

### The zero-shortcut mandate

**No shortcuts whatsoever.** No brute-force retries without diagnosis. No hardcoded routing tables that encode product policy. No sequential bottlenecks that exist only because “it was easier.” If a subsystem fails or hangs, the response is **structural repair** — dismantle the flawed assumption and rebuild — not a bypass that hides the bug. We do not attempt to code every future task explicitly; we code **the entity that can survive novelty**.

### Five core execution contexts + Symbiotic Router

Large reasoning models collapse many narrow “specialist agents” into **one mind** that adapts to the task. The codebase exposes **five execution contexts** (Brain) differentiated by tools and sandbox boundaries, backed by the legacy **Neural Mesh** (Peripheral Nervous System) as a Strangler Fig fallback — 24 classes extend its base, of which 16 are named `*Agent` and the remainder are adapters and services. (The long-standing "22 agents" figure matched neither count and has been replaced with the definition that produces one.)

| Context | Responsibility | Typical model tier (policy-driven) |
|---|---|---|
| **Executor** | Sees the screen, clicks, types, navigates apps — **motor loop** | Vision-capable / multimodal tier |
| **Architect** | Decomposes goals into **DAGs**, assigns steps, reconciles failures | Highest reasoning tier |
| **Developer** | Reads and writes code across repos, tests, PR flow | Same reasoning / coding tier as policy allows |
| **Communicator** | Email, calendar, messaging — protocol-heavy I/O | Fast tier by default; heavy tier when ambiguity is high |
| **Observer** | Monitors screen / logs, anomaly detection, briefings | Vision or fast tier depending on sensitivity |

**Symbiotic Router (`backend/core_contexts/facade.py`) — 3-tier dispatch:**

```
Tier 1: Core Contexts (Brain)
  │ Feature-flagged per vertical (JARVIS_CTX_EXECUTOR=true, etc.)
  │ 397B Architect routes goals to the appropriate context
  │ SUCCESS → return result
  │ FAIL/DISABLED → fall through ↓
  │
Tier 2: Legacy Agents (Peripheral Nervous System)
  │ 22 Neural Mesh agents (30K+ lines of production code)
  │ GoogleWorkspaceAgent (6.7K lines), VisualMonitorAgent (11K lines), etc.
  │ Keyword-based agent selection + lazy instantiation
  │ SUCCESS → return result
  │ FAIL → fall through ↓
  │
Tier 3: Ouroboros Neuroplasticity (Pillar 6)
    CapabilityGapEvent emitted to GapSignalBus
    GraduationOrchestrator JIT-synthesizes the missing capability
    “I'm learning how to do this.”
```

The **Strangler Fig pattern** enables incremental migration: flip one vertical at a time, verify it works, then flip the next. Legacy agents stay alive as Tier 2 fallback until the Core Context fully absorbs their logic. No big bang. No deletion of working code.

### Example: one goal, multiple contexts (Ouroboros-aligned)

**User:** “Refactor authentication to OAuth2.”

1. **Architect** (reasoning tier) emits a DAG: analyze current auth → research constraints → implement → test in sandbox → open PR.  
2. **Developer** executes code-reading and editing steps with repo tools.  
3. **Reactor Core** runs tests in **isolation** and returns pass/fail.  
4. **Architect** replans with failure context if needed; otherwise approves merge / PR narrative.  

The **same** model stack can serve Architect and Developer roles; the **separation is operational** (what tools and gates are attached), not a requirement for dozens of named agent files.

### Mapping manifesto → this repository

| Principle | Primary anchors in JARVIS |
|---|---|
| Unified kernel | `unified_supervisor.py` |
| Body–Mind bridge | `backend/core/mind_client.py`, `backend/core/prime_router.py` |
| Semantic routing | `backend/core/interactive_brain_router.py`, `backend/core/ouroboros/governance/brain_selection_policy.yaml` |
| Motor / vision loop | `backend/vision/lean_loop.py`, `backend/ghost_hands/` |
| Neuroplasticity | `backend/core/ouroboros/` (`governed_loop_service.py`, `orchestrator.py`, `saga/`, `intake/`) |
| Observability | `backend/core/telemetry_emitter.py`, governance narration, env-driven telemetry flags |

Contributors should align new work with these principles: **extend tools and policy**, do not add brittle routing ladders or duplicate “mini-agents” that differ only by prompt wording.

---
