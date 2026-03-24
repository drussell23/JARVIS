# Trinity Architecture Overview

## The Three Components

The JARVIS Trinity is a distributed AI system composed of three autonomous
components that operate as a single organism.  Each component maps to a
biological metaphor and a deployment boundary.

| Component | Metaphor | Runtime | Primary Repo |
|-----------|----------|---------|--------------|
| **JARVIS** | Body | macOS edge client (M-series Mac) | `JARVIS-AI-Agent` |
| **J-Prime** | Mind | GCP VM (g2-standard-4 + NVIDIA L4) | `jarvis-prime` |
| **Reactor Core** | Soul | Sandboxed execution environment | `jarvis-reactor` |

---

## JARVIS (Body)

The edge client running on macOS.  Responsible for perception, voice, vision,
action execution, and user interaction.

### Key Modules

| Module | Path | Purpose |
|--------|------|---------|
| Unified Supervisor | `unified_supervisor.py` | 73K+ line monolith kernel; lifecycle zones 1-9 |
| PrimeClient | `backend/core/prime_client.py` | HTTP client to J-Prime with circuit breakers |
| PrimeRouter | `backend/core/prime_router.py` | Routes inference to LOCAL, GCP, or Claude |
| RuntimeTaskOrchestrator | `backend/core/runtime_task_orchestrator.py` | Universal dispatcher: voice -> plan -> agents |
| Lean Vision Loop | `backend/vision/lean_loop.py` | 3-step see-think-act screen automation |
| Ghost Hands | `backend/ghost_hands/background_actuator.py` | Focus-preserving UI automation |
| GovernedLoopService | `backend/core/ouroboros/governance/governed_loop_service.py` | Self-development pipeline lifecycle |

### Supervisor Boot Zones

The unified supervisor boots services in numbered zones:

```
Zone 1-4   Core infrastructure (DLM, config, async safety)
Zone 5     GCP VM management, startup watchdog
Zone 6.8   GovernedLoopService (Ouroboros pipeline)
Zone 6.9   IntakeLayerService (trigger sensors)
Zone 7-9   Voice, TUI, health probes
```

---

## J-Prime (Mind)

The reasoning engine hosted on GCP.  Runs local models (Qwen2.5-7B text,
LLaVA v1.5 vision) on an NVIDIA L4 GPU.  Serves as the primary code
generation backend for the Ouroboros self-development pipeline.

### Deployment

```
Instance:    jarvis-prime-stable
Zone:        us-central1-b
Machine:     g2-standard-4 + NVIDIA L4 (24 GB VRAM)
Static IP:   136.113.252.164
Entry point: /opt/jarvis-prime/venv/bin/python run_server.py
             --port 8000 --host 0.0.0.0 --gpu-layers -1 --ctx-size 8192
```

### Capabilities

- Text generation via Qwen2.5 (7B/14B/32B, GGUF quantized)
- Vision inference via LLaVA v1.5 (32B)
- Brain selection API (`/v1/brains`) for boot handshake
- Schema-versioned code generation (2b.1 single-repo, 2c.1 multi-repo, 2d.1 execution-graph)
- On-demand lifecycle: golden image preserved, VM starts/stops automatically

---

## Reactor Core (Soul)

The sandboxed execution and learning environment.  Reactor Core runs
generated code in isolation, collects DPO (Direct Preference Optimization)
training data, and graduates proven capabilities back into the main system.

### Responsibilities

- Sandboxed test execution for Ouroboros-generated patches
- DPO training data collection from successful/failed operations
- Capability graduation: ephemeral tool -> permanent agent via Git PR
- Cross-repo verification for multi-repo saga operations

---

## Inter-Component Communication

### JARVIS -> J-Prime (HTTP)

```
                  PrimeClient
JARVIS (macOS)  =============>  J-Prime (GCP)
                  HTTP/REST
                  Port 8000

Endpoints:
  POST /v1/generate          Text generation
  POST /v1/generate/stream   Streaming generation
  GET  /v1/brains            Available brain inventory
  GET  /v1/health            Health check
```

PrimeClient features:
- Connection pooling via `aiohttp` persistent sessions
- Circuit breaker with automatic fallback to Claude API
- Hot-swap endpoint via `update_endpoint()` / `demote_to_fallback()`
- Trace header propagation for causal traceability
- Request queuing during temporary outages

### J-Prime -> Models (Local Inference)

```
J-Prime Server
  |
  +-- Qwen2.5-Coder-7B   (light tasks)
  +-- Qwen2.5-Coder-14B   (medium tasks)
  +-- Qwen2.5-Coder-32B   (heavy code, architecture)
  +-- LLaVA v1.5-32B       (vision tasks)
```

### JARVIS -> Reactor Core

Reactor Core receives generated patches from the Ouroboros pipeline
and executes them in isolation.  Communication flows through the
`RepoRegistry` and `SagaApplyStrategy` which resolve filesystem paths
per repo.

### Routing Decision Flow

```
User Request
     |
     v
PrimeRouter._decide_route()
     |
     +-- GCP_PRIME    (J-Prime VM healthy, model loaded)
     |
     +-- LOCAL_PRIME   (local llama.cpp instance running)
     |
     +-- CLOUD_CLAUDE  (fallback: Anthropic API)
     |
     +-- HYBRID        (try local first, then cloud)
     |
     +-- DEGRADED      (all backends down)
```

---

## Architecture Diagram

```
+---------------------------------------------------------------------+
|                        macOS Edge Client                             |
|                                                                      |
|  +------------------+    +------------------+    +-----------------+ |
|  | Unified          |    | RuntimeTask      |    | GovernedLoop    | |
|  | Supervisor       |    | Orchestrator     |    | Service         | |
|  | (Zones 1-9)      |    | (voice->agents)  |    | (Ouroboros)     | |
|  +--------+---------+    +--------+---------+    +--------+--------+ |
|           |                       |                       |          |
|  +--------+---------+    +--------+---------+    +--------+--------+ |
|  | Vision Loop      |    | Ghost Hands      |    | IntakeLayer     | |
|  | (see-think-act)  |    | (BG actuator)    |    | (sensors)       | |
|  +------------------+    +------------------+    +-----------------+ |
|                                                                      |
+------------------------+-----------------------------+---------------+
                         |                             |
                    PrimeClient                   Claude API
                    (HTTP/REST)                   (Anthropic)
                         |                             |
+------------------------+----------+    +-------------+----------+
|        GCP VM (g2-standard-4)     |    |  Anthropic Cloud       |
|                                   |    |                        |
|  +-----------------------------+  |    |  Claude Sonnet 4       |
|  | J-Prime Server              |  |    |  Claude Haiku 4.5      |
|  | +-- Qwen2.5-Coder (7/14/32)|  |    |  Claude Vision         |
|  | +-- LLaVA v1.5-32B         |  |    +-----------+------------+
|  +-----------------------------+  |                |
|                                   |    +-----------+------------+
+-----------------------------------+    |  Doubleword API (Tier 0)|
                                         |  Qwen3.5-397B / 35B    |
                                         |  Nemotron-3-Super 120B  |
                                         |  Qwen3-VL-235B         |
                                         +------------------------+
```

---

## The Symbiotic Manifesto

Six governing principles shape every design decision in Trinity.

### 1. Unified Organism

Trinity operates as one organism, not three services.  The Body, Mind, and
Soul are not microservices -- they are organs.  A failure in one triggers
adaptation in the others, not error pages.

### 2. Progressive Awakening

Services boot in dependency order with graduated readiness gates.  The
supervisor's zone-based boot sequence ensures each component is fully
initialized before downstream consumers start.  No component assumes another
is ready without a health probe.

### 3. Async Tendrils

All inter-component I/O is asynchronous.  PrimeClient uses `aiohttp`
persistent sessions; the supervisor uses `asyncio` throughout.  Blocking
calls are prohibited in the event loop -- subprocess execution uses
`asyncio.create_subprocess_exec()`, never `subprocess.run()`.

### 4. Intelligence-Driven Routing

No hardcoded if/elif chains for model selection.  Brain routing is driven by
intent classification (CAI), system health awareness (SAI), and cost gates
(BrainSelector).  The system discovers the optimal backend dynamically.

### 5. Threshold-Triggered Neuroplasticity

Capabilities are not manually added.  The Ouroboros pipeline detects
improvement opportunities, generates patches, validates them, and graduates
proven tools into permanent agents.  The graduation threshold (default: 3
successful uses) prevents code bloat while allowing genuine capabilities to
persist.

### 6. Absolute Observability

Every routing decision, model call, and state transition is recorded in the
operation ledger (`~/.jarvis/ouroboros/ledger/`).  Voice narration announces
significant decisions.  Langfuse-style traces provide full causal chains from
trigger to outcome.

---

## Environment Variables (Key)

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_PRIME_PATH` | `~/Documents/repos/jarvis-prime` | Path to J-Prime repo |
| `JARVIS_REPO_PATH` | `.` | Path to JARVIS repo |
| `JARVIS_REACTOR_REPO_PATH` | (none) | Path to Reactor repo |
| `JARVIS_GOVERNANCE_MODE` | `sandbox` | `sandbox`, `observe`, `governed` |
| `JARVIS_GCP_RECOVERY_TIMEOUT` | `450` | GCP VM recovery timeout (seconds) |
| `JARVIS_BACKEND_STARTUP_TIMEOUT` | `300` | Backend boot timeout (seconds) |
| `JARVIS_GENERATION_TIMEOUT_S` | `60` | Ouroboros generation timeout |
| `JARVIS_PIPELINE_TIMEOUT_S` | `150` | Ouroboros full pipeline timeout |
| `JARVIS_HARDWARE_PROFILE` | (auto) | `FULL` forces full capability on GCP |
| `ANTHROPIC_API_KEY` | (required) | Claude API fallback |
| `DOUBLEWORD_API_KEY` | (optional) | Tier 0 Doubleword batch inference |

---

## Cross-Repo Registry

The `RepoRegistry` (`backend/core/ouroboros/governance/multi_repo/registry.py`)
manages all three repos.  Environment variables feed `RepoRegistry.from_env()`:

```
JARVIS_REPO_PATH         -> jarvis (Body)
JARVIS_PRIME_REPO_PATH   -> jarvis-prime (Mind)
JARVIS_REACTOR_REPO_PATH -> jarvis-reactor (Soul)
```

Multi-repo patches use schema version 2c.1, with per-repo `patches` dicts
that the `SagaApplyStrategy` applies atomically across repositories.
