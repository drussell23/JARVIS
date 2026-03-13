# L3 Parallel Subagent Execution Plan

**Date:** 2026-03-13  
**Status:** Proposed  
**Scope:** JARVIS control plane, J-Prime planner, Reactor telemetry

## Goal

Implement `L3` as governed, deterministic parallel subagent execution across `jarvis`, `jarvis-prime`, and `reactor-core` without bypassing the existing approval, saga, and supervisor authority chain.

This is **not** a new autonomy layer and **not** an alternative startup path. The authoritative entrypoint remains:

```bash
python3 unified_supervisor.py
```

The implementation must preserve these invariants:

- `unified_supervisor.py` is the only lifecycle authority.
- `GovernedLoopService` remains the runtime owner of autonomy execution.
- `GovernedOrchestrator` remains the only write-authorizing orchestrator.
- `SagaApplyStrategy` remains the only cross-repo apply/promote path.
- J-Prime may propose plans, but it may not execute or promote changes.
- Reactor may score and observe execution, but it may not govern execution.

---

## Why L3 Belongs Here

The current repo already contains most of the substrate needed for `L3`:

- `backend/core/ouroboros/governance/governed_loop_service.py`
  - owns lifecycle, command bus, and background loops
- `backend/core/ouroboros/governance/orchestrator.py`
  - owns deterministic pipeline transitions
- `backend/core/ouroboros/governance/op_context.py`
  - already validates repo DAGs
- `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`
  - already owns multi-repo apply/promote/rollback
- `backend/core/ouroboros/governance/autonomy/command_bus.py`
  - already provides priority and dedup for inter-layer commands
- `backend/core/ouroboros/governance/autonomy/autonomy_types.py`
  - already defines typed command/event envelopes

The important architectural decision is:

`L3` should live in the existing governed execution plane, not in `advanced_coordination.py`.

`backend/core/ouroboros/governance/autonomy/advanced_coordination.py` is better treated as early `L4` advisory persistence. `L3` needs active scheduling, ownership, cancellation, merge barriers, and restart recovery under the supervisor lifecycle, so it belongs in the `L1/L2` runtime path.

---

## Design Rules

1. A subagent work unit may target multiple files, but only one repo.
2. Cross-repo work is expressed as a DAG of per-repo work units, not a single free-form parallel patch blob.
3. Parallel execution is allowed only for work units with disjoint file ownership and satisfied dependencies.
4. No subagent may write directly to a target branch or working tree outside the governed apply path.
5. All final writes still converge through `SagaApplyStrategy`.
6. The scheduler must use structured concurrency. No detached background tasks.
7. Scheduler state must be restart-recoverable and idempotent.
8. Merge decisions must be deterministic for the same graph input.
9. Observability is required for every work unit, barrier, merge, cancellation, and recovery decision.

---

## Phase 1: Execution Graph Contract

### Purpose

Introduce a first-class execution graph contract for parallel work, without storing large mutable graph payloads directly inside `OperationContext`.

### Files

- Modify: `backend/core/ouroboros/governance/autonomy/autonomy_types.py`
- Create: `backend/core/ouroboros/governance/autonomy/subagent_types.py`
- Modify: `backend/core/ouroboros/governance/op_context.py`
- Modify: `backend/core/ouroboros/governance/providers.py`

### Interface

#### `backend/core/ouroboros/governance/autonomy/subagent_types.py`

```python
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.saga.saga_types import RepoPatch


@dataclass(frozen=True)
class WorkUnitSpec:
    unit_id: str
    repo: str
    goal: str
    target_files: Tuple[str, ...]
    dependency_ids: Tuple[str, ...] = ()
    owned_paths: Tuple[str, ...] = ()
    barrier_id: str = ""
    max_attempts: int = 1
    timeout_s: float = 180.0
    acceptance_tests: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionGraph:
    graph_id: str
    op_id: str
    planner_id: str
    schema_version: str
    units: Tuple[WorkUnitSpec, ...]
    concurrency_limit: int
    plan_digest: str
    causal_trace_id: str


@dataclass(frozen=True)
class WorkUnitResult:
    unit_id: str
    repo: str
    status: str
    patch: Optional[RepoPatch]
    attempt_count: int
    started_at_ns: int
    finished_at_ns: int
    failure_class: str = ""
    error: str = ""
    causal_parent_id: str = ""


@dataclass(frozen=True)
class MergeDecision:
    graph_id: str
    barrier_id: str
    repo: str
    merged_unit_ids: Tuple[str, ...]
    skipped_unit_ids: Tuple[str, ...]
    conflict_units: Tuple[str, ...]
    decision_hash: str
```

#### `backend/core/ouroboros/governance/autonomy/autonomy_types.py`

Add command types:

```python
SUBMIT_EXECUTION_GRAPH = "submit_execution_graph"
REPORT_WORK_UNIT_RESULT = "report_work_unit_result"
ABORT_EXECUTION_GRAPH = "abort_execution_graph"
```

Add event types:

```python
EXECUTION_GRAPH_STATE_CHANGED = "execution_graph_state_changed"
WORK_UNIT_STATE_CHANGED = "work_unit_state_changed"
MERGE_DECISION_RECORDED = "merge_decision_recorded"
```

#### `backend/core/ouroboros/governance/op_context.py`

Add scalar-only fields:

```python
execution_graph_id: str = ""
execution_plan_digest: str = ""
subagent_count: int = 0
parallelism_budget: int = 0
causal_trace_id: str = ""
```

Do **not** store full `ExecutionGraph` payloads in `OperationContext`. That would create hash churn, oversized ledger entries, and restart complexity.

#### `backend/core/ouroboros/governance/providers.py`

Add a new J-Prime response schema:

```python
_SCHEMA_VERSION_EXECUTION_GRAPH = "2d.1"
```

Expected planner payload:

```json
{
  "schema_version": "2d.1",
  "execution_graph": {
    "graph_id": "graph-123",
    "planner_id": "jprime-dag-v1",
    "concurrency_limit": 2,
    "units": [
      {
        "unit_id": "jarvis-api",
        "repo": "jarvis",
        "goal": "Add request field and tests",
        "target_files": ["backend/..."],
        "owned_paths": ["backend/..."],
        "dependency_ids": [],
        "barrier_id": "api_contract",
        "acceptance_tests": ["pytest tests/... -q"]
      }
    ]
  }
}
```

### Acceptance Tests

- Create: `tests/governance/autonomy/test_subagent_types.py`
  - graph round-trip preserves field values
  - work unit repo is required and singular
  - duplicate `unit_id` values are rejected
- Extend: `tests/governance/test_op_context_upgrade.py`
  - `execution_graph_id` participates in hashing
  - `parallelism_budget` defaults safely
- Create: `tests/test_ouroboros_governance/test_provider_execution_graph_schema.py`
  - valid `2d.1` graph parses successfully
  - missing `repo` or duplicate `unit_id` fails hard
  - cyclic `dependency_ids` fails hard before scheduling

---

## Phase 2: Durable Scheduler and Restart Recovery

### Purpose

Introduce a scheduler that can run independent work units in parallel with strict ownership, cancellation, and restart guarantees.

### Files

- Create: `backend/core/ouroboros/governance/autonomy/execution_graph_store.py`
- Create: `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Modify: `backend/core/ouroboros/governance/integration.py`

### Interface

#### `backend/core/ouroboros/governance/autonomy/execution_graph_store.py`

```python
class ExecutionGraphStore:
    def __init__(self, state_dir: Path) -> None: ...
    def load_inflight(self) -> Dict[str, GraphExecutionState]: ...
    def save(self, state: GraphExecutionState) -> None: ...
    def mark_terminal(self, graph_id: str, terminal_state: str) -> None: ...
```

Persist one file per graph:

```json
{
  "graph_id": "...",
  "op_id": "...",
  "phase": "RUNNING",
  "ready_units": ["u2"],
  "running_units": ["u3"],
  "completed_units": ["u1"],
  "failed_units": [],
  "plan_digest": "...",
  "checksum": "..."
}
```

Use atomic file replace semantics only. No partial writes.

#### `backend/core/ouroboros/governance/autonomy/subagent_scheduler.py`

```python
class SubagentScheduler:
    def __init__(
        self,
        *,
        store: ExecutionGraphStore,
        command_bus: CommandBus,
        event_emitter: Any,
        executor: Any,
        merge_coordinator: Any,
        max_concurrent_graphs: int = 2,
    ) -> None: ...

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def submit(self, graph: ExecutionGraph) -> bool: ...
    async def recover_inflight(self) -> None: ...
```

Scheduler rules:

- ready-set ordering is `sorted(unit_id)`
- graph ordering is `sorted(graph_id)` when ties exist
- no unit starts unless dependencies are terminal-success
- no unit starts if any `owned_paths` collide with running units
- unit tasks are owned by the scheduler `TaskGroup`
- cancellation propagates from graph -> unit -> tool/test subprocess

#### `backend/core/ouroboros/governance/governed_loop_service.py`

Required changes:

- own a `SubagentScheduler` instance
- start it inside `start()`
- stop it before tearing down governance stack
- extend `_handle_advisory_command()` to route:
  - `SUBMIT_EXECUTION_GRAPH`
  - `REPORT_WORK_UNIT_RESULT`
  - `ABORT_EXECUTION_GRAPH`
- expose scheduler health in GLS health payload

#### `backend/core/ouroboros/governance/integration.py`

Add capability registration for `subagent_scheduler` and include it in degraded boot reporting.

### Acceptance Tests

- Create: `tests/governance/autonomy/test_execution_graph_store.py`
  - state persists atomically
  - corrupted file is ignored, not replayed
  - restart reloads inflight graph once
- Create: `tests/governance/autonomy/test_subagent_scheduler.py`
  - two independent units run concurrently
  - colliding `owned_paths` serialize deterministically
  - failed dependency prevents downstream execution
  - cancellation stops child tasks
  - recovered graph does not duplicate already completed units
- Create: `tests/governance/autonomy/test_subagent_scheduler_backpressure.py`
  - graph admission rejects when max concurrent graphs exceeded
  - queueing respects stable ordering
- Create: `tests/governance/test_governed_loop_l3_command_routing.py`
  - `GovernedLoopService` routes new L3 commands correctly
  - scheduler lifecycle follows GLS lifecycle

---

## Phase 3: Deterministic Merge and Saga Convergence

### Purpose

Parallel work units must still converge through the existing governed apply path. We do not let subagents independently mutate long-lived branches.

### Files

- Create: `backend/core/ouroboros/governance/saga/merge_coordinator.py`
- Modify: `backend/core/ouroboros/governance/saga/saga_types.py`
- Modify: `backend/core/ouroboros/governance/saga/saga_apply_strategy.py`
- Modify: `backend/core/ouroboros/governance/orchestrator.py`

### Interface

#### `backend/core/ouroboros/governance/saga/merge_coordinator.py`

```python
class MergeCoordinator:
    def build_barrier_batches(
        self,
        graph: ExecutionGraph,
        results: Dict[str, WorkUnitResult],
    ) -> Tuple[MergeDecision, ...]: ...

    def merge_repo_patches(
        self,
        decisions: Tuple[MergeDecision, ...],
        results: Dict[str, WorkUnitResult],
    ) -> Dict[str, RepoPatch]: ...
```

Determinism rules:

- merge unit order: `sorted(unit_id)`
- merge repo order: `sorted(repo)`
- conflicting `owned_paths` in the same satisfied barrier fail hard
- no last-writer-wins behavior
- same input graph and same results must yield identical `decision_hash`

#### `backend/core/ouroboros/governance/saga/saga_types.py`

Add:

```python
@dataclass(frozen=True)
class WorkUnitLedgerArtifact:
    graph_id: str
    unit_id: str
    repo: str
    state: str
    barrier_id: str
    causal_trace_id: str
    timestamp_ns: int
```

#### `backend/core/ouroboros/governance/orchestrator.py`

When the best candidate contains an execution graph:

1. validate graph
2. submit graph to `SubagentScheduler`
3. wait for graph terminal status
4. pass merged `RepoPatch` map to existing `SagaApplyStrategy`

Do not add a second apply path.

### Acceptance Tests

- Create: `tests/test_ouroboros_governance/test_merge_coordinator.py`
  - same input yields same `decision_hash`
  - overlapping path ownership fails hard
  - empty barrier batch is rejected
- Create: `tests/test_ouroboros_governance/test_orchestrator_l3.py`
  - graph candidate is scheduled instead of direct linear apply
  - merged repo patches still converge through `SagaApplyStrategy`
  - scheduler failure results in terminal rollback path
- Extend: `tests/test_ouroboros_governance/test_orchestrator_partial_promote.py`
  - no work unit can promote independently of saga promotion

---

## Phase 4: J-Prime DAG Planner

### Purpose

Teach J-Prime to emit graph-shaped plans instead of only a linear patch response when the task is cross-repo and parallelizable.

### Files in `jarvis-prime`

- Modify: `/Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/core/cross_repo_orchestrator.py`
- Modify: `/Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/server.py`
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/jarvis_prime/schemas/execution_graph_schema.py`
- Extend: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_managed_mode_contract.py`
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_execution_graph_schema.py`

### Interface

#### `jarvis_prime/schemas/execution_graph_schema.py`

Expose:

```python
EXECUTION_GRAPH_SCHEMA_VERSION = "2d.1"

def validate_execution_graph(payload: dict) -> None: ...
```

Validation rules:

- no duplicate `unit_id`
- every `dependency_id` must exist
- no cycles
- every unit declares exactly one `repo`
- `owned_paths` must be non-empty
- `concurrency_limit >= 1`

#### `jarvis_prime/core/cross_repo_orchestrator.py`

Add:

```python
def build_execution_graph_plan(request: dict) -> dict: ...
```

Planner obligations:

- only emit parallel units when ownership sets are disjoint
- emit barriers around API contract changes that span repos
- emit explicit acceptance tests per unit
- include stable `planner_id`

#### `jarvis_prime/server.py`

Expose planner capability in managed mode / health metadata:

```json
{
  "execution_graph_schema": "2d.1",
  "planner_capabilities": ["linear_patch", "execution_graph"]
}
```

### Acceptance Tests

- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_execution_graph_schema.py`
  - schema validator rejects duplicate ids and cycles
- Extend: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_managed_mode_contract.py`
  - planner capability advertised in server metadata
- Create: `/Users/djrussell23/Documents/repos/jarvis-prime/tests/test_cross_repo_orchestrator_execution_graph.py`
  - disjoint units become parallel-ready
  - overlapping file ownership is serialized by plan

---

## Phase 5: Reactor Telemetry and Causal Trace

### Purpose

Add L3 observability so we can prove parallel execution is helping instead of hiding failure.

### Files in `reactor-core`

- Modify: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/telemetry.py`
- Modify: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/server.py`
- Modify: `/Users/djrussell23/Documents/repos/reactor-core/reactor_core/api/health_aggregator.py`
- Extend: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_pipeline_events.py`
- Extend: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_managed_mode_contract.py`

### Interface

Telemetry event shape:

```python
{
  "graph_id": "...",
  "op_id": "...",
  "unit_id": "...",
  "repo": "jarvis",
  "state": "started|completed|failed|cancelled",
  "queue_wait_ms": 0.0,
  "runtime_ms": 0.0,
  "merge_barrier": "api_contract",
  "conflict_count": 0,
  "causal_parent_id": "...",
}
```

Required metrics:

- execution graph throughput
- ready-set size
- active units
- queue wait time
- merge conflict rate
- cancellation propagation latency
- recovered-after-restart count

### Acceptance Tests

- Extend: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_pipeline_events.py`
  - graph and work unit state events ingest successfully
- Extend: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_managed_mode_contract.py`
  - telemetry capability advertises L3 fields
- Create: `/Users/djrussell23/Documents/repos/reactor-core/tests/test_execution_graph_telemetry.py`
  - correlation by `graph_id` and `causal_parent_id` works

---

## Phase 6: Supervisor Boot and Recovery Wiring

### Purpose

Wire L3 into the only authoritative entrypoint without making `unified_supervisor.py` even more monolithic.

### Files

- Modify: `backend/core/ouroboros/governance/integration.py`
- Modify: `backend/core/ouroboros/governance/governed_loop_service.py`
- Minimize changes in: `unified_supervisor.py`
- Extend: `tests/governance/p0/test_gate_1_2_supervisor_boot.py`
- Create: `tests/governance/integration/test_l3_supervisor_recovery.py`

### Rules

- `unified_supervisor.py` only starts governance and observes health
- L3 scheduler construction lives in governance modules
- supervisor boot must fail fast on execution-graph contract mismatch
- supervisor shutdown must stop scheduler before governance stack stop
- restart must recover persisted inflight graphs exactly once

### Acceptance Tests

- Extend: `tests/governance/p0/test_gate_1_2_supervisor_boot.py`
  - L3 capability shows ready when enabled
- Create: `tests/governance/integration/test_l3_supervisor_recovery.py`
  - inflight graph recovers after forced restart
  - completed units are not rerun
  - orphaned unit tasks are not left behind

---

## Cross-Repo Hard Gates for L3

These are the acceptance gates that must pass before calling `L3` complete:

1. `python3 unified_supervisor.py --force` boots deterministically with L3 enabled.
2. Same graph input produces the same ready-set order and same merge decision hash.
3. Independent work units reduce wall-clock time by at least 30% on the benchmark suite.
4. Broken mainline rate does not exceed the current L2 baseline.
5. Restart recovery does not duplicate completed work units.
6. No work unit can bypass governance approval or saga promotion.
7. Every graph and every unit has a causal trace visible to Reactor.
8. Parallelism is bounded by explicit budgets, not implicit coroutine fan-out.

---

## Architectural Critique and Missing Nuances

The mandate is strong, but these are the advanced gaps most likely to cause another failure if we ignore them.

### 1. `unified_supervisor.py` is already a control-plane monolith

The repo currently treats `unified_supervisor.py` as both kernel and implementation surface. That is a scalability risk. If L3 logic is added there directly, the supervisor becomes harder to test, reason about, and restart safely.

**Correction:** keep L3 logic in governance modules and let the supervisor remain an assembly and lifecycle boundary.

### 2. State ownership is still at risk of fragmentation

Without discipline, the same execution state could exist in:

- supervisor memory
- `OperationContext`
- scheduler store
- ledger
- Reactor telemetry

**Correction:** declare one source of truth per domain:

- execution state: `ExecutionGraphStore`
- operation lifecycle: `OperationContext` + ledger
- final patch application state: saga ledger
- metrics and traces: Reactor

### 3. Planner-valid is not the same as scheduler-valid

A DAG can be syntactically valid and still be unsafe:

- overlapping `owned_paths`
- hidden runtime dependency on generated artifacts
- cross-repo API mismatch inside the same barrier

**Correction:** add a scheduler-side admission gate after J-Prime planning and before execution.

### 4. Storing full graph payloads in `OperationContext` is a trap

That would bloat hashes, increase ledger volume, and make idempotent resume harder.

**Correction:** store only stable identifiers and digests in `OperationContext`.

### 5. Parallel worktree explosion is a real failure mode

If every unit creates its own worktree or branch without quotas, disk usage, inode pressure, and cleanup risk can spike under repeated retries or crashes.

**Correction:** cap concurrent units, cap per-graph worktrees, and add cleanup on terminal states and supervisor restart.

### 6. File-level conflicts are not the only conflicts

Two units touching different repos can still conflict semantically:

- jarvis request schema changes
- prime classifier assumptions
- reactor telemetry contract changes

**Correction:** barrier units around interface contracts and require explicit acceptance tests for those barriers.

### 7. Deadline semantics can drift if wall clock is used

If one subsystem uses wall clock and another uses monotonic time, restart and timeout behavior become inconsistent.

**Correction:** use monotonic deadlines for runtime budgets and wall time only for human-readable timestamps.

### 8. Cancellation leaks are a top risk in async systems

If a graph is cancelled but child subprocesses or tool tasks remain alive, the system will look healthy while still mutating state.

**Correction:** require explicit cancellation propagation tests down to subprocess boundaries.

### 9. Telemetry can create its own backpressure collapse

If every unit state transition emits full-fidelity telemetry without rate awareness, Reactor can become part of the outage.

**Correction:** separate critical control events from high-volume analytics events and bound telemetry ingestion.

### 10. Supervisor self-healing is still unresolved

If the supervisor itself degrades, L3 recovery cannot depend on the full runtime it is trying to recover.

**Correction:** keep a minimal bootstrap capability outside the governed runtime surface. It only restores lifecycle authority and rehydrates persisted graph state.

### 11. Upgrade strategy is underspecified

Graph schema `2d.1`, telemetry fields, and managed mode contracts will drift unless version negotiation is explicit.

**Correction:** fail boot when `jarvis`, `jarvis-prime`, and `reactor-core` disagree on supported graph schema versions.

### 12. Performance pressure can hide correctness regressions

Parallel speedup can tempt us to widen concurrency too early.

**Correction:** correctness gates must block rollout even if performance improves.

---

## Recommended Implementation Order

1. Add execution graph contract and parser in JARVIS.
2. Add durable scheduler and recovery store in JARVIS.
3. Add merge coordinator and keep saga as the only apply path.
4. Teach J-Prime to emit `2d.1` graph plans.
5. Add Reactor causal-trace telemetry.
6. Wire supervisor boot, health, and restart recovery.
7. Run deterministic, restart, and stress harnesses before widening concurrency.

---

## Minimum Benchmark Suite for Sign-Off

- deterministic DAG scheduling harness
- restart recovery harness
- failure injection for unit crash, planner crash, and merge conflict
- concurrency saturation harness
- cross-repo API barrier harness
- supervisor forced-restart harness
- telemetry backpressure harness

If these are not passing, `L3` is not ready regardless of local success cases.
