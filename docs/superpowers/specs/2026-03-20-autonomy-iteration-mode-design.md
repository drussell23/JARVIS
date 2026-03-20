# Autonomy Iteration Mode — Design Specification

**Date**: 2026-03-20
**Status**: Draft
**Branch**: `feat/autonomy-iteration-mode`

---

## 1. Problem Statement

Ouroboros has a production-grade governance pipeline (10 phases, 36K LOC, 1382 tests) with
parallel execution (SubagentScheduler L3), cross-repo sagas, trust graduation, and durable
state. However, it operates reactively — it waits for signals (test failures, voice commands,
backlog entries) before acting.

The goal is to add an **Autonomy Iteration Mode** that runs fresh-iteration loops for
backlog/self-improvement, composing existing governance infrastructure. This makes the
Trinity (JARVIS/Prime/Reactor) proactively self-improving without requiring human initiation
of each task.

## 2. Decision Locks

These are non-negotiable constraints from the project owner:

1. **Prime is inference-only.** J-Prime owns model serving/reasoning APIs. No workflow loops in Prime.
2. **Approach B (ExecutionGraph Composer).** Not thin sequential feeder (A) or full LLM self-directed loop (C).
3. **Hybrid work selection (C).** Backlog-first, miner fallback, autonomy-tier-gated.
4. **Runner lives in JARVIS repo.** May execute changes across JARVIS + Prime (+ Reactor later).
5. **No direct auto-merge to main** in this phase. Auto-branch + auto-PR, human merges.
6. **Safety boundaries**: feature-flag gated, explicit stop conditions, budget/time caps, human approval gates for high-risk changes.

## 3. Architecture

### 3.1 Component Structure

```
AutonomyIterationService (new — Zone 6.10 in supervisor)
├── IterationPlanner (new)
│   ├── reads BacklogSensor findings
│   ├── reads OpportunityMinerSensor findings (fallback)
│   ├── queries GoalDecomposer + Oracle for file targeting
│   └── outputs: ExecutionGraph (DAG of WorkUnitSpecs)
│
├── IterationExecutor (thin wrapper)
│   ├── submits ExecutionGraph → SubagentScheduler.submit()
│   ├── awaits completion → SubagentScheduler.wait_for_graph()
│   ├── collects merged patches → get_merged_patches()
│   └── feeds outcomes to trust graduation + ledger
│
├── IterationBudgetGuard (new)
│   ├── reads BrainSelector.daily_spend
│   ├── enforces per-iteration cost cap
│   ├── enforces time cap (wall-clock per iteration)
│   ├── enforces error threshold (consecutive failures → circuit break)
│   └── outputs: go/no-go decision before each iteration
│
└── Composes (existing, unchanged):
    ├── SubagentScheduler (L3 parallel execution)
    ├── GoalDecomposer (intent expansion + Oracle file targeting)
    ├── TrustGraduator (autonomy tier tracking)
    ├── OperationLedger (durable audit trail)
    ├── ExecutionGraphStore (crash-resume state)
    ├── CommProtocol (Langfuse + VoiceNarrator + EventBridge)
    └── GovernedLoopService.submit() (single-op fallback path)
```

**Ownership boundaries:**

| Component | Owns | Does NOT Own |
|-----------|------|-------------|
| AutonomyIterationService | Loop lifecycle (start/stop/pause), state transitions | Brain selection, code generation, test execution, file writes |
| IterationPlanner | Work selection, graph construction, blast-radius checks | Trust decisions, execution, merge |
| IterationExecutor | Submission, outcome collection | Graph execution (delegated to SubagentScheduler) |
| IterationBudgetGuard | Stop conditions (stateless checks) | Budget tracking (reads BrainSelector) |

### 3.2 State Machine (9 states)

```
IDLE → SELECTING → PLANNING → EXECUTING → EVALUATING ──→ COOLDOWN → IDLE
                                    │           │
                                    │           ├──→ REVIEW_GATE → IDLE
                                    │           │
                                    ▼           ├──→ PAUSED (budget/blast/manual)
                               RECOVERING      │
                                    │           └──→ IDLE (success, next iteration)
                                    ▼
                                EVALUATING

                         any state → STOPPED (kill switch)
```

| State | What happens | Transition |
|-------|-------------|------------|
| **IDLE** | Wait `iteration_interval`. Check feature flag + budget window. | → SELECTING if enabled + budget |
| **SELECTING** | Backlog-first (priority-sorted). If empty or all blocked, check miner (weighted: every Nth cycle even if backlog has low-priority items). Apply trust tier ACK filter. Skip poisoned tasks. | → PLANNING if task selected, → IDLE if nothing actionable |
| **PLANNING** | Decompose task → ExecutionGraph via IterationPlanner. Validate blast-radius caps (multi-dimensional). Stamp `operation_policy_hash`. Assign stable `plan_id`. Run preflight invariant checks. | → EXECUTING if graph valid + preflight passes, → EVALUATING (skip) if rejected |
| **EXECUTING** | `scheduler.submit(graph)` → `wait_for_graph()`. Preemptible: if user traffic arrives, yield CPU via ResourceGovernor priority demotion. | → EVALUATING on terminal graph phase, → RECOVERING on crash/timeout |
| **RECOVERING** | Check graph state in ExecutionGraphStore. Terminal → EVALUATING. Partial apply → ledger checksum reconciliation. Non-terminal → resume via `scheduler.recover_inflight()`. Bounded: max 2 recovery attempts. | → EVALUATING if reconciled, → PAUSED if irrecoverable |
| **EVALUATING** | Record outcome in ledger. Feed TrustGraduator metrics. Update backlog task status. Check all stop conditions. If trust regression (error streak), auto-demote tier. | → REVIEW_GATE if changes applied, → COOLDOWN if failed, → IDLE if noop/skip, → PAUSED if stop condition |
| **REVIEW_GATE** | Check autonomy tier policy. OBSERVE/SUGGEST: emit PR draft, require human approval. GOVERNED: auto-create branch + PR, human merges. AUTONOMOUS: auto-merge if all risk gates pass. | → IDLE after gate resolved |
| **COOLDOWN** | Exponential backoff: `cooldown_base * 2^(failures - 1)`, capped at `max_cooldown_s`. | → IDLE after cooldown elapsed |
| **PAUSED** | Suspended. Persisted reason code + resume conditions to ledger. VoiceNarrator announces. Resume requires: budget reset (ledger-backed monotonic window) OR manual `resume()` with reason. | → IDLE on valid resume |
| **STOPPED** | Kill switch. Cancels in-flight graph safely. Writes terminal ledger event. Flushes all state. | Terminal |

### 3.3 Isolation from Runtime Path

The iteration loop runs on a **separate asyncio task** with its own concurrency slot:

- Dedicated `max_concurrent_iteration_graphs=1` (does not share GLS `max_concurrent_ops`)
- ResourceGovernor checks CPU/memory every 5s, pauses new waves if contention detected
- User requests (voice, API) always take priority over iteration work

## 4. Contract Interfaces

### 4.1 Path Canonicalization (shared)

```python
def canonicalize_path(path: str, repo_root: Path) -> str:
    """Single source of truth for path identity.

    Resolves: ./, ../, symlinks, case normalization (macOS), trailing slashes.
    Used by: IterationPlanner, SubagentScheduler, MergeCoordinator.
    Raises PathTraversalError if path escapes repo root.
    """
```

### 4.2 IterationTask (unified work item)

```python
@dataclass(frozen=True)
class IterationTask:
    task_id: str                      # stable across restarts
    source: str                       # "backlog" | "ai_miner"
    description: str                  # human-readable goal
    target_files: Tuple[str, ...]     # hint files (may be empty for miner)
    repo: str                         # primary repo
    priority: int                     # 1-5 (5=highest)
    requires_human_ack: bool          # True for miner in OBSERVE/SUGGEST
    evidence: Dict[str, Any]          # miner: complexity scores; backlog: metadata
```

### 4.3 PlanningContext (frozen snapshot)

```python
@dataclass(frozen=True)
class PlanningContext:
    repo_commit: str                  # git rev-parse HEAD at plan time
    oracle_snapshot_id: str           # oracle._last_indexed_monotonic_ns
    policy_hash: str                  # SHA256 of stop + blast policies
    schema_version: str               # "1.0"
    trust_tier: str                   # current autonomy tier
    budget_remaining_usd: float       # remaining iteration budget
```

### 4.4 PlannerOutcome (replaces raw Optional)

```python
class PlannerRejectReason(str, Enum):
    ORACLE_NO_DATA = "oracle_no_data"
    BLAST_RADIUS_EXCEEDED = "blast_radius_exceeded"
    ZERO_ACTIONABLE_UNITS = "zero_actionable_units"
    DAG_CYCLE_DETECTED = "dag_cycle_detected"
    SNAPSHOT_STALE = "snapshot_stale"
    TRUST_GATE_DENIED = "trust_gate_denied"
    TASK_POISONED = "task_poisoned"

@dataclass(frozen=True)
class PlannerOutcome:
    status: str                              # "planned" | "rejected"
    graph: Optional[ExecutionGraph] = None
    reject_reason: Optional[PlannerRejectReason] = None
    metadata: Optional[PlannedGraphMetadata] = None

@dataclass(frozen=True)
class PlannedGraphMetadata:
    selection_proof: Dict[str, Any]          # {source, task_id, priority, why_selected}
    expansion_proof: List[Dict[str, Any]]    # [{file, oracle_score, why_included}]
    partition_proof: List[Dict[str, Any]]    # [{unit_id, files, why_grouped, dependency_reason}]
    reject_reason_code: str                  # "" if planned
    planning_context: PlanningContext
```

### 4.5 Idempotency Keys (stable across restarts)

```python
# task_fingerprint: content-derived, not session-derived
task_fingerprint = sha256(task_description + sorted(target_files))

# plan_id: stable for same task + same policy
plan_id = f"plan-{sha256(task_fingerprint + policy_hash + target_scope)}"

# graph_id: 1:1 with plan_id
graph_id = f"graph-{plan_id}"
```

### 4.6 Blast Radius Policy (multi-dimensional)

```python
@dataclass(frozen=True)
class BlastRadiusPolicy:
    max_repos_touched: int = 2
    max_files_changed: int = 10
    max_lines_changed: int = 500
    max_public_api_files_touched: int = 3
    max_graph_units: int = 8
    max_open_prs: int = 3

    def check(self, graph: ExecutionGraph, oracle: TheOracle) -> Optional[str]:
        """Returns violation description or None if within bounds."""
```

### 4.7 Stop Policy

```python
@dataclass(frozen=True)
class IterationStopPolicy:
    max_iterations_per_session: int = 10
    max_consecutive_failures: int = 3
    max_wall_time_s: float = 3600.0         # 1 hour per session
    max_spend_usd: float = 0.50             # iteration-specific budget
    cooldown_base_s: float = 60.0           # exponential backoff base
    max_cooldown_s: float = 900.0           # 15 min cap
    miner_fairness_interval: int = 5        # every Nth cycle, miner gets slot
    blast_radius: BlastRadiusPolicy = BlastRadiusPolicy()
```

### 4.8 Budget Window (ledger-backed)

```python
@dataclass
class IterationBudgetWindow:
    window_start_utc: datetime
    spend_usd: float = 0.0
    iterations_count: int = 0

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc).date() > self.window_start_utc.date()

    # Persisted to ledger as LedgerEntry(state=BUDGET_CHECKPOINT)
    # Loaded on startup for monotonic enforcement
```

### 4.9 Resource Governor

```python
@dataclass(frozen=True)
class ResourceGovernor:
    iteration_priority: int = 19           # nice level (lowest priority)
    preempt_on_cpu_above: float = 80.0     # % CPU
    preempt_on_memory_above: float = 85.0  # % RAM
    yield_check_interval_s: float = 5.0

    async def should_yield(self) -> bool:
        """True if user traffic should take priority."""
```

### 4.10 Task Rejection Tracker (poison queue)

```python
@dataclass
class TaskRejectionTracker:
    poison_threshold: int = 3
    cooldown_s: float = 3600.0

    def record_rejection(self, task_id: str, reason: PlannerRejectReason) -> None
    def is_poisoned(self, task_id: str) -> bool
    def get_reject_history(self, task_id: str) -> List[PlannerRejectReason]
```

### 4.11 Recovery Contract

```python
class RecoveryDecision(str, Enum):
    EVALUATE = "evaluate"          # graph already terminal, skip to eval
    RESUME = "resume"              # safe to resume from checkpoint
    SKIP = "skip"                  # no state found, re-plan
    PAUSE_IRRECOVERABLE = "pause"  # partial apply with checksum mismatch

async def recover(graph_id: str) -> RecoveryDecision:
    """Reconcile graph state after crash.

    Checks:
      1. Graph exists in ExecutionGraphStore?
      2. Phase is terminal? → EVALUATE
      3. Any partially applied units? → checksum via ledger
      4. Checksums match? → RESUME remaining units
      5. Checksums mismatch? → PAUSE_IRRECOVERABLE
    """
```

### 4.12 Preflight Invariant Checks

```python
async def preflight_check(
    graph: ExecutionGraph,
    context: PlanningContext,
    oracle: TheOracle,
    trust_graduator: TrustGraduator,
    budget: IterationBudgetWindow,
) -> Optional[str]:
    """Run immediately before scheduler.submit(). Returns error or None.

    Checks:
      1. Repo HEAD unchanged since planning (snapshot stale guard)
      2. Blast radius still within policy
      3. Trust tier not demoted since planning
      4. Budget still has headroom
      5. No path conflicts with currently executing graphs
    """
```

### 4.13 Causal Trace Propagation

Every artifact carries the same `causal_trace_id`:

```
iteration_id → plan_id → graph_id → WorkUnitSpec.causal_trace_id
    → OperationContext.causal_trace_id → LedgerEntry
    → CommMessage.correlation_id → Langfuse trace
    → PR description
```

### 4.14 Operation Policy Hash

```python
operation_policy_hash = sha256(
    IterationStopPolicy.to_json() +
    BlastRadiusPolicy.to_json() +
    trust_tier.value +
    governance_mode
)
# Stamped on every ExecutionGraph and LedgerEntry
# If policy changes between planning and execution → abort (stale plan)
```

## 5. IterationPlanner — Task → ExecutionGraph

### 5.1 Planning Pipeline (4 deterministic steps)

```
IterationTask
    ↓
Step 1: File Expansion (Oracle.semantic_search)
    Merge with task.target_files, deduplicate, canonicalize
    Cap at blast_radius.max_files_changed
    ↓
Step 2: Dependency Analysis (Oracle.get_file_neighborhood)
    Build import/call dependency edges
    Determine execution order
    ↓
Step 3: Unit Partitioning
    Group files by shared dependencies → same unit (barrier group)
    Independent files → separate units (parallelizable)
    Each unit gets owned_paths = canonicalized target_files
    dependency_ids from Step 2 edges
    Cap at blast_radius.max_graph_units
    ↓
Step 4: Graph Assembly
    Build ExecutionGraph with stable graph_id
    Validate DAG (topological sort)
    Compute plan_digest (SHA256 of all unit specs)
    Verify blast_radius caps
    ↓
PlannerOutcome(status="planned", graph=..., metadata=...)
```

### 5.2 Deterministic Test Selection

```python
def select_acceptance_tests(target_files, repo_root) -> Tuple[str, ...]:
    """Rules (applied in order):
      1. backend/foo/bar.py → tests/test_foo/test_bar.py
      2. backend/foo/bar.py → tests/foo/test_bar.py
      3. If no test file found → parent directory test glob
      4. Cap at 5 test files per unit
    """
```

### 5.3 Planner Interface

```python
class IterationPlanner:
    def __init__(self, oracle, blast_radius, rejection_tracker, repo_root): ...

    async def plan(self, task, iteration_id, planning_context) -> PlannerOutcome:
        """Returns PlannerOutcome (never raises). All paths canonicalized."""
```

### 5.4 What the Planner Does NOT Do

- No LLM calls (Oracle graph only)
- No code generation (J-Prime's job during EXECUTING)
- No file I/O (reads Oracle's in-memory index)
- No side effects (pure function)
- No trust decisions (iteration service's job)

## 6. Autonomy Tier Policy

| Tier | Selection | Execution | Review | Merge |
|------|-----------|-----------|--------|-------|
| **0 — OBSERVE** | Finds tasks, logs intent | No code changes | N/A | N/A |
| **1 — SUGGEST** | Selects task, plans graph | Generates PR draft only | Human reviews | Human merges |
| **2 — GOVERNED** | Selects task, plans graph | Creates branch, writes code, runs tests | Auto-creates PR | Human merges |
| **3 — AUTONOMOUS** | Selects task, plans graph | Creates branch, writes code, runs tests | Auto-creates PR | Auto-merge if all risk gates pass |

Miner-discovered tasks require `requires_human_ack=True` in OBSERVE and SUGGEST tiers.
Auto-approve miner tasks only in AUTONOMOUS tier with risk gates.

## 7. Configuration

### 7.1 Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JARVIS_AUTONOMY_ITERATION_ENABLED` | `false` | Feature flag |
| `JARVIS_AUTONOMY_ITERATION_INTERVAL_S` | `300` | Seconds between iteration cycles |
| `JARVIS_AUTONOMY_MAX_ITERATIONS` | `10` | Max iterations per session |
| `JARVIS_AUTONOMY_MAX_SPEND_USD` | `0.50` | Per-session budget cap |
| `JARVIS_AUTONOMY_MAX_WALL_TIME_S` | `3600` | Per-session wall-clock cap |
| `JARVIS_AUTONOMY_COOLDOWN_BASE_S` | `60` | Exponential backoff base |
| `JARVIS_AUTONOMY_MINER_FAIRNESS_N` | `5` | Every Nth cycle, miner gets slot |
| `JARVIS_AUTONOMY_MAX_FILES` | `10` | Blast radius: files cap |
| `JARVIS_AUTONOMY_MAX_LINES` | `500` | Blast radius: lines cap |
| `JARVIS_AUTONOMY_MAX_PRS` | `3` | Blast radius: open PRs cap |

### 7.2 File Manifest (new files)

| File | Purpose | Est. Lines |
|------|---------|------------|
| `backend/core/ouroboros/governance/autonomy/iteration_service.py` | AutonomyIterationService (state machine + loop) | ~300 |
| `backend/core/ouroboros/governance/autonomy/iteration_planner.py` | IterationPlanner (task → graph) | ~250 |
| `backend/core/ouroboros/governance/autonomy/iteration_types.py` | All dataclasses (IterationTask, PlannerOutcome, policies, etc.) | ~200 |
| `backend/core/ouroboros/governance/autonomy/iteration_budget.py` | IterationBudgetGuard + BudgetWindow | ~100 |
| `backend/core/ouroboros/governance/autonomy/resource_governor.py` | ResourceGovernor (CPU/memory preemption) | ~60 |
| `backend/core/ouroboros/governance/autonomy/path_utils.py` | canonicalize_path + PathTraversalError | ~40 |
| `backend/core/ouroboros/governance/autonomy/preflight.py` | Preflight invariant checks | ~80 |

**Modified files:**

| File | Change |
|------|--------|
| `unified_supervisor.py` | Zone 6.10: create + start AutonomyIterationService |
| `backend/core/ouroboros/governance/intake/intake_layer_service.py` | Expose backlog + miner findings to iteration service |

**Test files** (one per new module):

| File | Test Count |
|------|------------|
| `tests/test_ouroboros_governance/test_iteration_service.py` | ~20 |
| `tests/test_ouroboros_governance/test_iteration_planner.py` | ~15 |
| `tests/test_ouroboros_governance/test_iteration_types.py` | ~10 |
| `tests/test_ouroboros_governance/test_iteration_budget.py` | ~10 |
| `tests/test_ouroboros_governance/test_resource_governor.py` | ~5 |
| `tests/test_ouroboros_governance/test_path_utils.py` | ~8 |
| `tests/test_ouroboros_governance/test_preflight.py` | ~8 |
| `tests/test_ouroboros_governance/test_iteration_e2e.py` | ~10 |

## 8. Go/No-Go Test Matrix

### Tier 0: Unit Tests (must pass before any execution)

| ID | Test | Verifies |
|----|------|----------|
| T01 | `test_canonicalize_path_resolves_dotslash` | `./foo` and `foo` produce identical canonical path |
| T02 | `test_canonicalize_path_resolves_symlink` | Symlinked path resolves to real target |
| T03 | `test_canonicalize_path_rejects_traversal` | `../../etc/passwd` raises PathTraversalError |
| T04 | `test_plan_id_stable_across_restarts` | Same task + policy → same plan_id |
| T05 | `test_plan_id_changes_on_policy_change` | Different policy_hash → different plan_id |
| T06 | `test_planner_returns_rejected_not_none` | Unplannable task → PlannerOutcome(status="rejected") |
| T07 | `test_poisoned_task_skipped_in_selection` | 3x rejected → is_poisoned() returns True |
| T08 | `test_blast_radius_rejects_oversized_graph` | 15 files → rejected |
| T09 | `test_blast_radius_checks_public_api_surface` | 4 public API files → rejected |
| T10 | `test_acceptance_tests_deterministic` | Same files always select same tests |
| T11 | `test_dag_validation_catches_cycle` | Circular dependency → rejected |
| T12 | `test_metadata_includes_expansion_proof` | PlannedGraphMetadata has non-empty proofs |

### Tier 1: Integration Tests (must pass before SUGGEST)

| ID | Test | Verifies |
|----|------|----------|
| T13 | `test_iteration_loop_idle_to_selecting` | Feature flag on + budget → SELECTING |
| T14 | `test_iteration_loop_stops_on_budget` | Spend exceeds cap → PAUSED |
| T15 | `test_iteration_loop_stops_on_error_streak` | 3 failures → PAUSED + trust demotion |
| T16 | `test_cooldown_exponential_backoff` | Failures 1,2,3 → cooldowns 60s, 120s, 240s |
| T17 | `test_recovery_resumes_nonterminal_graph` | Kill mid-execute → restart → resumes |
| T18 | `test_recovery_detects_partial_apply` | Partial writes → checksum mismatch → PAUSED |
| T19 | `test_preflight_rejects_stale_snapshot` | Repo commit changed → re-plan |
| T20 | `test_miner_fairness_every_nth_cycle` | Cycles 1-4 backlog, cycle 5 miner |
| T21 | `test_resource_governor_yields_on_high_cpu` | CPU > 80% → waves paused |
| T22 | `test_causal_trace_propagates_end_to_end` | iteration_id flows through entire chain |

### Tier 2: E2E Acceptance Tests (must pass before GOVERNED)

| ID | Test | Verifies |
|----|------|----------|
| T23 | `test_backlog_task_planned_executed_evaluated` | Happy path end-to-end |
| T24 | `test_miner_finding_requires_ack_in_suggest` | Miner + SUGGEST → requires_human_ack |
| T25 | `test_review_gate_creates_pr_in_governed` | GOVERNED → branch + PR created |
| T26 | `test_review_gate_blocks_merge_in_governed` | GOVERNED → PR exists, NOT auto-merged |
| T27 | `test_kill_switch_cancels_inflight` | STOPPED → graph cancelled + terminal ledger |
| T28 | `test_policy_hash_mismatch_aborts_execution` | Policy changed → abort |
| T29 | `test_cross_repo_graph_respects_barriers` | Multi-repo → barrier merge → atomic apply |
| T30 | `test_trust_regression_demotes_tier` | Error streak → tier downgraded |

### Go/No-Go Decision Rules

| Tier | Required | Decision |
|------|----------|----------|
| Deploy to OBSERVE | T01-T12 pass | Runs, logs only, no code changes |
| Promote to SUGGEST | T01-T22 pass | Suggests PRs, human approval required |
| Promote to GOVERNED | T01-T30 pass | Auto-branch + auto-PR, human merges |
| Promote to AUTONOMOUS | T01-T30 + 50 governed ops + 0 rollbacks | Auto-merge with risk gates |

## 9. Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                 AutonomyIterationService                        │
│                                                                 │
│  IDLE ──→ SELECTING ──→ PLANNING ──→ EXECUTING ──→ EVALUATING   │
│              │              │            │              │        │
│              ▼              ▼            ▼              ▼        │
│         BacklogSensor  IterationPlanner  SubagentScheduler  Ledger│
│         MinerSensor    Oracle+GoalDecomp GLS.submit()    TrustGrad│
│              │              │            │              │        │
│              │              │            ▼              │        │
│              │              │      ┌──────────┐        │        │
│              │              │      │ J-Prime  │        │        │
│              │              │      │ (GPU VM) │        │        │
│              │              │      └──────────┘        │        │
│              │              │            │              │        │
│              │              │            ▼              │        │
│              │              │      TestRunner           │        │
│              │              │      ChangeEngine         │        │
│              │              │            │              │        │
│              │              │            ▼              ▼        │
│              │              │      REVIEW_GATE ──→ Branch/PR    │
│              │              │                                    │
│              └──────────────┴────────────────────────────────────┘
│                                                                 │
│  CommProtocol: INTENT → PLAN → HEARTBEAT → DECISION → POSTMORTEM│
│  ├── LogTransport (always)                                      │
│  ├── LangfuseTransport (if configured)                          │
│  ├── VoiceNarrator (announces key events)                       │
│  └── EventBridge (cross-repo events to Reactor)                 │
└─────────────────────────────────────────────────────────────────┘
```

## 10. Safety Invariants

1. **Iteration never blocks user requests.** Separate concurrency slot + ResourceGovernor preemption.
2. **No auto-merge to main.** REVIEW_GATE enforces human merge in OBSERVE/SUGGEST/GOVERNED tiers.
3. **Budget is monotonic.** Ledger-backed window resets only on day boundary.
4. **Policy hash validates freshness.** Stale plans abort before execution.
5. **Poisoned tasks don't loop.** 3 rejections → cooldown before retry.
6. **Trust regression is automatic.** Error streak → tier demotion → miner tasks require ACK again.
7. **Kill switch is immediate.** STOPPED cancels in-flight + writes terminal ledger entry.
8. **All paths are canonical.** One function, shared across all components.
9. **Causal trace is unbroken.** iteration_id propagates through every artifact.
10. **Partial apply is detected.** Ledger checksum reconciliation prevents corrupted state.
