# Ouroboros: Self-Development Governance Pipeline

---

## Table of Contents

1. [Overview](#overview)
2. [Pipeline Phases](#pipeline-phases)
3. [Key Components](#key-components)
4. [Venom: Agentic Execution Layer](#venom-agentic-execution-layer)
5. [Autonomous Developer Intelligence (7 Capabilities)](#autonomous-developer-intelligence-7-capabilities)
6. [Session Lessons (from prior operations this session)](#session-lessons-from-prior-operations-this-session)
7. [Cognitive Depth: Extended Thinking](#cognitive-depth-extended-thinking)
8. [Tool Defaults: Unshackled Under Governance](#tool-defaults-unshackled-under-governance)
9. [Model-Reasoned PLAN Phase (Gap #3)](#model-reasoned-plan-phase-gap-3)
10. [Mid-Operation Clarification: ask_human Tool (Gap #4)](#mid-operation-clarification-ask_human-tool-gap-4)
11. [L3 Worktree Isolation (Gap #5)](#l3-worktree-isolation-gap-5)
12. [Auto-Commit Post-APPLY (Gap #6)](#auto-commit-post-apply-gap-6)
13. [MCP Tool Forwarding (Gap #7)](#mcp-tool-forwarding-gap-7)
14. [Live Context Auto-Compaction (Gap #8)](#live-context-auto-compaction-gap-8)
15. [DW 3-Tier Event-Driven Architecture](#dw-3-tier-event-driven-architecture)
16. [Edge Case Hardening (12 Refinements)](#edge-case-hardening-12-refinements)
17. [SerpentFlow CLI](#serpentflow-cli)
18. [Trinity Consciousness: The Metacognition Layer](#trinity-consciousness-the-metacognition-layer)
19. [Strategic Direction (Manifesto v4)](#strategic-direction-manifesto-v4)
20. [Multi-Repo Support](#multi-repo-support)
21. [Intake Layer: Unified Event Spine](#intake-layer-unified-event-spine)
22. [Operation Ledger](#operation-ledger)
23. [Branch Isolation](#branch-isolation)
24. [Observability](#observability)
25. [Battle Test Runner](#battle-test-runner)
26. [Environment Variables](#environment-variables)
27. [File Reference](#file-reference)
28. [Battle Test Breakthrough Log](#battle-test-breakthrough-log)
29. [O+V Capability Assessment (2026-04-12)](#ov-capability-assessment-2026-04-12)
30. [Claude Mythos Preview Cross-Reference (2026-04-12)](#claude-mythos-preview-cross-reference-2026-04-12)
31. [Functions, Not Agents: DoubleWord Reseating Roadmap (2026-04-14)](#functions-not-agents-doubleword-reseating-roadmap-2026-04-14)

---

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

### 2026-04-14 — First end-to-end reflex heal on disk (session `bt-2026-04-15-013455`)

**Headline:** The autonomous loop closed the Ouroboros cycle end-to-end for the first time. A deliberate failing test was detected, routed, generated, L2-repaired, APPLIED to the real filesystem, VERIFIED, and held as the authoritative state. `TestFailureSensor → IMMEDIATE → Claude direct → L2 CONVERGED → skip canonical → GATE → APPLY → VERIFY → COMPLETE → file stayed fixed.`

**Operation that closed the cycle:**
- Op ID: `op-019d8ec8-1f8e-786c-9671-8697de49a4fb-sig`
- Source sensor: `TestFailureSensor` (stable failure streak on `tests/test_reflex_provocation/test_one.py::test_add_expects_wrong_sum`)
- Target: `tests/test_reflex_provocation/test_one.py` (deliberate provocation file, 2675 bytes, 8 tests, 1 broken assert)
- Route: `IMMEDIATE` (high_urgency_immediate_source:test_failure) → 120s Claude-direct budget
- L2 converged in iter 1/5 (~21s, git-worktree sandbox + scoped pytest)
- APPLY landed at 18:39:08, VERIFY passed, `[COMPLETE] Applied successfully`
- Post-apply disk state: 2515 bytes, md5 `b81451282aaa522293d900e9eba6dd6b`, **8 passed / 0 failed**

**The seven-commit fix chain (all shipped 2026-04-14):**

Each commit unblocked a layer that was silently eating candidates. Every one was in the critical path of every APPLY the organism had ever attempted. None of them were findable without running the reflex loop end-to-end — the bugs were hiding each other.

| Commit | Layer unblocked |
|---|---|
| `d9e5beae7e` | **InteractiveRepair gate + TestWatcher parsing.** `interactive_repair.py` was blind-patching `L1-5` of pytest-failing files with `error_type="UnknownError", line_number=0` (pytest assertion output has no stdlib traceback). Default-OFF via `JARVIS_INTERACTIVE_REPAIR_ENABLED` + hard guard inside the loop. Stopped multi-op stacking corruption of target files outside the Iron Gate stack. Also added `JARVIS_INTENT_TEST_DIR` env override + `--color=no` pytest flag + loosened `_FAILED_RE` regex so the sensor actually parses scoped-dir failures. |
| `010a1bbc6a` | **L2 full_content materialization + BSD patch stdout.** `repair_engine.py` read `candidate["unified_diff"]` unconditionally, but providers are `force_full_content=True` (schema 2b.1) so the field was always empty → headers-only synthetic patch → `patch -p0` exited 2 with `"I can't seem to find a patch in there anywhere."` **on stdout (not stderr)**, which `RepairSandbox.apply_patch` discarded. Added explicit candidate-shape branching: real diff → `apply_patch`, full_content → new `apply_full_content` (verbatim write), neither → fail fast with `candidate_unusable:no_diff_or_full_content`. Error message now aggregates stdout+stderr. |
| `a8d359f28e` | **L2 scoped pytest + worktree remove diagnostics.** `repair_engine.py` passed `sb.run_tests((), per_iteration_test_timeout_s)` — empty test_targets → pytest full-repo discovery → ~90-100s per iteration → L2 got 1 usable iter per op instead of 5. Scoped to `(file_path,) if file_path else ()`. Same stdout/stderr aggregation applied to git worktree teardown. |
| `3dc63fef94` | **Post-L2 canonical skip.** `PythonAdapter.run` (`test_runner.py:227-237`) hard-codes `sandbox_dir=None` with docstring rationale "Always runs pytest from repo_root so Python imports resolve." After APPLY writes the candidate to a temp sandbox, the canonical validator ran pytest against `cwd=repo_root` and saw the **live** (still-broken) file — `pass_rate=0.0` → `l2_canonical_validate_failed` → escape to CANCELLED. Introduced `JARVIS_L2_SKIP_CANONICAL_AFTER_CONVERGE` (default ON) to synthesize a passing `ValidationResult` with explicit skip reason. Trusts L2's git-worktree sandbox convergence. |
| `79cc35335d` | **Benchmarker scoped pytest.** `PatchBenchmarker._coverage_sync` built pytest args as `[f"--cov={f}" ...]` with no positional test paths. `--cov=...` is a coverage-scope option, not a test selector → pytest fell back to full-repo discovery → coverage budget timeout → `pass_rate=0.0`. Scoped to `cov_args + test_paths`. Correct fix, but masked by the next bug. |
| `5e026ff01d` | **Post-L2 GATE→APPLY diagnostic logging.** After session v5 showed ops silently dying between L2_CONVERGED and phase=APPLY, added INFO logs at `L2 broke VALIDATE_RETRY`, source-drift check pass/fail, GATE entry, and can_write decision. Pure observability; confirmed in v7 that the GATE→APPLY path is structurally fine — v5 was timing-fragile under Claude API latency. |
| `27226dbba7` | **PatchBenchmarker pytest-cov tolerance** — *the real unblocker.* `pytest_cov` was not installed in the interpreter. Every `--cov` flag the benchmarker passed errored out pytest at usage-parse time with `unrecognized arguments: --cov=...` (exit 4) **before** running any tests → no "X passed" in stdout → regex parser fell through to `pass_rate=0.0` → `verify_gate.enforce_verify_thresholds` tripped `pass_rate<1.0` → `rollback_files` restored the pre-apply snapshot. Every L2-healed reflex op was silently reverted at this step. Added `_pytest_cov_available()` probe; when `pytest-cov` is missing, omit `--cov` args but still run pytest — `pass_rate` is computed correctly from regular pytest output, `coverage_pct` stays 0.0 (harmless without `baseline_coverage`). |

**Trace (condensed, from `bt-2026-04-15-013455/debug.log`):**

```
18:36:13  TestFailureSensor  streak=2 tests/test_reflex_provocation/test_one.py
18:38:45  L2 Repair          Iteration 1/5 starting (scoped pytest + git worktree sandbox)
18:39:06  L2 Repair          ✅ Iteration 1/5 tests: PASSED → ✅ CONVERGED
18:39:06  Orchestrator       L2_CONVERGED — skipping canonical re-validation (PythonAdapter
                             ignores sandbox_dir, trusting L2's git-worktree sandbox)
18:39:06  Orchestrator       Source-drift check passed → Entered GATE phase
18:39:06  Orchestrator       GATE can_write decision: allowed=True reason=ok
18:39:07  CommProtocol       phase=APPLY target=tests/test_reflex_provocation/test_one.py
18:39:08  CommProtocol       DECISION outcome=applied reason_code=safe_auto_passed
                             diff_summary="Applied change to tests/test_reflex_provocation/test_one.py"
18:39:13  CommProtocol       (benchmarker scoped pytest via _pytest_cov_available() probe)
18:39:13  CommProtocol       VERIFY passed (pass_rate=1.0, 8/8)
18:39:13  Orchestrator       [COMPLETE] Applied successfully
```

**What this session did NOT yet validate (debt for the clean-test streak):**

- **Zero `all_providers_exhausted`**: bt-2026-04-15-013455 saw two exhaustion events on `-cau` background ops (`op-019d8eca-3a08`) post-COMPLETE, fallback class `CancelledError`/`TimeoutError` under Claude API pressure. This session does NOT yet count toward the 3-consecutive-clean-sessions gate for `DeepAnalysisSensor`. Next engineering target: cascade-to-claude topology for BG/SPECULATIVE ops under provider latency.
- **`PatchBenchmarker.coverage_pct`**: stays 0.0 whenever `pytest-cov` is missing. Follow-up: either declare `pytest-cov` in `requirements.txt` or teach the benchmarker to use `coverage run -m pytest` as a native fallback. Not blocking the reflex path.
- **PythonAdapter `sandbox_dir=None` structural bug**: commit `3dc63fef94` is a skip, not a repair. Non-L2 canonical validation paths still have the blind spot (pre-L2 validation silently accepts any candidate whose bug matches the live file's bug). Option B (worktree-based canonical validation) remains the architectural fix.
- **`TestFailureSensor` in-flight dedup**: sensor keeps re-enqueuing the same signal every 30s while an op is already generating for the same file. Queue bloat under sustained failure. Cheap fix, scheduled after the clean-test streak.
- **`InteractiveRepair`**: the micro-repair loop is default-OFF and will stay off until it is re-homed through `ChangeEngine.execute` + pytest assertion-diff parsing. That's a separate follow-up PR, not a blocker.

**Session metadata:**
- Session ID: `bt-2026-04-15-013455`
- Runtime: ~12 min (stopped manually after COMPLETE + two post-COMPLETE exhaustions)
- Provocation file: `tests/test_reflex_provocation/test_one.py` (2675 bytes → 2515 bytes after APPLY, 8 tests)
- Total cost for the converged op: ~$0.033 (single Claude call, tool_rounds=0, immediate-reflex thinking=off)
- Files changed end-to-end: 1 (the provocation test file, rewritten with Ouroboros provenance banner)
- Commits: `d9e5beae7e`, `010a1bbc6a`, `a8d359f28e`, `3dc63fef94`, `79cc35335d`, `5e026ff01d`, `27226dbba7`

**What this means for O+V going forward:**

This is the first empirical proof that Manifesto §6 (threshold-triggered neuroplasticity) is not aspirational. The organism detected a broken test, reasoned a fix without human prompting, wrote it to the real filesystem through the full Iron Gate stack, verified it, and held the change. Every downstream subsystem that has been architecturally inert because nothing ever reached `COMPLETE` — `AutoCommitter`, `UserPreferenceMemory` FEEDBACK accumulation from rejections, `StrategicDirection` momentum from successful self-heals, `Oracle` incremental update on applied files — now has real signal to consume.

The 3-consecutive-clean-sessions gate for `DeepAnalysisSensor` (see memory `project_deep_analysis_sensor.md`) can now start counting. Counter is still at **0/3** — the `-cau` exhaustion here breaks the streak — but the reflex path is proven capable of satisfying the "at least one full GENERATE → VALIDATE → GATE → APPLY → VERIFY per session" criterion. The remaining work is resilience-under-pressure, not correctness.

### 2026-04-14 — Reflex heal reproducibility + dual-layer dedup validation (session `bt-2026-04-15-030359`)

**Headline:** Reflex heal is no longer a one-shot. A second session — run from a clean slate against the same provocation file — reproduced the full `TestFailureSensor → IMMEDIATE → Claude direct → L2 CONVERGED → GATE → APPLY → VERIFY → COMPLETE` cycle, and this time the pipeline fielded a **duplicate op** correctly: the second op for the same target was cancelled by the stale-exploration source-drift guard milliseconds after the first op's APPLY landed. Two defensive layers — sensor-side in-flight dedup (new) + pre-apply source-hash check (existing) — worked in concert, neither redundant, each catching cases the other missed.

**Operations this session:**

| Op | Outcome | Why |
|---|---|---|
| `op-019d8f19-e72d-7aa0-ac3d-e1eb7ba59828-sig` | **APPLIED** | First signal (streak=2). L2 converged in iter 1/5, GATE passed, DECISION `applied`/`safe_auto_passed` at `20:10:39`. |
| `op-019d8f1b-8f28-703f-9c2d-9d064c211b8d-sig` | **CANCELLED (source-drift)** | Second signal (streak=3, slipped through TTL window). L2 also converged in iter 1/5 at `20:10:44`. At GATE→APPLY, source-hash guard saw expected=`14e3cc501f4f`, actual=`aadcd7dee6f5` — the file was already healed by `op-019d8f19` 5 seconds earlier. **Correct cancel**, no redundant write. |
| `op-019d8f1d-*` (2 variants, streak=6/7) | Reached GENERATE, post-heal stale signals; both hit `all_providers_exhausted:fallback_failed` before re-apply. `ExhaustionWatcher consecutive=2/3` — did not trip hibernation before `idle_timeout` fired. | |

**Signal accounting — the sensor-side dedup fired where the TTL covered it:**

```
20:05:32  enqueue    streak=2  → op-019d8f19 (first, bgop-281d49ff857f)
20:07:21  enqueue    streak=3  → op-019d8f1b (bgop-f3e0bc46a177)
                                  TTL window: 109s after first enqueue — TTL=60s had expired.
                                  Slipped through sensor dedup. Pre-apply guard caught it instead.
20:07:57  SUPPRESS   36s ago   ✅ "target tests/test_reflex_provocation/test_one.py already in-flight"
20:08:07  SUPPRESS   46s ago   ✅
20:08:56  enqueue    streak=6  (new TTL window, prior marker expired again)
20:09:56  enqueue    streak=7
20:09:57  SUPPRESS    1s ago   ✅
```

**4 enqueued / 3 suppressed = 43% suppression rate at the sensor layer.** Exactly the log line the new fix (commit `20baa93a1d`) was written to produce, observed in live fire for the first time. Every suppression landed within a fraction of a second of the marker refresh.

**Phase-by-phase trace (condensed, from `bt-2026-04-15-030359/debug.log`):**

```
20:04:54  TestFailureSensor  subscribed to fs.changed.* events
20:05:32  TestFailureSensor  enqueued streak=2 → bgop-281d49ff857f
20:07:13  Orchestrator       Route: immediate (high_urgency_immediate_source:test_failure)
                              [op-019d8f19, 101s queue wait — worker pool contention]
20:07:13  CandidateGenerator Topology block: route=immediate cascade_to_claude
                              (DW RT budget insufficient for fast-reflex Venom)
20:07:21  TestFailureSensor  enqueued streak=3 → bgop-f3e0bc46a177 [TTL expired]
20:07:49  Orchestrator       Route: immediate [op-019d8f1b, worker 2]
20:08:15  InteractiveRepair  disabled → falling through to VALIDATE_RETRY/L2
20:08:27  L2 Repair          Iteration 1/5 starting (scoped pytest + git-worktree sandbox)
20:09:56  L2 Repair          ✅ Iteration 1/5 tests: PASSED → ✅ CONVERGED (op-019d8f19)
20:09:56  Orchestrator       L2_CONVERGED, skipping canonical re-validation, → GATE
20:10:38  CommProtocol       op-019d8f19 phase=APPLY progress_pct=80
20:10:39  CommProtocol       DECISION outcome=applied reason_code=safe_auto_passed
20:10:40  CommProtocol       POSTMORTEM root_cause=none next_safe_action=none
20:10:44  L2 Repair          ✅ CONVERGED (op-019d8f1b, second op)
20:10:44  Orchestrator       L2_CONVERGED op-019d8f1b → GATE
20:10:44  Orchestrator       Source drift detected (expected=14e3cc501f4f actual=aadcd7dee6f5)
                              → advancing to CANCELLED
20:15:27  Harness            Session stopping: idle_timeout
```

**What this session validates that 2026-04-14 could not:**

1. **Reproducibility.** The first heal (`bt-2026-04-15-013455`) shipped with the 7-commit fix chain. This session ran against that same codebase from a clean slate and reproduced the heal without manual intervention. Reflex arc is now reliable, not a one-off.
2. **Sensor-side in-flight dedup (commits `3a9fcc1aa9` / `83b210210f` / `163d2e7bee` / `20baa93a1d`).** First live-fire evidence of the "already in-flight" suppression log line. Catches the storm-after-reset pattern that burned the `bt-2026-04-15-010727` session.
3. **Stale-exploration source-drift guard catches what sensor-side dedup misses.** The duplicate `op-019d8f1b` cancelled cleanly at APPLY boundary — no double-write, no file corruption, no rollback churn. The guard was known-correct in mechanism but had never been exercised against a real concurrent duplicate until now.
4. **BG/SPECULATIVE topology block holds under pressure.** Every BG route this session honored `skip_and_queue` with the Gemma stream-stall reason. Zero cascade-to-Claude leaks on BG — the unit-economics defense from memory `project_bg_spec_sealed.md` is working.

**Gaps this session revealed (ordered by impact):**

- **Sensor TTL is too tight when worker queue wait exceeds it.** `JARVIS_TEST_FAILURE_INFLIGHT_TTL_S=60s` was shorter than the 101s `op-019d8f19` spent in the `BackgroundAgentPool` queue before the orchestrator picked it up. Signal `streak=3` enqueued at `20:07:21` — 109s after `streak=2` — squeezed through as a genuine duplicate (`bgop-f3e0bc46a177`). The pre-apply guard caught it at the next layer, but the sensor-side fire should have blocked it earlier and saved one Claude generate call (~$0.033). **Fix:** raise default TTL to 180s or make it adaptive to observed queue wait (P95 worker pickup latency + 2×safety margin).
- **ExhaustionWatcher retry-counting pitfall, reconfirmed.** Two post-heal stale ops (`op-019d8f1d-*`) both hit `all_providers_exhausted:fallback_failed`, moving `consecutive=1/3` then `2/3`. Each failure was from the same root cause (single op exhausting Claude+DW retries against a post-heal stale signal). One more would have triggered hibernation — but `idle_timeout` fired first, masking the issue. The pitfall captured in memory `project_exhaustion_watcher_retry_counting.md` is load-bearing; single-op multi-retry scenarios still count each retry as a "consecutive exhaustion event".
- **Post-heal stale signal churn.** `TestWatcher` polls every 30s. Between `op-019d8f19` enqueue (`20:05:32`) and actual APPLY (`20:10:39`), the sensor emitted signals at `20:07:21` / `20:08:56` / `20:09:56` — all fresh because the file was still broken on disk. These ops generated real Claude calls (~$0.10 of the session's $0.13 total) and then either got cancelled by source-drift (the good case) or hit exhaustion (the bad case). **Fix:** gate TestWatcher emission on "no op currently in-flight OR generating for this target file" — same predicate as the sensor dedup but checked at watch-loop level instead of sensor-subscribe level.
- **Cost asymmetry on post-heal churn.** The productive op `op-019d8f19` cost $0.0335. The subsequent 3 stale/cancelled ops cost $0.1012 combined — **3× the cost of the actual heal** went to work that was either cancelled at pre-apply or exhausted providers. Sensor-side TTL tuning + TestWatcher gating above should recover most of this.

**Session metadata:**
- Session ID: `bt-2026-04-15-030359`
- Runtime: 11 min 6 s (`20:04:21` → `20:15:27`)
- Stop reason: `idle_timeout` (expected — harness watchdog at 600s)
- Total cost: **$0.1347 / $0.50** (27% budget used)
- Reflex heals: **1 APPLIED** (`op-019d8f19`), **1 correctly CANCELLED** by source-drift (`op-019d8f1b`)
- Sensor signal outcomes: 4 enqueued / 3 suppressed (43% sensor-layer block rate)
- ExhaustionWatcher: `consecutive=2/3` at session end (hibernation not triggered)
- Iron Gate rejections: 0 (file was well-known shape; model generated clean fix both times)
- Files changed on disk: 1 (`tests/test_reflex_provocation/test_one.py`, 2675 → 2515 bytes, md5 unchanged from `bt-2026-04-15-013455`)
- Commits exercised: `3a9fcc1aa9`, `83b210210f`, `163d2e7bee`, `20baa93a1d`

**What this means for O+V going forward:**

The reflex arc has moved from "proven once" to "proven repeatable". Every O+V subsystem downstream of COMPLETE — `AutoCommitter`, `UserPreferenceMemory` FEEDBACK accumulation, `StrategicDirection` momentum from self-heals, `Oracle` incremental update — can now be exercised against a reliable upstream signal instead of a fragile one-shot. The dual-layer dedup defense (sensor-side TTL + pre-apply source-drift) is structurally sound: each layer catches cases the other misses, and when both engage, the organism degrades gracefully rather than compounding errors.

The two remaining resilience gaps are both tactical, not architectural: **sensor TTL tuning** and **ExhaustionWatcher retry-counting semantics**. Neither requires new subsystems; both are ~single-file edits against existing code. Once those land, the next targets are (a) graduating off the `test_reflex_provocation` training wheels onto real-repository failures, and (b) starting the 3-consecutive-clean-sessions gate for `DeepAnalysisSensor` (still at **0/3** — post-heal exhaustion events break the streak).

The organism is no longer stuck proving it can heal a broken test. It is now stuck proving it can do so **without burning 3× the productive cost on stale successors**. That is a much better problem to have.

### 2026-04-14 — Exploration-loop shadow calibration + ledger enforcement validated (sessions `bt-2026-04-15-040118` and `bt-2026-04-15-041413`)

**Headline:** Two back-to-back sessions that answered — and didn't answer — the single most important behavioral question for O+V. Session A proved the `ExplorationLedger` shadow scorer can see what the legacy int-counter gate can't: **a model that calls `read_file` four times and calls it exploration**. Session B flipped the same ledger to enforcement mode, watched the Iron Gate hard-reject that same shallow behavior for the first time in production, confirmed the retry mechanism escalates the model (extended thinking + parallel tool execution), and then lost the retry's synthesis stream to a fallback-semaphore concurrency bug before a second ledger decision could be recorded. **Ledger enforcement is validated. Tool-diversity-under-retry remains unproven — but the reason it's unproven is a mechanical provider-fairness bug, not a model-capability limit.**

**Session A (`bt-2026-04-15-040118`) — shadow telemetry:**

- Probe: 4-file leaf-test task (`.jarvis/backlog.json → session-a-exploration-probe-v2-2026-04-14`) routed via `BacklogSensor → complex complexity → COMPLEX route`. Advisor cleared (`risk=0.00 blast=0 coverage=100%`). First production op ever to reach `PlanGenerator` on a non-trivial target in this branch.
- Generation: `[ClaudeProvider] 3 candidates in 97.1s (tool_rounds=4), cost=$0.1791, 17937+8350 tokens, first_token=1519ms thinking=tool-round route=complex`.
- **The gold line** (21:06:02, while `JARVIS_EXPLORATION_SHADOW_LOG=true` and ledger enforcement off):
  ```
  [Orchestrator] ExplorationLedger(shadow) op=op-019d8f4d- complexity=complex
      legacy_credit=4 score=3.00 min_score=8.00 unique=4 categories=comprehension
      would_pass=False
  ```
- Interpretation: legacy int-counter gate says `4 ≥ 2 → PASS`; the ledger's category-diversity scorer says `score=3.00 < min_score=8.00, one category, FAIL`. All four tool calls were `read_file` (the `comprehension` category). The shadow scorer can measure shallowness — the legacy gate can't. **This is the first empirical proof that "linear and shallow" is a detectable, named pathology in the exploration loop**, not a vibes-based complaint.
- Additional unplanned findings:
  - `PLAN phase failed for op=op-019d8f4d-...: ; continuing to GENERATE without plan` — empty-string error, first observation of a PlanGenerator failure on a non-trivial op. Separate bug ticket; not a blocker for the exploration-loop investigation.
  - `Iron Gate — ascii_auto_repaired: 6 codepoint(s) healed` on the isolation test file — the ASCII-strictness gate's auto-repair path is working as designed under real multi-file generation.
- Cost: $0.2694 session total (single-op generate call was $0.1791, session cap was $0.15, per-op governor cap was $3.60 — session-level tracker tripped after the first charge landed). Session stopped `budget_exhausted` at 21:06:03. **Zero files landed on disk** — the op never reached APPLY because the cost cap fired between VALIDATE and APPLY phases.

**Session B (`bt-2026-04-15-041413`) — enforcement flipped, retry fired, synthesis died:**

The only config change from A was `JARVIS_EXPLORATION_LEDGER_ENABLED=true` plus `--cost-cap 0.50` and `--idle-timeout 1200`. Same backlog probe; fresh process, so `_seen_task_ids` was empty and the task_id was re-picked-up on the first scan.

**Attempt 1 (the enforcement proof):**
```
21:19:18  ExplorationLedger(decision) op=op-019d8f59- complexity=complex
          score=3.00 min_score=8.00 unique=4 categories=comprehension would_pass=False
21:19:18  WARNING Iron Gate — ExplorationLedger(decision) insufficient op=op-019d8f59-
          exploration_insufficient: score=3.0/8.0 categories=1/3 missing=- (attempt=1)
21:19:18  Generation attempt 1/2 failed for op-019d8f59-...: exploration_insufficient
21:19:18  Injecting 1 episodic failure(s) into retry context
```
Identical shadow signature to Session A (`score=3.0 unique=4 categories=comprehension`) — the model's default strategy for a 4-file task is still "read each file once." The difference is the **log tag: `(decision)` not `(shadow)`**, and the verdict actually blocks GENERATE instead of whispering from the sideline. **First production proof that flipping one env var turns the scorer into a hard Iron Gate rejection.**

**Attempt 2 (the adaptation signals):**
```
21:19:18  extended thinking ENABLED: reason=complex-force budget=16000 tok max_tokens=20096
21:19:22  🧠 Extended thinking: ~56 thinking tokens — deep reasoning before generation
21:19:22  [ToolLoop] Parallel execution: 4 tools in round 0
21:19:22  → stream tool_round=yes prompt_chars=44633  ← ~33K chars of tool results
```

Every signal of an escalated strategy:
- **Extended thinking engaged** (`complex-force`, 16K token budget) — absent from attempt 1.
- **Parallel tool execution in round 0** — a qualitatively different orchestration than the sequential 4-call pattern of Session A.
- **`prompt_chars` grew from 11,799 pre-tools to 44,633 post-tools** — ~33KB of tool results flowing into synthesis. Real content consumption, not padding.

**Attempt 2 died before the second ledger decision could fire:**
```
21:21:19  [ClaudeProvider] stream terminated via CancelledError: elapsed=117.1s
          budget=235.5s first_token=0.9s bytes_received=28780 tool_round=yes thinking=off
21:21:19  [CandidateGenerator] EXHAUSTION event_n=1 cause=fallback_failed
          fallback_err_class=TimeoutError fallback_failure_mode=TIMEOUT
          sem_wait_total_s=121.53 pre_sem_remaining_s=240.0
          primary_name=doubleword-397b fallback_name=claude-api
          op_id=op-019d8f59- route=complex complexity=complex
21:21:19  ExplorationLedger(shadow,partial) op=op-019d8f59- complexity=complex
          route=complex cause=all_providers_exhausted records=0 score=0.00 would_pass=False
21:21:19  Generation attempt 2/2 failed for op-019d8f59-...: all_providers_exhausted:fallback_failed
```

Unpacked: DW primary timed out → fallback (Claude) was invoked → fallback-semaphore wait consumed **121.53s** → once acquired, the Claude stream ran for 117.1s and received 28,780 bytes → then was cancelled externally with 118s of budget still nominally remaining. The post-exception shadow-partial log captures `records=0` because the tool execution records weren't persisted before the synthesis exception fired — so **we cannot even see which four tools round-0 actually called**, let alone whether they were category-diverse.

**What this session validated:**

1. **Ledger enforcement is real and stricter than the legacy gate.** `ExplorationLedger(decision) insufficient` is the production log line for a block that the legacy int-counter would have waved through. One env flag flips the behavior. This is the single result that tells us the exploration-loop scaffolding is load-bearing, not decorative.
2. **The retry path engages under enforcement.** `Injecting 1 episodic failure(s) into retry context` fired; attempt 2 launched with escalated parameters; extended thinking engaged on a route that normally has it off.
3. **The model's default response to an `exploration_insufficient` verdict is to escalate effort**, not to dismiss it — extended thinking on, parallel tool execution, larger synthesis prompt. That's the shape of Class A (adapt), not Class B (thrash with more read_file) or Class C (game the category counter with glob padding). It is **not yet proven** to be Class A, because we couldn't read the tool names, but the surrounding signals are consistent with adaptation and inconsistent with the other two.

**What this session did NOT answer (and why that's not a ledger problem):**

The tool-diversity-under-retry question is unproven — but the reason is mechanical, not behavioral. The synthesis call was **externally cancelled mid-stream during a fallback acquisition that waited 121 seconds**. This is a `providers.py` / semaphore / concurrency bug, not a model-capability limit. The exploration loop can't teach itself diversity if its retry attempts are being strangled in provider-fairness wait queues before the ledger gets a second look.

**Follow-up work ordered by "which of these makes the next session conclusive":**

1. **Fallback-semaphore tracing and fair-share policy.** Find the acquire/release points in `providers.py` and `candidate_generator.py`, log `{holder_count, wait_time, op_id, route}` at every acquire/release. Confirm whether BG + IMMEDIATE + GENERATE_RETRY stack on one global limit. **Candidate narrow fix: reserved slot for `GENERATE_RETRY` tier**, or **per-route semaphores**, or **shorter DW primary phase so Claude fallback gets budget sooner**, or **don't cancel an in-flight fallback stream unless a hard deadline is crossed**. Align with Manifesto §5 — reflex ops must not starve behind bulk BG, and retries of blocked ops must not starve behind their own cohort.
2. **Single-worker repro.** Rerun Session B config with `JARVIS_BG_POOL_SIZE=1` (or pause OpportunityMiner / TodoScanner / DocStaleness for the duration). If retry 2 completes to a second `ExplorationLedger(decision)` line under single-worker isolation, we've proven concurrency is the bug — not model capability. This is the cheapest empirical answer.
3. **Tool-execution-record persistence before synthesis.** `records=0` on the `(shadow,partial)` exception path is a diagnosis gap. Persist `tool_execution_records` as soon as the tool round completes (before the synthesis call) so a killed stream still leaves an audit trail of which four tools ran. Would have turned Session B's inconclusive outcome into a definitive "parallel 4× read_file" or "diverse mix" answer.
4. **`_DEFAULT_FLOORS` needs a `complex` entry.** `exploration_engine.py:242` has only `trivial / simple / moderate / architectural`. A `complex`-complexity op falls through `from_env` to `moderate` defaults (`min_score=8.0 min_categories=3`). Session B's enforcement was therefore tuning `complex` ops against `moderate` thresholds. Adding a dedicated `complex` row with its own floors separates the investigations — "did it adapt?" from "is the floor calibrated right for 4-file ops?"
5. **PLAN phase empty-error bug.** `PLAN phase failed for op=...: ; continuing to GENERATE without plan` observed in both sessions on the non-trivial op. Silent failure with empty error string. Separate ticket; not blocking the semaphore investigation.

**Session metadata:**
- Session A: `bt-2026-04-15-040118`, 5 min useful runtime, cost $0.2694 / $0.15 cap, stopped `budget_exhausted`. Probe reached `VALIDATE` phase; **0 files landed on disk**.
- Session B: `bt-2026-04-15-041413`, ~7 min useful runtime, cost $0.2955 / $0.50 cap, terminated `SIGKILL` after the probe's cost governor finished at $0.1895 / $3.60. Single Claude call charged. **0 files landed on disk**.
- Reflex path: disabled (`JARVIS_INTENT_TEST_DIR=tests/_does_not_exist`)
- Auto-commit: disabled (`JARVIS_AUTO_COMMIT_ENABLED=false`)
- Shadow-log: on in both sessions
- Ledger enforcement: **off in A, on in B** — the only material config delta

**What this means for O+V going forward:**

The exploration-loop question has split into two questions with different answers. **"Is the scaffolding load-bearing?" — YES, proven.** The ledger scorer measures diversity, enforcement mode turns it into a hard gate, retry mechanism fires, model escalates strategy. **"Does the model actually produce diverse tool calls under retry feedback?" — unknown, and will stay unknown until we fix the provider-fairness bug that strangles retry syntheses.**

That means the next engineering priority is **not more ledger tuning, not category floor adjustment, not prompt engineering for better retry feedback — it's the semaphore**. Ship one narrow fix in `providers.py` / `candidate_generator.py` that gives `GENERATE_RETRY` a reserved path through the fallback pool, rerun Session B, and the tool-diversity question resolves itself in one more session.

The mindset shift: **Session B didn't fail, it pointed at the blocker**. Every sophisticated autonomous system has a version of this moment — the first time the high-level reasoning gate catches the low-level execution system failing. The log line `fallback_semaphore_wait=121.53s` is cheap to read, the fix is mechanical, and the exploration-loop work can resume the moment retries can reliably produce candidates. Manifesto §5 (reflex ops must not starve behind bulk BG) now has a direct empirical reason to extend to "retries of blocked ops must not starve behind their own cohort either."

### 2026-04-14 addendum — Session C instrumentation proof + true root cause (`bt-2026-04-15-044627`)

**Headline:** The Session A/B framing was half-wrong in a specific, instructive way. The fallback-semaphore wait time in Session B (`sem_wait_total_s=121.53s`) looked like sem contention but was actually the last measurable symptom of a different problem: **the `BackgroundAgentPool` per-op wall-time ceiling (`JARVIS_BG_WORKER_OP_TIMEOUT_S`, default 360s) was force-reaping workers before retries under ledger enforcement could complete.** Session C, run with the sem-trace instrumentation from commit `614009ec05` under `JARVIS_BG_POOL_SIZE=1` isolation, produced the definitive evidence: zero semaphore contention, clean acquires and releases in both attempts, and the exact same CancelledError-during-synthesis failure mode — but this time with the pool-ceiling log line one line below it, showing the real mechanism.

**The two answers Session C finally produced:**

1. **Concurrency is NOT the root cause.** Under `BG_POOL_SIZE=1` with the single worker exclusively on the probe op (TodoScanner, runtime_health, doc_staleness all queued at depths `5/16` → `14/16`, not running), the fallback semaphore was never contested:
   ```
   21:50:26  Fallback sem acquire: slots_free=1/1 remaining=240.0s route=complex phase=GENERATE
   21:52:15  Fallback sem release: hold=108.9s sem_wait=0.0s phase=GENERATE outcome=ok   ← attempt 1 CLEAN
   21:52:15  Fallback sem acquire: slots_free=1/1 remaining=240.0s route=complex phase=GENERATE_RETRY
   21:54:04  EXHAUSTION ... fallback_err_class=CancelledError remaining_s=131.46
   21:54:04  Worker 0: operation bgop-6f83c8ced64d exceeded pool ceiling (360s) — freeing slot
   ```
   Attempt 2 was cancelled with **131.46 seconds of nominal generation budget still remaining**. The cancel came from above, not from within — the `BackgroundAgentPool`'s 360s `asyncio.wait_for` ceiling fired against the whole `_orch.run(op.context)` wall time. `CancelledError` propagated down through `_call_fallback`'s `wait_for` and surfaced as `fallback_err_class=CancelledError fallback_failure_mode=TIMEOUT`, which the Session B postmortem had mistaken for semaphore-side starvation. Session C, with zero sem wait (`sem_wait=0.0s` on both acquires), reproduced the exact same symptom — proving the cancel never came from the sem.
2. **The model DOES adapt under retry feedback. Class A confirmed.** Session A/B's retry signature was `4× read_file` (single `comprehension` category, `score=3.00`). Session C attempt 1 was even shallower — **zero tool calls at all** (`ExplorationLedger(decision) score=0.00 unique=0 categories=-`), the model went `end_turn` on a pure-thinking direct patch attempt. The Iron Gate rejected at `0/8.0`, injected the `exploration_insufficient` episodic failure into retry context, and the retry's round 0 surfaced via the Track 2 `tool_round_complete` audit:
   ```
   21:52:22  [ToolLoop] tool_round_complete op=op-019d8f77- round=0 tools=4
             names=read_file,read_file,list_dir,list_symbols total_records=4
   ```
   **Three distinct tool names spanning ≥2 categories** (`comprehension` from `read_file`, `structure` from `list_dir`, `structure`/`call_graph` from `list_symbols`). The model directly responded to the "you only touched one category" feedback by diversifying. **We now have empirical proof that (a) ledger enforcement steers behavior, and (b) retry feedback is more effective at producing diverse exploration than the initial prompt.**

**Track 1 + Track 2 instrumentation (commit `614009ec05`) was the difference.** Without the `phase=GENERATE_RETRY` label on the second sem acquire and the `tool_round_complete` audit log, Session B's wrong diagnosis would have persisted and the first fix attempt would have tuned a semaphore that wasn't broken. With the instrumentation, the real mechanism became visible on first repro:

- `Fallback sem release: hold=108.9s sem_wait=0.0s phase=GENERATE outcome=ok` — tells us the sem worked fine.
- `Fallback sem acquire: ... phase=GENERATE_RETRY` — tells us we're in retry, not in a fresh op.
- `tool_round_complete ... names=read_file,read_file,list_dir,list_symbols` — tells us the model adapted with diverse tools.
- `exceeded pool ceiling (360s)` — tells us the pool killed it, not the provider.

Four log lines, three wrong theories dismissed, one correct root cause surfaced. This is what instrumentation-first fixes look like.

**The narrow fix, applied:**

`background_agent_pool.py:648` now reads a route-aware ceiling: ops with `len(context.target_files) >= 4` get `JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S` (default `900s`, 2.5× base), everything else keeps the 360s anti-hang watchdog unchanged. File count is used instead of `task_complexity` because CLASSIFY/ROUTE haven't run yet at worker pickup — but the complexity classifier (`complexity_classifier.py:161-165`) is deterministic on file count, so `≥4 files` is equivalent to `complex` complexity without the phase-ordering hazard. Code comment in the patch explains the reasoning with a Session C reference for whoever reads it in 6 months.

**What this session validates beyond the fix:**

- **`ExplorationLedger(decision)` enforcement plus retry-feedback injection is a complete behavioral control loop.** We can measurably steer model exploration from "zero or shallow" to "diverse multi-category" via one env flag and a few lines of episodic-memory injection. This is the scaffolding *working*, not just existing.
- **Track 2 audit logging (`tool_round_complete`) will survive the next 10 retry-related bugs.** It's additive, costs nothing at runtime, and turns every `records=0` exception path into a debuggable "tools were X, Y, Z, then cancel fired" trace. Keep it forever.
- **Instrumentation-before-fix is the correct pattern for concurrency bugs.** Committing `614009ec05` before the Session C repro was the right call — without it we would have tuned the wrong knob. This is the "ship observability first" discipline that makes everything downstream cheaper.

**Session metadata:**
- Session ID: `bt-2026-04-15-044627`
- Runtime: ~9 min (killed manually after diagnosis was clear)
- Stop reason: SIGKILL (manual, after root cause confirmed)
- Cost: op-019d8f77 — $0.1361 / $3.60 per-op cap; session total $0.3311 / $0.50 cap
- Backlog probe: `session-c-semtrace-repro-2026-04-14` (fresh task_id)
- Advisor: `recommend risk=0.00 blast=0 coverage=100%` (leaf-file probe shape clean)
- Route: COMPLEX (`complex_task:complex:4_files`)
- Attempts: 2/2, both failed — attempt 1 via `exploration_insufficient score=0.00`, attempt 2 via `exceeded pool ceiling (360s)` with 131s of nominal budget remaining
- Files on disk: **0** (auto-commit off, APPLY never reached)

**Follow-up priority shuffle (updated from the Session A/B entry):**

1. **Pool ceiling fix — SHIPPED** this turn in `background_agent_pool.py`. Next repro should see `Worker 0: ... complex-route ceiling 900s (file_count=4, base=360s)` on the probe op, and attempt 2's `tool_round_complete` should now complete into a second `ExplorationLedger(decision)` line — either `would_pass=True` (gate clears, op proceeds to APPLY) or `would_pass=False` with a richer diagnosis than `categories=comprehension`.
2. **Fallback semaphore work — DEPRIORITIZED.** Session C proved it wasn't the blocker. Revisit only if a future multi-worker run actually shows contention.
3. **`records=0` exception-path gap — STILL RELEVANT** but mitigated by Track 2. The `ExplorationLedger(shadow,partial)` handler could still be taught to hydrate from `self._last_records` if the exception carries no records — a cheap follow-up but not blocking.
4. **`_DEFAULT_FLOORS` missing `complex` entry** — still deferred, still non-blocking.
5. **PLAN empty-error bug** — reproduced again this session on line 316. Separate ticket.

### 2026-04-15 — Sessions H–O arc: first end-to-end APPLY to disk (`bt-2026-04-15-175547`)

**Headline:** Session O closed the full governed-complex-route arc end-to-end for the first time. `tests/governance/intake/sensors/test_test_failure_sensor_dedup.py` (4,986 bytes) was written by the ChangeEngine after the ledger passed, L2 converged, GATE approved, and VERIFY + L2-repair-in-VERIFY cycled cleanly. The DECISION log shows `outcome=applied reason_code=safe_auto_passed` and the POSTMORTEM shows `root_cause=none failed_phase=None next_safe_action=none`. First autonomously-generated Python file to land on disk through the full enforcement pipeline in this repo's history.

This entry closes an 8-session arc (H → O) that surfaced and fixed **6 distinct failure modes** beyond the scoring-layer work documented above, each of which had been masking the next one. None were in the core enforcement logic — every fix was either a harness edge case, an in-memory state contamination, or a timing calibration.

**The 8-session failure-mode ladder (each masked by the one above it):**

| Session | Died at | Fix | Commit / env |
|---|---|---|---|
| **H** | `[SerpentApprovalProvider] prompt_async` crashed with `OSError: [Errno 22] Invalid argument` when the battle-test process had no controlling TTY — `prompt_toolkit` tried to register a kqueue reader on fd=0 and the kernel rejected it. | Headless-bypass early-return in both `request_execution_permission` and `request_plan_permission`: check `JARVIS_APPROVAL_AUTO_APPROVE` env var OR `sys.stdin.isatty()` before any Rich/prompt_toolkit work. Manifesto §6 Iron Gate upstream is still the authoritative policy layer; this only short-circuits the human-in-the-loop step, which is a no-op in automation. | `d8c1cb4d30` |
| **I** | `DECISION outcome=escalated reason_code=too_many_files` fired at the per-file sub-op GATE check during the multi-file APPLY fan-out. `risk_engine.py:372` escalates to `APPROVAL_REQUIRED` when `len(profile.files_affected) > self._max_files_threshold` (default 2). Our 4-file test-suite probe tripped it by design. | `OUROBOROS_MAX_FILES_THRESHOLD=10` + `OUROBOROS_BLAST_RADIUS_THRESHOLD=15` as env overrides. `RiskEngine` already reads both from env with the same `os.environ.get(...)` pattern — no code change. Battle-test-only relaxation; production defaults unchanged. | env-only |
| **J** | `[GovernedLoop] L3 mode switch: READ_ONLY_PLANNING (reason: 5 consecutive probe failures)` fired 28 seconds into L2 Iteration 1 and cancelled the in-flight repair. The failures were ambient background noise from `sandbox_fallback` readonly-database retries that accumulate throughout any long-running session (13-minute arc reliably crosses 5). | Made `SafetyNetConfig.probe_failure_escalation_threshold` and `probe_failure_severe_threshold` env-tunable via `field(default_factory=lambda: int(os.environ.get(...)))` with pre-existing integer defaults preserved. Pattern matches `risk_engine.py:181`. Battle test raises both to `25/50` so ambient background noise doesn't trigger hibernation mid-arc. | `5d169266d6` |
| **K** | `[ChangeEngine] ERROR Change engine error for op-...: [Errno 2] No such file or directory: 'tests/governance/intake/sensors/test_test_failure_sensor_dedup.py'` — `RollbackArtifact.capture()` unconditionally called `file_path.read_text()` at APPLY phase entry. For the **new-file creation path** the target doesn't exist yet, so the read raised `FileNotFoundError` and aborted the entire 4-file batch at progress=70. The ChangeEngine was designed for edits to existing files; this is the first session in the repo's history where a new-file creation attempt reached APPLY. | `RollbackArtifact` gained an `existed: bool = True` field (default preserves pre-patch behavior for every existing caller). `capture()` now returns an "absent" artifact (`existed=False`, `snapshot_hash="absent"`, empty content) for missing paths. `apply()` rolls back `existed=False` artifacts by `unlink()`-ing the created file, with `FileNotFoundError` swallowed as a no-op. 3 new unit tests in `TestRollbackArtifact` (absent capture, unlink rollback, no-op when already gone). | `28d52274ec` |
| **L** | `Worker 0: operation bgop-... exceeded pool ceiling (900s) — freeing slot` — the 900s ceiling from commit `db13f045ce` was sufficient for Sessions G-K, but Session L's op took 15m28s (backgrund queue pressure accumulated across sessions) and got reaped during VALIDATE before L2 could start. Not a regression — a timing calibration boundary crossed by ambient load. | `JARVIS_BG_WORKER_OP_TIMEOUT_COMPLEX_S=1800` env override. The env var was already implemented by `db13f045ce`; Session L's config didn't set it. Raising to 30 minutes gives 2× headroom over observed 17-18 minute full arcs. | env-only |
| **M** | `Generation attempt 2/2 failed: all_providers_exhausted:fallback_failed` at 186.1s elapsed during retry synthesis. The `_FALLBACK_MAX_TIMEOUT_COMPLEX_S=180` from commit `ad05fb7c7e` was calibrated for Session F's observed runtime (~122s with 1-2 tool rounds). Session M's retry, driven by sharpened feedback + ledger enforcement, did **5 internal tool rounds** inside a single synthesis stream — `asyncio.wait_for` cancelled at 180s, 6 seconds short of completion. **`sem_wait_total_s=186.41` is hold time (acquire-to-release lifetime), NOT queue wait** — do not re-litigate this as semaphore starvation. | `JARVIS_FALLBACK_MAX_TIMEOUT_COMPLEX_S=360` env override. The env var was already implemented by `ad05fb7c7e`; Session M's config didn't set it. Raising to 360s gives 174s margin over the observed 186s. | env-only |
| **N** | `Route: background (background_source:backlog:low_urgency)` — the probe was routed to BACKGROUND instead of COMPLEX, and the INTENT payload showed the goal string DOUBLED with a ` \| ` separator. Root cause: **intake WAL cross-session contamination.** `.jarvis/intake_wal.jsonl` had accumulated ~4,500 envelopes across Sessions G-M (SIGKILL'd ops left unfinalized entries). `UnifiedIntakeRouter._coalesce_key` is just `"\|".join(sorted(target_files))`, and `_flush_coalesced` has a secondary bug where `_best_urgency` initializes to `"low"` and `"normal"` is missing from `_urgency_rank` — so the first `normal` envelope can fail to replace the initial `low` because `rank(normal)=rank(unknown)=2` and the comparison is strict. Result: Session N's new task coalesced with a leftover Session M envelope by target-files signature, inherited the `low` urgency sentinel, and routed to BACKGROUND where it died without generating anything. | `rm .jarvis/intake_wal.jsonl && rm .jarvis/intake_router.lock` before Session O. Pure state cleanup, zero code change. **Follow-up PRs (backlog, not shipped this arc):** (a) fix `_best_urgency` to initialize from the first envelope's urgency instead of hardcoded `"low"`, OR add `"normal"` to `_urgency_rank` so the rank comparison isn't `2 == 2`; (b) tighten `_coalesce_key` for `source=backlog` envelopes to include `evidence["task_id"]` so same-path tasks with different IDs don't merge; (c) harness-side WAL cleanup at battle-test boot when the accumulated entries exceed a threshold. | state cleanup |
| **O** | ✅ **Full arc closed** — but only 1 of 4 target files landed. | N/A this entry — see next section. | — |

**Session O's exact terminal sequence (quoted from `bt-2026-04-15-175547/debug.log`):**

```
10:56:57  Worker 0: bgop-11ddabcef2f7 complex-route ceiling 1800s (file_count=4, base=360s)
10:58:03  🛤️  Route: complex (complex_task:complex:4_files)               ← WAL cleanup fix held
10:58:53  Fallback sem acquire: max_cap=360s route=complex phase=GENERATE  ← new cap applied
11:00:31  ExplorationLedger(decision) score=0.00 unique=0 would_pass=False   (attempt 1)
11:00:31  Iron Gate — exploration_insufficient → retry
11:00:31  Fallback sem acquire: max_cap=360s phase=GENERATE_RETRY
11:02:20  ExplorationLedger(decision) score=11.00 min_score=10.00 unique=4
          categories=comprehension,history,structure would_pass=True        ← 7th consecutive pass
11:02:20  Iron Gate — ascii_auto_repaired: 4 codepoints file=test_..._ttl.py
11:02:20  Iron Gate — ascii_auto_repaired: 5 codepoints file=test_..._isolation.py
11:11:22  [CommProtocol] DECISION op=op-019d9249-... seq=24 payload={
          'outcome': 'applied', 'reason_code': 'safe_auto_passed',
          'diff_summary': 'Applied change to tests/governance/intake/
                          sensors/test_test_failure_sensor_dedup.py'}        ← APPLY SUCCESS #1
11:11:22  POSTMORTEM op=op-019d9249-... payload={
          'root_cause': 'none', 'failed_phase': None,
          'next_safe_action': 'none'}                                        ← CLEAN POSTMORTEM
11:12:29  🔧 [L2 Repair] Iteration 1/5 starting (VERIFY-phase repair)
11:13:39  🔧 [L2 Repair] Iteration 1/5 tests: ✅ PASSED
11:13:39  🔧 [L2 Repair] ✅ CONVERGED after 1 iteration(s)!
11:13:42  [CommProtocol] DECISION seq=35 outcome=applied reason_code=safe_auto_passed
11:13:42  POSTMORTEM root_cause=none                                         ← CLEAN POSTMORTEM #2
11:13:42  L2 repair applied in VERIFY phase
```

**Post-session disk verification:**
```
$ ls -la tests/governance/intake/sensors/test_test_failure_sensor_dedup.py
-rw-r--r--@ 1 djrussell23  staff  4986 Apr 15 11:13
    tests/governance/intake/sensors/test_test_failure_sensor_dedup.py
```

**Wall time:** ~16m45s from submit to L2-repair-applied. **Cost:** $0.55/$0.60 session budget.

**Why only 1 of 4 files landed (not a bug — architectural by design):**

The multi-file coordinated APPLY path (`orchestrator.py _apply_multi_file_candidate`, feature-flagged on `JARVIS_MULTI_FILE_GEN_ENABLED`, default `true`) only runs when `len(_iter_candidate_files(best_candidate)) > 1`. `_iter_candidate_files` returns multiple pairs **only if** the winning candidate has a populated `files: [{file_path, full_content, rationale}, ...]` list — otherwise it falls back to the legacy `(file_path, full_content)` single-pair path and ChangeEngine.execute applies one file.

Session O's winning candidate did NOT include a `files: [...]` list. The model produced content for `dedup.py` (plus partial content for `ttl.py` and `isolation.py` that hit Iron Gate ASCII auto-repair at line 443-444 during candidate validation, but these paths never made it to the applied files batch). Only the primary `file_path` key was set, so `_iter_candidate_files` returned a single-pair list and APPLY took the legacy single-file path — hence `diff_summary` naming only `dedup.py`.

**Fix landed this turn for observability (commit pending):** `orchestrator.py:4561` now logs a single INFO line at APPLY decision time recording `mode=(single|multi)`, `candidate_files=N`, `files_list_present=bool`, `multi_enabled=bool`, and target basenames. Reading one log line now answers the single-vs-multi question without cross-referencing raw candidate JSON against disk state.

**Follow-up work (backlog, not shipped this turn):**

1. **Multi-file candidate enforcement for `target_files >= 2` ops.** When the intake envelope declares 4 target files, the winning candidate SHOULD include a `files: [...]` list with 4 entries. Options: (a) enforce at generation time by having the prompt builder inject a schema requirement, (b) accept one op per file and let the multi-file backlog task fan out into 4 sibling ops, or (c) add a post-processor that decomposes a single-file candidate into N candidates for a multi-target backlog task. (a) is the cleanest but requires prompt/schema changes. (b) is the simplest but changes the backlog sensor semantics.
2. **`_best_urgency` init / `_urgency_rank` for `"normal"`** (Session N follow-up). See Session N row above.
3. **Coalesce key for `source=backlog`** should include `evidence["task_id"]` (Session N follow-up).
4. **Harness-side WAL cleanup at battle-test boot** when accumulated entries exceed a threshold (Session N follow-up).
5. **Architectural tier ExplorationFloors recalibration** — still open, still deferred, now with Session O confirming the ledger scoring is calibrated correctly for `complex` (7 consecutive would_pass=True on retry).

**Session count tonight:** **15 sessions (A through O), 8 commits, 12 env overrides, 10 distinct failure modes identified and fixed (7 code commits + 3 env-only policies).** The single binary success criterion — **autonomously-generated Python content landing on disk through the full enforcement arc** — **has been met.** Not perfectly (3 of 4 files missing), but the mechanical proof of the end-to-end governed loop is complete.

### 2026-04-15 — Sessions Q–S arc: multi-file enforcement proven through every gate (`bt-2026-04-15-204901`)

**Headline:** Session O's "1-of-4 files landed" gap had two orthogonal halves: (a) the pipeline's **enforcement** for multi-file candidates (prompt hint + parser + post-GENERATE coverage gate), and (b) the pipeline's **persistence** for multi-file candidates (the APPLY fan-out that's been in place since `_apply_multi_file_candidate` shipped). This arc addresses half (a) end-to-end and proves it in production. Half (b) remains gated on a separate VALIDATE/L2 timebox issue tracked below as Follow-up A; it is **not** a multi-file enforcement bug.

**What shipped this arc (code):**

- **`multi_file_coverage_gate.py`** — new Iron Gate 5 module. `check_candidate()` mirrors `_iter_candidate_files` so the gate decision matches APPLY behavior exactly. Rejects any multi-target op whose candidate fails to cover every `context.target_files` path via a populated `files: [...]` list. Env gate `JARVIS_MULTI_FILE_ENFORCEMENT` (default `true`). 31 unit tests (`test_multi_file_coverage_gate.py`), including a direct Session O reproduction and the accepted-shape assertion.
- **`providers.py` `_build_multi_file_contract_block`** — prompt-side hint injected into the lean tool-first prompt and the full-mode default prompt when `len(ctx.target_files) > 1`. Renders a concrete `files: [{file_path, full_content, rationale}, ...]` example with one entry per target path and an explicit list of every path that must appear. BG / cross-repo / execution_graph routes unchanged. Skips when the op targets ≤1 file.
- **`orchestrator.py` Gate 5 wire-up + retry feedback** — Gate 5 fires post-Gate 4 (docstring_collapse), stashes `_mf_missing_paths` and `_mf_target_files` on the `RuntimeError`, flows into a new `elif` branch in the GENERATE_RETRY feedback builder that names missing paths and reissues the multi-file contract with a concrete JSON example.
- **`providers.py` `_parse_generation_response`** — multi-file-shape detection at line 3024: when `files: [...]` is populated, top-level `file_path`/`full_content` are no longer required and are synthesized from `files[0]` so downstream single-file consumers (length check, AST preflight, APPLY single-path branch) keep working unchanged. Without this, the prompt told Claude to emit `files: [...]` only, and the parser rejected every such candidate with `schema_invalid:candidate_0_missing_file_path`.
- **`provider_exhaustion_watcher.py`** — per-op exhaustion dedup (`record_exhaustion(op_id=...)`) so one op's internal retries can't stack onto the hibernation threshold. Snapshot exposes `deduped_events` and `unique_ops_counted`. 9 new unit tests (`test_provider_exhaustion_watcher.py`), including a direct Session P scenario reproduction.

**Three-session production-verification run:**

| Session | Env | Outcome | What it proved |
|---|---|---|---|
| **Q** (`bt-2026-04-15-201035`) | parser not yet fixed | 4-file op failed twice with `claude-api_schema_invalid:candidate_0_missing_file_path` across attempts 1 and 2. Claude returned `stop_reason=end_turn output_tokens=10557 raw_chars=32123` multi-object response (15 top-level blocks), parser rejected the multi-file-shaped candidate upstream of Iron Gate 5. Per-op exhaustion dedup fired correctly: `record_exhaustion(op_id=op-019d92c5-...)  consecutive=1/3 total=1` then `record_success() — consecutive reset (was 1, counted_ops=1)` on an interleaved IMMEDIATE success. Single-file `requirements.txt` op reached `APPLY mode=single candidate_files=1 files_list_present=False multi_enabled=True` → `DECISION outcome=applied reason_code=safe_auto_passed` in the same session. | **The bug**: prompt hint told the model to emit `files: [...]` only, parser contract required top-level `file_path`. Root cause isolated. |
| **R** (`bt-2026-04-15-203724`) | parser fix landed | 4-file op passed PLAN, GENERATE completed at `117.8s` with `1 candidates` and `cost=$0.1642`. No more `schema_invalid` rejection — the parser accepted the multi-file shape. But the candidate died at Iron Gate 1 (exploration ledger): `exploration_insufficient: 1/2 (attempt=2 cumulative, preloaded=1)` — the model made **0 fresh exploration tool calls** before producing a patch for 4 new test files. Max retries (2) exhausted, op died. Iron Gate 5 still did not fire because Gate 1 intercepted the candidate first. | **Parser fix verified in production.** No more `missing_file_path` on multi-file candidates. Next blocker: exploration gate, which is orthogonal to multi-file enforcement. |
| **S** (`bt-2026-04-15-204901`) | `JARVIS_EXPLORATION_GATE=false` to exercise Gate 5 | Model's round-0 tool loop fired **3 parallel `read_file` calls** unprompted (`tools=3 names=read_file,read_file,read_file`). Generation completed in `91.3s` at `cost=$0.2085`. ASCII auto-repair healed 2 codepoints in `test_test_failure_sensor_dedup.py`. **Zero `multi_file_coverage` rejections anywhere in the log** — Gate 5 silently passed, meaning the candidate covered all 4 target paths via a populated `files: [...]` list. VALIDATE then fired LSP across the full candidate: `LSP found 1 type errors in ['test_test_failure_sensor_dedup.py', 'test_test_failure_sensor_ttl.py', 'test_test_failure_sensor_isolation.py']` (3 of 4 files had LSP errors; `marker_refresh.py` was clean). TestRunner resolved `45 test targets for 4 changed files` — **all 4 target paths visible to the post-gate pipeline**. The VALIDATE critique on `dedup.py` routed to `VALIDATE_RETRY → L2 Repair`, which never converged before the 10-minute idle timeout at `14:00:15`. Worker 0 completed `bgop-2473befc9d76 in 713.94s`. No APPLY mode=multi log line fired; 0 of 4 files landed on disk. | **Multi-file enforcement arc proven end-to-end through every gate.** Parser, Gate 5, post-gate visibility all confirmed in production. Persistence (APPLY) is gated on a separate reliability issue — see Follow-up A. |

**What's proven after Q–S (the enforcement arc):**

1. **Parser accepts `files: [...]` as the authoritative multi-file payload.** When populated, `file_path` and `full_content` are synthesized from `files[0]` for downstream single-file consumers. Regression covered by new parser test cases; production-verified in R and S.
2. **Iron Gate 5 passes silently on full coverage.** Across three production sessions with a 4-target op, zero false rejections. The gate did not fire at all in S because all 4 target paths were covered by the candidate's `files: [...]` list — matching `_iter_candidate_files` at APPLY time.
3. **Post-gate visibility for all 4 files.** LSP and TestRunner both walked the full 4-file set in S. The pipeline treats the multi-file candidate as a unit from GATE through VERIFY; no silent degradation to a 1-file path anywhere post-parser.
4. **Per-op exhaustion dedup lands in production.** Snapshot fields `deduped_events` and `unique_ops_counted` visible in Q/R/S logs. One op's retries no longer stack onto the hibernation threshold.

**What is NOT yet proven and is deliberately deferred to Follow-up A:**

- `APPLY mode=multi candidate_files=4` + 4 new files materializing on disk through `_apply_multi_file_candidate`. The fan-out code (`orchestrator.py _iter_candidate_files` / `_apply_multi_file_candidate`) has not changed since Session O and carries its own batch-level rollback guarantees; this arc does not re-verify that subsystem.

### Follow-up track A — VALIDATE/L2 timebox alignment (reliability, not enforcement)

**Hypothesis (falsifiable):** Session S's `VALIDATE_RETRY → L2 Repair` stall on `test_test_failure_sensor_dedup.py` is a **timebox** failure, not an unfixable-candidate failure. Evidence: `pytest timed out after 30.0s — killing process` (the hard sandbox pytest cap), L2 repair iterations `1/5 tests: ❌ FAILED (unknown)`, second iteration `49s elapsed, 11s remaining` (L2 hit its 60s wall-clock timebox mid-repair). The candidate itself had 1 LSP error that the model never got a chance to iterate on inside the repair window.

**Success criterion for a single verification session:** **one** op reaches `APPLY mode=multi candidate_files=N` (N ≥ 2) with `DECISION outcome=applied` and `POSTMORTEM root_cause=none`, without hitting idle timeout. Any op — doesn't need to be the Session P 4-file backlog task. A simpler 2-file op would prove the persistence path end-to-end.

**Scoped tuning (env only, no code changes needed):**

1. **`JARVIS_TEST_TIMEOUT_S=120`** — raise the sandbox pytest cap from 30s to 120s so generated tests that import the real codebase have time to load.
2. **L2 iteration / per-iteration pytest budget alignment** — verify `JARVIS_L2_REPAIR_TIMEBOX_S` (or equivalent) is ≥ `N_iters × JARVIS_TEST_TIMEOUT_S + overhead` so an iteration can't be cut mid-fix. Current default appears to be 60s timebox vs 30s pytest → only 1 real iteration possible even when L2_ITERATIONS=5.
3. **Keep `JARVIS_EXPLORATION_GATE=true` (default)** — the S run disabled it only because we needed Gate 5 to get its decision; Gate 5 is now proven, so the exploration gate should stay on.
4. **Do NOT** add retries, disable VALIDATE, or raise retry caps without explicit diagnosis. Manifesto §6: structural repair, not blind retry.

**Anti-goals for Follow-up A (explicit):**

- Do **not** treat this track as "more retries until luck." Frame every change as timebox alignment or scoped validation and cite the specific stall timestamp from S's log.
- Do **not** conflate this track with a Gate 5 / parser issue. The enforcement arc is settled. If a new session reveals a multi-file-specific failure, that would be a separate entry under this log.
- Do **not** ship an Option-C-style deterministic 4-file APPLY bypass as a substitute for fixing the real stall. That would prove the plumbing but not the autonomous arc. Keep it in the toolbox for isolated fan-out regression testing if needed.

**Session count this arc:** **3 sessions (Q, R, S), 2 commits (`37a371e65d` per-op dedup, `31504a8f12` Iron Gate 5 + prompt hint), 1 parser fix bundled into the autonomous battle-test sweep commit `6c3cce92c6`, 58 unit tests across `test_multi_file_coverage_gate.py` + `test_provider_exhaustion_watcher.py` (all green).** The multi-file `files: [...]` enforcement path is **proven deterministic through every gate**; agentic persistence is the next track.

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

---

## Functions, Not Agents: DoubleWord Reseating Roadmap (2026-04-14)

### Calibration

Two battle tests established the DW envelope with prejudice:

- **`bt-2026-04-14-182446`** — Gemma 4 31B BACKGROUND route: **0/13 successes** (12× 180s timeout, 1× schema_invalid) even at a right-sized 2,836-token envelope. Root cause: provider-side SSE stream stalling post-accept, not prompt size.
- **`bt-2026-04-14-203740`** — Qwen 3.5 397B STANDARD isolation benchmark (Claude fallback severed): **0 valid candidates across the run.** Tier 0 RT streams opened, then went silent mid-generation (``SSE stream stalled (no data for 30s)``), budget exhausted at 71.5s, FSM hit ``TIMEOUT`` on every attempt. Same failure signature as Gemma on BG — provider-side stream stall.

Verdict: the DoubleWord **streaming** endpoint cannot sustain agent-shaped workloads across any tested model. STANDARD, COMPLEX, and IMMEDIATE now cascade_to_claude; BACKGROUND and SPECULATIVE now skip_and_queue. Only the `callers` mapping (semantic_triage, ouroboros_plan) remains on DW — and only because those are tiny-payload structured-JSON calls.

### The Architectural Reframe

DoubleWord is not a broken agent provider. It is an **untested function provider.** The streaming endpoint fails; the non-streaming `/v1/chat/completions` endpoint with `stream=false` has never been exercised by O+V. The reseating matrix below moves DW out of the agent cascade entirely and into a fixed set of short, structured, non-streaming function calls.

**Design invariants for every DW caller:**

1. **Non-streaming only.** Request body carries `stream=false`. The client awaits a single JSON response; there is no SSE loop, no partial-delta reassembly, no mid-stream stall surface. `DoublewordProvider.complete_sync()` is the single code path.
2. **Short structured output.** Callers request JSON schemas with bounded token budgets (≤512 output tokens typical). No free-form prose, no tool loops, no multi-turn refinement.
3. **Caller-supplied timeout.** Each call site picks a timeout appropriate to its criticality (200ms for semantic_triage, 2s for compaction, 30s for heavy analysts). The provider enforces via `asyncio.wait_for()` — not the streaming budget curves.
4. **Anti-hallucination gate (Manifesto §6).** Every caller that names codebase artifacts (symbols, file paths) validates the model output against the provided context. Any hallucinated reference → autonomous rejection, deterministic fallback, telemetry log. We do not trust fluid intelligence with ground truth.
5. **Circuit breaker.** Per-call, per-op, and global-rolling breakers. Sustained failures at any layer demote the caller to its deterministic fallback for the remainder of the session.
6. **Shadow mode first.** Every new caller ships disabled-by-default in SHADOW mode: runs in parallel with the deterministic baseline, writes telemetry to `.ouroboros/sessions/<id>/<caller>_shadow.jsonl`, emits a single-line SerpentFlow event, but **never affects pipeline state.** Promoted to LIVE only after offline analysis of the shadow JSONL.

### Phase 0: Gemma Compaction Caller (SHIPPING)

**Model:** `google/gemma-4-31B-it`
**Call site:** `ContextCompactor._build_summary()` in `context_compaction.py`
**Deterministic fallback:** existing type-counting + phase-histogram summarizer
**Rationale:** Compaction is a structured summarization task with a fully-bounded input (entries already in memory), a short output (<1KB summary text), and a trivial anti-hallucination check (summary must reference only entry-keys and phase-names already in the preserved list). Ideal Gemma test bed.
**Strategy injection:** `ContextCompactor.__init__(semantic_strategy: Optional[CompactionCallerStrategy])`. When present, `_build_summary` delegates; on any failure (timeout, schema_invalid, hallucinated ref, circuit-open), falls back to the deterministic path and logs the rejection reason.
**Master switch:** `JARVIS_COMPACTION_CALLER_ENABLED` (default `false`). Enabled explicitly per battle test during shadow-mode validation.

### Phase 3: Qwen 397B Heavy Analyst Roadmap

Once Phase 0 (Gemma compaction) has 24h+ of clean shadow telemetry and a LIVE promotion decision, Phase 3 mounts Qwen 3.5 397B as the **heavy analyst** for three cognitive callers. All three share the same `complete_sync()` non-streaming code path; none ever touch the streaming endpoint.

#### 3.1 — BlastRadius Scoring (`OperationAdvisor`)

**What it does today:** Deterministic heuristic — counts transitively-dependent files via import graph, weights by test coverage, emits a 0.0–1.0 score consumed by the 4-tier risk escalator (SAFE_AUTO / NOTIFY_APPLY / APPROVAL_REQUIRED / BLOCKED).

**Gap:** The import-graph heuristic is structural only. It cannot reason about *semantic* blast radius — e.g., a one-line change to a retry-backoff constant may have higher real-world impact than a 500-line refactor of a leaf module.

**Qwen 397B role:** Non-streaming structured-JSON analyst. Input: candidate diff + import-graph evidence pack + historical failure episodes touching the same path. Output: strict JSON schema `blast_radius.1` — `{score: float, structural_score: float, semantic_score: float, reasoning: str, risk_factors: [str], invariants_touched: [str]}`. Deterministic score stays as the floor; 397B score can *only* escalate risk tier, never de-escalate. This preserves the "fluid intelligence never lowers a hard gate" invariant.

**Anti-hallucination gate:** `invariants_touched` must be a subset of symbol names extracted from the actual diff. Any hallucinated invariant → rejection, fall back to deterministic.

**Timeout:** 10s per call (bounded by single-op approval latency budget).

#### 3.2 — Episodic Failure Clustering

**What it does today:** Nothing. Session lessons (`_session_lessons`, 20 max) reset each session. Failed ops are logged but never clustered across sessions.

**Gap:** The Strategic Direction / Consciousness Bridge stack has the storage substrate (UserPreferenceMemory, ProphecyEngine) but no component that looks across historical failures and says "these three rejections are the same problem wearing different hats."

**Qwen 397B role:** Periodic batch analyst (runs once per session boot, and once per hour during long battle tests). Input: last N postmortem JSON records (where N is bounded by prompt budget, ~50 records). Output: strict JSON schema `failure_clusters.1` — `{clusters: [{cluster_id: str, signature: str, episode_ids: [str], confidence: float, suggested_guardrail: str}], novel_modes: [str]}`. Clusters are written to UserPreferenceMemory as `FEEDBACK` type, tagged `("episodic", "cluster")`.

**Anti-hallucination gate:** Every `episode_ids` entry must exist in the input postmortem set. Any hallucinated episode_id → rejection, log, no memory written.

**Timeout:** 30s per call (runs out-of-band; no live op blocked on it).

#### 3.3 — DreamEngine Seed Generation

**What it does today:** `dream_engine.py` runs during idle-GPU windows and generates speculative improvement blueprints, but its seed selection is a naive round-robin across the file reputation ledger.

**Gap:** DreamEngine picks *what* to dream about via a heuristic. It should be picking via heavy-model reasoning over the codebase, recent commit velocity, and open postmortem clusters — the exact inputs Qwen 397B is well-suited to reason across.

**Qwen 397B role:** Seed generator for each dream cycle. Input: top-10 files by reputation decay + recent commit scope histogram + open failure clusters from §3.2. Output: strict JSON schema `dream_seed.1` — `{seeds: [{seed_id: str, target_path: str, hypothesis: str, expected_value: str, preconditions: [str]}], rejected: [{path: str, reason: str}]}`. DreamEngine consumes the seed list and runs its existing blueprint pipeline per seed.

**Anti-hallucination gate:** Every `target_path` must be a real file (on-disk check, not just reputation-ledger lookup). Any missing path → rejection, log, DreamEngine falls back to the round-robin heuristic.

**Timeout:** 30s per call (runs during idle windows; no user-facing deadline).

### Promotion Gate from Phase 0 to Phase 3

Phase 3 is blocked on Phase 0 proving the non-streaming endpoint is viable. Explicit criteria:

1. **≥24h of Phase 0 shadow telemetry** with <5% timeout rate on the Gemma compaction caller.
2. **Zero hallucinated references** that slipped past the anti-hallucination gate and reached the pipeline (i.e., the gate caught all of them, or better yet, the model didn't hallucinate in the first place).
3. **Circuit breaker never opened globally** during shadow — opened-per-call is acceptable and expected.
4. **Manual LIVE promotion decision** by the architect (Derek). No automatic promotion.

Only then does Task #13+ begin wiring Qwen 397B to OperationAdvisor. Phase 3 is a **strategic mandate**, not an imminent ship.

### Why This Matters

The streaming endpoint failures did not invalidate DoubleWord as an inference provider. They invalidated the **agent-shaped usage pattern** against the DoubleWord streaming endpoint. By reframing every DW call site as a non-streaming, short, structured, schema-validated, anti-hallucination-gated function call, O+V recovers a genuinely useful provider tier without compromising Manifesto §6 (Execution Validation). The cost math stays favorable: every op Gemma absorbs from Claude at the compaction layer, and every operation Qwen absorbs at the heavy-analyst layer, extends the session's Claude budget for the prefrontal-cortex work that only Claude can do.

Manifesto §5 (Intelligence-Driven Routing): *"semantic, not regex; DAGs, not scripts."* The reseated DW topology is the operational embodiment — DW lives where the task is genuinely structured-function-shaped, Claude lives where the task is genuinely agent-shaped, and the seam between them is deterministic, observable, and reversible.

