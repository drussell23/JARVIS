# Ouroboros: Self-Development Governance Pipeline

## Overview

Ouroboros is the autonomous self-development system that allows JARVIS to
detect improvement opportunities, generate code patches, validate them in
sandbox, and graduate proven capabilities into permanent agents.  Named after
the serpent eating its own tail, Ouroboros represents JARVIS continuously
improving itself.

The pipeline operates under strict governance: every operation is risk-
classified, cost-gated, and recorded in an append-only ledger.  Human
approval is required for high-risk changes.

---

## Pipeline Phases

```
CLASSIFY --> ROUTE --> CONTEXT_EXPANSION --> GENERATE --> VALIDATE
                                                            |
                                                            v
                    COMPLETE <-- VERIFY <-- APPLY <-- GATE/APPROVE
```

### Phase Definitions

| Phase | Purpose | Terminal? |
|-------|---------|-----------|
| `CLASSIFY` | Risk classification (RiskTier, ChangeType) | No |
| `ROUTE` | Brain selection and provider routing | No |
| `CONTEXT_EXPANSION` | Enrich operation context with related files | No |
| `GENERATE` | Produce candidate patches via model provider | No |
| `GENERATE_RETRY` | Retry generation after transient failure | No |
| `VALIDATE` | Run tests, lint, type-check against candidate | No |
| `VALIDATE_RETRY` | Retry validation after transient failure | No |
| `GATE` | Apply policy engine rules (auto-approve or escalate) | No |
| `APPROVE` | Wait for human approval (if required) | No |
| `APPLY` | Write patches to filesystem via ChangeEngine | No |
| `VERIFY` | Post-apply verification (tests pass with changes) | No |
| `COMPLETE` | Operation succeeded | Yes |
| `CANCELLED` | Operation cancelled (any phase) | Yes |
| `EXPIRED` | Approval timeout or deadline exceeded | Yes |
| `POSTMORTEM` | Unhandled exception caught at any phase | Yes |

### Phase Transition Table

```
CLASSIFY ---------> ROUTE, CANCELLED
ROUTE ------------> CONTEXT_EXPANSION, GENERATE, CANCELLED
CONTEXT_EXPANSION -> GENERATE, CANCELLED
GENERATE ---------> VALIDATE, GENERATE_RETRY, CANCELLED, COMPLETE (noop)
GENERATE_RETRY ---> VALIDATE, GENERATE_RETRY, CANCELLED
VALIDATE ---------> GATE, VALIDATE_RETRY, CANCELLED, POSTMORTEM
VALIDATE_RETRY ---> GATE, VALIDATE_RETRY, CANCELLED, POSTMORTEM
GATE -------------> APPROVE, APPLY, CANCELLED
APPROVE ----------> APPLY, CANCELLED, EXPIRED
APPLY ------------> VERIFY, CANCELLED, POSTMORTEM
VERIFY -----------> COMPLETE, CANCELLED, POSTMORTEM
```

### Noop Fast-Path

When J-Prime detects the proposed change already exists in the codebase, it
returns schema `2b.1-noop` with a reason string.  The orchestrator recognizes
`is_noop=True` on the `GenerationResult` and short-circuits directly from
GENERATE to COMPLETE, skipping all intermediate phases.

---

## Key Components

### GovernedLoopService

**Source**: `backend/core/ouroboros/governance/governed_loop_service.py`

Thin lifecycle manager instantiated by the supervisor at Zone 6.8.  Owns
provider wiring, orchestrator construction, and health probes.

Service states:
```
INACTIVE -> STARTING -> ACTIVE/DEGRADED
ACTIVE/DEGRADED -> STOPPING -> INACTIVE
STARTING -> FAILED (on error)
```

Key responsibilities:
- Constructs `GovernedOrchestrator` with configured providers
- Manages `_file_touch_cache` (3 touches / 10-min window per file -> hard block)
- Maintains `_active_brain_set` from boot handshake
- Runs `_oracle_index_loop` background task for continuous code indexing
- Exposes `_repo_registry` for Zone 6.9 reuse
- Delegates all operations via `submit()` to the orchestrator

### GovernedOrchestrator

**Source**: `backend/core/ouroboros/governance/orchestrator.py`

Central coordinator that ties together all pipeline components.  Owns **no
domain logic** -- only phase transitions and error handling.

Configuration (`OrchestratorConfig`):

| Parameter | Default | Purpose |
|-----------|---------|---------|
| `project_root` | (required) | Root directory of target project |
| `repo_registry` | None | Multi-repo registry (enables cross-repo sagas) |
| `generation_timeout_s` | 120s | Max seconds per generation attempt |
| `validation_timeout_s` | 60s | Max seconds per validation attempt |
| `approval_timeout_s` | 600s | Max seconds to wait for human approval |
| `max_generate_retries` | 1 | Additional generation attempts after failure |
| `max_validate_retries` | 2 | Additional validation attempts after failure |
| `context_expansion_enabled` | true | Enable pre-generation context expansion |
| `benchmark_enabled` | true | Enable patch benchmarking |
| `curriculum_enabled` | true | Enable curriculum publishing |

Guarantees:
- All unhandled exceptions transition to POSTMORTEM
- Retries are bounded by config limits
- BLOCKED operations are short-circuited at CLASSIFY
- Ledger entries recorded at every significant lifecycle event

### TheOracle

**Source**: `backend/core/ouroboros/oracle.py`

GraphRAG codebase knowledge graph providing structural understanding of the
entire codebase.

Architecture:
- **Nodes**: Files, Classes, Functions, Variables, Imports
- **Edges**: IMPORTS, CALLS, INHERITS, USES, DEFINES, OVERRIDES
- **Storage**: NetworkX DiGraph with persistent caching
- **Querying**: Graph traversal, shortest paths, subgraph extraction

Key capabilities:
- Async parallel file indexing with incremental updates
- Cross-repo graph connectivity (JARVIS + Prime + Reactor)
- Blast radius analysis for change impact assessment
- `FileNeighborhood`: structural graph topology with 7 edge categories,
  10 paths per category max
- `index_age_s()` for staleness detection (context expander warns > 300s)
- Dead code and circular dependency detection

### Context Expander

**Source**: `backend/core/ouroboros/governance/context_expander.py`

Runs bounded pre-generation context expansion.  Governor limits are hardcoded
and cannot be changed at runtime:

| Limit | Value |
|-------|-------|
| `MAX_ROUNDS` | 2 |
| `MAX_FILES_PER_ROUND` | 5 |
| `MAX_FILES_PER_CATEGORY` | 10 |

Each round:
1. Builds lightweight prompt (description + filenames only -- no file contents)
2. Calls `generator.plan(prompt, deadline)` for expansion suggestions
3. Parses `expansion.1` schema JSON response
4. Resolves file paths against repo root (missing files silently skipped)
5. Accumulates confirmed paths into `ctx.expanded_context_files`

Stops early if: empty response, invalid JSON, no confirmed files, or
generator raises.

### Providers

**Source**: `backend/core/ouroboros/governance/providers.py`, `doubleword_provider.py`

| Provider | Wraps | Cost (per 1M tokens) | Features |
|----------|-------|---------------------|----------|
| `DoublewordProvider` | Doubleword batch API (397B MoE) | $0.10 / $0.40 | Cost-gated, daily budget, per-request timeouts, RateLimitService, circuit breaker |
| `ClaudeProvider` | `anthropic.AsyncAnthropic` | $3.00 / $15.00 | Cost-gated, daily budget, tool-use support |
| `PrimeProvider` | `PrimeClient.generate()` (GCP) | ~$1.20/hr VM | Fixed temperature 0.2, schema enforcement |

All implement the `CandidateProvider` protocol used by `CandidateGenerator`'s
adaptive failback state machine.

### Adaptive Provider Routing

**Source**: `backend/core/ouroboros/governance/candidate_generator.py`

The `CandidateGenerator` routes generation requests through a tiered cascade
with **failure-mode-aware adaptive recovery**. DoubleWord (30-37x cheaper than
Claude) is always preferred. Claude is used only when DoubleWord is predicted
to be unavailable.

#### FailureMode Classification

When a provider fails, the exception is classified into a `FailureMode` that
determines recovery timing:

| Mode | Base Backoff | Max Backoff | Example |
|------|-------------|-------------|---------|
| `RATE_LIMITED` | 15s | 120s | HTTP 429, CircuitBreakerOpen |
| `TIMEOUT` | 45s | 300s | Connection/request timeout |
| `SERVER_ERROR` | 60s | 600s | HTTP 500/502/503 |
| `CONNECTION_ERROR` | 120s | 900s | Host unreachable, DNS failure |
| `CONTENT_FAILURE` | 0s | 0s | Bad output (no infra penalty) |

Recovery ETA uses exponential backoff: `base_s * 2^(consecutive_failures - 1)`,
capped at `max_s`.

#### Routing Decision Flow

```
Tier 0 (DoubleWord batch):
  - Skip if CONNECTION_ERROR backoff active
  - Submit batch (fast, <2s)
  - Poll with budgeted timeout (50% of deadline)
  - If timeout: classify failure, cascade to Tier 1

Tier 1 (Primary → Fallback):
  - should_attempt_primary() checks recovery ETA
  - If recovery window elapsed: try primary (cost-save)
  - If still in backoff: use fallback directly
  - Primary budget: 65% of remaining time
  - Fallback gets guaranteed 20s minimum
```

#### Deadline Budget Allocation

The total generation deadline is split deterministically:

| Component | Budget | Env Var | Default |
|-----------|--------|---------|---------|
| Tier 0 (DoubleWord) | 50% of total, max 90s | `OUROBOROS_TIER0_BUDGET_FRACTION` | 0.50 |
| Tier 1 reserve | Minimum 45s guaranteed | `OUROBOROS_TIER1_MIN_RESERVE_S` | 45 |
| Primary within Tier 1 | 65% of Tier 1 budget | `OUROBOROS_PRIMARY_BUDGET_FRACTION` | 0.65 |
| Fallback reserve | Minimum 20s guaranteed | `OUROBOROS_FALLBACK_MIN_RESERVE_S` | 20 |

#### QUEUE_ONLY Auto-Recovery

When both primary and fallback fail:
- **Transient failures** (TIMEOUT, RATE_LIMITED, SERVER_ERROR) stay in
  `FALLBACK_ACTIVE` -- the system retries on the next operation
- **Permanent failures** (CONNECTION_ERROR) transition to `QUEUE_ONLY`
- `QUEUE_ONLY` **auto-recovers** when a health probe succeeds -- transitions
  to `FALLBACK_ACTIVE`, then counts the probe toward `PRIMARY_DEGRADED`

#### Adaptive Health Probes

Probe interval adapts based on distance to recovery ETA:

| Distance to ETA | Interval |
|-----------------|----------|
| >60s away | 60s (relax) |
| 30-60s away | 20s (moderate) |
| <30s away | 10s (ramp up) |
| Past ETA | 5s (aggressive) |

#### Connector Resilience

The `DoublewordProvider` detects poisoned aiohttp connectors (caused by
`CancelledError` during connection attempts) and automatically creates
fresh sessions. Background poll tasks are capped at 3 concurrent to prevent
connector saturation.

Schema versions:
- `2b.1`: Single-repo patches
- `2b.1-noop`: Change already present (fast-path to COMPLETE)
- `2c.1`: Multi-repo patches (per-repo `patches` dict -> `RepoPatch` objects)
- `2d.1`: Execution-graph operations (L3 self-repair)

### PreemptionFsmEngine

**Source**: `backend/core/ouroboros/governance/preemption_fsm.py`

Deterministic state machine for loop preemption, suspension, and rehydration.

| Component | Role |
|-----------|------|
| `PreemptionFsmEngine` | Pure transition function (no side effects) |
| `PreemptionFsmExecutor` | Side-effect executor (durable-ledger-first) |
| `build_transition_input` | Factory helper for `TransitionInput` |

Invariants:
1. No state transition without a durable ledger append
2. Duplicate `(op_id, checkpoint_seq)` is a no-op (idempotent)
3. All side effects require `idempotency_key` and are replay-safe
4. `FAILED_PERMANENT` is a sink state -- any further event is a no-op

Backoff: Amazon-style full jitter or deterministic capped exponential,
configurable via `RetryBudget`.

### GraduationOrchestrator

**Source**: `backend/core/ouroboros/governance/graduation_orchestrator.py`

Converts ephemeral tools into permanent agents via the full graduation
lifecycle:

```
TRACKING --> EVALUATING --> WORKTREE_CREATING --> GENERATING -->
VALIDATING --> COMMITTING --> AWAITING_APPROVAL --> PUSHING -->
AWAITING_MERGE --> REGISTERING --> GRADUATED
```

Hardening requirements (baked in):
- H1: Git cleanliness check before mutation (`git status --porcelain`)
- H2: Contract tests (BaseNeuralMeshAgent interface)
- H3: `PUSH_FAILED` is an explicit phase (code preserved locally)
- H4: Approval timeout -> discard worktree + log (30min default)
- H5: Post-merge registration requires readiness probe
- H6: Cost metering per J-Prime call (accumulated on GraduationRecord)

Graduation threshold: 3 successful uses (configurable via
`JARVIS_GRADUATION_THRESHOLD`; set to 1 with `DEBUG_MUTATION_MODE=true`
for development).

The `EphemeralUsageTracker` normalizes goals, removes stop words, hashes
the canonical form, and fires the graduation threshold exactly once per
goal class.

---

## Multi-Repo Support

Ouroboros operates across three repositories simultaneously:

| Repo | Key | Registry Env Var |
|------|-----|-----------------|
| JARVIS (Body) | `jarvis` | `JARVIS_REPO_PATH` |
| J-Prime (Mind) | `jarvis-prime` | `JARVIS_PRIME_REPO_PATH` |
| Reactor (Soul) | `jarvis-reactor` | `JARVIS_REACTOR_REPO_PATH` |

Cross-repo operations use the **Saga pattern**:
- `SagaApplyStrategy`: Applies patches atomically across repos
- `CrossRepoVerifier`: Validates cross-repo patch consistency
- `RepoPatch`: Per-repo patch with file-level changes
- `SagaTerminalState`: Final outcome of a multi-repo saga

---

## Intake Layer: Unified Event Spine

**Source**: `backend/core/ouroboros/governance/intake/intake_layer_service.py`

The `IntakeLayerService` (Zone 6.9) detects improvement opportunities via
an **event-driven architecture** (Manifesto Section 3: "Zero polling. Pure reflex.").

### Architecture

```
PRODUCERS (push immediately)                    CONSUMERS (subscribe)
-------------------------------                 ----------------------
FileWatchGuard (watchdog) -----+                BacklogSensor
  *.py, *.json changes         |                TodoScannerSensor
                               |                OpportunityMinerSensor
pytest plugin -----------------+                TestFailureSensor
  .jarvis/test_results.json    |                DocStalenessSensor
                               +---> Trinity    CrossRepoDriftSensor
post-commit hook --------------+     EventBus   CapabilityGapSensor
  .jarvis/git_events.json      |     (unified)  SafetyNet (L3)
                               |                FeedbackEngine
GapSignalBus (bridge) ---------+                ...
EventEmitter (bridge) ---------+
EventChannelServer (bridge) ---+
```

### Event Spine Layers

| Layer | Component | What it does |
|-------|-----------|-------------|
| **Phase 1** | `FileSystemEventBridge` | Watches repo root via `watchdog`, publishes `fs.changed.*` events to TrinityEventBus |
| **Phase 2** | `ouroboros_pytest_plugin` | Captures test results during any pytest run, writes `.jarvis/test_results.json` |
| **Phase 3** | `post-commit` git hook | Writes `.jarvis/git_events.json` with commit metadata (changed files, branch, author) |
| **Phase 4** | Bus adapter bridges | GapSignalBus, EventEmitter, EventChannelServer forward to TrinityEventBus |

### Trigger Sensors

| Sensor | Trigger | Latency | Purpose |
|--------|---------|---------|---------|
| `BacklogSensor` | `fs.changed.*` on `backlog.json` | <1s | Processes queued improvement items |
| `TestFailureSensor` | `fs.changed.*` on `test_results.json` + debounced `.py` | <1s | Detects test failures, streak-based stability |
| `OpportunityMinerSensor` | `fs.changed.*` on `*.py` | <1s | AST cyclomatic complexity analysis |
| `TodoScannerSensor` | `fs.changed.*` on `*.py` | <1s | FIXME/TODO/HACK marker detection |
| `DocStalenessSensor` | `fs.changed.*` on `*.py` + `git_events.json` | <1s | Undocumented module detection |
| `CrossRepoDriftSensor` | `fs.changed.*` on contract files + `git_events.json` | <1s | Cross-repo contract drift |
| `VoiceCommandSensor` | Voice pipeline (event-driven, no polling) | Instant | Voice-triggered code changes |
| `CapabilityGapSensor` | GapSignalBus events (event-driven) | Instant | Shannon entropy gap detection |
| `ScheduledTriggerSensor` | Cron-based periodic triggers | Configurable | Time-based scheduled tasks |

All sensors retain `scan_once()` for manual/CLI invocation. Poll loops remain
as safety-net fallbacks when the event spine is unavailable.

### Event Storm Protection (4 layers)

1. **FileWatchGuard**: Debounce 0.3s + checksum dedup + LRU cache
2. **TrinityEventBus**: SHA-256 fingerprint dedup (60s window)
3. **TestFailureSensor**: 2s debounce before pytest spawn
4. **IntakeRouter**: Envelope `dedup_key` prevents duplicate operations

Each sensor fans out per registered repo when `repo_registry` is set;
falls back to a single "jarvis" sensor otherwise.

---

## Operation Ledger

**Source**: `backend/core/ouroboros/governance/ledger.py`

Append-only, file-backed operation state log with deduplication.

Storage: JSONL files in `~/.jarvis/ouroboros/ledger/`, one file per operation.

Each `LedgerEntry` records:
- `op_id`: Identifier (`op-<uuidv7>-<origin>`)
- `state`: Current `OperationState`
- `data`: Arbitrary metadata (error details, validation results, gate verdicts)
- `timestamp`: Monotonic clock for ordering
- `wall_time`: Unix timestamp for cross-process correlation
- `entry_id`: Optional disambiguator for multiple records at same state

Deduplication key: `op_id:state` (or `op_id:state:entry_id` when set).

---

## Branch Isolation

All Ouroboros operations work on ephemeral branches:

1. **Worktree creation**: `git worktree add` creates an isolated checkout
2. **Patch application**: Changes are written to the worktree, not `main`
3. **Two-tier locks**: DLM (Distributed Lock Manager) prevents concurrent
   operations on the same files
4. **Verification**: Tests run against the worktree branch
5. **Promote**: Fast-forward-only merge to `main` (no merge commits)
6. **Cleanup**: Worktree removed after merge or rejection

---

## Observability

### Voice Narration

| Narrator | Source | Events |
|----------|--------|--------|
| `IntelligentVoiceNarrator` | `backend/core/ouroboros/ui_integration.py` | INTENT, DECISION, POSTMORTEM |
| `CrossRepoNarrator` | `backend/core/ouroboros/governance/comms/cross_repo_narrator.py` | Cross-repo saga events |

Voice debounce: `OUROBOROS_VOICE_DEBOUNCE_S` (default 60s) prevents
narration spam.

### Telemetry

`HostTelemetry`, `RoutingIntentTelemetry`, and `TelemetryContext` are
attached to every `OperationContext` for full causal traceability.
The `_CommTelemetrySink` bridges the FSM telemetry into `CommProtocol`
heartbeats.

---

## Battle Test Runner

**Source**: `scripts/ouroboros_battle_test.py`

The battle test is a headless daemon that boots the full Ouroboros stack and
runs it autonomously. Usage:

```bash
python3 scripts/ouroboros_battle_test.py --cost-cap 0.50 --idle-timeout 600 -v
```

| Flag | Env Var | Default | Purpose |
|------|---------|---------|---------|
| `--cost-cap` | `OUROBOROS_BATTLE_COST_CAP` | 0.50 | Session budget (USD) |
| `--idle-timeout` | `OUROBOROS_BATTLE_IDLE_TIMEOUT` | 600 | Inactivity timeout (seconds) |
| `--branch-prefix` | `OUROBOROS_BATTLE_BRANCH_PREFIX` | `ouroboros/battle-test` | Git branch prefix |
| `--repo-path` | `JARVIS_REPO_PATH` | Project root | Repository root path |
| `-v` | -- | -- | Enable DEBUG logging |

### Cost Tracking

The `CostTracker` monitors real API spend during the session:
- Polls provider stats every 5s via `_monitor_provider_costs()` background task
- Feeds incremental cost deltas from DoubleWord (`get_stats()`) and Claude (`_daily_spend`)
- Fires `budget_event` when cumulative spend reaches the `--cost-cap`
- Persists state to `.ouroboros/sessions/{session_id}/cost_tracker.json`

### Stop Conditions

The session stops on whichever fires first:
1. **`budget_exhausted`** -- API spend reached `--cost-cap`
2. **`idle_timeout`** -- No operations in flight for `--idle-timeout` seconds
3. **`shutdown_signal`** -- Ctrl+C / SIGTERM

---

## Environment Variables

### Core Governance

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_GOVERNANCE_MODE` | `sandbox` | `sandbox`, `observe`, `governed` |
| `JARVIS_GENERATION_TIMEOUT_S` | `120` | Per-attempt generation timeout |
| `JARVIS_PIPELINE_TIMEOUT_S` | `600` | Full pipeline timeout |
| `JARVIS_GRADUATION_THRESHOLD` | `3` | Uses before ephemeral->permanent |
| `JARVIS_GRADUATION_APPROVAL_TIMEOUT_S` | `1800` | Approval wait (30min) |
| `JARVIS_MIN_GENERATION_BUDGET_S` | `30` | Minimum generation budget |
| `JARVIS_L2_ENABLED` | `false` | Enable L2 self-repair engine |
| `JARVIS_SHADOW_HARNESS_ENABLED` | `false` | Enable shadow harness |
| `OUROBOROS_VOICE_DEBOUNCE_S` | `60` | Voice narration debounce |
| `OUROBOROS_GCP_DAILY_BUDGET` | `0.50` | Daily cost gate budget (USD) |
| `DEBUG_MUTATION_MODE` | `false` | Reduce graduation threshold to 1 |

### Provider Routing (Adaptive)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOUBLEWORD_API_KEY` | -- | DoubleWord API key (enables Tier 0) |
| `DOUBLEWORD_BASE_URL` | `https://api.doubleword.ai/v1` | DoubleWord API base URL |
| `DOUBLEWORD_MODEL` | `Qwen/Qwen3.5-397B-A17B-FP8` | Model slug |
| `DOUBLEWORD_MAX_COST_PER_OP` | `0.10` | Per-operation cost cap (USD) |
| `DOUBLEWORD_DAILY_BUDGET` | `5.00` | Daily budget (USD) |
| `DOUBLEWORD_CONNECT_TIMEOUT_S` | `30` | TCP connect timeout |
| `DOUBLEWORD_REQUEST_TIMEOUT_S` | `120` | Total request timeout |
| `ANTHROPIC_API_KEY` | -- | Claude API key (enables Tier 1 fallback) |
| `JARVIS_GOVERNED_CLAUDE_MODEL` | `claude-sonnet-4-20250514` | Claude model |
| `JARVIS_GOVERNED_CLAUDE_MAX_COST_PER_OP` | `0.50` | Claude per-op cost cap |
| `JARVIS_GOVERNED_CLAUDE_DAILY_BUDGET` | `10.00` | Claude daily budget |

### Deadline Budget Allocation

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_TIER0_BUDGET_FRACTION` | `0.50` | Fraction of deadline for Tier 0 |
| `OUROBOROS_TIER0_MAX_WAIT_S` | `90` | Absolute max Tier 0 wait |
| `OUROBOROS_TIER1_MIN_RESERVE_S` | `45` | Minimum reserved for Tier 1 |
| `OUROBOROS_PRIMARY_BUDGET_FRACTION` | `0.65` | Primary's share within Tier 1 |
| `OUROBOROS_FALLBACK_MIN_RESERVE_S` | `20` | Minimum reserved for fallback |

### Event Spine

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_PYTEST_PLUGIN_DISABLED` | -- | Set to `1` to disable pytest plugin |
| `JARVIS_POST_COMMIT_HOOK_DISABLED` | -- | Set to `1` to disable post-commit hook |
| `JARVIS_INTENT_TEST_INTERVAL_S` | `300` | TestWatcher poll fallback interval |
| `JARVIS_TODO_SCAN_INTERVAL_S` | `86400` | TodoScanner poll fallback interval |

---

## File Reference

### Core Pipeline

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/governed_loop_service.py` | Lifecycle manager (Zone 6.8) |
| `backend/core/ouroboros/governance/orchestrator.py` | Pipeline coordinator |
| `backend/core/ouroboros/governance/op_context.py` | OperationPhase, phase transitions |
| `backend/core/ouroboros/governance/risk_engine.py` | RiskClassification, RiskTier, ChangeType |
| `backend/core/ouroboros/governance/policy_engine.py` | PolicyEngine, PolicyDecision |
| `backend/core/ouroboros/governance/context_expander.py` | Pre-generation context expansion |
| `backend/core/ouroboros/governance/change_engine.py` | Filesystem patch application |
| `backend/core/ouroboros/governance/ledger.py` | Append-only operation ledger |
| `backend/core/ouroboros/governance/preemption_fsm.py` | Durable preemption state machine |
| `backend/core/ouroboros/governance/graduation_orchestrator.py` | Ephemeral -> permanent graduation |
| `backend/core/ouroboros/oracle.py` | TheOracle GraphRAG knowledge graph |

### Provider Routing

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/candidate_generator.py` | Adaptive failback FSM, FailureMode, recovery prediction |
| `backend/core/ouroboros/governance/providers.py` | PrimeProvider + ClaudeProvider |
| `backend/core/ouroboros/governance/doubleword_provider.py` | DoublewordProvider (Tier 0 batch API) |
| `backend/core/ouroboros/governance/rate_limiter.py` | RateLimitService, circuit breaker, token bucket |
| `backend/core/ouroboros/governance/brain_selector.py` | 3-layer deterministic brain gate |

### Unified Event Spine

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Sensor orchestrator + event spine wiring |
| `backend/core/ouroboros/governance/intake/fs_event_bridge.py` | FileWatchGuard -> TrinityEventBus bridge |
| `backend/core/ouroboros/governance/intake/sensors/backlog_sensor.py` | Backlog task detection (event-driven) |
| `backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py` | Test failure detection (plugin + subprocess) |
| `backend/core/ouroboros/governance/intake/sensors/todo_scanner_sensor.py` | TODO/FIXME detection (incremental scan) |
| `backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py` | Cyclomatic complexity detection (incremental) |
| `backend/core/ouroboros/governance/intake/sensors/doc_staleness_sensor.py` | Undocumented module detection |
| `backend/core/ouroboros/governance/intake/sensors/cross_repo_drift_sensor.py` | Cross-repo contract drift |
| `backend/core/trinity_event_bus.py` | TrinityEventBus (unified pub-sub spine) |
| `backend/core/resilience/file_watch_guard.py` | FileWatchGuard (watchdog wrapper) |
| `tests/ouroboros_pytest_plugin.py` | pytest plugin -> .jarvis/test_results.json |
| `scripts/hooks/post-commit` | Git post-commit -> .jarvis/git_events.json |

### Battle Test

| File | Purpose |
|------|---------|
| `scripts/ouroboros_battle_test.py` | CLI entry point |
| `backend/core/ouroboros/battle_test/harness.py` | BattleTestHarness lifecycle |
| `backend/core/ouroboros/battle_test/cost_tracker.py` | CostTracker with budget_event |
| `backend/core/ouroboros/battle_test/idle_watchdog.py` | IdleWatchdog with idle_event |

### Multi-Repo

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/saga/saga_apply_strategy.py` | Multi-repo saga application |
| `backend/core/ouroboros/governance/saga/cross_repo_verifier.py` | Cross-repo patch verification |
| `backend/core/ouroboros/governance/multi_repo/registry.py` | RepoRegistry for 3 repos |
