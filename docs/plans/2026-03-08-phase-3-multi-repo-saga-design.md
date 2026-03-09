# Phase 2C.4 + Phase 3 — Multi-Repo Saga Autonomy Design

**Date:** 2026-03-08
**Status:** Approved
**Phase:** 2C.4 (Sensor D auto-submit) + 3 (Full multi-repo autonomy)

---

## Problem Statement

Phase 2C.2/2C.3 delivered a live intake layer (router + 4 sensors + A-narrator). Two gaps remain:

1. **Phase 2C.4:** `OpportunityMinerSensor` always sets `requires_human_ack=True`, blocking autonomous execution of high-confidence AI-mined candidates.
2. **Phase 3:** JARVIS operates only on a single repo (`jarvis`). `OperationContext` has no `repo` field; `RepoPipelineManager.submit()` drops the repo signal; cross-repo saga orchestration doesn't exist.

**Goal:** Make JARVIS fully autonomous across all three repos (jarvis, prime, reactor-core) using a deterministic, compensatable Saga pattern that fails closed.

---

## Plane Model

```
GovernedLoopService (Zone 6.8)
    └── ChangeEngine.apply()
            └── SagaApplyStrategy          ← NEW (multi-repo only)
                    ├── Apply Repo 1 (topological order)
                    ├── Apply Repo 2
                    └── Compensate Repo N..1 on failure

OperationContext                            ← UPGRADED
    ├── primary_repo: str
    ├── repo_scope: Tuple[str, ...]
    ├── cross_repo: bool (derived)
    ├── dependency_edges: Tuple[Tuple[str,str], ...]
    ├── apply_plan: Tuple[str, ...]
    ├── repo_snapshots: Tuple[Tuple[str,str], ...]
    ├── saga_id: str
    ├── saga_state: Tuple[RepoSagaStatus, ...]
    └── schema_version: str = "3.0"

RepoSagaStatus (frozen dataclass)
    ├── repo: str
    ├── status: SagaStepStatus
    ├── attempt: int
    ├── last_error: str
    └── compensation_attempted: bool
```

---

## Phase 2C.4: Sensor D Auto-Submit

### Problem
`OpportunityMinerSensor` always sets `requires_human_ack=True`, gating every AI-mined candidate on human approval regardless of confidence.

### Design
Introduce a confidence threshold (env-driven, default `0.75`) above which miners set `requires_human_ack=False`. Below threshold, human ACK is still required.

```python
# OpportunityMinerSensor._make_envelope()
requires_ack = (confidence < self._auto_submit_threshold)
```

**Config field added to `IntakeLayerConfig`:**
```python
miner_auto_submit_threshold: float = 0.75  # JARVIS_INTAKE_MINER_AUTO_SUBMIT_THRESHOLD
```

**Salience gate** (A-narrator) remains silent for `ai_miner` regardless — no change to narration policy.

---

## Phase 3: OperationContext Upgrade

### `RepoSagaStatus` (new frozen dataclass)

```python
@dataclass(frozen=True)
class RepoSagaStatus:
    repo: str
    status: SagaStepStatus          # PENDING|APPLYING|APPLIED|SKIPPED|FAILED|COMPENSATING|COMPENSATED|COMPENSATION_FAILED
    attempt: int = 0
    last_error: str = ""
    reason_code: str = ""           # WHY it failed — compensation uses this
    compensation_attempted: bool = False
```

### `SagaStepStatus` enum

```python
class SagaStepStatus(str, Enum):
    PENDING = "pending"
    APPLYING = "applying"
    APPLIED = "applied"
    SKIPPED = "skipped"             # repo_scope member with empty patch
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
```

### `OperationContext` new fields

```python
# Existing fields unchanged. New fields:
primary_repo: str = "jarvis"
repo_scope: Tuple[str, ...] = ("jarvis",)       # who participates
cross_repo: bool = field(init=False)             # derived: len(repo_scope) > 1
dependency_edges: Tuple[Tuple[str, str], ...] = ()  # (from_repo, to_repo); DAG
apply_plan: Tuple[str, ...] = ()                 # topological apply order (subset of repo_scope)
repo_snapshots: Tuple[Tuple[str, str], ...] = () # (repo, HEAD_hash) at CLASSIFY time
saga_id: str = ""                                # orchestration identity; stable across retries
saga_state: Tuple[RepoSagaStatus, ...] = ()      # one entry per repo in repo_scope
schema_version: str = "3.0"

def __post_init__(self):
    object.__setattr__(self, "cross_repo", len(self.repo_scope) > 1)
    _validate_dag(self.dependency_edges)         # raises ArchitecturalCycleError on cycle
```

### DAG validation

```python
def _validate_dag(edges: Tuple[Tuple[str, str], ...]) -> None:
    """Kahn's algorithm: raises ArchitecturalCycleError if cycle detected."""
    ...
```

Fires in `__post_init__` at CLASSIFY time — prevents deadlock before GENERATE phase.

---

## Phase 3: SagaApplyStrategy

**Location:** `backend/core/ouroboros/governance/change_engine.py`
**Selected when:** `ctx.cross_repo is True` at the top of the APPLY phase.
**Single-repo path:** Unchanged.

### `RepoPatch` file operation tagging

Each file in a `RepoPatch` is tagged with its operation type:

```python
class FileOp(str, Enum):
    MODIFY = "modify"
    CREATE = "create"
    DELETE = "delete"

@dataclass(frozen=True)
class PatchedFile:
    path: str
    op: FileOp
    preimage: Optional[bytes]       # original content for MODIFY/DELETE; None for CREATE
```

Compensation uses these tags:
- `MODIFY` → restore from `preimage` (not `HEAD` — preimage captured before write)
- `CREATE` → `os.remove(path)` (file didn't exist before)
- `DELETE` → write `preimage` back to disk

This closes the "untracked file" compensation failure disease.

### Execution flow

**Phase A — Pre-flight (before touching anything)**

1. Acquire repo leases in deterministic order (sorted repo IDs) — prevents cross-saga deadlocks
2. For every repo in `apply_plan`, verify current HEAD matches `ctx.repo_snapshots`
3. On any drift → abort with `SAGA_ABORTED / reason_code="drift_detected"` (no compensation needed)
4. Emit `saga.prepare` sub-event

**Phase B — Staged topological apply**

For each repo in `apply_plan` order:

1. Re-verify this repo's HEAD anchor immediately before writing (TOCTOU closed)
2. Capture file preimages for all `MODIFY` and `DELETE` files → store in `RepoPatch`
3. Write files to disk
4. Immediately `git add <changed_files>` — Git index becomes the transactional staging area
5. Emit `saga.apply_repo` sub-event
6. On success → update `RepoSagaStatus.status = APPLIED`
7. On failure → record `last_error + reason_code`, halt forward progress → Phase C
8. If patch is empty for a repo → mark `SKIPPED`, continue

**Phase C — Compensating rollback (reverse order)**

For each `APPLIED` repo in reverse `apply_plan` order:

1. Emit `saga.compensate_repo` sub-event (includes `reason_code` from Phase B failure)
2. For each file in patch:
   - `MODIFY` → write preimage back to disk
   - `CREATE` → `os.remove(path)`
   - `DELETE` → write preimage back to disk
3. `git restore --staged <changed_files>` to unstage
4. Update `RepoSagaStatus.status = COMPENSATED` or `COMPENSATION_FAILED`
5. Classify `COMPENSATION_FAILED` as recoverable vs terminal

**Phase D — Terminal state**

| State | Trigger | Effect |
|-------|---------|--------|
| `SAGA_APPLY_COMPLETED` | All repos APPLIED/SKIPPED | Forward to VERIFY phase |
| `SAGA_ROLLED_BACK` | All compensations succeeded | GLS marks op FAILED |
| `SAGA_STUCK` | Any COMPENSATION_FAILED | Supervisor enters SAFE_PAUSE; human review required |

`SAGA_STUCK` triggers `SAFE_PAUSE` on `unified_supervisor.py` — non-essential queues dropped, complex commands refused until human clears via CLI command.

### Idempotent resume

`saga_step_index` persisted durably in the ledger alongside per-repo status. On restart, the saga continues from the last unfinished step — no double-apply, no double-compensate.

Fencing tokens on saga records prevent split-brain runners from mutating the same saga concurrently.

### Sub-events emitted

| Sub-event | When |
|-----------|------|
| `saga.prepare` | Phase A start |
| `saga.apply_repo` | Before each repo apply |
| `saga.verify_global` | After SAGA_APPLY_COMPLETED, entering VERIFY |
| `saga.compensate_repo` | Before each rollback |
| `saga.stuck` | COMPENSATION_FAILED — critical |

---

## Phase 3: Cross-Repo Validation (VERIFY Phase)

Runs after `SAGA_APPLY_COMPLETED`. All checks run against the applied state in isolated worktrees.

### Three-tier structure

**Tier 1 — Per-repo verification (parallelized)**

Runs concurrently across all repos with non-empty patches:
- Type-check: `pyright --project=<repo>`
- Lint: `ruff check <changed_files>`
- Fast unit tests: tests touching changed files only (derived from `RepoPatch.file_list`)

Failure → `VERIFY_FAILED_PER_REPO { repo, check_type, output }`

**Tier 2 — Cross-repo interface contract validation (sequential, dependency order)**

- **Schema registry check**: Shared message schemas, typed API contracts across repos resolve without conflicts; schema versions in `repo_snapshots` compared against post-apply state
- **Import boundary check**: For each edge in `dependency_edges`, verify `python -c "import <boundary_module>"` resolves in the dependent repo's worktree
- **Configuration surface check**: Env vars/feature flags declared in multiple repos remain aligned (checked against per-repo contract manifest JSON if present; no-op if absent)

Failure → `VERIFY_FAILED_CROSS_REPO { edge, check_type, output }`

**Tier 3 — Global integration tests (no-op if none exist)**

Run tests tagged `@cross_repo` in any participating repo. These are the authoritative proof the combined change is safe. Missing tags → Tier 3 passes silently.

### On verification failure

`VERIFY_FAILED_*` triggers the same Phase C compensation from `SagaApplyStrategy`. The `reason_code` distinguishes:

| Phase | reason_code |
|-------|-------------|
| B write failure | `apply_write_error` |
| B drift re-check | `drift_detected_mid_apply` |
| Tier 1 typecheck | `verify_typecheck_failed` |
| Tier 1 tests | `verify_test_failed` |
| Tier 2 schema | `verify_contract_broken` |
| Tier 2 import | `verify_import_edge_broken` |

### Verification resume

Tier 1 pass results stored per-repo in `saga_state` before Tier 2 begins. On resume, completed tiers are skipped.

### Terminal states from VERIFY

| Outcome | Terminal state | Effect |
|---------|---------------|--------|
| All tiers pass | `SAGA_SUCCEEDED` | GLS closes op COMPLETED; B-narrator speaks |
| Any tier fails | `SAGA_VERIFY_FAILED` → compensation | `SAGA_ROLLED_BACK` or `SAGA_STUCK` |

---

## Files Touched

| Action | File |
|--------|------|
| Modify | `backend/core/ouroboros/governance/op_context.py` |
| Create | `backend/core/ouroboros/governance/saga/` (package) |
| Create | `backend/core/ouroboros/governance/saga/saga_types.py` |
| Create | `backend/core/ouroboros/governance/saga/saga_apply_strategy.py` |
| Create | `backend/core/ouroboros/governance/saga/cross_repo_verifier.py` |
| Modify | `backend/core/ouroboros/governance/change_engine.py` |
| Modify | `backend/core/ouroboros/governance/multi_repo/repo_pipeline.py` |
| Modify | `backend/core/ouroboros/governance/intake/sensors/opportunity_miner_sensor.py` |
| Modify | `backend/core/ouroboros/governance/intake/intake_layer_service.py` |
| Modify | `backend/core/ouroboros/governance/intake/__init__.py` |
| Modify | `unified_supervisor.py` (SAFE_PAUSE mode) |
| Modify | `backend/core/ouroboros/governance/multi_repo/registry.py` |
| Test | `tests/governance/saga/test_saga_apply_strategy.py` |
| Test | `tests/governance/saga/test_cross_repo_verifier.py` |
| Test | `tests/governance/test_op_context_upgrade.py` |
| Test | `tests/governance/intake/test_miner_auto_submit.py` |
| Test | `tests/governance/integration/test_phase3_acceptance.py` |

---

## Success Criteria

1. `OperationContext` with `cross_repo=True` validates DAG in `__post_init__`; cycle raises `ArchitecturalCycleError`
2. Single-repo path through `ChangeEngine` is unchanged — all existing 470+ tests still pass
3. `OpportunityMinerSensor` sets `requires_human_ack=False` for confidence ≥ threshold
4. `SagaApplyStrategy` applies repos in topological order; compensation runs in strict reverse
5. Mid-apply crash + restart resumes from correct `saga_step_index` without double-apply
6. `SAGA_STUCK` puts supervisor into `SAFE_PAUSE` — verified by integration test
7. Tier 2 import boundary check catches a broken cross-repo edge
8. `SAGA_SUCCEEDED` triggers B-narrator POSTMORTEM via existing CommProtocol path
9. All three repos registered in `RepoRegistry.from_env()`; `RepoPipelineManager.submit()` passes `repo` through to `OperationContext`
