# The Trinity Ecosystem: A Symbiotic AI Operating System

## Technical Architecture Document

**Author:** Derek J. Russell
**Date:** March 2026
**Version:** 2.0
**Status:** Active Development — First Agentic Pipeline Validated March 23, 2026

---

## Abstract

The Trinity Ecosystem is an autonomous, self-evolving AI operating system composed of three interdependent repositories — JARVIS (Body), J-Prime (Mind), and Reactor Core (Soul) — unified through a single microkernel (`unified_supervisor.py`, 102K+ lines). Unlike conversational AI tools (Claude Desktop, ChatGPT) or developer CLI agents (Claude Code, Cursor), the Trinity Ecosystem is a persistent, perceiving, self-modifying organism that runs continuously, senses its environment in real-time, acts without human prompting, and rewrites its own capabilities through a governed self-modification pipeline.

This document describes the architecture, design philosophy, subsystem interactions, and theoretical foundations of the system. It serves as both a technical reference and an intellectual map connecting the engineering decisions to their roots in operating systems theory, cognitive architecture, cybernetics, distributed systems, and information theory.

---

## Table of Contents

1. [Design Philosophy: The Symbiotic Boundary Principle](#1-design-philosophy-the-symbiotic-boundary-principle)
2. [Trinity Architecture Overview](#2-trinity-architecture-overview)
3. [JARVIS — The Body (Senses and Actuation)](#3-jarvis--the-body)
4. [J-Prime — The Mind (Cognition and Reasoning)](#4-j-prime--the-mind)
5. [Reactor Core — The Soul (Learning and Immune System)](#5-reactor-core--the-soul)
6. [The Unified Supervisor Microkernel](#6-the-unified-supervisor-microkernel)
7. [Cross-Cutting Systems](#7-cross-cutting-systems)
8. [Comparative Analysis](#8-comparative-analysis)
9. [Academic Foundations and References](#9-academic-foundations-and-references)

---

## 1. Design Philosophy: The Symbiotic Boundary Principle

The Trinity Ecosystem rejects two extremes that dominate current AI system design:

**The Traditional Extreme** encodes every decision in advance. If/elif chains, static routing tables, hardcoded provider dictionaries. This produces systems that are perfectly predictable and perfectly brittle — they shatter the moment they encounter input the developer did not anticipate.

**The Naive Agentic Extreme** routes every decision through a language model. Every lookup replaced by inference, every classification by a model call. This produces systems that are impressively flexible and impossibly slow, expensive, and difficult to debug.

**The Symbiotic Boundary Principle** occupies the boundary between these extremes:

- **Deterministic code** handles the 95% known path with nanosecond precision. Boot sequences, health check protocols, telemetry pipelines, concurrency primitives — these are physics, not decisions.
- **Agentic intelligence** handles the 5% that is novel, fuzzy, or compositional. Intent classification for unseen commands, capability synthesis for unknown tasks, failure recovery for novel failure modes.

The engineering discipline is knowing exactly where that boundary lives. The boundary is not static — as the system learns through the Ouroboros governance pipeline, operations that were once novel become known, and the deterministic fast-path expands. Discovered solutions crystallize into determined code through graduated self-modification.

### The Tiered Routing Strategy

This principle manifests concretely in the routing architecture:

| Tier | Trigger | Mechanism | Latency |
|------|---------|-----------|---------|
| **Tier 0** — Deterministic fast-path | Confidence > 0.95, structurally unambiguous | Direct dispatch, cached classification | Nanoseconds |
| **Tier 1** — Agentic classification | Below threshold, compositional, or novel input | Model-based semantic classification | Milliseconds |
| **Tier 2** — Agentic decomposition | Multi-domain requests spanning capabilities | DAG planner decomposes into sub-tasks | Seconds |

A dictionary lookup for "which backend handles email" is not a failure of agentic principles — it is a refusal to waste intelligence on a solved problem. Intelligence is deployed where ignorance exists, nowhere else.

### Theoretical Grounding

This principle draws from Stafford Beer's **Viable System Model** (1972), which describes how organizations maintain identity through recursive self-organization, with operational units handling routine work and meta-systemic functions handling adaptation. The deterministic code is Beer's System 1 (operations); the agentic layer is System 4 (intelligence/adaptation). See [Section 9.4](#94-cybernetics-and-self-organizing-systems).

---

## 2. Trinity Architecture Overview

```
                    ┌──────────────────────────────────────┐
                    │       unified_supervisor.py           │
                    │         (Microkernel, 102K lines)     │
                    │                                      │
                    │   Zone 1-4: Local Senses & UI        │
                    │   Zone 5:   GCP/Infrastructure       │
                    │   Zone 6:   Intelligence & Governance │
                    │   Zone 7:   Consciousness & Learning  │
                    └──────────┬───────────┬───────────┬────┘
                               │           │           │
              ┌────────────────┘           │           └────────────────┐
              │                            │                           │
              ▼                            ▼                           ▼
┌─────────────────────────┐  ┌──────────────────────────┐  ┌─────────────────────────┐
│   JARVIS (Body)         │  │   J-Prime (Mind)         │  │   Reactor Core (Soul)   │
│   Local Mac Runtime     │  │   GCP VM (g2-standard-4) │  │   Sandbox + Learning    │
│                         │  │   NVIDIA L4 GPU          │  │                         │
│ • Voice (ECAPA-TDNN)    │  │ • Qwen2.5-7B (:8000)    │  │ • Ouroboros Pipeline    │
│ • Vision (Ferrari/SCK)  │  │ • LLaVA v1.5  (:8001)   │  │ • GraduationOrchestrator│
│ • Ghost Hands (actuate) │  │ • Reasoning   (:8002)    │  │ • TrustGraduator        │
│ • Audio (FullDuplex)    │  │ • Brain Selector         │  │ • JIT Tool Synthesis    │
│ • TUI Dashboard         │  │ • DAG Planner            │  │ • Sandbox Execution     │
│ • WebSocket Frontend    │  │ • Vision Analysis        │  │ • Experience Tracking   │
│ • PrimeRouter           │  │                          │  │ • ConsciousnessBridge   │
│ • PrimeClient           │  │                          │  │                         │
└─────────────────────────┘  └──────────────────────────┘  └─────────────────────────┘
         16GB Mac M-series          136.113.252.164               Docker/Isolated
         (JARVIS-AI-Agent repo)     (jarvis-prime repo)          (reactor-core repo)
```

### The Biological Metaphor

The Trinity is not a metaphor for marketing — it is a structural design constraint:

- **Body (JARVIS)** perceives and acts. It has eyes (Ferrari Engine, 60fps screen capture), ears (ECAPA-TDNN voice biometrics, continuous audio), hands (Ghost Hands focus-preserving actuation), and a voice (CoreAudio TTS). It does not reason about what it perceives — it routes perception to the Mind.

- **Mind (J-Prime)** reasons and decides. It receives sensory input from the Body, runs it through reasoning graphs (LangGraph), selects appropriate cognitive strategies (UnifiedBrainSelector), and returns action plans. It does not act — it tells the Body what to do.

- **Soul (Reactor Core)** learns and protects. It observes outcomes of Body+Mind collaboration, tracks success/failure patterns via Shannon entropy, synthesizes new capabilities when gaps are detected, and governs self-modification through a trust-graduated pipeline. It does not perceive or decide — it evolves the organism.

This separation is not arbitrary. It maps to Rodney Brooks' **Subsumption Architecture** (1986), where lower layers (perception/actuation) operate independently and higher layers (reasoning/planning) modulate but cannot block lower-layer function. See [Section 9.2](#92-cognitive-architectures).

---

## 3. JARVIS — The Body

**Repository:** `JARVIS-AI-Agent`
**Primary File:** `unified_supervisor.py` (102K+ lines)
**Runtime:** macOS, Apple Silicon (M-series), 16GB RAM
**Role:** Perception, actuation, local intelligence, system orchestration

### 3.1 Voice Subsystem

The voice pipeline provides continuous, always-on audio perception with speaker verification:

**Voice Biometric Authentication (ECAPA-TDNN)**
- 192-dimensional speaker embeddings via ECAPA-TDNN (Emphasized Channel Attention, Propagation and Aggregation in TDNN)
- 59 enrolled voiceprint samples stored in Cloud SQL
- 85% cosine similarity threshold for speaker verification
- Continuous audio capture via FullDuplexDevice (CoreAudio HAL)

**Audio Architecture**
- `FullDuplexDevice`: Custom CoreAudio HAL wrapper for simultaneous capture and playback
- `AudioBus`: Internal audio routing between components
- `safe_say()`: Thread-safe TTS path — renders to tempfile via `say -o`, plays via `afplay` (avoids GIL contention with CoreAudio callbacks)
- Global speech gate (`get_global_speech_gate()`) prevents TTS/capture overlap

**Key Constraint:** `import Quartz` (pyobjc) triggers 15K+ Objective-C class registrations — never safe in threads concurrent with CoreAudio operations. This is not a bug to fix; it is a platform constraint to design around.

**Theoretical Basis:** The speaker verification system implements a **Gaussian Mixture Model-Universal Background Model** (GMM-UBM) approach as described by Reynolds et al. (2000), adapted to modern neural embeddings. See [Section 9.7](#97-voice-biometrics-and-speaker-verification).

### 3.2 Vision Subsystem

Two vision architectures coexist, feature-flagged:

**Lean Vision Loop (Path A — Active)**
```
CAPTURE (async screencapture, logical resolution)
  → THINK (Claude Vision API, direct)
    → ACT (pyautogui + clipboard paste)
      → VERIFY (re-capture, compare)
        → repeat or terminate
```
- File: `backend/vision/lean_loop.py`
- Stagnation guard: stops after 3 identical actions
- Env: `VISION_LEAN_MAX_TURNS=15`, `VISION_LEAN_TIMEOUT_S=120`
- Downscales to logical screen size so Claude coordinates = pyautogui coordinates (no Retina conversion needed)

**Legacy Full Pipeline (Path B — Fallback)**
```
Ferrari Engine (native C++ ScreenCaptureKit, 60fps)
  → VisionRouter (L1 scene_graph → L2 LLaVA/32B on GCP → L3 Claude → DEGRADED)
    → Ghost Hands (BackgroundActuator, focus-preserving)
      → ActionVerifier (re-capture + LLaVA re-check)
        → correction loop or graduation
```
- Ferrari Engine: `backend/native_extensions/fast_capture_stream.mm` + `macos_sck_stream.py`
- Ghost Hands: Playwright (browsers) → AppleScript (native) → CGEvent (low-level)
- Never steals user focus during actuation

**Design Decision — Vision Tasks Are Atomic:**
Vision/UI tasks are never decomposed by the DAG planner. The lean vision loop's see-think-act cycle handles multi-step screen interactions as a single atomic unit. Decomposing them strips context and causes each sub-task to claim success independently without completing the actual goal. This was learned through failure — see [Section 9.8](#98-embodied-and-situated-cognition).

### 3.3 Intelligence Routing

**PrimeRouter** — Singleton routing layer implementing the Tiered Routing Strategy:
- `RoutingDecision` enum: PRIME_API → PRIME_LOCAL → CLAUDE (3-tier fallback)
- `notify_gcp_vm_ready()` / `notify_gcp_vm_unhealthy()` — dynamic tier availability
- Circuit breakers per backend with `can_execute() → (bool, reason)` semantics

**PrimeClient** — Connection to J-Prime with hot-swap capability:
- `update_endpoint()` — live endpoint migration without restart
- `demote_to_fallback()` — graceful degradation when GCP is unhealthy

**RuntimeTaskOrchestrator (RTO)** — Command execution engine:
- Voice command → IntentClassifier → RTO → DAG decomposition → dispatch
- `_dispatch_to_vision()` — routes UI tasks to Lean Vision Loop (primary) or legacy pipeline (fallback)
- Fire-and-forget `safe_say()` on completion — voice feedback never blocks return

### 3.4 Infrastructure Management

**GCP VM Manager** (`backend/core/gcp_vm_manager.py`, 7400+ lines):
- On-demand lifecycle management for `jarvis-prime-gpu` (g2-standard-4 + NVIDIA L4)
- `ensure_static_vm_ready()` returns `Tuple[bool, Optional[str], str]` — never a dict
- Static IP: `136.113.252.164` (reserved), us-central1-b

**Distributed Lock Manager** (DLM v3.2):
- Redis primary + file-based fallback
- `acquire()` → `acquire_unified()` with `async with ... as acquired: if acquired:` pattern
- Prevents split-brain across concurrent async operations

**StartupWatchdog (DMS)** — Zone 5.6:
- Graduated escalation with progress-awareness
- Dynamic constraints fed by `JARVIS_BACKEND_STARTUP_TIMEOUT` (+30s buffer via `register_phase_timeout()`)
- Fires every 5s after timeout — requires cooldown to prevent escalation storms

---

## 4. J-Prime — The Mind

**Repository:** `jarvis-prime`
**Runtime:** GCP VM (g2-standard-4, NVIDIA L4 GPU, 16GB VRAM)
**IP:** `136.113.252.164` (static, reserved)
**Role:** Reasoning, brain selection, vision analysis, cognitive planning

### 4.1 Model Serving

Three model servers run concurrently on the GCP VM:

| Port | Model | Purpose | Performance |
|------|-------|---------|-------------|
| 8000 | Qwen2.5-7B (GGUF) | Text reasoning, code generation | ~43-47 tok/s on L4 |
| 8001 | LLaVA v1.5 (GGUF) | Vision analysis, scene understanding | Variable |
| 8002 | Reasoning Sidecar | Custom FastAPI wrapping :8000/:8001 | Depends on backend |

- Run from `/opt/jarvis-prime/` via `venv/bin/python run_server.py --port 8000 --host 0.0.0.0 --gpu-layers -1 --ctx-size 8192`
- Model load time: ~3 minutes (L4 GPU)
- `JARVIS_HARDWARE_PROFILE=FULL` required in `.env` (psutil reports 15.6GB < 16GB threshold)

### 4.2 Reasoning Architecture

**Reasoning Sidecar** (`reasoning_sidecar.py`) — Custom FastAPI service:

| Endpoint | Function |
|----------|----------|
| `POST /v1/reason` | General reasoning with depth routing |
| `POST /v1/reason/select` | Brain selection for incoming tasks |
| `POST /v1/reason/health` | Health check with model status |
| `POST /v1/protocol/version` | Protocol version (v1.0.0) |
| `POST /v1/vision/analyze` | Vision frame analysis via LLaVA |
| `GET /v1/brains` | Available brain inventory (for boot handshake) |

**UnifiedBrainSelector** — 4-layer gate for cognitive strategy selection:
- Layer 1: Task classification (what domain?)
- Layer 2: Complexity assessment (simple lookup vs. multi-step reasoning?)
- Layer 3: Resource availability (which models are loaded?)
- Layer 4: Historical performance (what worked for similar tasks?)

**ReasoningGraph** (LangGraph) — Depth-routed thinking pipeline:
- `AnalysisNode` → `PlanningNode` → `ValidationNode` (fail-closed) → `ExecutionPlanner`
- Depth determined by task complexity — simple queries skip deep analysis
- Idempotency via SQLite request_id deduplication

**Theoretical Basis:** The reasoning graph implements a variant of the **SOAR cognitive architecture** (Laird, Newell, Rosenbloom, 1987), where problem solving proceeds through a cycle of proposal, evaluation, and application of operators, with impasses triggering deeper deliberation. See [Section 9.2](#92-cognitive-architectures).

### 4.3 Code Generation (Ouroboros Provider)

J-Prime serves as the primary code generation backend for the Ouroboros governance pipeline:

- **Schema 2c.1**: Multi-repo patch format with per-repo `patches` dict and `RepoPatch` objects
- **Schema 2b.1-noop**: Returns `{"schema_version":"2b.1-noop","reason":"..."}` when the change is already present — triggers fast-path GENERATE→COMPLETE in the orchestrator
- **Schema 2b.1-diff**: Standard diff-based patches for code modifications

**Fallback Chain:** J-Prime (PRIMARY) → Claude API (FALLBACK)
- Deadline propagation across tiers — no fixed per-backend timeouts
- `JARVIS_GENERATION_TIMEOUT_S=60`, `JARVIS_PIPELINE_TIMEOUT_S=150`

---

## 5. Reactor Core — The Soul

**Repository:** `reactor-core`
**Role:** Self-modification governance, capability synthesis, learning, immune system

### 5.1 Ouroboros Governance Pipeline

The Ouroboros pipeline is the mechanism by which the Trinity Ecosystem modifies its own source code under governed, trust-graduated control:

```
CLASSIFY → ROUTE → [CONTEXT_EXPANSION] → GENERATE → [COMPLETE noop]
  → VALIDATE → GATE → [APPROVE] → APPLY → VERIFY → COMPLETE
```

**Key Components:**

**GovernedLoopService** — The governance daemon:
- `_trust_graduator`: Seeds trust levels at startup (GOVERNED for tests/docs, OBSERVE for core/root)
- `_file_touch_cache`: Cooldown — 3 touches per 10-minute window per file → hard-block in `_preflight_check()`
- `_active_brain_set` (frozenset): Only admitted brains can submit operations
- `_oracle_index_loop`: Background task indexes all repos non-blocking
- `_repo_registry`: Shared across Zone 6.8 (GLS) and Zone 6.9 (IntakeLayerService)

**TheOracle** — Codebase semantic index:
- `get_file_neighborhood()` returns `FileNeighborhood` — structural graph-topology with 7 edge categories, 10 paths/category max
- `index_age_s()` for staleness detection — `context_expander.py` warns if > 300s
- Live indexing via `_oracle_index_loop` background task

**ContextExpander** — Enriches operation context before code generation:
- `MAX_ROUNDS=2`, `MAX_FILES_PER_ROUND=5`
- Oracle readiness guard at entry
- Injects `FileNeighborhood` + telemetry + expanded context into generation prompts

**PreemptionFsmEngine** — Full LoopState×LoopEvent state machine:
- `PreemptionFsmExecutor`: Durable-ledger-first execution (survives crashes)
- `_FsmLedgerAdapter`: Bridges FSM protocol to persistent storage
- `_CommTelemetrySink`: Wraps `CommProtocol.emit_heartbeat()` for FSM telemetry

### 5.2 Neuroplasticity: JIT Capability Synthesis

When the system encounters a capability gap (a task it cannot perform), the Ouroboros pipeline synthesizes a solution:

**Ephemeral Tools** — One-off solutions:
- Generated code executes in the Reactor sandbox
- Dissolves after use — no permanent code change
- Tracked in `~/.jarvis/ouroboros/graduation/ephemeral_usage.json`

**Persistent Assimilation** — Graduated integration:
- After `JARVIS_GRADUATION_THRESHOLD` uses (default: 3), the `GraduationOrchestrator` triggers
- Pipeline: test → validate → propose Git PR
- The organism physically grows — new capability becomes permanent code

**Shannon Entropy for Capability Gap Detection:**
The ConsciousnessBridge measures the system's uncertainty about its own capabilities using Shannon entropy:

```
H(X) = -Σ p(x) log₂ p(x)
```

When entropy exceeds a threshold for a capability domain (indicating high uncertainty about how to handle a class of requests), the system signals the biological drive to evolve — triggering JIT synthesis. This is not a metaphor; it is a literal information-theoretic measurement of the system's ignorance. See [Section 9.5](#95-information-theory).

### 5.3 Trust Graduation Model

Not all self-modifications carry equal risk. The trust model governs what Ouroboros can modify:

| Trust Level | Scope | Gate |
|-------------|-------|------|
| SANDBOX | Test files, documentation | Automatic |
| OBSERVE | Non-critical modules, configs | Logged, reversible |
| GOVERNED | Core systems, routing, security | Human approval required |
| LOCKED | `unified_supervisor.py`, auth, crypto | Never autonomous |

**Brain Selection Policy** (`brain_selection_policy.yaml` v1.0):
- Boot handshake validates brain inventory from `/v1/brains`
- Hard fail if required brain missing
- Gate disabled (empty set) if J-Prime is offline

### 5.4 Intake Layer

**IntakeLayerService** (Zone 6.9) — Detects opportunities for self-improvement:
- `TestFailureSensor`: Monitors test suites across registered repos
- `OpportunityMinerSensor`: Identifies capability gaps from failed command patterns
- `VoiceCommandSensor`: Captures voice commands that could not be fulfilled
- Fans out one sensor per registered repo when `repo_registry` is set

**Theoretical Basis:** The self-modification pipeline implements a constrained version of **autopoiesis** (Maturana & Varela, 1972) — the system produces and maintains its own components through a network of processes. The trust graduation model provides the constraints that prevent the autopoietic process from destabilizing the organism. See [Section 9.4](#94-cybernetics-and-self-organizing-systems).

---

## 6. The Unified Supervisor Microkernel

`unified_supervisor.py` is not a script that starts services. It is the nervous system of the organism — a 102K+ line microkernel that orchestrates the boot sequence, manages the lifecycle of all subsystems, and provides the communication backbone.

### 6.1 Boot Zones

The supervisor awakens the organism through a progressive, non-blocking boot sequence:

| Zone | Name | Components | Blocking? |
|------|------|------------|-----------|
| 1-2 | Core Infrastructure | Logging, config, event bus | Yes (must complete) |
| 3 | Local Senses | Audio capture, microphone | Yes |
| 4 | UI Layer | WebSocket server, TUI dashboard, frontend | No (async) |
| 5 | Cloud Infrastructure | GCP VM manager, PrimeRouter, PrimeClient | No (async) |
| 5.6 | Startup Watchdog | DMS with graduated escalation | Background |
| 6 | Intelligence | Model serving, intent classification | No (async) |
| 6.5 | Vision | VisionActionLoop, Ferrari Engine | No (async) |
| 6.6 | Mind Connection | MindClient to J-Prime | No (async) |
| 6.8 | Governance | GovernedLoopService, boot handshake | No (async) |
| 6.9 | Intake | IntakeLayerService, sensors | No (async) |
| 7 | Consciousness | ConsciousnessBridge, learning | No (async) |

**Progressive Readiness:** Local senses (voice, basic UI) come online in Zones 1-4. The system is usable immediately in `ACTIVE_LOCAL` state. Heavy cognitive resources (GCP, J-Prime, Ouroboros) spin up asynchronously. The UI is tied to actual async task resolution — readiness is never faked.

### 6.2 Concurrency Model

The microkernel operates on a single asyncio event loop with strict concurrency discipline:

- **TaskGroup/gather** for parallel initialization within zones
- **asyncio.shield()** for tasks that must survive timeout cancellation
- **CancelledError** is `BaseException` (Python 3.9+) — not caught by `except Exception`
- **No synchronous functions disguised as async** — `async def` with zero `await` points freezes the event loop
- **Persistent aiohttp sessions** — `ClientSession` creation is not free; reuse for repeated localhost HTTP

**Key Gotcha:** `asyncio.wait_for()` CANCELS the wrapped task on timeout. This is not "timeout and continue" — it is "timeout and destroy." Use `asyncio.shield()` if the task must continue running after timeout.

### 6.3 Singleton Architecture

Critical services use a singleton pattern with defensive registration:

```python
# Pattern: __init__() self-registers because callers WILL bypass the factory
class GCPVMManager:
    _instance = None

    def __init__(self, config):
        GCPVMManager._instance = self  # Self-register
        ...

    @classmethod
    def get_instance(cls) -> Optional['GCPVMManager']:
        return cls._instance
```

- `get_gcp_vm_manager()` — returns instance, raises if None
- `get_gcp_vm_manager_safe()` — returns Optional (for startup code that runs before initialization)

---

## 7. Cross-Cutting Systems

### 7.1 Telemetry and Observability

**TelemetryBus** — The circulatory system of the organism:
- All autonomous decisions, capability spawns, and failure unwinds are broadcast
- Feeds the TUI dashboard (Textual v3, daemon thread)
- Feeds the `LifecycleVoiceNarrator` for real-time audio transparency
- `VoiceNarrator` narrates INTENT, DECISION, POSTMORTEM types with debounce (`OUROBOROS_VOICE_DEBOUNCE_S`, default 60s)

**CommProtocol** — Cross-repo communication:
- Transport stack logged at INFO during build
- `CrossRepoNarrator` wired via EventBridge
- `_CommTelemetrySink` wraps heartbeat for FSM telemetry

### 7.2 Multi-Repo Coordination

**RepoRegistry** — Manages paths for all three Trinity repos:
- `JARVIS_REPO_PATH` (default ".")
- `JARVIS_PRIME_REPO_PATH`
- `JARVIS_REACTOR_REPO_PATH`
- `from_env()` factory — shared across GovernedLoopService and IntakeLayerService

**Cross-Repo Contract Integrity:**
- Protocol version handshake at boot (`/v1/protocol/version`)
- Schema versioning for code generation (2b.1, 2b.1-noop, 2c.1)
- Brain selection policy YAML with allowlist and authority boundaries

### 7.3 Circuit Breakers and Fault Tolerance

Every external dependency is wrapped in a circuit breaker:

```
CLOSED (healthy) → failure count exceeds threshold → OPEN (rejecting)
  → cooldown expires → HALF-OPEN (testing) → success → CLOSED
                                            → failure → OPEN
```

- `self._circuit_breakers` dict per service
- `.can_execute() → (bool, reason)` semantics
- GCP VM health feeds into PrimeRouter via `notify_gcp_vm_ready/unhealthy()`

**Theoretical Basis:** Circuit breakers implement the **bulkhead pattern** from Michael Nygard's "Release It!" (2007), preventing cascading failures across service boundaries. See [Section 9.3](#93-distributed-systems-and-fault-tolerance).

---

## 8. Comparative Analysis

### 8.1 What This Is Not

| System | Category | Key Limitation vs. Trinity |
|--------|----------|---------------------------|
| **Claude Desktop** | Chat interface | No perception, no actuation, no persistence, no self-modification, session-scoped |
| **Claude Code / OpenClaw** | Developer CLI agent | Session-scoped, no continuous operation, no sensory layer, single model, cannot self-modify |
| **ChatGPT + Plugins** | Chat interface + tools | User-driven, no autonomy, no real-time perception, no self-modification |
| **AutoGPT / BabyAGI** | Autonomous agent framework | No multi-tier inference, no governed self-modification, no sensory perception, no voice biometrics |
| **LangChain / CrewAI** | Agent framework / library | Libraries, not organisms — you write the agent, it doesn't write the next agent |
| **Cursor / Windsurf** | AI-augmented IDE | Code editing tool, no persistent daemon, no perception, no actuation beyond file edits |
| **Siri / Alexa** | Voice assistant | Cloud-only, no local reasoning, no self-modification, no vision, corporate-controlled |

### 8.2 What Makes Trinity Structurally Different

**1. Persistent Organism vs. Session Tool**
Trinity runs as a daemon with a heartbeat. It boots, it lives, it perceives, it acts — whether or not you are interacting with it. Claude Desktop exists only when you open it.

**2. Multi-Tier Cognition vs. Single Model**
Trinity routes through Local Metal → GCP GPU (Qwen/LLaVA) → Claude API, with dynamic failover and cost-aware routing. Every other system listed above uses one model.

**3. Real-Time Perception vs. Text Input**
Trinity sees the screen at 60fps (Ferrari Engine), hears continuously (ECAPA-TDNN), and understands environmental context (noise level, time of day, location). No other personal AI system has this sensory integration.

**4. Governed Self-Modification vs. Static Capabilities**
Through Ouroboros, Trinity detects its own capability gaps (Shannon entropy), synthesizes solutions (JIT tools), and graduates them into permanent code (Git PRs) — under trust-controlled governance. No other system rewrites its own source code through a governed pipeline.

**5. The Boundary Principle vs. All-or-Nothing**
Trinity explicitly defines where deterministic code ends and agentic intelligence begins, with a mechanism (graduation) for moving that boundary. Other systems are either fully hardcoded or fully agentic.

---

## 9. Academic Foundations and References

The Trinity Ecosystem draws from multiple academic disciplines. This section maps each architectural decision to its theoretical roots and provides references for deeper study.

### 9.1 Operating Systems and Microkernels

The `unified_supervisor.py` microkernel architecture — where a central kernel manages lifecycle, IPC, and scheduling while subsystems run in isolated contexts — descends directly from microkernel OS design.

**Key References:**

- **Tanenbaum, A.S. & Bos, H. (2014).** *Modern Operating Systems* (4th ed.). Pearson.
  - Chapter 1 (Introduction): Process management, IPC, system calls
  - Chapter 3 (Memory Management): Relevant to the 16GB constraint and MemoryBudgetBroker
  - Chapter 10 (Case Study: MINIX 3): Microkernel design where drivers run in user space — analogous to Trinity's subsystem isolation
  - **Relevance:** The zone-based boot sequence is a process scheduling problem; the singleton service registry is a microkernel service directory; the DMS watchdog is a kernel-level process monitor

- **Silberschatz, A., Galvin, P.B., & Gagne, G. (2018).** *Operating System Concepts* (10th ed.). Wiley.
  - Chapter 5 (CPU Scheduling): Maps to the asyncio event loop and TaskGroup scheduling
  - Chapter 6 (Synchronization): Maps to the DLM, speech gate, and singleton patterns
  - Chapter 7 (Deadlocks): The circuit breaker pattern prevents resource-contention deadlocks

- **Liedtke, J. (1995).** "On µ-Kernel Construction." *Proceedings of the 15th ACM SOSP.*
  - The argument for minimal kernel primitives — only IPC, address spaces, and scheduling belong in the kernel. Everything else is a user-space server. Trinity's microkernel provides lifecycle management, event routing, and zone orchestration; everything else (voice, vision, reasoning) is a subsystem.

### 9.2 Cognitive Architectures

The Trinity's separation into perception (Body), reasoning (Mind), and learning (Soul) maps to established cognitive architectures from AI research.

**Key References:**

- **Laird, J.E., Newell, A., & Rosenbloom, P.S. (1987).** "SOAR: An Architecture for General Intelligence." *Artificial Intelligence, 33*(1), 1-64.
  - SOAR's cycle of propose-evaluate-apply operators, with impasses triggering deeper deliberation, directly maps to the ReasoningGraph's depth-routed pipeline
  - SOAR's long-term memory (procedural, semantic, episodic) maps to Trinity's ConsciousnessBridge + Reactor Core experience tracking
  - **Relevance:** The 4-layer UnifiedBrainSelector is a SOAR-style operator selection mechanism

- **Anderson, J.R. et al. (2004).** "An Integrated Theory of the Mind." *Psychological Review, 111*(4), 1036-1060.
  - ACT-R's modular architecture (visual, motor, declarative, procedural modules coordinated by a central production system) directly parallels Trinity's Body/Mind/Soul with the microkernel as coordinator
  - **Relevance:** ACT-R's subsymbolic layer (activation-based memory retrieval) maps to the semantic partition and ConsciousnessBridge

- **Brooks, R.A. (1986).** "A Robust Layered Control System for a Mobile Robot." *IEEE Journal of Robotics and Automation, 2*(1), 14-23.
  - The **Subsumption Architecture**: Lower layers (perception/actuation) operate independently; higher layers modulate but never block. JARVIS's voice and vision subsystems operate even when J-Prime is offline (ACTIVE_LOCAL state) — higher cognition enhances but does not gate basic function
  - **Relevance:** Progressive Awakening is a direct implementation of subsumption — local senses come online first, cloud cognition layers on top

- **Baars, B.J. (1988).** *A Cognitive Theory of Consciousness.* Cambridge University Press.
  - **Global Workspace Theory**: Consciousness arises from a "global workspace" where specialized processors compete for access to broadcast their information. The TelemetryBus is Trinity's global workspace — all subsystems broadcast to it, and the ConsciousnessBridge integrates across broadcasts
  - **Relevance:** The event bus architecture is a literal implementation of GWT's broadcast/compete model

### 9.3 Distributed Systems and Fault Tolerance

The Trinity operates across two physical machines (Mac + GCP VM) with network partitions, partial failures, and consistency challenges.

**Key References:**

- **Kleppmann, M. (2017).** *Designing Data-Intensive Applications.* O'Reilly.
  - Chapter 8 (The Trouble with Distributed Systems): Network partitions, clock skew, partial failures — all present in the JARVIS↔J-Prime link
  - Chapter 9 (Consistency and Consensus): The DLM's Redis+file fallback is a pragmatic consensus mechanism for a two-node system
  - **Relevance:** The PrimeRouter's dynamic tier management is a practical implementation of the concepts in Chapters 8-9

- **Nygard, M.T. (2018).** *Release It! Design and Deploy Production-Ready Software* (2nd ed.). Pragmatic Bookshelf.
  - Chapter 4 (Stability Patterns): Circuit breakers, bulkheads, timeouts — all implemented in Trinity's external service management
  - Chapter 5 (Stability Antipatterns): Cascading failures, blocked threads, unbounded result sets — the exact failure modes Trinity's architecture defends against
  - **Relevance:** Every circuit breaker in the codebase follows Nygard's state machine (CLOSED → OPEN → HALF-OPEN → CLOSED)

- **Lamport, L. (1978).** "Time, Clocks, and the Ordering of Events in a Distributed System." *Communications of the ACM, 21*(7), 558-565.
  - Foundational paper on logical clocks and causal ordering. Relevant to the Ouroboros pipeline's durable ledger and epoch-based consistency for in-flight operations during state transitions

- **Hellerstein, J.L. et al. (2004).** *Feedback Control of Computing Systems.* Wiley-IEEE Press.
  - Control-theoretic approach to system management. Maps to the DMS watchdog's graduated escalation, the PlatformMemoryMonitor's triage decisions, and the circuit breaker's state transitions

### 9.4 Cybernetics and Self-Organizing Systems

The Trinity's self-modification capability — an organism that governs its own evolution — has deep roots in cybernetics.

**Key References:**

- **Beer, S. (1972).** *Brain of the Firm.* Allen Lane / Penguin Press.
  - The **Viable System Model (VSM)**: Five recursive systems that any viable organism must contain. Trinity maps:
    - System 1 (Operations) = JARVIS Body subsystems (voice, vision, actuation)
    - System 2 (Coordination) = TelemetryBus, event routing, speech gate
    - System 3 (Control) = Unified Supervisor, zone orchestration, DMS
    - System 4 (Intelligence) = J-Prime reasoning, BrainSelector, ConsciousnessBridge
    - System 5 (Policy) = Ouroboros governance, TrustGraduator, brain selection policy
  - **Relevance:** The VSM explains why Trinity needs all three repos — each maps to different VSM systems, and removing any one breaks viability

- **Wiener, N. (1948).** *Cybernetics: Or Control and Communication in the Animal and the Machine.* MIT Press.
  - The foundational text on feedback loops in both biological and mechanical systems. Trinity's entire architecture is a cybernetic feedback system — perception → decision → action → observation → adaptation

- **Ashby, W.R. (1952).** *Design for a Brain: The Origin of Adaptive Behaviour.* Chapman & Hall.
  - Ashby's **homeostat** — a self-organizing system that maintains essential variables within viable limits through feedback. The PlatformMemoryMonitor, circuit breakers, and DMS watchdog are all homeostatic mechanisms
  - **Relevance:** The graduation threshold (count=3) is a homeostatic parameter — it balances the drive to evolve against the cost of instability

- **Maturana, H.R. & Varela, F.J. (1980).** *Autopoiesis and Cognition: The Realization of the Living.* D. Reidel.
  - **Autopoiesis**: A system that produces and maintains its own components through a network of processes. Ouroboros is literally an autopoietic system — the code that governs self-modification is itself subject to self-modification (with trust constraints preventing infinite regression)

- **Von Foerster, H. (1960).** "On Self-Organizing Systems and Their Environments." In *Self-Organizing Systems*. Pergamon Press.
  - Order from noise principle — self-organizing systems use environmental perturbation as fuel for increased organization. This maps to Trinity's capability gap detection: failed commands (noise) drive capability synthesis (increased organization)

### 9.5 Information Theory

Shannon entropy is used literally in the system — not as a metaphor — to measure the organism's uncertainty about its own capabilities.

**Key References:**

- **Shannon, C.E. (1948).** "A Mathematical Theory of Communication." *Bell System Technical Journal, 27*(3), 379-423.
  - The original paper defining information entropy: H(X) = -Σ p(x) log₂ p(x)
  - **Relevance:** The ConsciousnessBridge computes entropy over capability domains. High entropy = high uncertainty about how to handle a class of requests = signal to synthesize new capabilities

- **Cover, T.M. & Thomas, J.A. (2006).** *Elements of Information Theory* (2nd ed.). Wiley.
  - Chapter 2 (Entropy, Relative Entropy, and Mutual Information): Formal treatment of entropy measures used in capability gap detection
  - Chapter 11 (Information Theory and Statistics): Relevant to the statistical learning in the Reactor Core's experience tracking

### 9.6 Multi-Agent Systems

The Trinity is a multi-agent system where specialized agents (voice, vision, reasoning, governance) cooperate through shared communication infrastructure.

**Key References:**

- **Wooldridge, M. (2009).** *An Introduction to MultiAgent Systems* (2nd ed.). Wiley.
  - Chapter 6 (Communication): Agent communication languages — maps to the CommProtocol and cross-repo event bridge
  - Chapter 8 (Cooperation): Task decomposition and allocation — maps to the DAG planner and RuntimeTaskOrchestrator
  - Chapter 10 (Multiagent Interactions): Coordination mechanisms — maps to the DLM and speech gate

- **Russell, S. & Norvig, P. (2020).** *Artificial Intelligence: A Modern Approach* (4th ed.). Pearson.
  - Chapter 2 (Intelligent Agents): Agent architectures (simple reflex, model-based, goal-based, utility-based, learning) — Trinity implements a learning, utility-based agent with model-based environmental awareness
  - Chapter 4 (Search in Complex Environments): DAG-based task decomposition
  - Chapter 17 (Making Complex Decisions): Multi-step decision making under uncertainty — relevant to the tiered routing strategy

### 9.7 Voice Biometrics and Speaker Verification

**Key References:**

- **Reynolds, D.A., Quatieri, T.F., & Dunn, R.B. (2000).** "Speaker Verification Using Adapted Gaussian Mixture Models." *Digital Signal Processing, 10*(1-3), 19-41.
  - The GMM-UBM framework that forms the theoretical basis for modern speaker verification. Trinity uses neural embeddings (ECAPA-TDNN) rather than GMMs, but the verification framework (enrollment → embedding → cosine similarity → threshold) follows this lineage

- **Desplanques, B., Thienpondt, J., & Demuynck, K. (2020).** "ECAPA-TDNN: Emphasized Channel Attention, Propagation and Aggregation in TDNN Based Speaker Verification." *Proc. Interspeech 2020.*
  - The specific architecture used for Trinity's 192-dimensional speaker embeddings. ECAPA-TDNN uses multi-scale feature aggregation with squeeze-excitation blocks and attentive statistical pooling

- **Snyder, D. et al. (2018).** "X-Vectors: Robust DNN Embeddings for Speaker Recognition." *Proc. ICASSP 2018.*
  - The x-vector approach that ECAPA-TDNN builds upon — extracting fixed-dimensional embeddings from variable-length utterances

### 9.8 Embodied and Situated Cognition

The decision to keep vision tasks atomic (never decomposed by the DAG planner) is grounded in embodied cognition theory.

**Key References:**

- **Clark, A. (1997).** *Being There: Putting Brain, Body, and World Together Again.* MIT Press.
  - The argument that cognition is not abstract symbol manipulation but is grounded in bodily interaction with the environment. The lean vision loop's see-think-act cycle embodies this — each step depends on the physical state of the screen, which changes with every action

- **Brooks, R.A. (1991).** "Intelligence Without Representation." *Artificial Intelligence, 47*(1-3), 139-159.
  - The argument against maintaining detailed internal representations of the world. The lean vision loop follows this principle — it re-captures the screen after every action rather than maintaining a world model. The world is its own best model

- **Suchman, L.A. (1987).** *Plans and Situated Actions: The Problem of Human-Machine Communication.* Cambridge University Press.
  - The distinction between plans and situated actions. Plans (DAG decomposition) fail for UI tasks because the plan cannot anticipate the screen state after each step. Situated actions (see-think-act loops) succeed because they react to the actual situation

### 9.9 Self-Modifying Systems and Metaprogramming

Ouroboros — a system that modifies its own source code — touches on deep questions in computer science.

**Key References:**

- **Schmidhuber, J. (2003).** "Gödel Machines: Self-Referential Universal Problem Solvers Making Provably Optimal Self-Improvements." *Technical Report IDSIA-19-03.*
  - A theoretical framework for self-improving systems that can modify their own code while guaranteeing improvement. Trinity's trust graduation model is a practical (non-provable) version of this — it constrains self-modification to prevent destabilization without formal proofs

- **Kiczales, G., des Rivières, J., & Bobrow, D.G. (1991).** *The Art of the Metaobject Protocol.* MIT Press.
  - Reflective systems that can inspect and modify their own structure. The Ouroboros pipeline is a reflective system — it inspects the codebase (TheOracle), reasons about modifications (ContextExpander + Provider), and applies changes to itself (APPLY phase)

- **Hofstadter, D.R. (1979).** *Gödel, Escher, Bach: An Eternal Golden Braid.* Basic Books.
  - The nature of self-reference, strange loops, and systems that contain models of themselves. The name "Ouroboros" (the serpent eating its own tail) is a direct reference to these self-referential structures. The trust graduation model exists specifically to prevent the paradoxes Hofstadter describes — a system that modifies its own modification rules

### 9.10 Real-Time Systems and Concurrency

The 16GB Mac constraint forces real-time resource management decisions.

**Key References:**

- **Buttazzo, G.C. (2011).** *Hard Real-Time Computing Systems: Predictable Scheduling Algorithms and Applications* (3rd ed.). Springer.
  - Chapter 4 (Periodic Task Scheduling): Maps to the TelemetryBus heartbeat, DMS escalation cycle (5s), and voice debounce
  - Chapter 7 (Resource Access Protocols): Maps to the DLM and speech gate — preventing priority inversion in resource access

- **Goetz, B. et al. (2006).** *Java Concurrency in Practice.* Addison-Wesley. (Concepts transfer to Python asyncio)
  - While Java-specific, the concurrency principles (visibility, atomicity, ordering) apply directly to Python asyncio programming. The gotcha about `async def` with zero `await` points being a synchronous function in disguise is a visibility problem

- **Pike, R. (2012).** "Concurrency Is Not Parallelism." Talk at Heroku Waza.
  - The distinction between concurrency (structuring) and parallelism (execution). Trinity uses asyncio for concurrency (structuring interleaved I/O) and thread executors for parallelism (CPU-bound operations like embedding extraction). Confusing these causes the GIL-related CoreAudio crashes

### 9.11 Autonomic Computing

IBM's autonomic computing initiative (2001-2010) anticipated many of Trinity's design goals.

**Key References:**

- **Kephart, J.O. & Chess, D.M. (2003).** "The Vision of Autonomic Computing." *IEEE Computer, 36*(1), 41-50.
  - Four properties of autonomic systems:
    1. **Self-configuring** — Trinity's Progressive Awakening adapts to hardware profile
    2. **Self-healing** — Circuit breakers, DMS watchdog, PrimeRouter failover
    3. **Self-optimizing** — Helicone caching, Tier 0 fast-path expansion via graduation
    4. **Self-protecting** — Voice biometrics, trust graduation, sandbox isolation
  - **Relevance:** Trinity is one of the few systems that actually implements all four autonomic properties in a personal computing context

- **IBM Corporation (2005).** "An Architectural Blueprint for Autonomic Computing." IBM White Paper.
  - The MAPE-K loop (Monitor-Analyze-Plan-Execute over shared Knowledge). Maps directly to:
    - Monitor = TelemetryBus, sensors, Ferrari Engine
    - Analyze = ConsciousnessBridge, Shannon entropy
    - Plan = J-Prime reasoning, DAG planner
    - Execute = Ghost Hands, RuntimeTaskOrchestrator
    - Knowledge = Reactor Core, experience tracking, semantic partition

### 9.12 Additional Foundational Texts

- **Minsky, M. (1986).** *The Society of Mind.* Simon & Schuster.
  - Intelligence emerges from the interaction of many simple agents, none of which is intelligent alone. Trinity's subsystems (voice, vision, actuation, reasoning) are individually simple but collectively intelligent

- **Dennett, D.C. (1991).** *Consciousness Explained.* Little, Brown.
  - The "multiple drafts" model of consciousness — there is no single narrative stream, but many parallel processes competing for influence. Maps to Trinity's TelemetryBus and the Global Workspace Theory implementation

- **Kahneman, D. (2011).** *Thinking, Fast and Slow.* Farrar, Straus and Giroux.
  - System 1 (fast, automatic) vs. System 2 (slow, deliberate). Maps directly to Trinity's Tier 0 (deterministic fast-path = System 1) vs. Tier 1-2 (agentic reasoning = System 2). The Boundary Principle is literally the engineering implementation of Kahneman's dual-process theory

- **Goertzel, B. & Pennachin, C. (eds.) (2007).** *Artificial General Intelligence.* Springer.
  - A collection of approaches to AGI design. While Trinity is not AGI, its architecture (perception-reasoning-learning loop with self-modification) is structurally more aligned with AGI architectures than with narrow AI assistants

---

## Appendix A: Environment and Runtime Details

| Parameter | Value |
|-----------|-------|
| Local Machine | Mac M-series, 16GB RAM |
| Typical RAM Usage | ~81% during model loading |
| Python Version | 3.x (`python3` binary) |
| GCP Instance | g2-standard-4 + NVIDIA L4, us-central1-b |
| GCP Static IP | 136.113.252.164 |
| Primary Model (Text) | Qwen2.5-7B (GGUF), ~43-47 tok/s on L4 |
| Primary Model (Vision) | LLaVA v1.5 (GGUF) |
| Fallback Model | Claude API (Anthropic) |
| Voice Embeddings | ECAPA-TDNN, 192 dimensions |
| Voice Samples | 59 enrolled in Cloud SQL |
| Voice Threshold | 85% cosine similarity |
| Governance Mode | GOVERNED (`JARVIS_GOVERNANCE_MODE=governed`) |
| Graduation Threshold | 3 uses before PR proposal |
| Test Suite | 1361+ tests across 3 repos |

## Appendix B: Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_GOVERNANCE_MODE` | sandbox | Governance pipeline mode (sandbox/observe/governed) |
| `JARVIS_GENERATION_TIMEOUT_S` | 60 | Code generation timeout |
| `JARVIS_PIPELINE_TIMEOUT_S` | 150 | Full pipeline timeout |
| `JARVIS_GRADUATION_THRESHOLD` | 3 | Ephemeral tool persistence threshold |
| `JARVIS_BACKEND_STARTUP_TIMEOUT` | 300 | Backend startup timeout (DMS adds +30s) |
| `JARVIS_GCP_RECOVERY_TIMEOUT` | 450 | GCP recovery timeout |
| `JARVIS_HARDWARE_PROFILE` | (auto) | Force hardware detection (FULL for g2-standard-4) |
| `VISION_LEAN_ENABLED` | true | Enable Lean Vision Loop (Path A) |
| `VISION_LEAN_MAX_TURNS` | 15 | Max see-think-act cycles |
| `VISION_LEAN_TIMEOUT_S` | 120 | Vision loop timeout |
| `JARVIS_VISION_LOOP_ENABLED` | true | Enable VisionActionLoop at boot |
| `JARVIS_LIFECYCLE_NARRATOR_ENABLED` | true | Master voice narrator toggle |
| `JARVIS_AGI_NARRATION_ENABLED` | false | AGI OS event auto-narration |
| `OUROBOROS_VOICE_DEBOUNCE_S` | 60 | Voice narrator debounce interval |
| `OPENBLAS_CORETYPE` | ARMV8 | Fix numpy ARM64 GEMM bugs |

## Appendix C: Repository Map

```
JARVIS-AI-Agent/                     ← Body (this repo)
├── unified_supervisor.py            ← Microkernel (102K+ lines)
├── backend/
│   ├── core/
│   │   ├── prime_router.py          ← 3-tier intelligence routing
│   │   ├── prime_client.py          ← J-Prime connection with hot-swap
│   │   ├── gcp_vm_manager.py        ← GCP VM lifecycle (7400+ lines)
│   │   ├── distributed_lock_manager.py ← DLM v3.2
│   │   ├── supervisor_gcp_controller.py ← GCP orchestration
│   │   ├── supervisor_tui.py        ← Textual TUI dashboard
│   │   ├── mind_client.py           ← MindClient singleton
│   │   └── ouroboros/               ← Governance pipeline
│   │       ├── governance/
│   │       │   ├── governed_loop_service.py
│   │       │   ├── orchestrator.py
│   │       │   ├── providers.py
│   │       │   ├── context_expander.py
│   │       │   ├── trust_graduator.py
│   │       │   └── comms/
│   │       ├── intake/
│   │       │   ├── intake_layer_service.py
│   │       │   └── sensors/
│   │       └── oracle/
│   │           └── the_oracle.py
│   ├── vision/
│   │   ├── lean_loop.py             ← Lean Vision Loop (Path A)
│   │   └── realtime/                ← Legacy full pipeline
│   │       ├── vision_action_loop.py
│   │       ├── frame_pipeline.py
│   │       ├── action_executor.py
│   │       └── verification.py
│   ├── ghost_hands/                 ← Focus-preserving actuation
│   ├── intelligence/
│   │   └── unified_model_serving.py ← 3-tier model routing
│   ├── native_extensions/
│   │   └── fast_capture_stream.mm   ← Ferrari Engine (C++ SCK)
│   └── knowledge/
│       ├── fabric.py
│       ├── scene_partition.py
│       └── fabric_router.py
├── frontend/                        ← Next.js WebSocket UI
└── scripts/
    └── activate_trinity.sh          ← Activation checker

jarvis-prime/                        ← Mind (separate repo)
├── run_server.py                    ← Model server launcher
├── reasoning_sidecar.py             ← Custom FastAPI reasoning service
├── reasoning/
│   ├── protocol.py                  ← v1.0.0 Pydantic schemas
│   ├── endpoints.py                 ← /v1/reason, /v1/vision/analyze, etc.
│   ├── unified_brain_selector.py    ← 4-layer cognitive gate
│   ├── reasoning_graph.py           ← LangGraph depth-routed pipeline
│   ├── model_provider.py            ← ModelProvider protocol
│   ├── idempotency_store.py         ← SQLite request dedup
│   ├── vision_assist.py             ← LLaVA vision endpoint
│   └── graph_nodes/
│       ├── analysis_node.py
│       ├── planning_node.py
│       ├── validation_node.py       ← Fail-closed
│       └── execution_planner.py
└── knowledge/
    └── semantic_partition.py        ← L2 learned UI patterns (24h TTL)

reactor-core/                        ← Soul (separate repo)
├── ouroboros/                       ← Self-modification engine
│   ├── graduation_orchestrator.py
│   └── ledger/
├── training/
│   └── vision_calibrator.py
└── experience/                      ← 8 experience types registered
```

---

*"We do not code the infinite possibilities of the universe. We do not ask a model to solve problems that have known answers. We code the entity capable of knowing the difference — and we place the intelligence exactly where it creates leverage."*

— The Symbiotic AI-Native Manifesto v2
