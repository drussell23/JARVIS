# L3 Execution Spec: Parallel Subagent Execution

**Date:** 2026-03-12
**Status:** Approved — v2 (post spec review)
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
- [ ] `JARVIS_L3_ENABLED=false` by default; no L3 code path reachable from live ops without opt-in
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
- **Worktree inheritance rule:** nodes with `depends_on` relationships use `source.kind="inherited"` from their upstream node's worktree (when `retain_on_converge=True`). Nodes with no `depends_on` (root nodes) always use `source.kind="HEAD"`. All conflict group members inherit from the prior node in their serialization order.

### 3.2 Pipeline Integration

L3 activates at GENERATE phase when J-Prime returns schema `2d.1` AND `dag_capable=true` in config. **Phase 1 (shadow mode):** L3 code paths are importable but only invoked from test code; no live op request paths reach L3. **Phase 2+:** live DAGs emitted when `JARVIS_L3_DAG_CAPABLE=true`.

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
    + reason_code  [workers settle; SCHED_MERGING]
                           │
                  MergeCoordinator.run()
                           │
          ┌────────────────┼──────────────────┐
    CONVERGED          PARTIAL             ABORTED
          ↓                ↓                   ↓
  canonical VALIDATE  SCHED_PARTIAL       CANCELLED
  → GATE → APPLY      (no auto-apply;     + reason_code
  → VERIFY → COMPLETE  escalate to GATE)
```

When `MergeCoordinator` returns `terminal="ABORTED"`, the scheduler transitions to `SCHED_ABORTED` and emits `sched.aborted.v1`. The scheduler owns all `SCHED_*` terminal state emissions; `MergeCoordinator` only returns a `MergeResult` — it does not emit scheduler-level events.

### 3.3 New Files

| File | Role |
|---|---|
| `backend/core/ouroboros/governance/patch_bundle.py` | `PatchBundle`, `FilePatch` (canonical definition; see umbrella Section 5.0) |
| `backend/core/ouroboros/governance/task_dag.py` | TaskDAG, DAGNode, ConflictGroup, validation, helpers |
| `backend/core/ouroboros/governance/subagent_scheduler.py` | Scheduler FSM, semaphores, node lifecycle |
| `backend/core/ouroboros/governance/worker_sandbox.py` | WorkerSandbox extending L2 primitives |
| `backend/core/ouroboros/governance/merge_coordinator.py` | Sequential barrier + regen protocol |

### 3.4 Modified Files

| File | Change |
|---|---|
| `backend/core/ouroboros/governance/orchestrator.py` | Schema dispatch at GENERATE; L3 hook; MergeResult→SCHED_ABORTED propagation |
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
    schema_version_emitted: str          # must be "2d.1"; populated from initial response only.
                                         # Regen responses (2d.1-regen) do NOT produce a new
                                         # DAGGenerationMetadata — they produce a WorkerResult.
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None

@dataclass(frozen=True)
class DAGNode:
    node_id: str           # sha256(op_id + "\x00" + repo + "\x00" + "\x00".join(sorted_bundle_files))[:16]
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
    serialization_order: tuple[str, ...]   # stable: sorted by (priority, repo, node_id)

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
    dag_id: str            # sha256(op_id + "\x00" + "\x00".join(sorted(node_ids)))[:16]
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

#### Scheduler FSM States + Transitions

```
SCHED_INIT
  → SCHED_VALIDATING     (DAG.validate() + dag_capable check)
      ├─ validation fails  → SCHED_ABORTED (kill: DAG_PREFLIGHT_REJECTED) + L1/L2 fallback
      └─ validation passes → SCHED_RUNNING (emits sched.dag_validated.v1)

SCHED_RUNNING             (dispatching ready nodes, tracking in-flight)
  → SCHED_DRAINING        (fires when ready_nodes() returns empty AND in-flight count > 0;
                           emits sched.draining.v1)
  → SCHED_MERGING         (fires when ready_nodes() empty AND in-flight count == 0;
                           all workers have settled without a draining phase)

SCHED_DRAINING            (all ready nodes dispatched, awaiting final worker completions)
  → SCHED_MERGING         (last in-flight worker settles)

SCHED_MERGING             (MergeCoordinator.run() executing)
  → SCHED_CONVERGED       (MergeResult.terminal == "CONVERGED")
  → SCHED_PARTIAL         (MergeResult.terminal == "PARTIAL")
  → SCHED_ABORTED         (MergeResult.terminal == "ABORTED" OR any kill condition fires)

Terminal:
  SCHED_CONVERGED          (all nodes NODE_CONVERGED; proceed to canonical VALIDATE)
  SCHED_PARTIAL            (≥1 node failed; no auto-apply; escalate to governance)
  SCHED_ABORTED            (kill condition or external cancel or MergeCoordinator ABORTED)
```

**`SCHED_PARTIAL` is a terminal FSM state, not a failure reason code.** It is reached when `MergeResult.terminal == "PARTIAL"`. The scheduler emits `sched.partial.v1` with the outcome map. No kill condition produces `SCHED_PARTIAL` — it is a graceful degradation path.

#### Node FSM States

```
NODE_PENDING      (waiting for depends_on to complete)
  → NODE_BLOCKED  (dependency entered NODE_FAILED; propagated via _failure_closure)
  → NODE_QUEUED   (deps satisfied; awaiting semaphore)

NODE_QUEUED
  → NODE_RUNNING  (semaphore acquired; WorkerSandbox dispatched)

NODE_RUNNING      (WorkerSandbox.run() executing in subprocess sandbox)
  [NOTE: NODE_RUNNING → NODE_VALIDATING is an INTERNAL state within WorkerSandbox.run()
   representing test execution underway. It is NOT the orchestrator's canonical VALIDATE phase.
   From the scheduler's perspective, the node is RUNNING until WorkerSandbox returns a WorkerResult.]

  → NODE_REGEN_PENDING  (WorkerSandbox returns WorkerResult with failure_class=CONFLICT_STALE;
                         scheduler hands off to MergeCoordinator for regen decision;
                         emits sched.node.regen.v1 for NODE_RUNNING → NODE_REGEN_PENDING)
  → NODE_CONVERGED      (WorkerSandbox returns status=CONVERGED)
  → NODE_FAILED         (WorkerSandbox returns status=FAILED, INFRA_ERROR, or CANCELLED)

NODE_REGEN_PENDING → NODE_CONVERGED | NODE_FAILED  (after MergeCoordinator regen attempt)

Any in-flight  → NODE_CANCELLED  (external cancel received)
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
    oscillation_max_same_sig: int = 2        # consecutive same failure_signature → OSCILLATION
```

All values configurable via env vars (see Section 9). Frozen at operation intake.

#### Kill Conditions

| Code | Trigger | Outcome |
|---|---|---|
| `DAG_PREFLIGHT_REJECTED` | `DAG.validate()` fails | `SCHED_ABORTED` → L1/L2 fallback |
| `DAG_CAPABLE_FLAG_UNSET` | `dag_capable=false` | `SCHED_ABORTED` → L1/L2 fallback |
| `SCHED_BUDGET_EXHAUSTED` | wall-clock > `total_timebox_s` | `SCHED_ABORTED` |
| `SCHED_CONFIDENCE_COLLAPSE` | `failed/total >= confidence_collapse_threshold` | `SCHED_ABORTED` |
| `SCHED_OSCILLATION` | same `(node_id, failure_signature)` pair seen `oscillation_max_same_sig` times | node → `NODE_FAILED` |
| `SCHED_SEMAPHORE_STARVATION` | semaphore not acquired within `semaphore_acquire_timeout_s` | node → `NODE_FAILED` |
| `SCHED_EXTERNAL_CANCEL` | `asyncio.CancelledError` | `SCHED_ABORTED` + kill all |
| `SCHED_FATAL_INFRA` | unrecoverable subprocess error | `SCHED_ABORTED` |
| `POLICY_DENY` | GATE rejects | `SCHED_ABORTED` |

#### Cancellation Protocol

1. Stop dispatching new nodes immediately.
2. For each `NODE_RUNNING`/`NODE_REGEN_PENDING`: `SIGTERM` → `wait_for(proc.wait(), 5s)` → `SIGKILL`.
3. `asyncio.shield(worktree.cleanup())` — cleanup completes even if parent cancelled.
4. Release all semaphores.
5. Emit `sched.aborted.v1`.

#### Dispatch Ordering (Determinism)

`DAG.ready_nodes()` is the single dispatch source. Sort key: `(priority, repo, node_id)`. `asyncio.Semaphore` is FIFO (CPython). No scheduler-side reordering. All budget gates are count-based (not deadline-based in decision paths) — see Section 8 determinism proof.

#### Startup Sweep (Orphan Recovery)

`SubagentScheduler.__init__()` scans `WorkerSandboxConfig.worktree_base_dir` for stale `l3-*` directories with `mtime > total_timebox_s` seconds ago and removes them. This prevents orphaned worktrees accumulating from prior SIGKILL events.

---

### 4.3 WorkerSandbox (`worker_sandbox.py`)

Extends L2 `repair_sandbox.py` primitives. New capability: worktree inheritance.

#### Type Dependencies

- `TestRunResult`: imported from `backend/core/ouroboros/governance/tool_executor.py` (L1 type). Carries `passed: bool`, `stdout: str`, `stderr: str`, `exit_code: int`, `duration_ms: float`.
- `PatchBundle`: imported from `backend/core/ouroboros/governance/patch_bundle.py` (see umbrella Section 5.0).
- `FailureClassifier`: extended in `failure_classifier.py` with `CONFLICT_STALE` class.

#### Worktree Inheritance

```python
@dataclass(frozen=True)
class WorktreeSource:
    kind: Literal["HEAD", "inherited"]
    inherited_from_node_id: Optional[str] = None
    inherited_path: Optional[Path] = None
    # HEAD:      git worktree add --detach <path> HEAD
    #            Used for: root nodes (no depends_on)
    # inherited: rsync -a <inherited_path>/ <new_path>/
    #            Used for: nodes with depends_on, or conflict group serialized followers
    #            L3-v1 uses rsync — no temp commits, no git history noise
```

**Inheritance assignment rule:** JARVIS's scheduler assigns `WorktreeSource` at dispatch time:
- Node has `depends_on == frozenset()`: `source.kind = "HEAD"`
- Node has `depends_on != frozenset()` AND upstream node converged with `worktree_path` retained: `source.kind = "inherited"`, `inherited_from_node_id = upstream_node_id`, `inherited_path = upstream_worktree_ref()`
- Conflict group follower (not root of group): `source.kind = "inherited"` from prior in `serialization_order`

#### CONFLICT_STALE Detection

Extends `FailureClass` enum (from L2 `failure_classifier.py`):

```python
CONFLICT_STALE = "conflict_stale"
```

**Classification logic** (added to `FailureClassifier`):

1. Pre-condition: `source.kind == "inherited"` (CONFLICT_STALE is never raised for `HEAD` nodes).
2. `FailureClassifier` runs its existing SYNTAX/TEST/FLAKE/ENV detection on stdout/stderr.
3. If class would be `SYNTAX` or `TEST`, additionally check whether any error message references a symbol, path, or line number from the prior node's `bundle_files` (available via `WorktreeSource.inherited_from_node_id` → `DAGNode.bundle_files`).
4. If match found: override class to `CONFLICT_STALE`.
5. If no match: keep original class (SYNTAX or TEST — not all failures on inherited nodes are conflict-induced).

This heuristic is conservative: it only promotes to `CONFLICT_STALE` when the error has a traceable link to upstream changes. False negatives (missed conflict detection) fall through to normal SYNTAX/TEST handling.

#### Config + Result

```python
@dataclass(frozen=True)
class WorkerSandboxConfig:
    worktree_base_dir: Path = field(default=Path(".claude/worktrees/l3"))
    # Override with JARVIS_L3_WORKTREE_BASE_DIR env var
    test_timeout_s: float = 60.0
    patch_apply_timeout_s: float = 10.0
    cleanup_timeout_s: float = 10.0
    retain_on_converge: bool = True    # retain path for downstream inheritance

@dataclass(frozen=True)
class WorkerResult:
    node_id: str
    status: Literal["CONVERGED", "FAILED", "CANCELLED", "INFRA_ERROR"]
    patch_applied: bool
    test_result: Optional[TestRunResult]   # from tool_executor.TestRunResult
    failure_class: Optional[FailureClass]
    failure_signature: Optional[str]   # sha256(failure_class + error_message)
    worktree_path: Optional[Path]      # non-None if retained (CONVERGED + retain_on_converge)
    source: WorktreeSource
    duration_ms: float
    causal_id: str
```

#### Interface

```python
class WorkerSandbox:
    async def run(self, patch: PatchBundle) -> WorkerResult:
        # Internal phases within this call:
        # 1. Create worktree from source (git worktree add OR rsync)
        # 2. Apply patch bundle atomically (all files or none)
        # 3. [NODE_VALIDATING internal state] Run test subprocess (asyncio.create_subprocess_exec)
        #    with test_timeout_s; this is NOT the orchestrator's canonical VALIDATE phase
        # 4. Classify failure if not passing (CONFLICT_STALE only if source.kind="inherited")
        # 5. If CONVERGED + retain_on_converge: retain worktree, set worktree_path
        # 6. If FAILED/CANCELLED: cleanup immediately
        # 7. Report WorkerResult to scheduler (scheduler emits ledger events)
    async def cancel(self) -> None: ...          # SIGTERM → wait(5s) → SIGKILL → shield(cleanup)
    async def cleanup(self) -> None: ...         # git worktree remove || rm -rf
    def worktree_ref(self) -> Optional[Path]: ...  # for downstream inheritance
```

#### Hard Invariants

1. No write to real working tree — ever.
2. Atomic patch apply — all files or none.
3. Cleanup on all terminal states — `asyncio.shield()` guards cleanup.
4. `CONFLICT_STALE` only on `inherited` source.
5. Subprocess via `asyncio.create_subprocess_exec` — never `subprocess.run(check=True)` in thread executor.

#### Reuse from L2

| Primitive | Source | Action |
|---|---|---|
| Git worktree create/remove | `repair_sandbox.py` | Reused directly |
| rsync fallback | `repair_sandbox.py` | Reused; also used for inheritance fork |
| `TestRunResult` | `tool_executor.py` | Reused unchanged |
| `FailureClassifier` | `failure_classifier.py` | Extended with `CONFLICT_STALE` class + detection logic |
| `asyncio.create_subprocess_exec` test runner | `repair_sandbox.py` | Reused |

---

### 4.4 MergeCoordinator (`merge_coordinator.py`)

Runs after scheduler settles (`SCHED_MERGING`). Owns A→B regen fallback and final patch assembly. Returns `MergeResult` to scheduler; scheduler owns all `SCHED_*` state emissions.

#### Config

```python
@dataclass(frozen=True)
class MergeCoordinatorConfig:
    max_regens_per_node: int = 1       # hard cap per node; regen_index ranges 1..max_regens_per_node
    max_regens_per_op: int = 3         # hard cap across entire op
    # NOTE: no deadline-based gate — regen budget is purely count-based to preserve determinism.
    # Deadline enforcement is the scheduler's responsibility via total_timebox_s.
```

#### Key Types

```python
@dataclass(frozen=True)
class RegenRequest:
    node_id: str
    conflict_group_id: str
    prior_node_id: str
    prior_patch_context: str    # extracted from prior node's PatchBundle; no git subprocess needed
    failure_signature: str
    causal_id: str

@dataclass(frozen=True)
class RegenOutcome:
    node_id: str
    result: Literal["CONVERGED", "FAILED_CONFLICT_STALE"]
    worker_result: Optional[WorkerResult]
    duration_ms: float
    regen_index: int             # 1-indexed attempt counter within this op (1..max_regens_per_node)

@dataclass(frozen=True)
class MergeResult:
    terminal: Literal["CONVERGED", "PARTIAL", "ABORTED"]
    converged_patches: Mapping[str, PatchBundle]   # node_id → final patch for all CONVERGED nodes
    failed_nodes: frozenset[str]
    regen_outcomes: tuple[RegenOutcome, ...]
    total_regens: int
    kill_reason: Optional[str]   # set when terminal == "ABORTED"
    summary: Mapping[str, Any]
```

#### Regen Protocol

For each `CONFLICT_STALE` node, processed in `ConflictGroup.serialization_order`:

1. **Budget gate (2 count-based checks, both must pass):**
   - `regen_count_for_node < max_regens_per_node`
   - `total_regens_this_op < max_regens_per_op`
   - (No deadline check — deadline enforcement is the scheduler's timebox, not the coordinator's)
2. **On gate failure:** node → `FAILED_CONFLICT_STALE`. Emit `merge.regen.budget_denied.v1` with which gate failed (`per_node` or `per_op`).
3. **On budget pass:** Build `RegenRequest` using prior node's `PatchBundle` content (available from `WorkerResult.worktree_path`; extract via file read — no `git diff` subprocess). Call `provider._build_conflict_regen_prompt()`. Spawn new `WorkerSandbox(source=inherited from prior)`. Run validation.
4. **CONVERGED:** add to `converged_patches`, increment `total_regens`.
5. **FAILED:** `FAILED_CONFLICT_STALE` terminal for this node.
6. **OSCILLATION:** if `failure_signature` from regen attempt matches prior attempt for same node → override terminal to `SCHED_OSCILLATION` kill.

#### Regen Prompt Extension

`PrimeProvider._build_conflict_regen_prompt(node, regen_request)` — injects `conflict_regen_context` block into existing `_build_codegen_prompt()`. Uses schema `2d.1-regen` sub-variant (same response shape as `2b.1`/`2b.1-diff`). No new J-Prime endpoint needed. `DAGGenerationMetadata` is NOT updated for regen responses.

---

## 5. J-Prime Schema: `2d.1`

J-Prime emits `2d.1` for COMPLEX / multi-file operations when routed to a `dag_capable` brain (32B+).

```json
{
  "schema_version": "2d.1",
  "dag": {
    "nodes": [
      {
        "node_id": "<hint-hash>",
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

JARVIS calls `TaskDAG.build()` on this response. `node_id` from J-Prime is treated as a hint; JARVIS re-derives the canonical `node_id` via `sha256(op_id + "\x00" + repo + "\x00" + "\x00".join(sorted_bundle_files))[:16]`. `patch` wraps a `PatchBundle` for that node's `repo`.

---

## 6. Failure Taxonomy (L3-Specific)

`SCHED_PARTIAL` is a **terminal scheduler FSM state**, not a failure code. It is reached when `MergeResult.terminal == "PARTIAL"` — indicating graceful degradation (some nodes failed, independent branches succeeded). It does not appear as a `kill_reason`.

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
| `SCHED_OSCILLATION` | Scheduler | node `NODE_FAILED` (same fail_sig repeated) |
| `SCHED_SEMAPHORE_STARVATION` | Scheduler | node `NODE_FAILED` |
| `SCHED_EXTERNAL_CANCEL` | Scheduler | `SCHED_ABORTED` |
| `SCHED_FATAL_INFRA` | Scheduler | `SCHED_ABORTED` |
| `NODE_BLOCKED` | Scheduler | `NODE_BLOCKED` (emits sched.node.blocked.v1; no abort) |
| `WORKER_PATCH_APPLY_FAILED` | WorkerSandbox | node `NODE_FAILED`, class=`SYNTAX` |
| `WORKER_INFRA_ERROR` | WorkerSandbox | node `NODE_FAILED` |
| `NODE_FAILED_CONFLICT_STALE` | MergeCoordinator | node `NODE_FAILED`; DAG → `SCHED_PARTIAL` if any branches survived |
| `MERGE_REGEN_OP_CAP` | MergeCoordinator | regen count-gate denied; node `NODE_FAILED` |
| `MERGE_ABORTED` | MergeCoordinator | MergeResult.terminal=="ABORTED" → `SCHED_ABORTED` |

L3 inherits L2 failure classes: `SYNTAX`, `TEST`, `FLAKE`, `ENV`. Adds: `CONFLICT_STALE`.

---

## 7. Telemetry Events

All events carry base fields from umbrella Section 6 (`event_id`, `kind`, `op_id`, `causal_id`, `parent_event_id`, `timestamp_ns`, `phase="L3"`, `component`, `decision_reason`).

L3-specific additional fields on every event: `dag_id`, `dag_digest`, `node_id` (where applicable), `conflict_key` (where applicable).

All events emitted by scheduler/coordinator on behalf of workers — workers do NOT write to ledger directly (umbrella G7 + single-authority rule).

| Event kind | Emitted when |
|---|---|
| `sched.started.v1` | `SCHED_INIT → SCHED_VALIDATING` |
| `sched.dag_validated.v1` | `SCHED_VALIDATING → SCHED_RUNNING` (validation passed; carries node_count, conflict_group_count) |
| `sched.node.queued.v1` | node enters `NODE_QUEUED` |
| `sched.node.dispatched.v1` | worker subprocess launched; node → `NODE_RUNNING` |
| `sched.node.regen.v1` | `NODE_RUNNING → NODE_REGEN_PENDING` (CONFLICT_STALE detected; regen pending) |
| `sched.node.converged.v1` | node → `NODE_CONVERGED` |
| `sched.node.failed.v1` | node → `NODE_FAILED` + `kill_reason` |
| `sched.node.blocked.v1` | node → `NODE_BLOCKED` + `blocked_by` node_id |
| `sched.draining.v1` | `SCHED_RUNNING → SCHED_DRAINING` (ready_nodes() empty, in-flight > 0) |
| `sched.converged.v1` | `SCHED_CONVERGED` terminal |
| `sched.partial.v1` | `SCHED_PARTIAL` terminal + outcome map |
| `sched.aborted.v1` | `SCHED_ABORTED` terminal + `kill_reason` (emitted by scheduler for all abort paths including MergeCoordinator ABORTED) |
| `worker.started.v1` | `WorkerSandbox.run()` begins |
| `worker.patch_applied.v1` | patch bundle applied to worktree |
| `worker.completed.v1` | `WorkerSandbox` terminal state |
| `worker.sandbox.cleaned.v1` | worktree removed |
| `merge.started.v1` | `MergeCoordinator.run()` begins |
| `merge.regen.requested.v1` | regen attempt started; `NODE_REGEN_PENDING → (running regen)` |
| `merge.regen.completed.v1` | regen attempt finished |
| `merge.regen.budget_denied.v1` | count-gate blocked regen (`per_node` or `per_op`) |
| `merge.completed.v1` | `MergeCoordinator` terminal |

---

## 8. Determinism Proof Points

| Claim | Mechanism |
|---|---|
| Same DAG → same dispatch order | `DAG.ready_nodes()` stable sort `(priority, repo, node_id)` |
| Same DAG → same conflict groups | Pre-computed in `TaskDAG.build()`, never re-derived |
| Same conflict → same serialization | `ConflictGroup.serialization_order` stable sort at build time |
| Same failures → same regen decisions | Regen budget gates are **count-based only** (not deadline-based); same failure counts → same allow/deny decisions |
| Same inputs → same terminal state | No wall-clock in decision paths; `asyncio.Semaphore` FIFO; count-based budgets |
| Full replay from ledger | `dag.digest()` + ordered event chain = complete replay key |

**Deadline and determinism:** `total_timebox_s` can cause `SCHED_BUDGET_EXHAUSTED` if workers run slowly. This is the one non-deterministic kill condition (timing-dependent). The determinism SLO (50-run gate) is measured on controlled benchmark inputs where workers complete well within the deadline. Production ops under load may hit deadline variance; this is logged but does not invalidate the determinism property for the benchmark gate.

---

## 9. Configuration (Environment Variables)

| Variable | Default | Meaning |
|---|---|---|
| `JARVIS_L3_ENABLED` | `false` | Master switch |
| `JARVIS_L3_DAG_CAPABLE` | `false` | Overrides per-brain YAML `dag_capable` field. `true` = all 32B+ brains tagged dag_capable. `false` = defer to per-brain YAML entry (default `false` in YAML). |
| `JARVIS_L3_MAX_PARALLEL_NODES` | `4` | Global semaphore cap |
| `JARVIS_L3_MAX_NODES_PER_REPO` | `2` | Per-repo semaphore cap |
| `JARVIS_L3_MAX_DAG_NODES` | `8` | Max nodes per DAG |
| `JARVIS_L3_NODE_TIMEBOX_S` | `120.0` | Per-node wall-clock limit |
| `JARVIS_L3_TOTAL_TIMEBOX_S` | `600.0` | Op-level deadline (scheduler-owned) |
| `JARVIS_L3_SEMAPHORE_ACQUIRE_TIMEOUT_S` | `30.0` | Semaphore wait cap |
| `JARVIS_L3_MAX_CONFLICT_REGENS_PER_NODE` | `1` | Hard cap per node |
| `JARVIS_L3_MAX_TOTAL_REGENS_PER_OP` | `3` | Hard cap per op |
| `JARVIS_L3_WORKER_TEST_TIMEOUT_S` | `60.0` | Per-worker test timeout |
| `JARVIS_L3_CONFIDENCE_COLLAPSE_THRESHOLD` | `0.5` | Failed fraction → ABORTED |
| `JARVIS_L3_OSCILLATION_MAX_SAME_SIG` | `2` | Same failure_signature count → OSCILLATION kill |
| `JARVIS_L3_WORKTREE_BASE_DIR` | `.claude/worktrees/l3` | Base dir for all L3 worktrees |

All values frozen to `SchedulerConfig` / `MergeCoordinatorConfig` at operation intake.

---

## 10. Benchmark Suite + SLO Gates

### Test Files

| File | Covers |
|---|---|
| `tests/l3/test_task_dag.py` | All DAG_PREFLIGHT codes, cycle detection, cross-repo edge deny, normalization, dag_id hash |
| `tests/l3/test_scheduler_fsm.py` | All state transitions including RUNNING→DRAINING trigger; kill conditions; semaphore ordering |
| `tests/l3/test_worker_sandbox.py` | Inheritance fork (HEAD and inherited), CONFLICT_STALE detection logic, cancel cleanup, orphan sweep |
| `tests/l3/test_merge_coordinator.py` | Count-based regen budget gates, FAILED_CONFLICT_STALE, deterministic ordering, MergeResult→SCHED_ABORTED propagation |
| `tests/l3/test_l3_determinism.py` | Same DAG input → same decisions × 50 runs |
| `tests/l3/test_l3_throughput.py` | Wall-clock vs sequential baseline |
| `tests/l3/test_l3_cleanup.py` | Zero orphaned worktrees on cancel/timeout × 20; startup sweep |
| `tests/l3/test_l3_integration.py` | End-to-end: 3-node DAG, 1 conflict group, full pipeline through canonical VALIDATE |

### Hard Gates

| Gate | Measurement | Pass threshold |
|---|---|---|
| Wall-clock reduction | `L3_time / sequential_time` on benchmark suite | ≤ 0.70 |
| Mainline regression | broken-mainline rate delta vs L0 | 0 |
| Causal trace completeness | events missing `causal_id` or with null `parent_event_id` (non-root) | 0 |
| Determinism | identical results across 50 benchmark runs (count-based gate paths) | 100% |
| Cleanup correctness | orphaned worktrees after cancel × 20 runs | 0 |
| CONFLICT_STALE false positive rate | false positives on HEAD-source independent nodes | 0 |

---

## 11. Rollback Plan

- **Single env var:** `JARVIS_L3_ENABLED=false` → immediate L1/L2 fallback. No restart required.
- **Schema dispatch guard:** non-`2d.1` schemas never reach L3 code paths.
- **Worktree isolation:** all L3 worktrees under `JARVIS_L3_WORKTREE_BASE_DIR` (default `.claude/worktrees/l3/`). Safe to `rm -rf` independently.
- **No ledger schema changes:** L3 events are additive new `kind` values; existing queries unaffected.
- **No DB migrations.**

---

## 12. Migration Plan

### Phase 1: Shadow Mode
```
JARVIS_L3_ENABLED=true
JARVIS_L3_DAG_CAPABLE=false
```
L3 modules importable; no L3 request paths reachable from live ops. Only test code invokes L3 paths. Zero behavioral change in production. All unit tests run here.

### Phase 2: Single-Repo Test Ops
```
JARVIS_L3_DAG_CAPABLE=true
# brain_selection_policy.yaml: tag qwen_coder_32b as dag_capable: true for TEST complexity tier
```
First live DAGs on test operations. Monitor telemetry. Verify determinism and cleanup SLO gates.

### Phase 3: Multi-Repo COMPLEX Ops
```
# brain_selection_policy.yaml: tag all 32B+ brains as dag_capable: true for COMPLEX tier
```
Full L3 surface enabled. L4 activation allowed after SLO gates pass.

### Backward Compatibility
- `brain_selection_policy.yaml`: new optional `dag_capable` field; absent = `false`. `JARVIS_L3_DAG_CAPABLE=true` overrides all YAML entries to `true`.
- `BrainSelectionResult`: new `dag_capable: bool` field; defaults `false`.
- No breaking changes to any existing API surface.

---

## 13. Advanced Risks

| Risk | Mitigation |
|---|---|
| **DAG race collapse:** two nodes that appear independent both modify a shared transitive import, causing runtime failure not detectable by file-set intersection | L3-v1 accepts this; file-set intersection catches direct conflicts. `CONFLICT_STALE` heuristic may catch some transitive cases. Hunk-aware detection is L3-v1.1+. |
| **Deterministic merge conflict canonicalization:** two nodes produce valid patches that together cause a conflict only detectable post-merge | Sequential inheritance with `CONFLICT_STALE` detection catches this at validation time. Regen with upstream context resolves. |
| **Semaphore starvation under high conflict density:** all nodes in conflict groups serialize, eliminating parallelism benefit | `SCHED_SEMAPHORE_STARVATION` kill + `SCHED_CONFIDENCE_COLLAPSE` threshold prevent indefinite blocking. Telemetry surfaces the pattern for policy tuning. |
| **Orphaned worktrees on SIGKILL:** parent process killed before cleanup runs | Startup sweep in `SubagentScheduler.__init__()` removes stale dirs older than `total_timebox_s`. |
| **J-Prime emits malformed `2d.1`:** DAG with cycle or invalid node_ids | `TaskDAG.validate()` catches all cases in preflight; hard-fallback to L1/L2 with reason code. |
| **CONFLICT_STALE false negative:** inherited node fails for conflict reason but heuristic misses | Falls through to SYNTAX/TEST handling; regen is not attempted. Worst case: node fails normally → `SCHED_PARTIAL`. No correctness violation; throughput impact only. |
