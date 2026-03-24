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

**Source**: `backend/core/ouroboros/governance/providers.py`

| Provider | Wraps | Features |
|----------|-------|----------|
| `PrimeProvider` | `PrimeClient.generate()` | Fixed temperature 0.2, schema enforcement |
| `ClaudeProvider` | `anthropic.AsyncAnthropic` | Cost-gated ($3/$15 per 1M), daily budget |

Both implement the `CandidateProvider` protocol used by `CandidateGenerator`'s
failback state machine.

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

## Intake Layer: Trigger Sensors

**Source**: `backend/core/ouroboros/governance/intake/intake_layer_service.py`

The `IntakeLayerService` (Zone 6.9) runs trigger sensors that detect
improvement opportunities:

| Sensor | Purpose |
|--------|---------|
| `TestFailureSensor` | Detects test failures and proposes fixes |
| `OpportunityMinerSensor` | Finds code quality improvements |
| `VoiceCommandSensor` | Processes voice-triggered code changes |
| `CapabilityGapSensor` | Detects missing capabilities |
| `BacklogSensor` | Processes queued improvement items |
| `ScheduledTriggerSensor` | Time-based periodic triggers |

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

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_GOVERNANCE_MODE` | `sandbox` | `sandbox`, `observe`, `governed` |
| `JARVIS_GENERATION_TIMEOUT_S` | `60` | Per-attempt generation timeout |
| `JARVIS_PIPELINE_TIMEOUT_S` | `150` | Full pipeline timeout |
| `JARVIS_GRADUATION_THRESHOLD` | `3` | Uses before ephemeral->permanent |
| `JARVIS_GRADUATION_APPROVAL_TIMEOUT_S` | `1800` | Approval wait (30min) |
| `JARVIS_MIN_GENERATION_BUDGET_S` | `30` | Minimum generation budget |
| `JARVIS_L2_ENABLED` | `false` | Enable L2 self-repair engine |
| `JARVIS_SHADOW_HARNESS_ENABLED` | `false` | Enable shadow harness |
| `OUROBOROS_VOICE_DEBOUNCE_S` | `60` | Voice narration debounce |
| `OUROBOROS_GCP_DAILY_BUDGET` | `0.50` | Daily cost gate budget (USD) |
| `DEBUG_MUTATION_MODE` | `false` | Reduce graduation threshold to 1 |

---

## File Reference

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/governed_loop_service.py` | Lifecycle manager (Zone 6.8) |
| `backend/core/ouroboros/governance/orchestrator.py` | Pipeline coordinator |
| `backend/core/ouroboros/governance/op_context.py` | OperationPhase, phase transitions |
| `backend/core/ouroboros/governance/risk_engine.py` | RiskClassification, RiskTier, ChangeType |
| `backend/core/ouroboros/governance/policy_engine.py` | PolicyEngine, PolicyDecision |
| `backend/core/ouroboros/governance/context_expander.py` | Pre-generation context expansion |
| `backend/core/ouroboros/governance/providers.py` | PrimeProvider + ClaudeProvider |
| `backend/core/ouroboros/governance/candidate_generator.py` | CandidateGenerator with failback FSM |
| `backend/core/ouroboros/governance/change_engine.py` | Filesystem patch application |
| `backend/core/ouroboros/governance/ledger.py` | Append-only operation ledger |
| `backend/core/ouroboros/governance/preemption_fsm.py` | Durable preemption state machine |
| `backend/core/ouroboros/governance/graduation_orchestrator.py` | Ephemeral -> permanent graduation |
| `backend/core/ouroboros/oracle.py` | TheOracle GraphRAG knowledge graph |
| `backend/core/ouroboros/governance/brain_selector.py` | 3-layer deterministic brain gate |
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Trigger sensor orchestrator |
| `backend/core/ouroboros/governance/saga/saga_apply_strategy.py` | Multi-repo saga application |
| `backend/core/ouroboros/governance/saga/cross_repo_verifier.py` | Cross-repo patch verification |
| `backend/core/ouroboros/governance/multi_repo/registry.py` | RepoRegistry for 3 repos |
