# L3 Execution Spec: Parallel Subagent Execution

**Date:** 2026-03-12
**Status:** Approved
**Umbrella doc:** `2026-03-12-l3-l5-umbrella-architecture.md`
**Phase:** L3 — Parallel Subagent Execution (Cross-Repo)
**Builds on:** L1 (tool-use runtime), L2 (iterative self-repair loop)

---

## 1. Goal

Enable the Ouroboros governance pipeline to decompose a complex multi-file or cross-repo operation into a DAG of independent patch bundles, execute bundles in parallel with proper isolation, and merge results deterministically — reducing wall-clock latency by ≥30% vs sequential execution for the same operation.

---

## 2. Entry / Exit Criteria

### Entry Criteria (must all pass before L3 activates)

- [ ] L1 hard gates passing (tool-use audit completeness, no policy bypass)
- [ ] L2 hard gates passing (median iterations ≤3, no retry storms, regression below L0)
- [ ] `JARVIS_L3_ENABLED=false` by default; no L3 code path reachable without opt-in
- [ ] All L3 unit tests passing (see Section 10)
- [ ] Umbrella doc committed and reviewed

### Exit Criteria / Hard Gates (must all pass before L3 ships)

- [ ] **Wall-clock SLO:** parallel time / sequential time ≤ 0.70 (≥30% faster) on benchmark suite
- [ ] **Mainline regression:** broken-mainline rate no higher than L0 baseline
- [ ] **Causal trace completeness:** zero ledger events missing `causal_id`
- [ ] **Determinism:** same DAG input → same schedule + merge decisions × 50 runs = 100%
- [ ] **Cleanup correctness:** zero orphaned worktrees after cancel/timeout × 20 runs
- [ ] **No false CONFLICT_STALE on independent nodes:** zero false positives in benchmark suite

---

## 3. Architecture

### 3.1 Execution Model (Hybrid — Decision Locked)

- **Coordinator/scheduler:** asyncio tasks in-process (non-blocking coordination)
- **Execution primitives:** isolated subprocess sandboxes per node (patch apply, test run, validation)
- **No node writes to real working tree** before canonical VALIDATE + GATE pass
- **Merge/promote:** saga-governed, deterministic

### 3.2 Pipeline Integration

L3 activates at GENERATE phase when J-Prime returns schema `2d.1` AND `dag_capable=true` in config:

```
CLASSIFY → ROUTE → CONTEXT_EXPANSION → GENERATE
                                            │
                   ┌────────────────────────┴──────────────────────┐
                   │ schema == "2d.1"                               │ schema 2b.x / 2c.x
                   │ AND config.dag_capable == True                 │
                   ↓ YES                                            ↓ NO
             TaskDAG.build()                              existing L1/L2 path
             TaskDAG.validate()                           (behaviorally untouched)
                   │
          [validation fails?]
          YES ↓          NO ↓
    hard-fallback    SubagentScheduler.run()
    to L1/L2               │
    + reason_code  [workers settle]
                           │
                  MergeCoordinator.run()
                           │
          ┌────────────────┼──────────────────┐
    CONVERGED          PARTIAL             ABORTED
          ↓                ↓                   ↓
  canonical VALIDATE  SCHED_PARTIAL       CANCELLED
  → GATE → APPLY      (no auto-apply;     + reason_code
  → VERIFY → COMPLETE  escalate)
```

### 3.3 New Files

| File | Role |
|---|---|
| `backend/core/ouroboros/governance/task_dag.py` | TaskDAG, DAGNode, ConflictGroup, validation, helpers |
| `backend/core/ouroboros/governance/subagent_scheduler.py` | Scheduler FSM, semaphores, node lifecycle |
| `backend/core/ouroboros/governance/worker_sandbox.py` | WorkerSandbox extending L2 primitives |
| `backend/core/ouroboros/governance/merge_coordinator.py` | Sequential barrier + regen protocol |

### 3.4 Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/orchestrator.py` | Schema dispatch at GENERATE; L3 hook |
| `backend/core/ouroboros/governance/providers.py` | `2d.1` schema emit; `_build_conflict_regen_prompt()` |
| `backend/core/ouroboros/governance/failure_classifier.py` | Add `CONFLICT_STALE` class |
| `backend/core/ouroboros/governance/governed_loop_config.py` | `SchedulerConfig`, `MergeCoordinatorConfig` |
| `backend/core/ouroboros/governance/brain_selector.py` | `dag_capable: bool` in `BrainSelectionResult` |
| `brain_selection_policy.yaml` | `dag_capable: bool` per brain entry (default `false`) |
| `jarvis-prime/jarvis_prime/` | `2d.1` planner prompt + response schema |

---

## 4. Component Contracts

### 4.1 TaskDAG (`task_dag.py`)

**Normalization rules (enforced by `build()`):**
1. Path separators: normalize `os.sep` → `/`
2. Reject paths containing `..` or starting with `/`
3. Reject empty string paths
4. Case: preserve as-is (case-sensitive)
5. Duplicates within bundle: collapse → raise `DAG_DUPLICATE_BUNDLE_FILE`
6. Result: `tuple(sorted(unique(normalized_paths)))`

```python
@dataclass(frozen=True)
class DAGGenerationMetadata:
    model_id: str
    brain_id: str
    generation_latency_ms: float
    schema_version_emitted: str          # must be "2d.1"
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None

@dataclass(frozen=True)
class DAGNode:
    node_id: str           # sha256(op_id + repo + sorted_bundle_files)[:16]
    repo: str              # must be in RepoRegistry
    bundle_files: tuple[str, ...]        # normalized, sorted, unique
    intent_summary: str
    estimated_risk: RiskLevel
    depends_on: frozenset[str]           # cross-repo edges blocked unless dag.allow_cross_repo_edges
    apply_mode: Literal["atomic"]
    conflict_key: str      # sha256(repo + "\x00" + "\x00".join(bundle_files))
    priority: int = 0      # lower = higher priority; tie-breaker only

@dataclass(frozen=True)
class ConflictGroup:
    group_id: str          # sha256(repo + "\x00" + "\x00".join(sorted(nodes)))
    repo: str              # invariant: all nodes share this repo
    nodes: tuple[str, ...]             # sorted node_ids
    serialization_order: tuple[str, ...]   # stable: (priority, repo, node_id)

@dataclass(frozen=True)
class DAGValidationResult:
    valid: bool
    reason_code: Optional[str]
    # DAG_EMPTY | DAG_DUPLICATE_NODE | DAG_UNKNOWN_REPO | DAG_EMPTY_BUNDLE
    # DAG_DUPLICATE_BUNDLE_FILE | DAG_INVALID_PRIORITY | DAG_DANGLING_DEP
    # DAG_CROSS_REPO_EDGE_DENIED | DAG_CYCLE
    details: str

@dataclass(frozen=True)
class TaskDAG:
    dag_id: str            # sha256(op_id + "\x00" + sorted(node_ids))[:16]
    op_id: str
    schema_version: Literal["2d.1"]
    nodes: tuple[DAGNode, ...]
    conflict_groups: tuple[ConflictGroup, ...]
    allow_cross_repo_edges: bool          # default False
    created_at_ns: int
    generation_metadata: DAGGenerationMetadata
    _failure_closure: Mapping[str, frozenset[str]] = field(repr=False)
    # Precomputed at build(): node_id → frozenset of transitively dependent node_ids

    @staticmethod
    def build(op_id, nodes, metadata, allow_cross_repo_edges=False, created_at_ns=None) -> "TaskDAG": ...
    def validate(self, repo_registry) -> DAGValidationResult: ...
    def ready_nodes(self, completed: frozenset[str], failed: frozenset[str]) -> tuple[DAGNode, ...]: ...
    def blocked_by_failure(self, failed_node_id: str) -> frozenset[str]: ...  # O(1)
    def topological_layers(self) -> tuple[tuple[str, ...], ...]: ...
    def conflicts_for(self, node_id: str) -> Optional[ConflictGroup]: ...
    def digest(self) -> str: ...  # replay/audit key
```

**L3-v1 conflict rule:** Two nodes conflict iff `same repo AND file-set intersection non-empty`.
**L3-v1.1+:** path-aware or hunk-aware relaxation after replayability is proven.

---

### 4.2 SubagentScheduler (`subagent_scheduler.py`)

#### Scheduler FSM States

```
SCHED_INIT
  → SCHED_VALIDATING   (DAG.validate() + dag_capable check)
  → SCHED_RUNNING      (dispatching ready nodes, tracking in-flight)
  → SCHED_DRAINING     (all nodes dispatched, awaiting completions)
  → SCHED_MERGING      (all workers settled, MergeCoordinator running)

Terminal:
  SCHED_CONVERGED      (all nodes NODE_CONVERGED)
  SCHED_PARTIAL        (≥1 failed; no auto-apply)
  SCHED_ABORTED        (kill condition or external cancel)
```

#### Node FSM States

```
NODE_PENDING     → NODE_BLOCKED  (dependency failed)
NODE_PENDING     → NODE_QUEUED   (deps satisfied; awaiting semaphore)
NODE_QUEUED      → NODE_RUNNING  (semaphore acquired; worker dispatched)
NODE_RUNNING     → NODE_VALIDATING
NODE_VALIDATING  → NODE_REGEN_PENDING  (CONFLICT_STALE detected)
NODE_VALIDATING  → NODE_CONVERGED
NODE_VALIDATING  → NODE_FAILED
NODE_REGEN_PENDING → NODE_CONVERGED | NODE_FAILED
Any in-flight  → NODE_CANCELLED  (external cancel)
```

#### Config

```python
@dataclass(frozen=True)
class SchedulerConfig:
    dag_capable: bool = False
    max_parallel_nodes: int = 4             # global asyncio.Semaphore
    max_parallel_per_repo: int = 2          # per-repo asyncio.Semaphore
    node_timebox_s: float = 120.0
    total_timebox_s: float = 600.0
    semaphore_acquire_timeout_s: float = 30.0
    confidence_collapse_threshold: float = 0.5   # fraction failed → SCHED_ABORTED
```

All values configurable via env vars (see Section 9).

#### Kill Conditions

| Code | Trigger | Outcome |
|---|---|---|
| `DAG_PREFLIGHT_REJECTED` | `DAG.validate()` fails | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_CAPABLE_FLAG_UNSET` | `dag_capable=false` | `SCHED_ABORTED` → L1/L2 fallback |
| `BUDGET_EXHAUSTED` | wall-clock > `total_timebox_s` | `SCHED_ABORTED` |
| `CONFIDENCE_COLLAPSE` | `failed/total >= collapse_threshold` | `SCHED_ABORTED` |
| `SEMAPHORE_STARVATION` | semaphore timeout | node → `NODE_FAILED` |
| `EXTERNAL_CANCEL` | `asyncio.CancelledError` | `SCHED_ABORTED` + kill all |
| `FATAL_INFRA` | unrecoverable subprocess error | `SCHED_ABORTED` |
| `POLICY_DENY` | GATE rejects | `SCHED_ABORTED` |

#### Cancellation Protocol

1. Stop dispatching new nodes immediately.
2. For each `NODE_RUNNING`/`NODE_VALIDATING`: `SIGTERM` → `wait_for(proc.wait(), 5s)` → `SIGKILL`.
3. `asyncio.shield(worktree.cleanup())` — cleanup completes even if parent cancelled.
4. Release all semaphores.
5. Emit `sched.aborted.v1`.

#### Dispatch Ordering (Determinism)

`DAG.ready_nodes()` is the single dispatch source. Sort key: `(priority, repo, node_id)`. Asyncio semaphores are FIFO (CPython). No scheduler-side reordering.

---

### 4.3 WorkerSandbox (`worker_sandbox.py`)

Extends L2 `repair_sandbox.py` primitives.

#### Worktree Inheritance

```python
@dataclass(frozen=True)
class WorktreeSource:
    kind: Literal["HEAD", "inherited"]
    inherited_from_node_id: Optional[str] = None
    inherited_path: Optional[Path] = None
    # HEAD: git worktree add --detach <path> HEAD
    # inherited: rsync -a <inherited_path>/ <new_path>/
    # L3-v1 uses rsync for inheritance (no temp commits, no git history noise)
```

#### CONFLICT_STALE Detection

Extends `FailureClass` enum (from L2):

```python
CONFLICT_STALE = "conflict_stale"
# Detected ONLY when source.kind == "inherited"
# Signals: import/symbol collision from prior bundle, patch apply mismatch,
#          duplicate definition, API signature mismatch traceable to upstream change
```

#### Config + Result

```python
@dataclass(frozen=True)
class WorkerSandboxConfig:
    worktree_base_dir: Path
    test_timeout_s: float = 60.0
    patch_apply_timeout_s: float = 10.0
    cleanup_timeout_s: float = 10.0
    retain_on_converge: bool = True    # retain path for downstream inheritance

@dataclass(frozen=True)
class WorkerResult:
    node_id: str
    status: Literal["CONVERGED", "FAILED", "CANCELLED", "INFRA_ERROR"]
    patch_applied: bool
    test_result: Optional[TestRunResult]
    failure_class: Optional[FailureClass]
    failure_signature: Optional[str]   # sha256(failure_class + error_message)
    worktree_path: Optional[Path]      # non-None if retained
    source: WorktreeSource
    duration_ms: float
    causal_id: str
```

#### Interface

```python
class WorkerSandbox:
    async def run(self, patch: PatchBundle) -> WorkerResult: ...
    async def cancel(self) -> None: ...          # SIGTERM → wait → SIGKILL → shield(cleanup)
    async def cleanup(self) -> None: ...         # git worktree remove || rm -rf
    def worktree_ref(self) -> Optional[Path]: ...  # for downstream inheritance
```

#### Hard Invariants

1. No write to real working tree — ever.
2. Atomic patch apply — all files or none.
3. Cleanup on all terminal states — `asyncio.shield()` guards cleanup.
4. `CONFLICT_STALE` only on `inherited` source.
5. Subprocess via `asyncio.create_subprocess_exec` — never `subprocess.run(check=True)` in thread.

#### Reuse from L2

| Primitive | Action |
|---|---|
| Git worktree create/remove | Reused directly |
| rsync fallback | Reused; also used for inheritance |
| `TestRunResult` | Reused unchanged |
| `FailureClassifier` | Extended with `CONFLICT_STALE` |
| Test runner | Reused |

---

### 4.4 MergeCoordinator (`merge_coordinator.py`)

Runs after scheduler settles. Owns A→B regen fallback and final patch assembly.

#### Config

```python
@dataclass(frozen=True)
class MergeCoordinatorConfig:
    max_regens_per_node: int = 1
    max_regens_per_op: int = 3
    regen_timeout_s: float = 60.0
```

#### Key Types

```python
@dataclass(frozen=True)
class RegenRequest:
    node_id: str
    conflict_group_id: str
    prior_node_id: str
    prior_patch_context: str    # extracted from prior node's PatchBundle; no git subprocess
    failure_signature: str
    causal_id: str

@dataclass(frozen=True)
class RegenOutcome:
    node_id: str
    result: Literal["CONVERGED", "FAILED_CONFLICT_STALE"]
    worker_result: Optional[WorkerResult]
    duration_ms: float
    regen_index: int             # always 1 in L3-v1

@dataclass(frozen=True)
class MergeResult:
    terminal: Literal["CONVERGED", "PARTIAL", "ABORTED"]
    converged_patches: Mapping[str, PatchBundle]
    failed_nodes: frozenset[str]
    regen_outcomes: tuple[RegenOutcome, ...]
    total_regens: int
    kill_reason: Optional[str]
    summary: Mapping[str, Any]
```

#### Regen Protocol

For each `CONFLICT_STALE` node, processed in `ConflictGroup.serialization_order`:

1. **Budget gate (all 3 must pass):** `regen_count_for_node < max_regens_per_node` AND `total_regens < max_regens_per_op` AND `remaining_time > regen_timeout_s`.
2. **On gate failure:** node → `FAILED_CONFLICT_STALE`. Emit `merge.regen.budget_denied.v1` with which gate failed.
3. **On budget pass:** Build `RegenRequest` using `prior_worker_result.worktree_path` extracted diff. Call `provider._build_conflict_regen_prompt()`. Spawn new `WorkerSandbox(source=inherited from prior)`. Run validation.
4. **CONVERGED:** add to `converged_patches`, increment counters.
5. **FAILED:** `FAILED_CONFLICT_STALE` terminal for this node.

#### Regen Prompt Extension

`PrimeProvider._build_conflict_regen_prompt(node, regen_request)` — injects `conflict_regen_context` block into existing `_build_codegen_prompt()`. Uses schema `2d.1-regen` sub-variant (same response shape as `2b.1`/`2b.1-diff`). No new J-Prime endpoint needed.

---

## 5. J-Prime Schema: `2d.1`

J-Prime emits `2d.1` for COMPLEX / multi-file operations when routed to a `dag_capable` brain (32B+).

```json
{
  "schema_version": "2d.1",
  "dag": {
    "nodes": [
      {
        "node_id": "<stable-hash>",
        "repo": "jarvis",
        "bundle_files": ["backend/core/foo.py", "tests/test_foo.py"],
        "intent_summary": "Add retry budget enforcement to RepairEngine",
        "estimated_risk": "MODERATE",
        "depends_on": [],
        "apply_mode": "atomic",
        "priority": 0,
        "patch": {
          "schema_version": "2b.1",
          "candidates": [...]
        }
      }
    ]
  },
  "provider_metadata": {
    "model_id": "qwen-2.5-32b",
    "brain_id": "qwen_coder_32b",
    "generation_latency_ms": 1240
  }
}
```

JARVIS calls `TaskDAG.build()` on this response. `node_id` from J-Prime is treated as a hint; JARVIS re-derives the canonical `node_id` via `sha256(op_id + repo + sorted_bundle_files)[:16]`.

---

## 6. Failure Taxonomy (L3-Specific)

| Code | Layer | Terminal state |
|---|---|---|
| `DAG_EMPTY` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_CYCLE` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_UNKNOWN_REPO` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_EMPTY_BUNDLE` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_DUPLICATE_BUNDLE_FILE` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_INVALID_PRIORITY` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_DANGLING_DEP` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_CROSS_REPO_EDGE_DENIED` | TaskDAG | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_CAPABLE_FLAG_UNSET` | Config | `SCHED_ABORTED` → L1/L2 fallback |
| `SCHED_BUDGET_EXHAUSTED` | Scheduler | `SCHED_ABORTED` |
| `SCHED_CONFIDENCE_COLLAPSE` | Scheduler | `SCHED_ABORTED` |
| `SCHED_SEMAPHORE_STARVATION` | Scheduler | node `NODE_FAILED` |
| `SCHED_EXTERNAL_CANCEL` | Scheduler | `SCHED_ABORTED` |
| `SCHED_FATAL_INFRA` | Scheduler | `SCHED_ABORTED` |
| `NODE_BLOCKED` | Scheduler | `NODE_BLOCKED` (silent; logged) |
| `WORKER_PATCH_APPLY_FAILED` | WorkerSandbox | node `NODE_FAILED`, class=`SYNTAX` |
| `WORKER_INFRA_ERROR` | WorkerSandbox | node `NODE_FAILED` |
| `NODE_FAILED_CONFLICT_STALE` | MergeCoordinator | node `NODE_FAILED` → `SCHED_PARTIAL` |
| `MERGE_REGEN_OP_CAP` | MergeCoordinator | regen denied; node `NODE_FAILED` |
| `MERGE_DEADLINE_DURING_REGEN` | MergeCoordinator | `SCHED_ABORTED` |
| `SCHED_PARTIAL` | MergeCoordinator | no apply; governance escalation |

L3 inherits L2 failure classes: `SYNTAX`, `TEST`, `FLAKE`, `ENV`.

---

## 7. Telemetry Events

All events carry base fields from umbrella Section 6 (`event_id`, `kind`, `op_id`, `causal_id`, `timestamp_ns`, `phase="L3"`, `component`, `decision_reason`).

L3-specific additional fields on every event: `dag_id`, `dag_digest`, `node_id` (where applicable), `conflict_key` (where applicable).

| Event kind | Emitted when |
|---|---|
| `sched.started.v1` | SCHED_INIT → SCHED_VALIDATING |
| `sched.node.queued.v1` | node enters NODE_QUEUED |
| `sched.node.dispatched.v1` | worker subprocess launched |
| `sched.node.regen.v1` | CONFLICT_STALE regen requested |
| `sched.node.converged.v1` | node → NODE_CONVERGED |
| `sched.node.failed.v1` | node → NODE_FAILED + `kill_reason` |
| `sched.node.blocked.v1` | node → NODE_BLOCKED + `blocked_by` |
| `sched.converged.v1` | SCHED_CONVERGED terminal |
| `sched.partial.v1` | SCHED_PARTIAL terminal + outcome map |
| `sched.aborted.v1` | SCHED_ABORTED + `kill_reason` |
| `worker.started.v1` | WorkerSandbox.run() begins |
| `worker.patch_applied.v1` | patch bundle applied to worktree |
| `worker.completed.v1` | WorkerSandbox terminal state |
| `worker.sandbox.cleaned.v1` | worktree removed |
| `merge.started.v1` | MergeCoordinator.run() begins |
| `merge.regen.requested.v1` | regen attempt started |
| `merge.regen.completed.v1` | regen attempt finished |
| `merge.regen.budget_denied.v1` | budget gate blocked regen |
| `merge.completed.v1` | MergeCoordinator terminal |

---

## 8. Determinism Proof Points

| Claim | Mechanism |
|---|---|
| Same DAG → same dispatch order | `DAG.ready_nodes()` stable sort `(priority, repo, node_id)` |
| Same DAG → same conflict groups | Pre-computed in `TaskDAG.build()`, never re-derived |
| Same conflict → same serialization | `ConflictGroup.serialization_order` stable sort at build time |
| Same failures → same regen decisions | Budget gates are threshold-based, deadline uses passed-in monotonic value |
| Same inputs → same terminal state | No wall-clock in decision paths; `asyncio.Semaphore` FIFO |
| Full replay from ledger | `dag.digest()` + ordered event chain = complete replay key |

---

## 9. Configuration (Environment Variables)

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_L3_ENABLED` | `false` | Master switch |
| `JARVIS_L3_DAG_CAPABLE` | `false` | Brain selector tags dag_capable |
| `JARVIS_L3_MAX_PARALLEL_NODES` | `4` | Global semaphore cap |
| `JARVIS_L3_MAX_NODES_PER_REPO` | `2` | Per-repo semaphore cap |
| `JARVIS_L3_MAX_DAG_NODES` | `8` | Max nodes per DAG |
| `JARVIS_L3_NODE_TIMEBOX_S` | `120.0` | Per-node wall-clock limit |
| `JARVIS_L3_TOTAL_TIMEBOX_S` | `600.0` | Op-level deadline |
| `JARVIS_L3_SEMAPHORE_ACQUIRE_TIMEOUT_S` | `30.0` | Semaphore wait cap |
| `JARVIS_L3_MAX_CONFLICT_REGENS_PER_NODE` | `1` | Hard cap per node |
| `JARVIS_L3_MAX_TOTAL_REGENS_PER_OP` | `3` | Hard cap per op |
| `JARVIS_L3_REGEN_TIMEOUT_S` | `60.0` | Per-regen time budget |
| `JARVIS_L3_WORKER_TEST_TIMEOUT_S` | `60.0` | Per-worker test timeout |
| `JARVIS_L3_CONFIDENCE_COLLAPSE_THRESHOLD` | `0.5` | Failed fraction → ABORTED |

All values frozen to `SchedulerConfig` / `MergeCoordinatorConfig` at operation intake.

---

## 10. Benchmark Suite + SLO Gates

### Test Files

| File | Covers |
|---|---|
| `tests/l3/test_task_dag.py` | All DAG_PREFLIGHT codes, cycle detection, cross-repo edge deny, normalization |
| `tests/l3/test_scheduler_fsm.py` | All state transitions, kill conditions, semaphore ordering |
| `tests/l3/test_worker_sandbox.py` | Inheritance fork, CONFLICT_STALE detection, cancel cleanup |
| `tests/l3/test_merge_coordinator.py` | Regen budget gates, FAILED_CONFLICT_STALE, ordering |
| `tests/l3/test_l3_determinism.py` | Same DAG input → same decisions × 50 runs |
| `tests/l3/test_l3_throughput.py` | Wall-clock vs sequential baseline |
| `tests/l3/test_l3_cleanup.py` | Zero orphaned worktrees on cancel/timeout × 20 |
| `tests/l3/test_l3_integration.py` | End-to-end: 3-node DAG, 1 conflict group, full pipeline |

### Hard Gates

| Gate | Measurement | Pass threshold |
|---|---|---|
| Wall-clock reduction | `L3_time / sequential_time` | ≤ 0.70 |
| Mainline regression | broken-mainline rate delta vs L0 | 0 |
| Causal trace completeness | events missing `causal_id` | 0 |
| Determinism | identical results across 50 runs | 100% |
| Cleanup correctness | orphaned worktrees after cancel × 20 runs | 0 |
| CONFLICT_STALE false positive rate | false positives on independent nodes | 0 |

---

## 11. Rollback Plan

- **Single env var:** `JARVIS_L3_ENABLED=false` → immediate L1/L2 fallback. No restart required.
- **Schema dispatch guard:** non-`2d.1` schemas never reach L3 code paths.
- **Worktree isolation:** all L3 worktrees under `.claude/worktrees/l3-*/`. Safe to `rm -rf` independently.
- **No ledger schema changes:** L3 events are additive new `kind` values; existing queries unaffected.
- **No DB migrations.**

---

## 12. Migration Plan

### Phase 1: Shadow Mode
```
JARVIS_L3_ENABLED=true
JARVIS_L3_DAG_CAPABLE=false
```
All L3 code paths active and exercised in tests. No DAGs emitted by brain selector. Zero behavioral change in production.

### Phase 2: Single-Repo Test Ops
```
JARVIS_L3_DAG_CAPABLE=true
# brain_selection_policy.yaml: tag qwen_coder_32b as dag_capable: true for TEST complexity tier
```
First live DAGs on test operations. Monitor telemetry. Verify determinism and cleanup gates.

### Phase 3: Multi-Repo COMPLEX Ops
```
# brain_selection_policy.yaml: tag all 32B+ brains as dag_capable: true for COMPLEX tier
```
Full L3 surface enabled. L4 activation allowed after SLO gates pass.

### Backward Compatibility
- `brain_selection_policy.yaml`: new optional `dag_capable` field; absent = `false`.
- `BrainSelectionResult`: new `dag_capable: bool` field; defaults `false`.
- No breaking changes to any existing API surface.

---

## 13. Advanced Risks

| Risk | Mitigation |
|---|---|
| **DAG race collapse:** two nodes that appear independent both modify a shared transitive import, causing runtime failure not detectable by file-set intersection | L3-v1 accepts this; file-set intersection catches direct conflicts. Hunk-aware detection is L3-v1.1+. |
| **Deterministic merge conflict canonicalization:** two nodes produce valid patches that together cause a conflict only detectable post-merge | Sequential inheritance with `CONFLICT_STALE` detection catches this at validation time. Regen with upstream context is the fix. |
| **Semaphore starvation under high conflict density:** all nodes in conflict groups serialize, eliminating parallelism benefit | `SCHED_SEMAPHORE_STARVATION` kill condition + `CONFIDENCE_COLLAPSE` threshold prevent indefinite blocking. Telemetry surfaces the pattern for policy tuning. |
| **Orphaned worktrees on SIGKILL:** parent process killed before cleanup runs | Startup sweep: on L3 init, scan `worktree_base_dir` for stale `l3-*` dirs older than `total_timebox_s` and clean them. |
| **J-Prime emits malformed `2d.1`:** DAG with cycle or invalid node_ids | `TaskDAG.validate()` catches all cases in preflight; hard-fallback to L1/L2 with reason code. |
