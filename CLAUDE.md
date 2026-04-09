# CLAUDE.md -- JARVIS Trinity AI Ecosystem

**Repository**: [github.com/drussell23/JARVIS-AI-Agent](https://github.com/drussell23/JARVIS-AI-Agent)
**Author**: Derek J. Russell -- RSI/AGI Researcher & Trinity Architect

## What This Is

JARVIS is the Body of a tri-partite AGI OS. This repo contains the macOS integration layer (screen capture, voice, keyboard automation), the unified supervisor (102K-line monolithic kernel), and the Ouroboros self-development governance engine. All model references are resolved from `brain_selection_policy.yaml` -- zero hardcoded model names.

## Architecture at a Glance

```
JARVIS (Body) <--HTTP/WS--> J-Prime (Mind) <--sandbox--> Reactor Core (Soul)
     |
     +-- unified_supervisor.py (102K lines, Zones 0-7)
     +-- backend/core/ouroboros/ (autonomous self-development)
     +-- backend/vision/ (VLA pipeline, OCR, frame server)
     +-- backend/ghost_hands/ (focus-preserving UI automation)
     +-- backend/voice/ (voice I/O, wake word, TTS)
     +-- backend/core_contexts/ (5 execution contexts: Executor, Architect, Developer, Communicator, Observer)
     +-- backend/neural_mesh/ (22 legacy agents, Strangler Fig fallback)
```

## Key Conventions

- **Async-first**: All I/O uses `asyncio`. No blocking calls on the event loop.
- **No hardcoded models**: All model names come from `brain_selection_policy.yaml` or env vars.
- **Env-var driven config**: Every tunable parameter reads from environment variables with sensible defaults.
- **Python 3.9+**: No `asyncio.timeout` (3.11+) -- use `asyncio.wait_for` everywhere.
- **`from __future__ import annotations`**: Required in all files for forward reference support.

## Ouroboros Pipeline

The self-development engine runs an 11-phase governance pipeline:

```
CLASSIFY -> ROUTE -> [CONTEXT_EXPANSION] -> [PLAN] -> GENERATE -> VALIDATE -> GATE -> [APPROVE] -> APPLY -> VERIFY -> COMPLETE
```

### Provider Chain (3-Tier Failback)

| Tier | Provider | Cost | Notes |
|------|----------|------|-------|
| 0 | DoubleWord 397B (batch + real-time) | $0.10/$0.40/M | 16384 max_tokens, 5s poll interval, preferred |
| 1 | Claude (Anthropic API) | $3/$15/M | Extended thinking + prompt caching, 60s fallback cap |
| 2 | J-Prime (GCP self-hosted) | VM cost only | When available |

### Timeout Enforcement

- **Generation**: `asyncio.wait_for(timeout=180s + 5s grace)` in orchestrator
- **Fallback provider**: Hard cap at 60s (`_FALLBACK_MAX_TIMEOUT_S`)
- **Tier 1 reserve**: 25s minimum (not 45s -- reduced to avoid starving Tier 0)
- **DW poll interval**: 5s (not 15s)
- **pytest (TestWatcher)**: 30s timeout

### 16 Autonomous Sensors

TestFailure, VoiceCommand, OpportunityMiner, CapabilityGap, Scheduled, Backlog, RuntimeHealth, WebIntelligence, PerformanceRegression, DocStaleness, GitHubIssue, ProactiveExploration, CrossRepoDrift, TodoScanner, CUExecution, IntentDiscovery.

All flow through `UnifiedIntakeRouter` with priority queuing, deduplication, and WAL persistence.

### Key Subsystems

- **PlanGenerator** (`plan_generator.py`): Model-reasoned implementation planning (PLAN phase) -- structured JSON plan (schema plan.1) with approach, ordered changes, risk factors, test strategy. Injected into GENERATE prompt.
- **SemanticTriage** (`semantic_triage.py`): Pre-generation filter -- classifies NO_OP/REDIRECT/ENRICH/GENERATE before expensive generation
- **CommProtocol** (`comm_protocol.py`): 5-phase observability -- INTENT -> PLAN -> HEARTBEAT -> DECISION -> POSTMORTEM
- **SerpentFlow** (`battle_test/serpent_flow.py`, 1900+ lines): CC-style flowing CLI with `Update(path)` blocks, numbered diffs, per-op reasoning
- **LiveDashboard** (`battle_test/live_dashboard.py`, 1233 lines): Persistent Rich TUI with 3-channel terminal muting
- **Venom** (`tool_executor.py`): Multi-turn agentic tool loop -- 15 tools (read_file, search_code, edit_file, write_file, bash, web_fetch, web_search, run_tests, get_callers, glob_files, list_dir, list_symbols, git_log, git_diff, git_blame). All enabled by default under governance.
- **L2 Repair** (`repair_engine.py`): Iterative self-repair FSM (5 iterations, 120s timebox)
- **ConsciousnessBridge** (`consciousness_bridge.py`): Injects memory/prediction into pipeline
- **StrategicDirection** (`strategic_direction.py`): Manifesto principles injected into every generation prompt
- **DreamEngine** (`consciousness/dream_engine.py`): Idle GPU speculative improvement blueprints

### Autonomous Developer Intelligence (O+V)

O+V is **proactive** (self-initiating), not reactive (human-prompted). Key capabilities:

- **4-tier risk escalation**: `SAFE_AUTO` / `NOTIFY_APPLY` / `APPROVAL_REQUIRED` / `BLOCKED` -- Green/Yellow auto-apply, Orange blocks for human
- **Exploration-first**: Generation prompt requires 2+ tool calls before any patch. Enforced at VALIDATE gate + budget cap in tool loop.
- **Post-apply verification**: Scoped test run after APPLY, routes failures to L2 repair. L2 candidate now applied via change_engine.
- **Session intelligence**: `_session_lessons` buffer (20 max) with infra/code tagging. Convergence metric auto-clears misleading lessons.
- **Cost-aware priority**: `_compute_priority()` factors urgency, file count, confidence, dependency credit (capped at 3).
- **Dependency DAG**: `_active_file_ops` with TTL-based stale lock release (`JARVIS_FILE_LOCK_TTL_S`, default 300s).
- **Signal coalescing**: Same-file signals merged into single operation within `JARVIS_COALESCE_WINDOW_S` (default 30s).
- **Stale exploration guard**: File hashes snapshotted at GENERATE, verified at APPLY. Stale candidates logged.
- **REPL /cancel**: `cancel <op-id>` cooperative cancellation, checked at GENERATE and APPLY phase boundaries.
- **Diff preview for Yellow**: `JARVIS_NOTIFY_APPLY_DELAY_S` (default 5s) delay with diff rendered before auto-apply.
- **Per-op reasoning**: Model rationale captured at GENERATE, displayed in SerpentFlow `Update` blocks.
- **Model-reasoned planning**: PLAN phase between CONTEXT_EXPANSION and GENERATE. Model reasons about implementation strategy (schema plan.1) before writing code. Trivial ops skip planning.

## Battle Test

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

Boots the full 6-layer stack: GovernedLoopService, IntakeLayer (16 sensors), TrinityConsciousness, StrategicDirection, CommProtocol, SerpentFlow CLI.

## File Layout (Key Paths)

```
backend/core/ouroboros/
  governance/
    governed_loop_service.py    # Main loop (Zone 6.8)
    orchestrator.py             # 11-phase FSM
    candidate_generator.py      # 3-tier failback
    providers.py                # Claude + Prime providers
    doubleword_provider.py      # DW 397B
    plan_generator.py           # Model-reasoned PLAN phase (schema plan.1)
    semantic_triage.py          # Pre-generation filter
    comm_protocol.py            # 5-phase observability
    tool_executor.py            # Venom tool loop
    repair_engine.py            # L2 self-repair
    consciousness_bridge.py     # Consciousness integration
    strategic_direction.py      # Manifesto injection
    serpent_animation.py        # ASCII animation
    intake/
      sensors/                  # 16 sensors (5,400+ lines)
    intent/
      test_watcher.py           # Pytest polling (30s timeout)
      signals.py                # IntentSignal dataclass
  consciousness/                # Zone 6.11 (7,063 lines)
    consciousness_service.py    # TrinityConsciousness orchestrator
    health_cortex.py            # System health monitoring
    memory_engine.py            # Per-file reputation tracking
    dream_engine.py             # Idle GPU speculative analysis
    prophecy_engine.py          # Regression prediction
  battle_test/
    harness.py                  # 6-layer stack boot
    serpent_flow.py             # SerpentFlow: CC-style CLI (1,900+ lines)
    live_dashboard.py           # Persistent Rich TUI (1,233 lines)
  oracle.py                     # Codebase semantic index
```

## The Governing Philosophy

This is not a software refactor. It is the genesis of an autonomous, self-evolving AI Operating System. The 7 principles:

1. **Unified organism** -- tri-partite microkernel, single entry point
2. **Progressive awakening** -- adaptive lifecycle, no blocking boot chains
3. **Asynchronous tendrils** -- structured concurrency, no event loop starvation
4. **Synthetic soul** -- episodic awareness, cross-session learning
5. **Intelligence-driven routing** -- semantic, not regex; DAGs, not scripts
6. **Threshold-triggered neuroplasticity** -- Ouroboros: detect gaps, synthesize, graduate
7. **Absolute observability** -- every autonomous decision is visible

**Zero-shortcut mandate**: No brute-force retries without diagnosis. No hardcoded routing tables. Structural repair, not bypasses.
