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

**7. Absolute observability (systemic transparency)**  
Autonomous decisions must be **visible**. A **TelemetryBus** (and related emitters) broadcasts decisions, state transitions, and errors into **live dashboards** and, where appropriate, **voice narration** — the circulatory and reporting layer of the symbiote.

### The zero-shortcut mandate

**No shortcuts whatsoever.** No brute-force retries without diagnosis. No hardcoded routing tables that encode product policy. No sequential bottlenecks that exist only because “it was easier.” If a subsystem fails or hangs, the response is **structural repair** — dismantle the flawed assumption and rebuild — not a bypass that hides the bug. We do not attempt to code every future task explicitly; we code **the entity that can survive novelty**.

### Five core execution contexts (why not sixty agents)

Large reasoning models collapse many narrow “specialist agents” into **one mind** that adapts to the task. The codebase should expose **a small number of execution contexts** differentiated by **tools and sandbox boundaries**, not by dozens of parallel `if` branches.

| Context | Responsibility | Typical model tier (policy-driven) |
|---|---|---|
| **Executor** | Sees the screen, clicks, types, navigates apps — **motor loop** | Vision-capable / multimodal tier |
| **Architect** | Decomposes goals into **DAGs**, assigns steps, reconciles failures | Highest reasoning tier |
| **Developer** | Reads and writes code across repos, tests, PR flow | Same reasoning / coding tier as policy allows |
| **Communicator** | Email, calendar, messaging — protocol-heavy I/O | Fast tier by default; heavy tier when ambiguity is high |
| **Observer** | Monitors screen / logs, anomaly detection, briefings | Vision or fast tier depending on sensitivity |

**Intelligence lives in the model and the plan**, not in the number of Python classes. Agent code supplies: **tools** (filesystem, browser, screen, APIs), **execution locale** (live vs sandbox), and **safety gates** (human approval for destructive actions).

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

### Lean Vision Loop (Path A)

**`backend/vision/lean_loop.py`**

A stripped-down three-step see-think-act loop that replaced the original 12-hop pipeline. Each turn captures a Retina-aware screenshot, sends it to Claude Vision for spatial reasoning, and executes the returned action (click, type, scroll, keyboard shortcut) via pyautogui with coordinate scaling. The loop runs up to `VISION_LEAN_MAX_TURNS` iterations (default 10), settling between turns to let the UI react. All tunables are environment-variable driven.

### Ghost Hands

**`backend/ghost_hands/background_actuator.py`**

Focus-preserving UI automation. Ghost Hands executes actions on background windows without stealing keyboard focus from the user's active window. Three backends are available: Playwright for browser DOM manipulation, AppleScript/JXA for native macOS apps, and Quartz CGEvent injection for low-level input. A `FocusGuard` singleton saves and restores the frontmost application around every action.

### Ouroboros Governance

**`backend/core/ouroboros/`**

The self-developing code pipeline. Ouroboros detects improvement opportunities (test failures, mined patterns), generates multi-repo patches via J-Prime or Claude (schema 2c.1), validates them, applies them through branch-isolated sagas with two-tier locks and fast-forward-only promote gates, and narrates every decision via voice and TUI. Key modules: `governed_loop_service.py` (main loop), `orchestrator.py` (FSM pipeline), `providers.py` (code generation), `brain_selector.py` (model selection), `saga/` (branch isolation and rollback), `intake/` (sensors for test failures and opportunities).

### Voice Biometric Authentication

**`backend/voice_unlock/`**

Speaker verification using ECAPA-TDNN embeddings (192-dimensional vectors). Voiceprints are stored in Cloud SQL. The system captures audio continuously, extracts embeddings, and compares them against enrolled profiles with an 85% cosine similarity threshold. Supports contextual awareness (time-of-day, location, microphone type), continuous learning from successful unlocks, and anti-spoofing detection. The unlock flow is wired through `backend/api/voice_unlock_api.py`.

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
| `DOUBLEWORD_API_KEY` | *(empty)* | Doubleword batch API access (Tier 0 routing) |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-35B-A3B-FP8` | Default Doubleword model for batch inference |
| `JARVIS_CLAUDE_VISION_MODEL` | `claude-sonnet-4-20250514` | Claude model used by the Lean Vision Loop |
| `VISION_LEAN_ENABLED` | `true` | Enable the 3-step Lean Vision Loop (set `false` for legacy pipeline) |
| `VISION_LEAN_MAX_TURNS` | `10` | Maximum see-think-act iterations per vision task |
| `JARVIS_TELEMETRY_ENABLED` | `true` | Emit telemetry events (disk + optional remote) |
| `JARVIS_PROACTIVE_MONITORING` | `false` | Enable proactive screen analysis and suggestions |
| `JARVIS_GOVERNANCE_MODE` | `sandbox` | Ouroboros governance mode: `sandbox`, `observe`, or `governed` |
| `JARVIS_SAGA_BRANCH_ISOLATION` | `false` | Enable branch-isolated sagas for Ouroboros patches |
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
|   |       |   |-- governed_loop_service.py  # Main autonomous loop
|   |       |   |-- orchestrator.py           # FSM pipeline (classify-route-generate-validate-apply)
|   |       |   |-- brain_selector.py         # Model selection for code generation
|   |       |   |-- brain_selection_policy.yaml  # Single source of truth for all model routing
|   |       |   |-- providers.py              # J-Prime + Claude code generation backends
|   |       |   |-- saga/                     # Branch-isolated patch application
|   |       |   `-- intake/                   # Sensors (test failures, opportunities)
|   |       `-- oracle.py                     # Codebase semantic index
|   |-- vision/
|   |   |-- lean_loop.py           # 3-step see-think-act vision loop (Path A)
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
| Ouroboros architecture | `docs/architecture/OUROBOROS.md` | Governance pipeline, graduation, sandbox vs assimilation |
| Async Architecture | `docs/architecture/async-architecture.md` | Event loop design, cooperative cancellation, async-first patterns |
| WebSocket Architecture | `docs/architecture/websocket-architecture.md` | Real-time communication protocol between frontend and backend |
| Voice Sidecar Control Plane | `docs/architecture/VOICE_SIDECAR_CONTROL_PLANE.md` | Voice pipeline orchestration and audio bus design |
| Doubleword Integration | `docs/integrations/DOUBLEWORD_INTEGRATION.md` | Tier 0 batch inference setup, cost benchmarks, routing policy |
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
