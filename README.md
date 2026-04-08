# JARVIS -- Just A Rather Very Intelligent System

**The Body of the Trinity AGI OS**

JARVIS is the control plane and execution layer of a three-repository AGI ecosystem. It owns macOS integration, screen vision, voice biometric authentication, focus-preserving UI automation, and the 102K-line unified supervisor that boots and coordinates the entire stack with a single command. All model references are resolved at runtime from a shared YAML policy -- zero hardcoded model names remain anywhere in the interactive or governance pipelines.

---

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
- **Adaptive provider routing + 6-layer cost optimization** — DoubleWord 397B now supports both batch and **real-time** (`/v1/chat/completions`) with full Venom tool loop at $0.10/$0.40/M — 30-37x cheaper than Claude. Claude fallback uses **prompt caching** (90% input savings). Smart `max_tokens` during tool rounds (1024 vs 8192). Prompt compression (20KB max per file vs 65KB). Complexity routing skips Venom for trivial tasks. **50-150+ operations per $0.50 budget** (vs 5-15 before optimization).
- **Venom: Agentic Execution** — The `ToolLoopCoordinator` transforms Ouroboros from a one-shot patch generator into a multi-turn agentic loop. During generation, the provider can call `read_file`, `search_code`, `run_tests`, and `get_callers` — reading the codebase, running tests, and revising its output across multiple turns. When validation fails, the **L2 Repair Engine** takes over with an iterative `generate → test → classify failure → revise` loop (up to 5 iterations, 120s timebox). Policy enforcement via `GoverningToolPolicy` ensures all tool calls stay within repo boundaries.
- **Trinity Consciousness: Metacognition** — The self-awareness layer (Zone 6.11) with 4 core engines (HealthCortex, MemoryEngine, DreamEngine, ProphecyEngine) and 3 fusion engines (CAI, SAI, UAE). **MemoryEngine** records operation outcomes and builds per-file reputation (success rate, fragility score). **ProphecyEngine** predicts regression risk. **ConsciousnessBridge** injects this intelligence into the pipeline: regression assessment at CLASSIFY, fragile file context at GENERATE RETRY, outcome recording at POST-APPLY. The organism **learns from its own history**.
- **Strategic Direction Awareness** — The `StrategicDirectionService` reads the Manifesto (README.md) and architecture docs on boot, extracts the 7 core principles, and injects them into every operation's generation prompt. The organism understands the developer's architectural vision and generates Manifesto-compliant code — async patterns, cross-repo integrity, observability, structural repair over shortcuts.
- **Streaming output** — Both DW and Claude stream tokens character-by-character to the Rich TUI as code is being generated — like Claude Code shows its output appearing in real-time. SSE parsing for DW, `client.messages.stream()` for Claude.
- **Battle Test Runner (A+ grade)** — `scripts/ouroboros_battle_test.py` boots the full 6-layer stack. Rich TUI with provider badges, tool calls, colored diffs, streaming output, Ctrl+O expand, Ctrl+B background. 50-150+ operations per $0.50 budget. Every commit signed `Generated-By: Ouroboros + Venom + Consciousness`. Exceeds Claude Code in 9 of 15 dimensions (autonomous detection, cost optimization, cross-session learning, risk prediction, self-healing, multi-repo, strategic direction, parallel execution, budget control).

**7. Absolute observability (systemic transparency)**  
Autonomous decisions must be **visible**. A **TelemetryBus** (and related emitters) broadcasts decisions, state transitions, and errors into **live dashboards** and, where appropriate, **voice narration** — the circulatory and reporting layer of the symbiote.

### The zero-shortcut mandate

**No shortcuts whatsoever.** No brute-force retries without diagnosis. No hardcoded routing tables that encode product policy. No sequential bottlenecks that exist only because “it was easier.” If a subsystem fails or hangs, the response is **structural repair** — dismantle the flawed assumption and rebuild — not a bypass that hides the bug. We do not attempt to code every future task explicitly; we code **the entity that can survive novelty**.

### Five core execution contexts + Symbiotic Router

Large reasoning models collapse many narrow “specialist agents” into **one mind** that adapts to the task. The codebase exposes **five execution contexts** (Brain) differentiated by tools and sandbox boundaries, backed by **22 legacy Neural Mesh agents** (Peripheral Nervous System) as a Strangler Fig fallback.

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

## Trinity Architecture

The system is split across three repositories that map to **Body**, **Mind**, and **Soul**.

| Repository | Role | Responsibilities |
|---|---|---|
| **JARVIS** (this repo) | Body / Senses | macOS integration, screen capture, keyboard/mouse automation, voice I/O, vision loop, unified supervisor, Ouroboros governance engine, WebSocket + REST API, React frontend |
| **J-Prime** (jarvis-prime) | Mind / Cognition | Model serving (GGUF on NVIDIA L4), brain selection policy, reasoning graphs, `/v1/reason/*` endpoints, LangGraph orchestration |
| **Reactor Core** (reactor-core) | Soul / Immune System | Sandbox execution of generated patches, DPO training pipeline, model lineage tracking, post-deployment probation, self-improvement feedback loop |

The supervisor in this repo starts all three. J-Prime runs on a GCP `g2-standard-4` VM with an NVIDIA L4 GPU; Reactor Core runs its sandbox locally or in Docker. Communication is HTTP + WebSocket, with the `MindClient` maintaining a hysteresis-based operational level state machine (PRIMARY / DEGRADED / REFLEX) so the Body degrades gracefully when the Mind is unreachable.

---

## Key Systems

### Vision-Language-Action (VLA) Pipeline

**`backend/vision/lean_loop.py` + `backend/vision/vision_reflex.py` + `backend/vision/apple_ocr.py`**

**The Biological Analogy: Screenshots = Blinking. Video Streaming = Eyes Open.**

Traditional AI assistants (Claude Desktop, Cursor) take periodic screenshots to "see" the screen. This is like a human who blinks every 2 seconds and is blind between blinks -- they miss everything that moves. JARVIS keeps its eyes open. A continuous video stream (macOS ScreenCaptureKit, indicated by the purple recording icon) feeds raw numpy frames at 10-15fps. The BallTracker processes every frame in ~9ms -- JARVIS sees motion, predicts trajectories, and tracks objects between cloud API calls. The cloud models (235B + Claude) provide deep reasoning, like the visual cortex processing a scene. But the eyes never close.

Ouroboros is the neuroplasticity -- the brain learning to see better over time. A newborn has open eyes but cannot track a moving object. Over weeks, the visual cortex rewires itself: neurons that fire together wire together. Ouroboros does the same: the 397B reasoning model observes what the cloud models extract, then writes local numpy code that replicates that extraction in 2ms. Each graduation makes the brain faster. The eyes stay the same; the brain improves. The longer JARVIS runs, the more scenes it encounters, the more reflexes Ouroboros compiles, and the less it needs the cloud.

JARVIS sees through a three-layer parallel perception pipeline. Deterministic code handles the fast path; agentic intelligence handles novel scene understanding.

**Layer 1 -- Local OCR (deterministic skeleton, every cycle, ~2s)**
Apple Vision Framework via a compiled Swift binary extracts text from the screen. The `apple_ocr.py` bridge runs macOS-native `VNRecognizeTextRequest` at 1.00 confidence on clean text, handling glow/shadow/stylized fonts that Tesseract cannot. Visual Telemetry saves every perception frame to `/tmp/claude/vision_telemetry/` as a timestamped artifact so the operator can verify what the agent saw without altering the host environment.

**Layer 2 -- Doubleword 235B VL (structural analysis, parallel, ~8s)**
`Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` performs fast structural reads: text extraction, UI element detection, object position, quadrant classification. Fires in parallel with Layer 3 on the same frame via `asyncio.create_task()`.

**Layer 3 -- Claude Vision (semantic reasoning, parallel, ~8s)**
Claude Sonnet provides deep contextual understanding: spatial relationships, motion direction, scene description, anomaly detection. Both cloud models run concurrently -- the VLA loop never blocks on either.

**Cross-Validation Layer:**
When both cloud models return results from the same frame, their outputs are compared:
- **Numbers**: Does the 235B text read match OCR? (Consistently zero drift in testing.)
- **Position**: Do both agree on the object quadrant? (Disagreements indicate temporal lag.)
- **Motion**: Do both agree on trail direction? (High consensus on diagonal/upward/downward.)

Cross-validation data is fed to the Ouroboros feedback loop as a learning signal. Position disagreements caused by temporal lag (object moves between API calls) are the primary signal that triggers Ouroboros to compile a local reflex.

**Targeted Window Capture:**
The frame server uses `CGDisplayBounds(CGMainDisplayID())` to capture only the primary display, not virtual ghost displays. For the VLA test, `CGWindowListCreateImage` with `kCGWindowListOptionIncludingWindow` captures a specific Chrome window by ID -- JARVIS sees the target application even when the terminal has focus.

### Ouroboros Neuro-Compilation (Vision Reflex System)

**`backend/vision/vision_reflex.py`**

When the VLA cross-validation detects repeated patterns (e.g., the same OCR query firing 3+ times), Ouroboros triggers Neuro-Compilation -- using a reasoning model to write local code that replaces expensive cloud API calls with deterministic pixel-level extraction.

**The Neuro-Compilation cycle:**

1. **CognitiveInefficiencyEvent** fires after 3 repeated VLA calls (`OUROBOROS_GRADUATION_THRESHOLD`).
2. The 235B VLM analyzes the current frame (the "conscious read").
3. The 35B reasoning model (`Qwen/Qwen3.5-35B-A3B-FP8`) receives the 235B analysis + cross-validation consensus and generates a Python function that replicates the extraction locally.
4. The generated code is compiled in a sandboxed namespace with pre-populated imports (numpy, PIL, subprocess, etc.).
5. The reflex is validated against the last known-good result.
6. On validation pass, the reflex is hot-swapped into the live loop. All subsequent reads bypass the cloud API.

The reasoning model runs in the background (`asyncio.create_task`) -- the VLA loop continues uninterrupted during the ~60s synthesis. When the reflex is ready, it is hot-swapped on the next cycle.

**Tier cascade (highest performance first):**

| Tier | Method | Latency | Description |
|------|--------|---------|-------------|
| 4 | 35B-generated reflex | ~5ms | Numpy pixel analysis, centroid tracking, compiled OCR |
| 3 | Persistent OCR server | ~150ms | Pre-compiled Swift binary, Vision Framework kept warm |
| 1 | Compiled Swift binary | ~900ms | One-shot swiftc compilation, reused across calls |
| 0 | Interpreted Swift | ~2000ms | Baseline -- swift recompiles the script each invocation |

**Telemetry events:**
- `vision.perception@1.0.0` -- every captured frame with artifact path
- `ouroboros.cognitive_inefficiency@1.0.0` -- graduation threshold breached
- `ouroboros.reflex_graduation@1.0.0` -- successful reflex assimilation

**Current baseline: 13.7fps (27x over screenshots). Target: 60fps.**

The zero-copy shared memory ring buffer (`shm_frame_bridge.h`) eliminates the pybind11 GIL bottleneck. The C++ SCK daemon writes BGRA frames into a 5-slot POSIX shm ring buffer at up to 21fps. Python reads the latest slot via `numpy.frombuffer()` -- zero copy, zero GIL. Two C++ bugs block the path to 60fps (documented as Ouroboros CapabilityGapEvents for 397B resolution):

1. **Retina Stride Bug**: `bytesPerRow != width * 4` on retina displays causes the C++ downsampler to produce mixed-resolution frames. The SHM writer receives alternating logical/retina coordinates, breaking velocity tracking.
2. **Window Crop Bug**: Chrome window detection via `CGWindowListCopyWindowInfo` fails intermittently when the terminal steals focus, forcing full-screen processing and green-pixel noise from non-ball UI elements.

### Lean Vision Loop (UI Automation)

**`backend/vision/lean_loop.py`**

The underlying capture-think-act loop for UI automation tasks. Supports Claude Computer Use native API and an agentic fallback with provider cascade (Doubleword 235B VL, Claude Vision, J-Prime LLaVA). Each turn captures a screenshot at CU display resolution (1280x800), sends it with visual memory to the model, parses the action response, executes via pyautogui with Retina coordinate scaling, and verifies via pixel-diff. All tunables are environment-variable driven.

### Ghost Hands

**`backend/ghost_hands/background_actuator.py`**

Focus-preserving UI automation. Ghost Hands executes actions on background windows without stealing keyboard focus from the user's active window. Three backends are available: Playwright for browser DOM manipulation, AppleScript/JXA for native macOS apps, and Quartz CGEvent injection for low-level input. A `FocusGuard` singleton saves and restores the frontmost application around every action.

### Ouroboros Governance Engine

**`backend/core/ouroboros/`**

The self-developing code pipeline — the organism's immune system and neuroplasticity layer. Ouroboros runs a 10-phase governance pipeline (CLASSIFY → ROUTE → CONTEXT_EXPANSION → GENERATE → VALIDATE → GATE → APPROVE → APPLY → VERIFY → COMPLETE) that detects improvement opportunities, generates multi-repo patches, validates them in sandboxed worktrees, applies them through branch-isolated sagas, and narrates every decision via voice and TUI.

**10-Phase Pipeline:**

| Phase | Component | Function |
|---|---|---|
| CLASSIFY | RiskEngine + ComplexityClassifier | Deterministic risk tier assignment |
| ROUTE | BrainSelector + RouteDecisionService | Policy-driven provider selection |
| CONTEXT_EXPANSION | ContextExpander + TheOracle + DocFetcher + WebSearch | Semantic file neighborhood + external docs + web search with epistemic allowlist |
| GENERATE | DoublewordProvider → PrimeProvider → ClaudeProvider | 3-tier failback code generation (397B → 7B → Claude) |
| VALIDATE | TestRunner (Python + C++ adapters) | Flake detection, structured critique, episodic memory |
| GATE | PolicyEngine + ContractGate | Declarative YAML rules, FSM contract validation |
| APPROVE | ApprovalProvider | Human-in-the-loop gate (idempotent, timeout → EXPIRED) |
| APPLY | ChangeEngine + SagaApplyStrategy + InfrastructureApplicator | Transactional file writes + deterministic post-apply hooks (pip install, env reload) |
| VERIFY | ShadowHarness + PatchBenchmarker + Shannon Entropy | Structural integrity + performance + composite ignorance measurement |
| COMPLETE | Ledger + LearningBridge | Immutable audit trail + outcome feedback for future operations |

**13 Autonomous Sensors (Intake Layer):**

| Sensor | Detects | Poll Interval |
|---|---|---|
| TestFailureSensor | Real-time pytest failures (streak-based stability) | Event-driven |
| VoiceCommandSensor | Voice intents with STT confidence gating | Event-driven |
| OpportunityMinerSensor | Cyclomatic complexity violations via AST | Hourly |
| CapabilityGapSensor | Neural mesh capability boundaries | Event-driven |
| ScheduledTriggerSensor | Cron-based governance operations (YAML config) | Configurable |
| BacklogSensor | `.jarvis/backlog.json` task queue | 30s |
| RuntimeHealthSensor | Python EOL, package staleness, import errors, security audit, legacy shim detection | Daily |
| WebIntelligenceSensor | PyPI CVE/advisory vulnerabilities against installed packages | Daily |
| PerformanceRegressionSensor | P50 latency drift, success rate drops, code quality degradation | Hourly |
| DocStalenessSensor | Undocumented Python modules via AST analysis | Daily |
| GitHubIssueSensor | Open issues across JARVIS/J-Prime/Reactor — auto-resolve bugs, test failures, dependency issues | Hourly |
| ProactiveExplorationSensor | High-entropy domains identified for curiosity-driven learning | 2 hours |
| CrossRepoDriftSensor | Contract/schema hash drift across Trinity repos | Hourly |

**Shannon Entropy Calculator (Pillar 4 — Synthetic Soul):**

Computes a CompositeEntropySignal from two deterministic sources:
- **Acute Ignorance** (per-generation): validation pass/fail, critique severity distribution, shadow harness confidence, retry exhaustion
- **Chronic Ignorance** (historical domain): failure rate and outcome entropy from LearningBridge history

Fused into a SystemicEntropyScore via `H(X) = -Σ p·log₂(p)` with a 4-quadrant decision matrix:

| Acute | Chronic | Quadrant | Action |
|---|---|---|---|
| High | High | IMMEDIATE_TRIGGER | Emit CapabilityGapEvent → Ouroboros neuroplasticity |
| High | Low | WARNING_RETRY | Bad prompt, not bad domain — retry with adjusted context |
| Low | High | FALSE_CONFIDENCE | Force sandbox validation despite passing tests |
| Low | Low | HEALTHY | No action needed |

**Infrastructure Applicator (Boundary Principle):**

Deterministic post-APPLY hook. When the agentic layer modifies `requirements.txt`, the deterministic skeleton automatically runs `pip install`. When `.env` is modified, it reloads environment variables in-process. The agentic layer decides WHAT to change; the skeleton executes the KNOWN consequence.

| File Modified | Deterministic Action |
|---|---|
| `requirements.txt` | `venv/bin/pip install -r requirements.txt` |
| `package.json` | `npm install` |
| `.env`, `backend/.env` | In-process env var reload (additive merge) |

**Graduation Orchestrator (Pillar 6):**

Converts ephemeral tools into permanent agents: TRACKING → EVALUATING → WORKTREE_CREATING → GENERATING → VALIDATING → COMMITTING → AWAITING_APPROVAL → PUSHING → AWAITING_MERGE → REGISTERING → GRADUATED. After `JARVIS_GRADUATION_THRESHOLD` (default 3) successful uses, synthesizes production-ready agent code, runs contract tests, creates a Git PR, and hot-loads the new agent on merge.

**Web Search Capability (Epistemic Allowlist):**

Structured web search for CONTEXT_EXPANSION. When the 397B Architect encounters a capability gap, it can search for solutions across developer-verified domains. Three backends: DuckDuckGo (free, default, no API key), Brave Search API, Google Custom Search.

Safety: Results are domain-restricted to an epistemic allowlist of 28 high-signal developer domains (stackoverflow.com, github.com, docs.python.org, readthedocs.io, pytorch.org, etc.). Results from unverified blogs, social media, and random websites are silently dropped to prevent prompt injection. Bounded: 3 results max, 10s timeout, 6K chars per page.

**Adaptive Learning System:**

Three interconnected learning components that make the organism smarter over time:

- **LearningConsolidator**: Periodically synthesizes domain-level rules from outcome history. "Domain X fails 67% of the time due to import_error" becomes actionable context injected into future generation prompts. Persisted to `~/.jarvis/ouroboros/learning/`.
- **SuccessPatternStore**: Records successful (domain, context, approach) triples. On future similar tasks, injects "a similar task succeeded with this approach." The positive counterpart to EpisodicMemory's failure tracking — the organism learns from what WORKS, not just what breaks.
- **ThresholdTuner**: Analyzes false positive and miss rates for each threshold parameter. If the system triggers too often without value (FP > 40%), raises threshold. If regressions slip through (miss > 30%), lowers threshold. Self-calibrating organism.

**Intelligence Hooks (Pre-GENERATE):**

- **TestCoverageEnforcer**: Checks if target files have existing test coverage. If zero tests exist, injects "also generate tests" into the generation prompt. No code ships untested.
- **TestGenerationHook**: Detects when candidates create new modules without companion test files. Flags for the retry/repair loop.
- **SemanticReviewGate**: Path-based pre-filter for the existing SecurityReviewer. Identifies security-sensitive files (auth, crypto, unlock, supervisor) for focused LLM-as-a-Judge review before APPROVE.

**GitHub Issue Auto-Resolution:**

The organism fixes its own bugs. GitHubIssueSensor polls open issues across all three Trinity repos (JARVIS, J-Prime, Reactor) via the `gh` CLI. Issues are classified by labels and content to determine urgency and whether Ouroboros can auto-resolve them (test failures, dependency issues, tracebacks → yes; design decisions, architecture changes → requires human). Recurring issues (e.g., daily "Unlock Test Suite Failed") are deduplicated and emitted as a single high-priority envelope with `recurring_count`.

### Voice Biometric Authentication

**`backend/voice_unlock/`**

Speaker verification using ECAPA-TDNN embeddings (192-dimensional vectors). Voiceprints are stored in Cloud SQL. The system captures audio continuously, extracts embeddings, and compares them against enrolled profiles with an 85% cosine similarity threshold. Supports contextual awareness (time-of-day, location, microphone type), continuous learning from successful unlocks, and anti-spoofing detection. The unlock flow is wired through `backend/api/voice_unlock_api.py`.

### Voice-First Interactive Conversation

**`backend/voice/conversation_manager.py` + `barge_in_detector.py` + `jarvis_voice_bridge.py`**

The primary interface is voice. Derek talks to JARVIS and JARVIS talks back — no keyboard required.

**ConversationManager** classifies 11 utterance types (greeting, status, code task, code question, confirmation, denial, emergency, positive/negative feedback, farewell, ambient) via deterministic keyword matching and routes each to the appropriate handler. Multi-turn context tracks the last 10 turns with topic continuity, pending questions, and active operation status.

**BargeInDetector** monitors audio capture energy (RMS) every 50ms during TTS playback. If Derek speaks while JARVIS is talking, the afplay process is killed immediately (SIGTERM → SIGKILL) and audio capture resumes. Like Alexa/Siri — interrupt anytime.

**JarvisVoiceBridge** is the integration glue: registered as a transcript hook on `RealTimeVoiceCommunicator`, all transcribed speech flows through the ConversationManager. Code tasks route to `VoiceCommandSensor` → Ouroboros pipeline. Proactive speech (predictions, emergencies, milestones) can be injected via `inject_proactive()`.

**ProactiveSpeechEngine** allows JARVIS to speak first — predictions, emergency alerts, operation completions, milestones — with configurable debounce (30s default).

### JARVIS-Level Intelligence (7 Tiers)

**`backend/core/ouroboros/governance/` — 7 tiers of autonomous intelligence**

| Tier | Module | What It Does |
|---|---|---|
| **1. Proactive Judgment** | `operation_advisor.py` | Evaluates blast radius, test coverage, chronic entropy, time, staleness. BLOCK / ADVISE_AGAINST / CAUTION / RECOMMEND. "I wouldn't recommend that, sir." |
| **2. Emergency Protocols** | `emergency_protocols.py` | 5-level escalation: GREEN → YELLOW → ORANGE → RED → HOUSE PARTY. Alert accumulation with exponential decay. Named protocols: HOUSE_PARTY, CLEAN_SLATE, IRON_LEGION, VERONICA. |
| **3. Predictive Intelligence** | `predictive_engine.py` | Anticipates regressions: code velocity (22 changes/7d → 100% risk), dependency fragility, test decay, resource trajectory. Background task every 4h. |
| **4. Self-Preservation** | `distributed_resilience.py` | Heartbeat to GCP (60s), state sync (5m), automatic failover if primary offline for 5 min. Survives crashes. |
| **5. Cross-Domain Reasoning** | `jarvis_intelligence.py` | Fuses code + infrastructure + user behavior + security + business into unified insights. |
| **6. Personality** | `jarvis_intelligence.py` | 5 states: CONFIDENT / CAUTIOUS / CONCERNED / PROUD / URGENT. Deterministic from metrics. Voice templates per state. |
| **7. Autonomous Judgment** | `jarvis_intelligence.py` | Daily self-review, strategic planning, value alignment (7 explicit principles). The organism governs its own evolution. |

### Self-Evolution Engine

**`backend/core/ouroboros/governance/self_evolution.py`**

9 research-grade self-programming techniques from 5 academic papers:

1. **Runtime Prompt Adaptation** (Live-SWE-Agent) — prompts evolve based on execution outcomes
2. **Module-Level Mutation** (CSE) — surgical function-level code evolution via AST
3. **Negative Constraints** (CSE) — explicit "never do X" rules from failed attempts
4. **Code Metrics Feedback** (SPA) — complexity, docstring coverage, line count drive generation
5. **Dynamic Re-Planning** (Devin v3.0) — pattern-matched failure → alternative strategy
6. **Multi-Version Evolution** (SWE-EVO) — epoch-based tracking: improving / stable / degrading
7. Generate-Verify-Refine cycle (strengthened)
8. Hierarchical Memory with positive/negative distinction (strengthened)
9. Repository auto-documentation via CodeMetricsAnalyzer

### Advanced Repair Techniques

**`backend/core/ouroboros/governance/advanced_repair.py`**

3 state-of-the-art APR techniques from 2026 research:

1. **Hierarchical Fault Localization** (Agentless + RepoRepair) — 3-stage narrowing: file → function → line. Reduces prompt size ~10x.
2. **Slow/Fast Thinking Router** (SIADAFIX) — simple fixes get 0.5x tokens, complex get 2x + force Tier 0.
3. **Documentation-Augmented Repair** (RepoRepair) — auto-generate docs via AST FIRST, use as repair context.

### Unified Supervisor

**`unified_supervisor.py`** (102K lines)

The monolithic kernel and single entry point for the entire ecosystem. Organized into seven zones:

| Zone | Name | Purpose |
|---|---|---|
| 0 | Early Protection | Signal handling, virtualenv detection, fast-fail checks |
| 1 | Foundation | Imports, configuration, constants |
| 2 | Core Utilities | Logging, distributed locks, retry logic |
| 3 | Resource Managers | Docker, GCP VM lifecycle, port management, storage |
| 4 | Intelligence Layer | ML model routing, goal inference, SAI |
| 5 | Process Orchestration | Signal handling, cleanup, hot reload, Trinity coordination |
| 6 | The Kernel | `JarvisSystemKernel` class, Ouroboros governance (Zones 6.0--6.9) |
| 7 | Entry Point | CLI argument parsing, `main()` |

Design principles: async-first parallel initialization, graceful degradation (components fail independently), self-healing (auto-restart crashed processes), lazy ML model loading, and adaptive thresholds that learn from outcomes.

---

## Model Routing (3-Tier Cascade)

All inference requests flow through `PrimeRouter` (`backend/core/prime_router.py`) and `MindClient` (`backend/core/mind_client.py`), which implement a tiered fallback cascade.

| Tier | Name | Backend | Models | When Used |
|---|---|---|---|---|
| 0 | PRIMARY | Doubleword Batch API | Qwen3.5-397B-A17B (reasoning), Qwen3.5-35B-A3B (fast), vision and Nemotron variants | Latency-insensitive tasks above complexity 0.85; 29x cheaper than J-Prime VM time for batch workloads |
| 1 | SECONDARY | Anthropic Claude API | claude-sonnet-4, claude-haiku-4-5 | Real-time interactive commands, vision verification, fallback when J-Prime is unavailable |
| 2 | TERTIARY | GCP J-Prime (self-hosted) | Qwen2.5-Coder-7B/14B/32B, LLaVA v1.5, Phi-3 Mini | On-demand when the GCP VM is running; free per-request (VM spot cost only) |

Brain selection for interactive commands is handled by `InteractiveBrainRouter` (`backend/core/interactive_brain_router.py`), which reads `brain_selection_policy.yaml` and maps task types to complexity tiers at runtime. Both the interactive router and the Ouroboros `BrainSelector` share the same YAML policy file as their single source of truth.

---

## Quick Start

```bash
# Clone
git clone https://github.com/yourusername/JARVIS-AI-Agent.git
cd JARVIS-AI-Agent

# Install dependencies (Python 3.12+ recommended)
pip install -r requirements.txt

# Configure environment
cp backend/.env.example backend/.env
# Edit backend/.env -- add at minimum: ANTHROPIC_API_KEY

# Launch the full stack
python3 unified_supervisor.py

# Or launch with options
python3 unified_supervisor.py --skip-docker --skip-gcp   # local-only, no cloud
python3 unified_supervisor.py --mode production           # no hot reload
python3 unified_supervisor.py --status                    # check running kernel
python3 unified_supervisor.py --shutdown                  # graceful stop
```

The supervisor auto-detects available components and starts what it can. GCP VM, Docker, and J-Prime are optional -- the system degrades gracefully to Claude API when they are unavailable.

---

## Environment Variables

Core configuration. All values have sensible defaults; only `ANTHROPIC_API_KEY` is required for basic operation.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | *(required)* | Claude API access for vision, reasoning, and fallback inference |
| `DOUBLEWORD_API_KEY` | *(empty)* | Doubleword batch API access (Tier 0 routing + VLA vision) |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Default Doubleword model for batch inference |
| `DOUBLEWORD_VISION_MODEL` | `Qwen/Qwen3-VL-235B-A22B-Instruct-FP8` | Doubleword VLM for Layer 2 structural vision |
| `DOUBLEWORD_ARCHITECT_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Reasoning model for Ouroboros Neuro-Compilation |
| `JARVIS_CLAUDE_VISION_MODEL` | `claude-sonnet-4-20250514` | Claude model for Layer 3 semantic vision |
| `VISION_LEAN_ENABLED` | `true` | Enable the Lean Vision Loop (set `false` for legacy pipeline) |
| `VISION_LEAN_MAX_TURNS` | `10` | Maximum see-think-act iterations per vision task |
| `OUROBOROS_GRADUATION_THRESHOLD` | `3` | Repeated VLA calls before Ouroboros triggers Neuro-Compilation |
| `VISION_TELEMETRY_DIR` | `/tmp/claude/vision_telemetry` | Directory for Visual Telemetry perception artifacts |
| `VISION_TELEMETRY_MAX_ARTIFACTS` | `50` | Rolling window of saved perception frames |
| `JARVIS_TELEMETRY_ENABLED` | `true` | Emit telemetry events (disk + optional remote) |
| `JARVIS_PROACTIVE_MONITORING` | `false` | Enable proactive screen analysis and suggestions |
| `JARVIS_GOVERNANCE_MODE` | `sandbox` | Ouroboros governance mode: `sandbox`, `observe`, or `governed` |
| `JARVIS_SAGA_BRANCH_ISOLATION` | `false` | Enable branch-isolated sagas for Ouroboros patches |
| `JARVIS_INFRA_APPLICATOR_ENABLED` | `true` | Enable deterministic post-APPLY hooks (pip install, env reload) |
| `JARVIS_ENTROPY_SYSTEMIC_THRESHOLD` | `0.7` | Shannon entropy threshold for CapabilityGapEvent emission |
| `JARVIS_ENTROPY_ACUTE_WEIGHT` | `0.6` | Weight of per-generation signal in composite entropy |
| `JARVIS_ENTROPY_CHRONIC_WEIGHT` | `0.4` | Weight of historical domain signal in composite entropy |
| `JARVIS_WEB_INTEL_INTERVAL_S` | `86400` | WebIntelligenceSensor poll interval (seconds) |
| `JARVIS_RUNTIME_HEALTH_INTERVAL_S` | `86400` | RuntimeHealthSensor poll interval (seconds) |
| `JARVIS_PERF_REGRESSION_INTERVAL_S` | `3600` | PerformanceRegressionSensor poll interval (seconds) |
| `JARVIS_CTX_EXECUTOR` | `false` | Enable Executor Core Context (Strangler Fig migration) |
| `JARVIS_CTX_COMMUNICATOR` | `false` | Enable Communicator Core Context |
| `JARVIS_CTX_DEVELOPER` | `false` | Enable Developer Core Context |
| `JARVIS_CTX_OBSERVER` | `false` | Enable Observer Core Context |
| `BRAVE_SEARCH_API_KEY` | *(empty)* | Brave Search API (optional — DuckDuckGo is free default) |
| `JARVIS_GITHUB_ISSUE_INTERVAL_S` | `3600` | GitHubIssueSensor poll interval (seconds) |
| `JARVIS_EXPLORATION_INTERVAL_S` | `7200` | ProactiveExplorationSensor poll interval (seconds) |
| `JARVIS_DRIFT_DETECTION_INTERVAL_S` | `3600` | CrossRepoDriftSensor poll interval (seconds) |
| `JARVIS_SEMANTIC_REVIEW_ENABLED` | `true` | Enable semantic code review gate for sensitive files |
| `JARVIS_VOICE_ENABLED` | `true` | Enable voice input/output |
| `JARVIS_AUDIO_BUS_ENABLED` | `false` | Enable real-time full-duplex audio bus |
| `JARVIS_DEBUG` | `false` | Verbose debug logging |
| `BACKEND_PORT` | `8000` | HTTP/WebSocket server port |

See `backend/.env.example` for the complete list with descriptions.

---

## Project Structure

```
JARVIS-AI-Agent/
|-- unified_supervisor.py          # 102K-line monolithic kernel (Zones 0-7)
|-- backend/
|   |-- api/                       # WebSocket + REST endpoints (FastAPI)
|   |-- core/
|   |   |-- mind_client.py         # Body-to-Mind HTTP bridge (hysteresis state machine)
|   |   |-- prime_router.py        # Central inference router with fallback cascade
|   |   |-- interactive_brain_router.py  # Task-type brain selection from YAML policy
|   |   |-- runtime_task_orchestrator.py # Routes voice commands to vision/app/browser
|   |   |-- gcp_vm_manager.py      # GCP VM lifecycle (create, start, stop, health)
|   |   |-- distributed_lock_manager.py  # DLM v3.2 (Redis + file fallback)
|   |   |-- telemetry_emitter.py   # Observability event pipeline
|   |   `-- ouroboros/              # Self-developing governance engine
|   |       |-- governance/
|   |       |   |-- governed_loop_service.py  # Main autonomous loop (Zone 6.8)
|   |       |   |-- orchestrator.py           # 10-phase FSM pipeline
|   |       |   |-- brain_selector.py         # Model selection + boot handshake
|   |       |   |-- brain_selection_policy.yaml  # Single source of truth for all model routing
|   |       |   |-- providers.py              # PrimeProvider + ClaudeProvider
|   |       |   |-- doubleword_provider.py    # Tier 0: Doubleword 397B batch API
|   |       |   |-- entropy_calculator.py     # Shannon entropy composite ignorance measurement
|   |       |   |-- infrastructure_applicator.py  # Deterministic post-APPLY hooks (pip, npm, env)
|   |       |   |-- doc_fetcher.py            # Bounded external doc retrieval for CONTEXT_EXPANSION
|   |       |   |-- candidate_generator.py    # 3-tier failback: Doubleword -> J-Prime -> Claude
|   |       |   |-- change_engine.py          # Transactional file writes with rollback
|   |       |   |-- repair_engine.py          # Iterative self-repair on validation failures
|   |       |   |-- test_runner.py            # Multi-adapter test validation (Python + C++)
|   |       |   |-- graduation_orchestrator.py  # Ephemeral -> permanent agent via Git PR
|   |       |   |-- saga/                     # Branch-isolated multi-repo patch application
|   |       |   |-- autonomy/                 # L3 subagent scheduler + execution graphs
|   |       |   |   |-- subagent_scheduler.py # Parallel work unit dispatch (DAG)
|   |       |   |   |-- iteration_planner.py  # Goal -> ExecutionGraph decomposition
|   |       |   |   `-- iteration_service.py  # 10-state autonomy FSM
|   |       |   `-- intake/                   # 10 autonomous sensors
|   |       |       |-- intake_layer_service.py  # Sensor lifecycle (Zone 6.9)
|   |       |       |-- unified_intake_router.py # Priority queue + dedup + WAL
|   |       |       `-- sensors/
|   |       |           |-- test_failure_sensor.py
|   |       |           |-- voice_command_sensor.py
|   |       |           |-- opportunity_miner_sensor.py
|   |       |           |-- capability_gap_sensor.py
|   |       |           |-- scheduled_sensor.py
|   |       |           |-- backlog_sensor.py
|   |       |           |-- runtime_health_sensor.py
|   |       |           |-- web_intelligence_sensor.py
|   |       |           |-- performance_regression_sensor.py
|   |       |           `-- doc_staleness_sensor.py
|   |       `-- oracle.py                     # Codebase semantic index
|   |-- core_contexts/                # 5 Core Execution Contexts (Brain)
|   |   |-- facade.py                 # Symbiotic Router: 3-tier dispatch
|   |   |-- executor.py              # Screen vision, clicks, typing, app navigation
|   |   |-- architect.py             # DAG planning, goal decomposition, context selection
|   |   |-- developer.py             # Code generation, review, testing, Ouroboros
|   |   |-- communicator.py          # Email, calendar, messaging, web search
|   |   |-- observer.py              # Monitoring, anomaly detection, pattern recognition
|   |   `-- tools/                   # 11 atomic tool modules (screen, input, apps, etc.)
|   |-- neural_mesh/                  # 22 Legacy Agents (Peripheral Nervous System)
|   |   |-- agents/                   # Production agents (30K+ lines total)
|   |   |   |-- google_workspace_agent.py   # 6.7K lines: Gmail, Calendar, Drive, Contacts
|   |   |   |-- visual_monitor_agent.py     # 11K lines: background visual surveillance
|   |   |   |-- native_app_control_agent.py # 1.3K lines: macOS app automation
|   |   |   `-- ...                         # 19 more specialized agents
|   |   `-- synthesis/                # Capability gap detection + JIT agent synthesis
|   |       |-- gap_signal_bus.py     # Fire-and-forget CapabilityGapEvent bus
|   |       `-- agent_synthesis_loader.py  # J-Prime-driven agent synthesis
|   |-- vision/
|   |   |-- lean_loop.py           # VLA capture-think-act loop + Visual Telemetry
|   |   |-- vision_reflex.py       # Ouroboros Neuro-Compilation (reflex compiler)
|   |   |-- apple_ocr.py           # Apple Vision Framework OCR bridge (Swift)
|   |   |-- frame_server.py        # Persistent Quartz screen capture (main display only)
|   |   |-- realtime/              # Real-time frame pipeline
|   |   `-- handlers/              # Vision event handlers
|   |-- ghost_hands/
|   |   |-- background_actuator.py # Focus-preserving UI automation
|   |   |-- cgevent_worker.py      # Quartz low-level event injection
|   |   `-- orchestrator.py        # Ghost Hands task coordination
|   |-- voice_unlock/
|   |   |-- core/                  # ECAPA-TDNN embedding, verification logic
|   |   |-- services/              # Unlock service orchestration
|   |   |-- ml/                    # ML feature extraction
|   |   `-- security/              # Anti-spoofing, secure password typing
|   |-- intelligence/
|   |   `-- unified_model_serving.py  # 3-tier inference (PRIME_API / PRIME_LOCAL / CLAUDE)
|   `-- voice/                     # Voice I/O, wake word, TTS
|-- frontend/
|   |-- src/                       # React UI
|   `-- public/                    # Static assets
|-- docs/                          # Architecture and integration documentation
|-- benchmarks/                    # Performance and cost benchmarks
|-- tests/                         # Test suites
|-- config/                        # Runtime configuration files
|-- scripts/                       # Utility and deployment scripts
`-- requirements.txt               # Python dependencies
```

---

## Documentation

The **Symbiotic AI-Native Manifesto** (Trinity unification, seven principles, five execution contexts, zero-shortcut mandate) is the authoritative philosophical and architectural preamble in **this file**, under [Symbiotic AI-Native Manifesto: Trinity Ecosystem Unification](#symbiotic-ai-native-manifesto-trinity-ecosystem-unification).

Detailed documentation also lives in the `docs/` directory.

| Document | Path | Covers |
|---|---|---|
| Symbiotic manifesto (Trinity OS) | `README.md` | Genesis thesis, progressive awakening, Ouroboros neuroplasticity, observability, five contexts vs agent sprawl |
| **Trinity Ecosystem Technical Document** | `docs/architecture/TRINITY_ECOSYSTEM_TECHNICAL_DOCUMENT.md` | Full architecture, 20+ academic references (SOAR, VSM, Shannon, Brooks, Kahneman), subsystem deep dives, comparative analysis vs Claude Desktop/Code |
| Ouroboros architecture | `docs/architecture/OUROBOROS.md` | Governance pipeline, graduation, sandbox vs assimilation |
| Brain routing | `docs/architecture/BRAIN_ROUTING.md` | 3-tier cascade, Doubleword Tier 0, brain selection policy |
| Doubleword Integration | `docs/integrations/DOUBLEWORD_INTEGRATION.md` | Tier 0 batch inference, 397B MoE reasoning, cost benchmarks, async batch protocol |
| **JARVIS-Level Ouroboros** | `docs/architecture/JARVIS_LEVEL_OUROBOROS.md` | 7 tiers of transcendence: proactive judgment, emergency protocols, predictive intelligence, self-preservation, cross-domain reasoning, personality, autonomous judgment |
| **Voice-First Conversation** | `docs/architecture/VOICE_FIRST_CONVERSATION.md` | ConversationManager, barge-in detection, proactive speech, multi-turn context, utterance classification, personality-aware responses |
| Async Architecture | `docs/architecture/async-architecture.md` | Event loop design, cooperative cancellation, async-first patterns |
| WebSocket Architecture | `docs/architecture/websocket-architecture.md` | Real-time communication protocol between frontend and backend |
| Voice Sidecar Control Plane | `docs/architecture/VOICE_SIDECAR_CONTROL_PLANE.md` | Voice pipeline orchestration and audio bus design |
| Ouroboros Production Readiness | `docs/ouroboros_production_readiness.md` | Governance pipeline deployment checklist |
| Vision System | `docs/vision/` | Vision pipeline architecture and configuration |
| Voice Unlock | `docs/voice_unlock/` | Voice biometric authentication flow diagrams |

Additional inline documentation is embedded in module docstrings throughout the codebase. The unified supervisor's zone headers serve as navigational landmarks for the 102K-line kernel.

---

## Platform Requirements

| Requirement | Details |
|---|---|
| Operating System | macOS (Apple Silicon recommended; uses CoreAudio, Quartz, AppleScript) |
| Python | 3.12+ |
| Node.js | 18+ (for React frontend) |
| API Keys | Anthropic (required), Doubleword (optional, for Tier 0) |
| GCP (optional) | `g2-standard-4` + NVIDIA L4 in `us-central1-b` for J-Prime self-hosted inference |
| System Permissions | Screen Recording, Accessibility, Microphone (macOS will prompt on first use) |

---

## Contributing

1. Fork the repository and create a feature branch.
2. Follow existing code conventions: async-first, environment-variable-driven configuration, no hardcoded model names or magic strings.
3. Ensure the Ouroboros governance pipeline passes (`JARVIS_GOVERNANCE_MODE=sandbox`).
4. Submit a pull request with a clear description of the change and its motivation.

For changes that affect model routing, update `brain_selection_policy.yaml` -- do not add model names to Python source files.

---

## License

This project is proprietary software. All rights reserved.
