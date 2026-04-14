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
CLASSIFY --> ROUTE --> [CONTEXT_EXPANSION] --> [PLAN] --> GENERATE --> VALIDATE
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
| `PLAN` | Model-reasoned implementation planning (schema plan.1) | No |
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
CONTEXT_EXPANSION -> PLAN, GENERATE, CANCELLED
PLAN -------------> GENERATE, CANCELLED
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
| `generation_timeout_s` | 180s | Max seconds per generation attempt (hard `asyncio.wait_for` + 5s grace) |
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
| `DoublewordProvider` | Doubleword batch + real-time API (397B MoE) | $0.10 / $0.40 | Cost-gated, daily budget, 16384 max_tokens, 5s poll interval, per-request timeouts, RateLimitService, circuit breaker |
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
  - Fallback hard cap: 60s max (`_FALLBACK_MAX_TIMEOUT_S`) — prevents
    unreachable J-Prime from consuming the entire pipeline budget
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

## Venom: Agentic Execution Layer

The Ouroboros pipeline was originally a one-shot code generator: send a prompt,
get a patch back. **Venom** transforms it into a multi-turn agentic loop where
the provider can **read files, search code, run tests, and revise its output**
across multiple turns -- the same capability that makes Claude Code powerful.

Named after the Marvel symbiote, Venom is the **nervous system** that gives
Ouroboros fluid, adaptive intelligence within the deterministic governance
skeleton.

### ToolLoopCoordinator

**Source**: `backend/core/ouroboros/governance/tool_executor.py`

The core multi-turn agentic executor. Each iteration:
1. Provider generates a response (may include tool calls)
2. `parse_fn` extracts `ToolCall` objects from the response
3. `GoverningToolPolicy.evaluate()` checks each call against repo containment rules
4. `AsyncProcessToolBackend.execute()` runs approved tools in subprocess sandboxes
5. Tool results are appended to the conversation and sent back to the provider
6. Loop continues until the provider produces a final answer (no tool calls)

**Available Tools (L1)**:

| Tool | Purpose | Policy Gate |
|------|---------|-------------|
| `read_file` | Read repository files | Path must be within repo root |
| `search_code` | Grep-style pattern search | No `..` in glob patterns |
| `list_symbols` | Extract classes/functions from Python modules | Path within repo root |
| `run_tests` | Run pytest on specific test files | Requires `JARVIS_TOOL_RUN_TESTS_ALLOWED=true` |
| `get_callers` | Find call sites of functions | Path within repo root |
| `bash` | Sandboxed shell execution (Phase C) | Policy-gated |
| `web_fetch` | HTTP content retrieval (Phase D) | Policy-gated |

**Configuration**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_GOVERNED_TOOL_USE_ENABLED` | `false` | Master switch for Venom tool loop |
| `JARVIS_GOVERNED_TOOL_MAX_ROUNDS` | `5` | Max tool iterations per generation |
| `JARVIS_GOVERNED_TOOL_TIMEOUT_S` | `30` | Per-tool execution timeout |
| `JARVIS_GOVERNED_TOOL_MAX_CONCURRENT` | `2` | Concurrent tool executions |
| `JARVIS_TOOL_RUN_TESTS_ALLOWED` | `false` | Allow run_tests tool |
| `JARVIS_TOOL_OUTPUT_CAP_BYTES` | `4096` | Max tool output size in prompt |

**Provider Integration**: Both `ClaudeProvider` and `PrimeProvider` accept a
`tool_loop: Optional[ToolLoopCoordinator]` parameter. When provided, their
`generate()` method delegates to `tool_loop.run()` instead of making a single
API call. The coordinator handles deadline enforcement, token budget management,
and audit trail recording via `ToolExecutionRecord` objects.

### L2 Self-Repair Engine

**Source**: `backend/core/ouroboros/governance/repair_engine.py`

When L1 validation fails (tests don't pass), the L2 repair engine takes over
with an iterative convergence loop:

```
L2_INIT → L2_GENERATE_PATCH → L2_RUN_VALIDATION → L2_CLASSIFY_FAILURE
     ↑                                                      |
     |                                                      v
     +------------ L2_BUILD_REPAIR_PROMPT ←---- L2_DECIDE_RETRY
                                                      |
                                              (max iters or converged)
                                                      v
                                              L2_CONVERGED / L2_STOPPED
```

Each iteration:
1. **Generate patch** with failure context (error messages, failing tests)
2. **Run validation** in sandbox (pytest on affected files)
3. **Classify failure** (syntax, test, environment, flaky)
4. **Evaluate progress** (new failures? same failures? oscillation?)
5. **Decide retry** (progress streak? class-specific retry budget?)
6. **Build repair prompt** with specific failure analysis

Kill conditions: deadline exhaustion, timebox (120s), max iterations (5),
no-progress streak (2), oscillation detection (signature hash matching).

**Configuration**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_L2_ENABLED` | `true` | Master switch for L2 repair (set `false` to disable) |
| `JARVIS_L2_MAX_ITERS` | `5` | Max repair iterations |
| `JARVIS_L2_TIMEBOX_S` | `120` | Total wall-clock budget |
| `JARVIS_L2_ITER_TEST_TIMEOUT_S` | `60` | Per-iteration test timeout |
| `JARVIS_L2_MAX_DIFF_LINES` | `150` | Max diff size per iteration |
| `JARVIS_L2_MAX_FILES_CHANGED` | `3` | Max files per repair patch |

### How Venom Works with Ouroboros

```
Ouroboros GENERATE phase
    ├── Provider.generate() called with tool_loop attached
    │   └── ToolLoopCoordinator.run()
    │       ├── Turn 1: Model reads target files (read_file tool)
    │       ├── Turn 2: Model searches for related code (search_code)
    │       ├── Turn 3: Model generates patch with full context
    │       └── Turn 4: Model runs tests to verify (run_tests)
    │
    ├── VALIDATE phase: run tests on candidate patch
    │
    └── If validation fails → L2 Repair Engine activates
        ├── Iteration 1: Analyze failure, generate fix
        ├── Iteration 2: Test fix, classify remaining failures
        ├── Iteration 3: Revise based on specific test errors
        └── ... up to 5 iterations or convergence
```

---

## Autonomous Developer Intelligence (7 Capabilities)

These capabilities transform O+V from a patch generator into a proactive
autonomous developer that understands the codebase, learns from its own
history, and makes graduated autonomy decisions.

### 1. Operation Intent Visibility

**Source**: `serpent_flow.py` (`set_op_reasoning`, `show_diff`)

Every operation displays the model's reasoning for why it made a specific
change. The rationale is captured from `candidate_rationales` at GENERATE
and stored per-op via `set_op_reasoning(op_id, reasoning)`. Displayed as a
`reasoning:` line under each `Update(path)` block in the SerpentFlow CLI.

### 2. Severity-Based Human Escalation Protocol

**Source**: `risk_engine.py` (`RiskTier.NOTIFY_APPLY`), `orchestrator.py` (Phase 5b)

The `RiskTier` enum now has 4 tiers:

| Tier | Color | Auto-Apply? | Trigger Rules |
|------|-------|-------------|---------------|
| `SAFE_AUTO` | Green | Yes, silent | Single-file, non-core |
| `NOTIFY_APPLY` | Yellow | Yes, visible | New files (Rule 10), multi-file changes (Rule 11), core orchestration paths (Rule 12) |
| `APPROVAL_REQUIRED` | Orange | No, blocks | Security-sensitive, breaking API |
| `BLOCKED` | Red | Rejected | Supervisor, credentials, governance engine |

The orchestrator handles `NOTIFY_APPLY` at Phase 5b: it emits a
`notify_apply` decision via CommProtocol (surfaced prominently in
SerpentFlow as `NOTIFY ... auto-applying (Yellow severity)`), then
continues to APPLY without blocking.

### 3. Operation Dependency Chains (DAG-Based Signal Merging)

**Source**: `unified_intake_router.py` (`_active_file_ops`, `_queued_behind`, `release_op`)

When multiple sensors detect opportunities targeting the same files, the
intake router prevents conflicting concurrent patches:

```
Signal A targets [foo.py] → registered in _active_file_ops
Signal B targets [foo.py] → _find_file_conflict() returns A's op_id
  → B queued behind A in _queued_behind[op_id_A]
Signal A completes → release_op(op_id_A)
  → removes foo.py from _active_file_ops
  → re-ingests Signal B automatically
```

This eliminates merge conflicts from the organism fighting itself.

### 4. Exploration-First Enforcement

**Source**: `providers.py` (`_build_tool_section`)

The generation prompt requires the model to call at least 2 exploration
tools before proposing any code change:

```
### CRITICAL: Exploration-first protocol

Before proposing ANY code change, you MUST verify the current state using
at least 2 exploration tools:
1. Read the target file — read_file to see the actual current code.
   NEVER generate a patch from parametric memory alone.
2. Check dependents — search_code or get_callers to find code that
   imports/calls the function you're changing.
```

This prevents patches generated from stale weights. The model reads first,
then writes -- like a senior engineer.

#### 4a. Ledger-Based Exploration Scoring (Task #102 — Shadow-Log Phase)

**Source**: `exploration_engine.py` (pure module), `orchestrator.py` (Iron Gate call site)

The raw counter floor (`_op_explore_credit >= _min_explore`) is being
phased out in favor of **diversity-weighted scoring** that rewards
structured understanding over repeated reads. An `ExplorationLedger`
lifts every exploration tool call out of `generation.tool_execution_records`,
classifies it into one of five categories (COMPREHENSION, DISCOVERY,
CALL_GRAPH, STRUCTURE, HISTORY), and scores it with per-tool base weights.
Duplicate calls (same tool + same `arguments_hash`) get **zero credit** —
forward progress is measured as new files/queries, not repeated fetches.

The gate passes when all three conditions hold:

    diversity_score >= min_score
    |categories_covered| >= min_categories
    required_categories ⊆ categories_covered

Floors are env-driven (`JARVIS_EXPLORATION_MIN_SCORE_<C>`,
`JARVIS_EXPLORATION_MIN_CATEGORIES_<C>`). Default thresholds: trivial=0/0
(bypass), simple=4.0/2, moderate=8.0/3, architectural=14.0/4 with
required `{call_graph, history}` — architectural ops MUST read call
graphs and git history.

**Anti-gaming invariant**: failed tool calls accrue *score* (a failed
grep is still signal) but **not category coverage** — the AND-gate
prevents score-only inflation via failing-call floods.

**Rollout (phased)**:

1. **Shadow log** (current) — set `JARVIS_EXPLORATION_SHADOW_LOG=true`
   to log `ExplorationLedger(shadow)` lines alongside the legacy
   counter decision. No behavior change; used to compare
   distributions on STANDARD-heavy sessions before enforcement.
2. **Enforce** — set `JARVIS_EXPLORATION_LEDGER_ENABLED=true` to flip
   the Iron Gate to ledger-based evaluation. `render_retry_feedback`
   emits deterministic missing-category hints to GENERATE_RETRY.
3. **Deprecate legacy counter** — after one release of clean enforce
   metrics, remove `_op_explore_credit` and `_min_explore`.

**Deferred to later patches**: dedicated EXPLORE FSM phase between
CONTEXT_EXPANSION and PLAN; `plan_exploration()` model step;
`emit_hypothesis` meta-tool; per-route exploration timeboxes; Oracle
graph credit.

#### 4b. Background Worker Ceiling Invariant (Task #104)

**Source**: `background_agent_pool.py` (~line 618), `candidate_generator.py` (route budgets)

The `BackgroundAgentPool` per-op watchdog
(`JARVIS_BG_WORKER_OP_TIMEOUT_S`) is a last-resort ceiling that
prevents a wedged worker from monopolizing a slot. It MUST remain
**larger than the slowest legitimate generation path plus slack**, or
healthy ops get force-reaped before `generation` exists.

Invariant:

    worker_op_timeout
        >= max(route_generation_budget)
         +  tool_loop_overhead
         +  candidate_assembly
         +  verify_phase
         +  slack

Current budgets (BACKGROUND is the slowest legitimate path):

| Component                | Budget |
|--------------------------|--------|
| BACKGROUND DW generation | 180s   |
| Tool-loop overhead       | 15s    |
| Candidate assembly       | 30s    |
| Post-apply verify        | 60s    |
| Slack                    | 75s    |
| **Total**                | **360s** |

Default raised from 240s → 360s in Task #104 after session
`bt-2026-04-14-005028` showed **3 simple/BACKGROUND ops force-killed
at the 240s ceiling** (cost=$0, tool_execution_records=0), masking
shadow-log validation of Task #102. Battle tests exercising slow
BACKGROUND paths should either accept the 360s default or raise
further via env override; never lower below the slowest route's
generation budget plus assembly.

### 5. Post-Apply Verification Loop

**Source**: `orchestrator.py` (Phase 8a), `serpent_flow.py` (`op_verify_start`, `op_verify_result`)

After APPLY succeeds, O+V runs a **scoped test suite** targeting only the
files that were just modified. This catches regressions before the broader
benchmark gate and enables targeted L2 repair:

```
Phase 8a: Scoped post-apply test run
  ├── _validation_runner.run(changed_files, timeout=60s)
  ├── Emit heartbeat with verify_test_starting / verify_test_passed
  ├── SerpentFlow renders ⏺ Verify(files) with pass/fail counts
  │
  ├── On PASS → continue to benchmark gate → COMPLETE
  └── On FAIL → route to L2 repair engine
      ├── Build synthetic ValidationResult
      ├── _l2_hook(ctx, synth_val, deadline)
      ├── If L2 converges → continue to COMPLETE
      └── If L2 fails → combine with benchmark result → rollback
```

Budget: `JARVIS_VERIFY_TIMEOUT_S` (default 60s).

### 6. Cumulative Session Intelligence

**Source**: `orchestrator.py` (`_session_lessons`), `providers.py` (prompt injection), `op_context.py` (`session_lessons` field)

An ephemeral lessons buffer on `GovernedOrchestrator` accumulates compact
insights from every operation in the session:

- Success: `[OK] Fix assertion error in test_provider.py (test_provider.py)`
- Failure: `[FAIL:test] Add type hints to config loader (config_loader.py)`

Before each GENERATE phase, the lessons are injected into the
`OperationContext.session_lessons` field and rendered in the generation
prompt as a `## Session Lessons` section:

```
## Session Lessons (from prior operations this session)

Use these to avoid repeating mistakes and build on successes:

- [OK] Fix off-by-one in provider parser (providers.py)
- [FAIL:build] Add async wrapper — missing await (event_channel.py)
- [OK] Suppress PyPI timeout tracebacks (runtime_health_sensor.py)
```

Capped at 20 lessons (configurable via `JARVIS_SESSION_LESSONS_MAX`).

### 7. Cost-Aware Operation Prioritization

**Source**: `unified_intake_router.py` (`_compute_priority`)

The intake router's `PriorityQueue` uses a composite score instead of
raw source-type mapping:

```python
def _compute_priority(envelope):
    base = _PRIORITY_MAP.get(envelope.source, 99)    # source tier
    urgency = _URGENCY_BOOST.get(envelope.urgency, 0) # critical=3, high=1
    cost_penalty = 0 if files <= 1 else (1 if files <= 4 else 2)
    confidence_bonus = 1 if envelope.confidence >= 0.9 else 0
    return base - urgency + cost_penalty - confidence_bonus
```

Within the same source tier, focused single-file ops are processed before
sprawling multi-file ones. Critical/high-urgency signals are promoted
regardless of file count. All three enqueue paths (initial, retry,
WAL replay) use the composite score.

---

## Cognitive Depth: Extended Thinking

**Source**: `providers.py` (`ClaudeProvider._extended_thinking`)

When Claude is the generation provider, the Anthropic extended thinking API is
enabled by default (`JARVIS_EXTENDED_THINKING_ENABLED=true`).  This gives the
model a configurable thinking budget (`JARVIS_THINKING_BUDGET`, default 10000
tokens) to reason deeply before producing code.

**Manifesto alignment**: §6 — "deploy intelligence where it creates true
leverage."  Extended thinking is intelligence at the reasoning boundary.  The
model thinks through edge cases, considers alternatives, and plans changes
before writing them.  This is the difference between a junior developer who
writes the first thing that comes to mind and a senior developer who considers
the implications.

**Implementation details**:
- Thinking is enabled for generation calls only (not tool rounds or planning)
- `temperature=1.0` is required by Anthropic when thinking is enabled
- Tool rounds use `temperature=0.2` and no thinking (fast JSON responses)
- Response parsing extracts only `text` blocks, skipping `thinking` blocks
- Token usage tracking includes thinking tokens in the budget

## Tool Defaults: Unshackled Under Governance

All 15 Venom tools are **enabled by default**.  The safety perimeter is the
governance stack (Iron Gate, risk engine, approval gates), not env-var opt-in:

| Tool | Gate | Default |
|------|------|---------|
| `read_file`, `search_code`, `get_callers`, `glob_files`, `list_dir`, `list_symbols` | Always allowed | ON |
| `git_log`, `git_diff`, `git_blame` | Read-only | ON |
| `edit_file`, `write_file` | `JARVIS_TOOL_EDIT_ALLOWED` | **ON** |
| `bash` | `JARVIS_TOOL_BASH_ALLOWED` + Iron Gate blocklist | **ON** |
| `run_tests` | `JARVIS_TOOL_RUN_TESTS_ALLOWED` | **ON** |
| `web_fetch`, `web_search` | `JARVIS_WEB_TOOL_ENABLED` + domain allowlist | **ON** |
| `code_explore` | Sandbox subprocess | ON |

The Venom tool loop master switch (`JARVIS_GOVERNED_TOOL_USE_ENABLED`) also
defaults to `true`.  To disable all tools, set it to `false`.

**Manifesto alignment**: §1 — "Deterministic code is the skeleton; agentic
intelligence is the nervous system."  The Iron Gate (AST parser, command
blocklist) is the deterministic skeleton.  The tools are the nervous system.
The skeleton does not think; the nervous system does not hold weight.

---

## Model-Reasoned PLAN Phase (Gap #3)

**Source**: `plan_generator.py` (`PlanGenerator`, `PlanResult`)

Between CONTEXT_EXPANSION and GENERATE, the model reasons about **HOW** to
implement a change before writing code. This replicates Claude Code's internal
planning step as an explicit, observable phase.

**Schema**: `plan.1` -- structured JSON with:
- `approach`: 1-3 sentence strategy summary
- `complexity`: trivial | moderate | complex | architectural
- `ordered_changes`: dependency-ordered file change descriptors
- `risk_factors`: specific, actionable risks
- `test_strategy`: verification approach
- `architectural_notes`: cross-cutting concerns

**Behavior**:
- Trivial ops (single file, short description) skip planning entirely
- Planning failures are soft -- pipeline falls through to GENERATE
- The plan is injected into the GENERATE prompt as an `## Implementation Plan`
  section so the code-generation model follows a coherent strategy
- Coherence validation: planned files must overlap with target files,
  internal dependencies must form a DAG (no cycles)

**Environment variables**:
- `JARVIS_PLAN_TIMEOUT_S` (default 45): planning phase timeout
- `JARVIS_PLAN_FILE_CONTEXT_CHARS` (default 6000): max chars per file in plan prompt
- `JARVIS_PLAN_TRIVIAL_MAX_FILES` (default 1): skip planning below this file count

**SerpentFlow rendering**: Plan phase shows as `🗺️  planning` during execution
and `🗺️  planned` with complexity badge on completion.

**Manifesto alignment**: §5 -- "Deploy intelligence only where it creates true
leverage." Multi-file coordinated changes benefit most from upfront planning.
Trivial single-file edits skip the overhead.

---

## Mid-Operation Clarification: ask_human Tool (Gap #4)

**Source**: `tool_executor.py` (`_L1_MANIFESTS["ask_human"]`)

When the model is uncertain about intent, scope, or approach during the Venom
tool loop, it can call the `ask_human` tool to ask the human operator for
clarification. This prevents wrong-direction work on non-trivial changes.

**Risk tier gating** (Manifesto §5):
- `SAFE_AUTO` (Green): **DENIED** — low-risk ops should not interrupt the human
- `NOTIFY_APPLY` (Yellow): **ALLOWED** — model can ask before auto-applying
- `APPROVAL_REQUIRED` (Orange): **ALLOWED** — model can clarify before submission
- `BLOCKED` (Red): **DENIED** — blocked ops cannot interact at all

**Mechanism**: Calls `CLIApprovalProvider.elicit()` which sets a per-request
`asyncio.Event` and waits for the REPL or MCP handler to deliver an answer.
Timeout defaults to 300s (5 minutes). Returns `{"status": "answered", "answer": "..."}` or `{"status": "timeout", "answer": null}`.

**Manifesto alignment**: §5 — Deploy intelligence where it creates leverage.
Asking the human a 10-second question can save 5 minutes of wrong-direction work.

---

## L3 Worktree Isolation (Gap #5)

**Source**: `governed_loop_service.py` (`l3_enabled`), `worktree_manager.py`

Enabled by default (`JARVIS_GOVERNED_L3_ENABLED=true`). When execution graphs
run parallel work units, each unit gets an isolated git worktree to prevent
filesystem conflicts between concurrent operations.

**Environment variables**:
- `JARVIS_GOVERNED_L3_ENABLED` (default `true`): Master switch
- `JARVIS_GOVERNED_L3_MAX_CONCURRENT_GRAPHS` (default `2`): Limits disk/resource usage
- `JARVIS_GOVERNED_L3_STATE_DIR` (default `~/.jarvis/ouroboros/execution_graphs`): Graph state persistence

**Safety guarantees**:
- Worktree cleanup always runs in `finally` block (idempotent)
- Fallback to shared repo if worktree creation fails
- All git operations use `subprocess_exec` (no shell injection)
- Branch names derived from deterministic unit_id + graph_id

**Manifesto alignment**: §6 — The Iron Gate. Worktree isolation is a
deterministic safety boundary that prevents concurrent agentic operations from
interfering with each other.

---

## Auto-Commit Post-APPLY (Gap #6)

**Source**: `auto_committer.py` (`AutoCommitter`), `orchestrator.py` (Phase 8b)

Closes the autonomy loop: after O+V applies a change and verifies it passes
tests, the AutoCommitter creates a structured git commit with the O+V
signature. Without this, applied changes sit on disk as uncommitted
modifications, breaking the self-development cycle.

### Commit Message Format

```text
<type>(<scope>): <description>

Op-ID: <op_id>
Risk: <risk_tier>
Provider: <provider> ($<cost>)
Files: <file_list>

Ouroboros+Venom [O+V] — Autonomous Self-Development Engine
Co-Authored-By: Ouroboros+Venom <ouroboros@jarvis.trinity>
```

- **Type**: Inferred from description keywords (`fix`, `feat`, `refactor`, `test`, `docs`, `perf`, `style`)
- **Scope**: Inferred from common path prefix of target files
- **O+V Signature**: Non-negotiable identity block on every autonomous commit

### Risk-Tier Behavior

| Tier | Behavior |
|------|----------|
| `SAFE_AUTO` (Green) | Commit immediately after VERIFY passes |
| `NOTIFY_APPLY` (Yellow) | Commit after diff preview delay |
| `APPROVAL_REQUIRED` (Orange) | Commit after human approval |
| `BLOCKED` (Red) | Never reaches APPLY — no commit |

### Environment Variables

- `JARVIS_AUTO_COMMIT_ENABLED` (default `true`): Master switch
- `JARVIS_AUTO_PUSH_BRANCH` (default `""`): If set, push to this branch after commit. Empty = no push. Never pushes to protected branches (`main`, `master`, `production`, `release`).

### Orchestrator Integration

Phase 8b in the 11-phase pipeline, between VERIFY success and COMPLETE:

1. AutoCommitter stages only the target files (not `git add -A`)
2. Builds structured commit message with O+V signature
3. Creates commit via `asyncio.create_subprocess_exec` (no shell injection)
4. Emits heartbeat with commit hash for SerpentFlow rendering
5. Optional push to non-protected branch

### SerpentFlow Rendering

Commit results appear in the flowing CLI output:

```
📝 committed  a1b2c3d  -> feature-branch  O+V
```

### Manifesto Alignment

- **§6 — The Iron Gate**: All git operations use `create_subprocess_exec` arrays, never shell strings. Push gated to non-protected branches.
- **§7 — Absolute Observability**: Commit hash emitted via heartbeat for SerpentFlow rendering.

---

## MCP Tool Forwarding (Gap #7)

**Source**: `mcp_tool_client.py` (`GovernanceMCPClient.discover_tools`), `providers.py` (`_build_tool_section`), `tool_executor.py` (MCP dispatch)

External MCP tools from connected servers are discovered at prompt construction
time and injected into the generation prompt alongside the 16 built-in Venom
tools. The model can call any MCP tool using the `mcp_{server}_{tool}` naming
convention.

### Architecture

```
GovernanceMCPClient.discover_tools()
    ↓  (tools/list JSON-RPC to each connected server)
List[{name, description, input_schema}]
    ↓  (injected into _build_tool_section)
Generation prompt: "**External MCP tools (connected servers):**"
    ↓  (model calls mcp_github_create_issue)
ToolLoopCoordinator → GoverningToolPolicy (Rule 0b: auto-allow)
    ↓
AsyncProcessToolBackend._run_mcp_tool()
    ↓  (tools/call JSON-RPC to server)
GovernanceMCPClient.call_tool(qualified_name, arguments)
```

### Policy

MCP tools bypass the standard manifest check (Rule 0) and are auto-allowed
(Rule 0b: `tool.allowed.mcp_external`). External servers handle their own
authentication and authorization.

### Configuration

- `JARVIS_MCP_CONFIG`: YAML path for MCP server connections
- Each server can be `stdio` (subprocess) or `sse` (remote)

### Manifesto Alignment

- **§5 — Intelligence-Driven Routing**: MCP tools are discovered dynamically,
  not hardcoded. The model chooses which tools to call based on context.
- **§6 — The Iron Gate**: MCP subprocess transport uses
  `create_subprocess_exec` (no shell injection). JSON-RPC 2.0 protocol.

---

## Live Context Auto-Compaction (Gap #8)

**Source**: `tool_executor.py` (`ToolLoopCoordinator._compact_prompt`), `context_compaction.py` (`ContextCompactor`)

During long tool loop runs, the accumulated prompt grows as tool results are
appended. When the prompt exceeds 75% of the maximum budget (default 98,304
chars), older tool results are compacted into a deterministic summary.

### Algorithm

1. Split accumulated prompt at `[TOOL RESULT]` / `[TOOL ERROR]` boundaries
2. Preserve the most recent 6 tool result chunks (configurable via
   `JARVIS_COMPACT_PRESERVE_TOOL_CHUNKS`)
3. Summarize older chunks: count tool calls by name, total chars removed
4. Replace older chunks with summary block:
   ```
   [CONTEXT COMPACTED]
   Compacted 12 earlier tool results (45,230 chars): 5 read_file, 4 search_code, 3 bash.
   Recent results preserved below.
   [END CONTEXT COMPACTED]
   ```

### Properties

- **No model inference**: Pure deterministic counting and string manipulation
- **Preserves recent context**: Model always sees its most recent tool results
- **Graceful degradation**: Only triggers when needed (75% threshold). If still
  over budget after compaction, the hard `_MAX_PROMPT_CHARS` limit applies.

### Environment Variables

- `JARVIS_TOOL_LOOP_COMPACT_THRESHOLD`: Trigger threshold in chars (default: 75% of max)
- `JARVIS_COMPACT_PRESERVE_TOOL_CHUNKS`: Number of recent chunks to preserve (default: 6)

### Manifesto Alignment

- **§3 — Disciplined Concurrency**: Context compaction prevents runaway memory
  growth in long tool loops without blocking the event loop.

---

## DW 3-Tier Event-Driven Architecture

**Source**: `doubleword_provider.py`, `batch_future_registry.py`, `event_channel.py`, `governed_loop_service.py`

The DoubleWord provider implements a 3-tier architecture that eliminates
polling for the primary path (Manifesto §3: Zero polling. Pure reflex.).

### Tier 0: Real-Time SSE (Primary)

Default path. Uses `/v1/chat/completions` with SSE streaming and Venom tool
loop. Zero polling. Token-by-token streaming. Falls back to batch on 429/503
(stays within cheap DW instead of cascading to 150x more expensive Claude).

- `DOUBLEWORD_REALTIME_ENABLED` (default `true`)

### Tier 1: Webhook-Driven Batch (Zero-Poll)

For batch operations that fall through from Tier 0. The `BatchFutureRegistry`
maps `batch_id` to `asyncio.Future`, resolved by incoming DW webhooks via the
`EventChannelServer`.

- `BatchFutureRegistry`: register/resolve/reject/wait + TTL auto-pruning
- `EventChannelServer`: `POST /webhook/doubleword` with Standard Webhooks
  HMAC-SHA256 signature verification
- `DOUBLEWORD_WEBHOOK_SECRET`: Signing key from DW dashboard

### Tier 2: Adaptive Backoff Poll (Safety Net)

Fallback when webhooks aren't configured. Replaces fixed 5s polling with
exponential backoff + jitter:

- Starting interval: 2s, multiplier: 1.5x, cap: 30s, jitter: ±25%
- Network-aware: connection errors jump to 15s base
- One-line debug logs (no tracebacks on transient failures)

### Manifesto Alignment

- **§3 — Zero Polling. Pure Reflex**: Tier 0 (SSE) and Tier 1 (webhook)
  eliminate polling entirely. Tier 2 uses adaptive backoff only as last resort.
- **§6 — The Iron Gate**: Webhook signature verification prevents spoofed
  batch completions.

---

## Edge Case Hardening (12 Refinements)

These refinements close failure modes discovered during the first battle tests.

### 8. Session Intelligence Poisoning Guard

**Source**: `orchestrator.py` (`_INFRA_PATTERNS`, `_lesson_type`)

Infrastructure failures (timeouts, provider outages, rate limits) are tagged
as `"infra"` and excluded from the `## Session Lessons` injection into
generation prompts. Only `"code"` lessons (actual logic failures, test
regressions) reach the model. This prevents transient infrastructure noise
from teaching the model false patterns.

### 9. Cost-Aware Priority with Dependency Credit

**Source**: `unified_intake_router.py` (`_compute_priority`, `dependency_credit`)

Signals that block other signals get a priority boost: `_dep_credit` counts
how many envelopes are queued behind files this op would touch. The credit
is capped at 3 to prevent runaway promotion.

### 10. Post-Apply Verify + L2 Repair Scope Fix

**Source**: `orchestrator.py` (Phase 8a)

When the scoped test suite fails after APPLY, the L2 repair candidate is now
actually written to disk via `change_engine.execute()` — previously the fix
was generated but not applied.

### 11. DAG Queue Starvation Prevention (TTL)

**Source**: `unified_intake_router.py` (`_file_lock_ttl_s`, `_find_file_conflict`)

Active file locks now carry timestamps. `_find_file_conflict()` force-releases
locks older than `JARVIS_FILE_LOCK_TTL_S` (default 300s) with a warning log.
Prevents signals from being permanently queued behind crashed operations.

### 12. Exploration-First Runtime Enforcement

**Source**: `orchestrator.py` (VALIDATE gate), `tool_executor.py` (`_max_exploration_rounds`)

At VALIDATE time, `GenerationResult.tool_execution_records` is scanned to
verify the model called at least `JARVIS_MIN_EXPLORATION_CALLS` (default 2)
exploration tools (`read_file`, `search_code`, `get_callers`). Violations
produce a warning and are recorded in the per-candidate ledger.

The tool loop also enforces an exploration budget: after
`JARVIS_MAX_EXPLORATION_ROUNDS` (default 5) exploration-only rounds, a system
nudge forces the model to produce its final answer.

### 13. Session Intelligence Thread-Safety

`_session_lessons` is safe under the asyncio single-threaded event loop.
A comment documents that an `asyncio.Lock` is needed if the orchestrator ever
moves to multi-threaded execution.

### 14. Stale Exploration Guard (File Hash)

**Source**: `orchestrator.py` (`generate_file_hashes`), `op_context.py`

Target file SHA-256 hashes are snapshotted at GENERATE time and stored on
`OperationContext.generate_file_hashes`. At APPLY time, hashes are
recomputed — if any differ, the candidate was built on stale file state.
Currently a soft gate (warning + ledger), preventing silent data loss from
concurrent operations.

### 15. Signal Coalescing Window

**Source**: `unified_intake_router.py` (`_coalesce_buffer`, `_coalesce_window_s`)

When multiple sensors detect issues in the same file within a configurable
window (`JARVIS_COALESCE_WINDOW_S`, default 30s), their envelopes are merged
into a single multi-goal operation. This reduces N sequential operations to
one, cutting cost by up to Nx. HIGH urgency signals bypass coalescing.

### 16. Operation Cancellation from REPL

**Source**: `governed_loop_service.py` (`request_cancel`, `is_cancel_requested`),
`serpent_flow.py` (`SerpentREPL._handle_cancel`), `orchestrator.py`

REPL command: `cancel <op-id>` (prefix match). Sets a cooperative
cancellation flag checked at GENERATE and APPLY phase boundaries. The
orchestrator transitions to `CANCELLED` with reason `user_cancelled`.

### 17. Diff Preview for NOTIFY_APPLY (Yellow)

**Source**: `orchestrator.py` (Phase 5b)

Before auto-applying Yellow-tier changes, the diff is rendered in the CLI
with a configurable delay (`JARVIS_NOTIFY_APPLY_DELAY_S`, default 5s).
During this window, `/cancel` (or REPL `cancel`) can reject the change.
After the window, apply proceeds.

### 18. Session Intelligence Convergence Metric

**Source**: `orchestrator.py` (`_ops_before_lesson`, `_ops_after_lesson`)

Tracks success rates before and after the first lesson is recorded. Every
`JARVIS_LESSON_CONVERGENCE_CHECK_INTERVAL` (default 10) post-lesson operations,
compares the rates. If post-lesson success rate is lower than pre-lesson,
the lessons are considered misleading and the buffer is cleared.

### 19. Exploration Budget Control

**Source**: `tool_executor.py` (`_max_exploration_rounds`)

Caps exploration-only tool rounds at `JARVIS_MAX_EXPLORATION_ROUNDS` (default 5).
After the cap, a system message nudges the model: "You have enough context.
Produce your final code change now." Prevents unbounded codebase scanning
before generation.

---

## SerpentFlow CLI

**Source**: `battle_test/serpent_flow.py` (1,900+ lines)

The SerpentFlow CLI is the default interface for the battle test runner.
It renders autonomous operations using the visual language pioneered by
Claude Code, adapted for O+V's proactive nature.

### Architecture

```
SerpentFlow (Rich Console + prompt_toolkit)
  ├── SerpentTransport (CommProtocol transport)
  │   └── Routes INTENT/HEARTBEAT/DECISION/POSTMORTEM → flow methods
  ├── SerpentApprovalProvider (Iron Gate)
  │   └── prompt_toolkit session for Y/N approval
  └── SerpentREPL (interactive commands)
      └── /status, /cost, /pause, /resume, /q
```

### Visual Elements

| Block | Trigger | Renders |
|-------|---------|---------|
| `┌ op-id ... goal` | INTENT message | Op header with sensor type |
| `⏺ Read(path)` | read_file tool call | File path |
| `⏺ Update(path)` | edit_file tool call | CC-style diff with +/- lines |
| `⏺ Write(path)` | write_file tool call | Line count |
| `⏺ Verify(files)` | Post-apply test | Pass/fail counts |
| `🧬 synthesized` | Generation complete | Provider, tokens, duration |
| `🛡️ immune` | Validation result | Test pass/fail |
| `🩹 repair` | L2 iteration | Iteration count, status |
| `⚠ NOTIFY` | NOTIFY_APPLY decision | Reason code, files |
| `└ ✅ complete` | DECISION:completed | Provider, cost, duration |

### Terminal Rendering

SerpentFlow uses `Console(force_terminal=True)` to force Rich ANSI output
through prompt_toolkit's stdout proxy, and `patch_stdout(raw=True)` to
preserve ANSI escape codes. Without these, Rich output appears as raw
`?[2m` sequences in the prompt_toolkit-patched terminal.

---

## Trinity Consciousness: The Metacognition Layer

**Source**: `backend/core/ouroboros/consciousness/`

Trinity Consciousness is **Zone 6.11** -- the self-awareness layer that gives
Ouroboros episodic memory, failure prediction, and adaptive risk assessment.
It is the **soul** of the organism (Manifesto Section 4: "The Synthetic Soul").

### Architecture: 4 Engines + 3 Awareness Fusion

```
┌─────────────────────────────────────────────────┐
│              Trinity Consciousness               │
│                                                  │
│  Core Engines:                                   │
│  ├── HealthCortex (30s health polling)           │
│  ├── MemoryEngine (episodic outcomes, 168h TTL)  │
│  ├── DreamEngine (idle-time improvement plans)   │
│  └── ProphecyEngine (regression prediction)      │
│                                                  │
│  Awareness Fusion:                               │
│  ├── CAI (Contextual Awareness Intelligence)     │
│  ├── SAI (Situational Awareness Intelligence)    │
│  └── UAE (Unified Awareness Engine)              │
│                                                  │
│  Integration:                                    │
│  ├── ConsciousnessBridge (5 methods → pipeline)  │
│  └── GoalMemoryBridge (ChromaDB cross-session)   │
│                                                  │
│  Strategic Direction:                            │
│  └── StrategicDirectionService (Manifesto → ops) │
└─────────────────────────────────────────────────┘
```

### ConsciousnessBridge Integration Points

| Method | Phase | What it does |
|--------|-------|-------------|
| `assess_regression_risk()` | CLASSIFY | Queries ProphecyEngine + MemoryEngine. Elevates risk to HIGH if target files have success_rate < 0.5 |
| `get_fragile_file_context()` | GENERATE RETRY | Returns markdown context about historically fragile files for prompt injection |
| `is_system_healthy_for_exploration()` | Intake gating | Returns (healthy, reason) from HealthCortex for ProactiveDriveService |
| `record_operation_outcome()` | POST-APPLY | Feeds op_id, files, success/failure back into MemoryEngine for cross-session learning |
| `assess_operation_awareness()` | PRE-GENERATE | Returns UAE OperationAwareness: risk, confidence, suggested provider tier, thinking budget, prompt injection |

### Core Engines

**MemoryEngine** (`memory_engine.py`, 680 lines):
- Ingests terminal operation outcomes (APPLIED, FAILED, ROLLED_BACK)
- Builds `FileReputation`: success_rate, fragility_score, co_failures per file
- Persists to `~/.jarvis/ouroboros/consciousness/` (insights.jsonl, file_reputations.json)
- TTL-based decay: 168 hours default

**ProphecyEngine** (`prophecy_engine.py`, 402 lines):
- Heuristic-only failure predictor (no LLM)
- Risk score: `(1-success_rate)*0.3 + fragility*0.3 + dependents/20*0.2 + 0.1`
- Risk levels: LOW (<0.3), MEDIUM (<0.6), HIGH (<0.8), CRITICAL (>=0.8)
- Confidence capped at 0.6 (heuristic-only)

**HealthCortex** (`health_cortex.py`, 668 lines):
- Polls JARVIS, PRIME, Reactor health every 30s
- Rolling HealthTrend (720 snapshots = 6 hours)
- Output: TrinityHealthSnapshot with overall_score, resource pressure

**DreamEngine** (`dream_engine.py`, 828 lines):
- Pre-computes ImprovementBlueprint during idle time (>300s)
- Daily budget: 120 minutes
- Blueprints keyed on (repo_sha, policy_hash)

### Awareness Fusion Engines

**CAI (Contextual Awareness)** (`contextual_awareness.py`, 879 lines):
- Discovers hotspots, coupling, drift from MemoryEngine data
- Background analysis every 300s
- Output: ContextAssessment with relevant insights, complexity estimate

**SAI (Situational Awareness)** (`situational_awareness.py`, 1333 lines):
- Tracks temporal patterns and causal chains
- Detects post-deploy cascades, time-correlated behaviors
- Output: SituationAssessment with timing advice, risk modifiers

**UAE (Unified Awareness)** (`unified_awareness.py`, 1232 lines):
- Fuses CAI + SAI into holistic state
- Output: `OperationAwareness` with suggested_provider_tier, thinking_budget, prompt_injection
- Awareness levels: DORMANT, OBSERVING, ATTENTIVE, FOCUSED, HYPERAWARE

### Strategic Direction Service

**Source**: `backend/core/ouroboros/governance/strategic_direction.py`

Reads the Manifesto (README.md) and architecture docs on boot, extracts
the 7 core principles, and injects a ~2500-character strategic context
digest into every operation's `strategic_memory_prompt`.

**What the provider sees in every generation prompt:**

```
## Strategic Direction (Manifesto v4)

You are generating code for the JARVIS Trinity AI Ecosystem — an autonomous,
self-evolving AI Operating System. Every change must align with these principles:

1. The unified organism (tri-partite microkernel)
2. Progressive awakening (adaptive lifecycle)
3. Asynchronous tendrils (disciplined concurrency)
4. The synthetic soul (Trinity consciousness)
5. Intelligence-driven routing (the cognitive forge)
6. Threshold-triggered neuroplasticity (Ouroboros)
7. Absolute observability (systemic transparency)

MANDATE: Structural repair, not patches.
```

**Sources read on boot:**

| Doc | What's extracted |
|-----|-----------------|
| `README.md` | 7 principles, zero-shortcut mandate, Trinity architecture |
| `docs/architecture/OUROBOROS.md` | Pipeline overview, provider routing |
| `docs/architecture/BRAIN_ROUTING.md` | 3-tier cascade overview |

This means the organism generates code that aligns with the developer's
architectural vision — not generic fixes, but Manifesto-compliant code.

### The Complete Loop: 6 Layers Working Together

```
Strategic Direction (compass — WHERE are we going?)
    │  Manifesto: 7 principles, Trinity ecosystem, zero-shortcut mandate
    │  Injected into every operation's generation prompt
    │
    ▼
Trinity Consciousness (soul — WHY evolve?)
    │  MemoryEngine: "tests/test_utils.py has failed 60% of the time"
    │  ProphecyEngine: "HIGH regression risk for this file"
    │  GoalMemoryBridge: cross-session ChromaDB episodic learning
    │
    ▼
Event Spine (senses — WHEN to act?)
    │  FileWatchGuard: .py file changed → fs.changed.modified
    │  pytest plugin: test_results.json → TestFailureSensor
    │  post-commit hook: git_events.json → DocStalenessSensor
    │
    ▼
Ouroboros Pipeline (skeleton — WHAT to do, safely)
    │  CLASSIFY: risk + strategic direction + consciousness context
    │  ROUTE: adaptive 3-tier cascade (DW → Claude → GCP)
    │  2 parallel operations via BackgroundAgentPool
    │
    ▼
Venom Agentic Loop (nervous system — HOW to do it)
    │  read_file → search_code → bash → run_tests → web_search → revise
    │  Deadline-based loop (iterate until done or time expires)
    │  L2 Repair: generate → test → classify → fix → test again (5x)
    │
    ▼
Code Applied, Tests Pass, Operation COMPLETE
    │  Signed: Generated-By: Ouroboros + Venom + Consciousness
    │  Thought log: .jarvis/ouroboros_thoughts.jsonl
    │
    ▼
Trinity Consciousness (learns from outcome)
    │  MemoryEngine: records success → file reputation improves
    │  GoalMemory: records to ChromaDB for cross-session retrieval
    │  Next operation benefits from accumulated experience
```

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

### Thought Log

**Source**: `backend/core/ouroboros/governance/goal_memory_bridge.py`

The `GoalMemoryBridge` writes a human-readable JSONL thought log to
`.jarvis/ouroboros_thoughts.jsonl` showing the organism's reasoning
process at each phase:

| Phase | What is logged |
|-------|---------------|
| `BOOT` | Scanning codebase for opportunities |
| `MEMORY_RECALL` | What memories were found, how many, relevance |
| `TOOL` | Which tool was called and why (read_file, bash, run_tests) |
| `GENERATE` | Generation strategy, provider used, context size |
| `REPAIR` | L2 iteration progress, failure classification, convergence |
| `POST_APPLY` | Success/failure outcome, what was learned |

Logged at INFO level so `battle_test.py -v` shows the thought process
in real time. Also persisted to disk for post-session review.

### Autonomous Commit Signature

Every commit made by Ouroboros includes a dynamic `Generated-By` trailer
identifying which subsystems contributed:

```
[ouroboros] fix FIXME in governed_loop_service.py

op_id: op-019d6633-10ed-71ae-b324-fd5d412cfc3b
saga_id: saga-abc123
repo: jarvis
provider: claude-api
phase: apply

Generated-By: Ouroboros + Venom + Consciousness
Signed-off-by: JARVIS Ouroboros <ouroboros@jarvis.local>
```

| Signature component | When present |
|---------------------|-------------|
| `Ouroboros` | Always (governance pipeline) |
| `+ Venom` | When `tool_execution_records` exist (multi-turn tool use) |
| `+ Consciousness` | When `ConsciousnessBridge` is active (memory/prediction) |

Git author: `JARVIS Ouroboros <ouroboros@jarvis.local>`.

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
| `JARVIS_L2_ENABLED` | `true` | Enable L2 self-repair engine (set `false` to disable) |
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
| `OUROBOROS_TIER1_MIN_RESERVE_S` | `25` | Minimum reserved for Tier 1 (reduced from 45 to avoid starving Tier 0) |
| `OUROBOROS_PRIMARY_BUDGET_FRACTION` | `0.65` | Primary's share within Tier 1 |
| `OUROBOROS_FALLBACK_MIN_RESERVE_S` | `20` | Minimum reserved for fallback |

### Event Spine

| Variable | Default | Purpose |
|----------|---------|---------|
| `OUROBOROS_PYTEST_PLUGIN_DISABLED` | -- | Set to `1` to disable pytest plugin |
| `JARVIS_POST_COMMIT_HOOK_DISABLED` | -- | Set to `1` to disable post-commit hook |
| `JARVIS_INTENT_TEST_INTERVAL_S` | `300` | TestWatcher poll fallback interval |
| `JARVIS_TODO_SCAN_INTERVAL_S` | `86400` | TodoScanner poll fallback interval |

### Cost Optimization

| Variable | Default | Purpose |
|----------|---------|---------|
| `DOUBLEWORD_REALTIME_ENABLED` | `true` | Use `/v1/chat/completions` instead of batch (enables Venom on DW) |
| `JARVIS_TOOL_ROUND_MAX_TOKENS` | `1024` | max_tokens during Venom tool rounds (lower = cheaper) |
| `JARVIS_CODEGEN_MAX_FILE_CHARS` | `20000` | Max source snapshot per file (was 65536, 3x reduction) |
| `JARVIS_CODEGEN_HEAD_CHARS` | `16000` | Head portion of truncated files |
| `JARVIS_CODEGEN_TAIL_CHARS` | `4000` | Tail portion of truncated files |

### Cost Architecture

The organism uses 6 layers of cost optimization to maximize operations per budget:

| Layer | Mechanism | Savings |
|-------|-----------|---------|
| **DW real-time primary** | `/v1/chat/completions` at $0.10/$0.40/M instead of Claude $3/$15/M | 30-37x |
| **Claude prompt caching** | `cache_control: {"type": "ephemeral"}` on system prompt | 90% input cost |
| **Smart max_tokens** | 1024 during Venom tool rounds, 8192 only for final patch | ~75% output reduction |
| **Prompt compression** | 20KB max per file (with Venom, model can `read_file` for details) | ~60% input reduction |
| **Complexity routing** | TRIVIAL tasks skip Venom tool loop (one-shot, no tool overhead) | Skip 4-5 API calls |
| **Adaptive provider routing** | FailbackFSM routes to cheapest available provider with recovery prediction | Dynamic |

**Projected: 50-150+ operations per $0.50 budget** (vs 5-15 before optimization).

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
| `backend/core/ouroboros/governance/comm_protocol.py` | 5-phase communication protocol (INTENT→PLAN→HEARTBEAT→DECISION→POSTMORTEM) |
| `backend/core/ouroboros/governance/semantic_triage.py` | SemanticTriage pre-generation filter (NO_OP/REDIRECT/ENRICH/GENERATE) |
| `backend/core/ouroboros/governance/serpent_animation.py` | ASCII Ouroboros animation (auto-suppressed by LiveDashboard) |
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
| `backend/core/ouroboros/governance/intake/sensors/github_issue_sensor.py` | GitHub issue polling across Trinity repos |
| `backend/core/ouroboros/governance/intake/sensors/proactive_exploration_sensor.py` | Entropy-driven curiosity exploration |
| `backend/core/ouroboros/governance/intake/sensors/intent_discovery_sensor.py` | Manifesto-driven proactive improvement synthesis |
| `backend/core/ouroboros/governance/intake/sensors/cu_execution_sensor.py` | Compute unit execution tracking |
| `backend/core/ouroboros/governance/intake/sensors/runtime_health_sensor.py` | Python EOL, package staleness, security audit |
| `backend/core/ouroboros/governance/intake/sensors/web_intelligence_sensor.py` | PyPI CVE/advisory scanning |
| `backend/core/ouroboros/governance/intake/sensors/performance_regression_sensor.py` | Latency drift and quality degradation |
| `backend/core/ouroboros/governance/intake/sensors/voice_command_sensor.py` | Voice-triggered code changes |
| `backend/core/ouroboros/governance/intake/sensors/capability_gap_sensor.py` | Shannon entropy gap detection |
| `backend/core/ouroboros/governance/intake/sensors/scheduled_sensor.py` | Cron-based scheduled triggers |
| `backend/core/ouroboros/governance/intent/test_watcher.py` | Pytest polling + stable failure detection (30s timeout) |
| `backend/core/ouroboros/governance/intent/signals.py` | IntentSignal dataclass |
| `backend/core/trinity_event_bus.py` | TrinityEventBus (unified pub-sub spine) |
| `backend/core/resilience/file_watch_guard.py` | FileWatchGuard (watchdog wrapper) |
| `tests/ouroboros_pytest_plugin.py` | pytest plugin -> .jarvis/test_results.json |
| `scripts/hooks/post-commit` | Git post-commit -> .jarvis/git_events.json |

### Battle Test

| File | Purpose |
|------|---------|
| `scripts/ouroboros_battle_test.py` | CLI entry point |
| `backend/core/ouroboros/battle_test/harness.py` | BattleTestHarness lifecycle (6-layer stack boot) |
| `backend/core/ouroboros/battle_test/live_dashboard.py` | LiveDashboard TUI (1,233 lines) — persistent Rich Live terminal interface with DashboardTransport, streaming code, colored diffs, 3-channel terminal muting |
| `backend/core/ouroboros/battle_test/cost_tracker.py` | CostTracker with budget_event |
| `backend/core/ouroboros/battle_test/idle_watchdog.py` | IdleWatchdog with idle_event |

### Multi-Repo

| File | Purpose |
|------|---------|
| `backend/core/ouroboros/governance/saga/saga_apply_strategy.py` | Multi-repo saga application + commit signature |
| `backend/core/ouroboros/governance/goal_memory_bridge.py` | GoalMemoryBridge + thought log |
| `backend/core/ouroboros/governance/strategic_direction.py` | StrategicDirectionService (Manifesto → prompt) |
| `backend/core/ouroboros/governance/saga/cross_repo_verifier.py` | Cross-repo patch verification |
| `backend/core/ouroboros/governance/multi_repo/registry.py` | RepoRegistry for 3 repos |

---

## Battle Test Breakthrough Log

Empirical milestones reached by the autonomous loop under the full 6-layer harness. Each entry is sourced from the corresponding session's `debug.log` — anecdote is not evidence, only the harness counts.

### 2026-04-11 — First sustained full-pipeline completion (session `bt-2026-04-11-154947`)

**Headline:** O+V autonomously generated, Iron-Gated, repaired, applied, verified, and post-mortemed its first operation end-to-end since the Apr 9–10 Iron Gate tightening. The same session shipped two further worker completions within an 11-minute idle budget.

**Operation that reached APPLY + VERIFY:**
- Op ID: `op-019d7d3e-7f4a-7391-ae2a-5cdca6a9f3c5-cau`
- Source sensor: `RuntimeHealthSensor` (critical urgency)
- Goal: "Python 3.9.6 is PAST end-of-life (EOL: 2025-10). No security patches. Upgrade required."
- Target: `requirements.txt`
- Classification: `trivial` complexity, `SAFE_AUTO` risk tier, `fast_path=True`
- Route: `IMMEDIATE` (critical_urgency:runtime_health) → 120s Claude-direct budget
- Total cost: $0.2503 across 2 generate calls
- Worker elapsed: 402.38s

**Phase-by-phase trace (from debug.log):**

```
08:52:51  CLASSIFY   trivial/SAFE_AUTO/auto_approve/fast_path
08:52:51  ROUTE      immediate (critical_urgency:runtime_health)
08:52:51  PLAN       skipped (trivial_op: 1 file, short description)
08:52:51  GENERATE   stream start, thinking=on, 16384 max_tokens, 12341 prompt chars
08:54:49  GENERATE   end_turn, 117.8s, 4756+9999 tokens, $0.1643, 1 candidate
08:54:49  IRON_GATE  dependency_file_integrity REJECTED attempt 1:
                     "anthropic -> anthropichttp" (hallucinated package rename)
08:54:49  RETRY      episodic failure injected, attempt 2/2 launched
08:55:59  GENERATE   end_turn, 70s, $0.0861 cumulative=$0.2503, 1 candidate
08:55:59  VALIDATE   hash 03a420e57acc advanced
08:56:04  GATE       entropy=healthy, trigger=False
08:56:20  APPLY      phase=APPLY target=requirements.txt
08:57:42  DECISION   applied / safe_auto_passed
08:57:42  INFRA      InfrastructureApplicator triggered Python dependency install
08:57:47  INFRA      SUCCESS (4.7s)
08:57:47  VERIFY     phase=verify
08:58:29  VERIFY     test failed (0/0) -> routed to L2 repair
08:59:33  L2         escaped VERIFY phase, ctx advanced to POSTMORTEM
08:59:33  COMPLETE   cost summary $0.2503/$0.7500 (2 calls), productivity stable=1
08:59:33  WORKER     bgop-3b7c9bfa3b11 completed in 402.38s
```

**Other worker completions in the same session:**
- `bgop-45a495e6509a` — docstring coverage on `graduation_orchestrator.py` (simple, NOTIFY_APPLY) — 364.91s
- `bgop-7f373cc4a35b` — docstring coverage on `graduation_orchestrator.py` second op — 11.21s (short-circuit)

**Captured-client race fix (the unblocker):**

The breakthrough was gated on a race condition that had silently broken every battle test of the preceding stretch. `ClaudeProvider.generate()` captured `client = self._ensure_client()` at the top, then closure-referenced that local inside the nested `_do_stream` / `_create_with_prefill_fallback` / `_legacy_create` / `_plan_create` coroutines. When a transport-level hard failure (httpx `RemoteProtocolError`, `ReadError`) unwrapped from an Anthropic `APIConnectionError` fired `_recycle_client()` mid-backoff, `self._client` was replaced but the closure-captured local still pointed at the now-closed client. The next retry through `_call_with_backoff` therefore raised:

```
APIConnectionError(cause=RuntimeError:Cannot send a request,
as the client has been closed.) elapsed=3ms
```

— zero bytes transmitted, zero chance of progress. This masked the hard-pool recycle entirely: recycle fired, retry fired, retry instantly failed on stale client, generation exhausted retries, worker completed with zero calls charged.

**The fix** re-acquires `self._client` on every attempt:

```python
async def _do_stream() -> None:
    nonlocal raw_content, input_tokens, output_tokens, _cached_input
    # Re-acquire the client on every attempt so retries after
    # _recycle_client() pick up the new generation instead of
    # the original closure-captured instance.
    _current_client = self._ensure_client()
    _stream_kwargs: Dict[str, Any] = { ... }
    async with _current_client.messages.stream(**_stream_kwargs) as stream:
        ...
```

Applied identically to `_create_with_prefill_fallback` (non-streaming path), `_legacy_create` (backward-compat tool loop), and `_plan_create` (PLAN phase). Validation criterion was deliberately narrow: **zero `client has been closed` occurrences across the full debug log of a 10-minute session**. Met.

**Iron Gate vindication:**

Attempt 1 of `op-019d7d3e` produced a real code pathology — the model mutated the existing `anthropic==X.Y.Z` pin into `anthropichttp==X.Y.Z`, a hallucinated package name that would have broken `pip install` and silently deleted the real dependency. The `dependency_file_integrity` Iron Gate caught it on first production exposure:

```
Iron Gate — dependency_file_integrity: 1 offender(s)
[anthropic -> anthropichttp] op=op-019d7d3e-
Generation attempt 1/2 failed: Dependency file rename/truncation suspected:
1 package(s) deleted and replaced with near-identical name(s).
These look like model hallucinations or typos, not legitimate upgrades.
```

The retry loop then injected the rejection reason as an episodic failure into the attempt-2 prompt, and the model produced a clean patch. This is exactly the Manifesto §6 Iron Discipline loop: gate catches pathology → structured feedback → model self-corrects → patch graduates to APPLY. No brute-force retry, no bypass, no silent drift.

**What this session did NOT yet validate:**

- **Iron Gate `exploration_insufficient` with `complexity=simple → threshold=1`** — The three simple ops that entered the pipeline (`op-019d7d40`, `op-019d7d43` both variants) all hit provider-level exhaustion (`all_providers_exhausted`) before reaching the gate. The threshold-scaling fix in `orchestrator.py:1908-1941` is deployed but unexercised. Next battle test must force a simple-complexity op to reach GENERATE cleanly.
- **Hard-pool recycle on ReadError causes** — No `ReadError` / `RemoteProtocolError` was observed in this session's log. The fix is believed correct on mechanism (the prior session `bf1vf9icr` validated the recycle firing with both cause classes via the split `exc_class_bare` / `exc_class_display` logger), but a clean in-session reproduction is still pending.
- **Full L2 repair on a real verification failure** — L2 engaged and escaped in this session, but the underlying cause was `0/0 tests` (no tests exist for `requirements.txt`), not a genuine failing test. L2's `generate → test → classify → revise` loop still needs an honest signal to prove itself against.

**Stacked preconditions that had to land first (historical context):**

1. `Claude-sonnet-4-6` stream endpoint prefill incompatibility — disabled `JARVIS_CLAUDE_JSON_PREFILL` by default, stream path now tolerates plain messages array
2. IMMEDIATE generation budget raised 60s → 120s to accommodate Venom tool rounds
3. Claude retry demotion IMMEDIATE → STANDARD added after upstream provider exhaustion
4. `CancelledError` correct re-raise (PEP 479 / asyncio contract) so outer `wait_for` races don't swallow diagnostics
5. `_CLAUDE_HARD_POOL_EXC_NAMES` check made cause-aware (unwrap `APIConnectionError` → `ReadError` / `RemoteProtocolError`) — the `exc_class_bare` vs `exc_class_display` split
6. Iron Gate `exploration_insufficient` threshold scaled by complexity (`simple=1`, else `2`, trivial bypassed)
7. LiveWorkSensor unblocked on stale `requirements.txt` (one-time `git stash`)
8. **This session's fix:** captured-client race across all four `ClaudeProvider` invocation paths

Only (8) was actually the unblocker. (1)–(7) were necessary but not sufficient — they ensured that when the loop finally ran, it ran on the right rails.

**Files changed for the breakthrough:**
- `backend/core/ouroboros/governance/providers.py` — captured-client re-acquire on every `_call_with_backoff` attempt (4 call sites)
- `backend/core/ouroboros/governance/orchestrator.py` — `_min_explore` scaled by `task_complexity` with `JARVIS_MIN_EXPLORATION_CALLS` override

**Session metadata:**
- Runtime: 678.77s (11 min 19 s)
- Stop reason: `idle_timeout` (expected — harness idle watchdog, not a crash)
- Cost: $0.4257 (entirely Claude; DW Tier 0 was primed but the one DW RT attempt hit a CancelledError during harness shutdown at 09:01:02)
- Provider errors observed: 1 `APITimeoutError(cause=ConnectTimeout:)` on op-019d7d43 during worker shutdown race — cosmetic, post-test, unrelated to the fix
- **Zero** `client has been closed` occurrences across 119KB of debug log — the fix held
- Known counter bug: `summary.json.stats.attempted=0` even though 3 workers completed and 1 op reached APPLY. The counter only increments on specific phase transitions; the debug log is the authoritative source. Filing follow-up to fix the counter semantics.

### 2026-04-12 — IMMEDIATE thinking cap + DurableJSONL sandbox fix (session `bt-2026-04-12-073546`)

**Headline:** First session with IMMEDIATE thinking cap and DurableJSONL sandbox fix. Validated 5 independent fixes. Pipeline reached VALIDATE but blocked by test runner empty JSON report (0/0 tests).

**Duration:** 7m12s, clean `budget_exhausted` shutdown at $0.505.

**Fixes validated:**
1. `fallback_concurrency=3` aligned with pool size — zero sem contention across 3 concurrent workers
2. Outer gate grace raised 5s → 15s
3. Async sensor scans (OpportunityMiner, TodoScanner, DocStaleness) via `run_in_executor`
4. IMMEDIATE route thinking disabled — first_token dropped from 94.5s to 961ms (98x improvement)
5. DurableJSONL routed through `sandbox_fallback()` with error suppression after first occurrence

**Furthest progression:** INTENT → GENERATE → IRON_GATE(ascii_auto_repaired) → VALIDATE (blocked by test runner 0/0)

**Key metrics:**
- 3 workers concurrent with zero sem contention
- 0 DurableJSONL error spam (previously flooded logs)
- 19 trigger-tagged TODO items detected by TodoScanner

**Remaining blocker (now resolved):** `resolve_affected_tests()` walked temp sandbox path instead of repo-relative path, resulting in 0/0 test results. Fixed in commit 22f297d — `original_paths` mapping + multi-strategy test discovery. This was the single gate between current state and full APPLY → VERIFY → COMPLETE progression.

## O+V Capability Assessment (2026-04-12)

### Current Pipeline Maturity

| Phase | Status | Notes |
|-------|--------|-------|
| CLASSIFY | Production | SemanticTriage with 35B model, <1ms routing |
| ROUTE | Production | UrgencyRouter: 5 routes (IMMEDIATE/STANDARD/COMPLEX/BACKGROUND/SPECULATIVE) |
| CONTEXT_EXPANSION | Production | Oracle 242K nodes, semantic index, strategic direction injection |
| PLAN | Production | PlanGenerator schema plan.1, model-reasoned implementation strategy |
| GENERATE | Production | 3-tier failback, Venom 16-tool loop, route-aware thinking, Iron Gate |
| VALIDATE | Fixed | Was Broken -- sandbox path bug resolved in commit 22f297d (original_paths mapping + multi-strategy discovery) |
| GATE | Production | Iron Gate: exploration-first + ASCII strictness |
| APPROVE | Production | 4-tier risk escalation (SAFE_AUTO/NOTIFY_APPLY/APPROVAL_REQUIRED/BLOCKED) |
| APPLY | Unblocked | Previously blocked by VALIDATE, now unblocked |
| VERIFY | Unblocked | Previously blocked by VALIDATE, now unblocked |
| COMPLETE | Unblocked | Previously blocked by VALIDATE, now unblocked |

### O+V vs Claude Code Comparison

**Advantages O+V has over CC (12):**

1. Proactive autonomous operation (self-initiating, no human prompt needed)
2. 16 autonomous sensors (TestFailure, OpportunityMiner, RuntimeHealth, etc.)
3. 4-tier risk escalation (SAFE_AUTO/NOTIFY_APPLY/APPROVAL_REQUIRED/BLOCKED)
4. Multi-provider failback (DW 397B -> Claude API -> J-Prime)
5. Route-aware extended thinking budgets
6. Iron Gate enforcement (exploration-first, ASCII strictness)
7. L2 self-repair (iterative FSM, 5 iterations, 120s timebox)
8. Signal coalescing and dependency DAG
9. Strategic direction injection (manifesto principles in every prompt)
10. Consciousness bridge (memory/prediction integration)
11. DreamEngine (idle GPU speculative analysis)
12. Auto-commit with O+V signature

**Gaps where CC leads (10):**

1. Interactive context gathering (CC asks clarifying questions naturally)
2. File-level diff preview before apply (CC shows unified diffs)
3. Undo/rollback UX (CC has git-integrated undo)
4. Streaming incremental output (CC streams tokens to terminal)
5. Session persistence and resume (CC resumes mid-task)
6. Multi-turn refinement (CC iterates based on feedback)
7. Test-first development (CC runs tests before and after)
8. Dependency analysis (CC traces imports and call graphs)
9. Concurrent file editing (CC edits multiple files atomically)
10. Plugin/extension ecosystem (CC has MCP servers, hooks, skills)

### Known Issues & Edge Cases

1. **all_providers_exhausted (30-50% of ops)** -- budget geometry mismatch: DW Tier 0 consumes 70s, leaving Claude with insufficient budget for tool rounds. Mitigation: IMMEDIATE route now skips DW; STANDARD route needs budget preemption for Tier 0.

2. **Signal-to-noise in sensor queue** -- TodoScanner/DocStaleness flooding the 16-slot queue within minutes. IMMEDIATE signals can sit behind BACKGROUND opportunities. Need: separate priority-tier queues or preemption.

3. **Session lessons not persisting across sessions** -- `_session_lessons` buffer (20 max) resets each run. Discovery from one session lost by next. Wire into UserPreferenceMemory (PROJECT type).

4. **Cost tracking accuracy** -- Tier 0 DW costs estimated, not metered. Per-op cost attribution relies on token counts from provider responses, but DW batch responses lack granular token breakdown.

5. **Venom tool loop context overflow on deep explorations** -- when tool loop exceeds 75% of context budget, auto-compaction fires but can discard critical early-round context. Preserves recent 6 chunks only.

6. **No incremental apply** -- full file replacement only. ChangeEngine writes complete file content; no line-level diff application. Large files risk unnecessary churn.

7. **No cross-session learning persistence** -- consciousness layer (MemoryEngine, ProphecyEngine) state resets per session. UserPreferenceMemory provides typed persistence but is not yet wired to session lessons or prediction history.

### Current Letter Grade: B+

| Dimension | Grade | Rationale |
|-----------|-------|-----------|
| Architecture | A | 11-phase pipeline, clean FSM, 3-tier providers, consciousness layer |
| Autonomy | A- | 16 sensors, proactive signal discovery, trigger-tags, event-driven |
| Reliability | C+ | 30-50% provider exhaustion rate still wastes budget and blocks ops |
| UX/Observability | B+ | SerpentFlow + LiveDashboard, CommProtocol, debug.log |
| Testing | B | 81 governance tests, but VALIDATE was broken until commit 22f297d |
| Cost Efficiency | B- | DW Tier 0 works but exhaustion wastes budget on failed cascades |
| Safety | A- | Iron Gate (exploration-first + ASCII), 4-tier risk, protected branches |
| Documentation | B+ | CLAUDE.md comprehensive, battle test logs with phase traces |
| **Overall** | **B+** | Architecturally A-tier, execution improving, test runner fix unblocks pipeline |

### Path from B+ to A-

1. Fix test runner (done -- commit 22f297d)
2. Reduce provider exhaustion rate below 10% (budget geometry + preemption)
3. Add incremental diff apply (line-level patches instead of full file replacement)
4. Persist session lessons across sessions (wire to UserPreferenceMemory)
5. Add rate-limited sensor priority (separate IMMEDIATE/BACKGROUND queues)

---

## Claude Mythos Preview Cross-Reference (2026-04-12)

For a **line-by-line translation** of the Claude Mythos Preview System Card (Anthropic, April 2026) into concrete O+V additions, see:

**[`docs/architecture/CLAUDE_MYTHOS_OV_INTEGRATION.md`](./CLAUDE_MYTHOS_OV_INTEGRATION.md)**

That document contains:

- **10 highest-leverage findings** from the Mythos card mapped to specific O+V mechanisms (destructive-action replay harness, six-dimension code behavior rubric, scratchpad-clean reasoning caveats, cover-up patterns, impossible-task hacking, extended-thinking prompt-injection defense, seasoned-guide risk model, evaluation awareness, training reward-hacking catalog, closure-pressure drift)
- **Section-by-section deep dive** of Mythos §§2.3, 4.1, 4.2.2, 4.2.3, 4.3.1, 4.3.2, 4.3.3, 4.4, 4.5.3, 4.5.4, 4.5.5, 7.4, 8.3 with direct quotes and O+V implications
- **O+V vs Claude Code capability matrix** (22 dimensions)
- **Prioritized feature-gap list** with leverage/cost ratings across 4 priority bands
- **Edge cases specific to proactive-autonomous operation** (what becomes worse when the human leaves the loop)
- **UX design feedback** (what to keep, what's missing vs CC)
- **11 new Iron Gate / VALIDATE / protected-path classes** derived from documented Mythos failure modes
- **Letter-grade breakdown** across 10 dimensions, with B+ rationale and specific A- blockers
- **Sprint 1/2 roadmap** to move the grade from B+ → A- → A
- **27-item implementation checklist** (PR-sized items, ordered)
- **Glossary** of Mythos/O+V terminology
- **Cross-references** to internal docs and source files

**Source PDF:** `/Users/djrussell23/Documents/PDF Books/Trinity Ecosystem/Claude Mythos Preview System Card (3).pdf` (245 pages, published April 7, 2026 under RSP v3.0).

The single most important finding from that document, translated for O+V:

> The most severe incidents in Mythos came from **"reckless excessive measures when attempting to complete a difficult user-specified task"** (§4.1.1) -- not misaligned goals, not hostile intent, just task-completion drive overriding safety checks. That is exactly O+V's failure surface. The defenses are deterministic (gates, replay harnesses, protected paths, outcome-based monitoring), not introspective, because interpretability work (§4.5.3) shows scratchpad reasoning can look clean while concealment features fire in the model's internals.

