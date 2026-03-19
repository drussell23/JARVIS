# GCP Operation Lifecycle Poller — Design Spec v1.0
**Date:** 2026-03-19
**Status:** Approved
**Repos:** JARVIS (primary), JARVIS-Prime (peer)

---

## Problem Statement

Repeated 404 warnings spam the log every ~10 seconds because `_wait_for_operation` treats `NotFound` (HTTP 404) from the GCP Zone Operations API as a transient error and continues retrying until the 300-second timeout fires.

Two confirmed root causes (both must be handled deterministically):

1. **Zone mismatch** — `_wait_for_operation` always polls `self.config.zone`, but operations created during multi-zone failover use `_effective_zone = zone or _invincible_node_zone or config.zone`. When these differ, every single poll is a deterministic 404.

2. **Stale/GC'd operation** — GCP zone operations are garbage-collected after completion. A session restart or slow initial poll can reference an operation that no longer exists.

Both cases are indistinguishable to the poller — and neither should cause retry spam. The fix is **state-reconciliation-first truth**, not poll-only truth.

---

## Architecture

### New Module: `backend/core/gcp_operation_poller.py`

Single canonical implementation used by both `backend/core/gcp_vm_manager.py` (JARVIS) and `jarvis_prime/core/gcp_vm_manager.py` (JARVIS-Prime, via path import with fallback copy).

---

## State Machine

GCP zone operation `.status` values: `PENDING`, `RUNNING`, `DONE`, `ABORTING`.
`PENDING`/`RUNNING` keep the poll loop alive. `ABORTING` is treated as `DONE` with error.

```
           ┌──────────────┐
  start()  │              │
    ──────>│   POLLING    │──── op.status==DONE (no error) ───────────────> DONE_SUCCESS
           │              │──── op.status==DONE (with error) ─────────────> DONE_FAILURE
           │              │──── op.status==ABORTING ───────────────────────> DONE_FAILURE
           │              │──── op.status==PENDING|RUNNING ───────────────> [continue polling]
           │              │──── NotFound(404) + correlated + postcond=OK ─> TERMINAL_SUCCESS
           │              │──── NotFound(404) + uncorrelated/scope_miss ──> TERMINAL_UNKNOWN
           │              │──── NotFound(404) + postcond=False ───────────> TERMINAL_UNKNOWN
           │              │──── Forbidden/Unauthorized ───────────────────> PERM_FAILURE
           │              │──── BadRequest/InvalidArgument ───────────────> PERM_FAILURE
           │              │──── transient (500/503/429/504/network) ──────> [backoff, retry]
           │              │──── retry budget exhausted ───────────────────> RETRY_EXHAUSTED
           │              │──── deadline exceeded ─────────────────────────> TIMEOUT
           │              │──── CancelledError ────────────────────────────> CANCELLED
           └──────────────┘
```

---

## Classification Table

| HTTP Status | google.api_core exception | Classification | Action |
|-------------|--------------------------|----------------|--------|
| 200 + DONE | — | Terminal (success or failure per op.error) | Return result |
| 404 | `NotFound` | **Needs correlation check** | See 404 rules below |
| 403 | `Forbidden` | Permanent failure | Raise immediately |
| 401 | `Unauthorized` | Permanent failure | Raise immediately |
| 400 | `BadRequest` / `InvalidArgument` | Permanent failure | Raise immediately |
| 500 | `InternalServerError` | Transient | Backoff + retry |
| 503 | `ServiceUnavailable` | Transient | Backoff + retry |
| 429 | `TooManyRequests` / `ResourceExhausted` | Transient | Backoff + retry |
| 504 | `DeadlineExceeded` | Transient | Backoff + retry |
| Network error | `requests.exceptions.ConnectionError` | Transient | Backoff + retry |

---

## 404 Correlation Rules

```
404 received for operation_id in scope (project, zone)
│
├── Is op_id in OperationLifecycleRegistry?
│   └── NO  → terminal-unknown (NOT_FOUND_UNCORRELATED)
│           → emit stale_operation_detected
│
└── YES — Is registry scope == poll scope?
    │
    ├── NO  → terminal-unknown (NOT_FOUND_SCOPE_MISMATCH)
    │       → emit stale_operation_detected (zone bug)
    │
    └── YES — Is postcondition provided?
        │
        ├── NO  → terminal-unknown (NOT_FOUND_NO_POSTCONDITION)
        │       → emit operation_gc_404_terminal(unknown)
        │
        └── YES — postcondition() returns True?
            │
            ├── YES → terminal-success (NOT_FOUND_CORRELATED)
            │       → emit operation_gc_404_terminal(success)
            │
            └── NO  → terminal-unknown (NOT_FOUND_POSTCONDITION_FAIL)
                    → emit operation_gc_404_terminal(failure)
```

This prevents the zone mismatch bug from silently masking as "success" — a 404 for the wrong zone is `NOT_FOUND_SCOPE_MISMATCH`, not success.

**Postcondition retry window:** Because a VM may still be transitioning (e.g., `STAGING → RUNNING`) when the 404 fires, the postcondition is retried for up to `postcondition_retry_s` (default 30s, env `JARVIS_OP_POSTCONDITION_RETRY_S`) with 2s backoff before declaring `NOT_FOUND_POSTCONDITION_FAIL`. The postcondition retry does not count against the main poller's `max_retries`. If the postcondition itself throws an exception (e.g., the describe call fails), it is treated as `NOT_FOUND_POSTCONDITION_FAIL` (conservative — no false success).

---

## Components

### `OperationScope`
Extracted exclusively from `operation.self_link` or `operation.zone` URL.
**Never** falls back to `config.zone`.
Raises `ScopeContractError` if neither field is populated on the operation object.

```python
@dataclass(frozen=True)
class OperationScope:
    project: str
    zone: Optional[str]      # for zonal operations
    region: Optional[str]    # for regional operations
    scope_type: str          # "zonal" | "regional" | "global"

    @classmethod
    def from_operation(cls, op, fallback_project: str) -> "OperationScope": ...
```

### `TerminalReason` (enum)
```
OP_DONE_SUCCESS              # op.status==DONE, op.error empty
OP_DONE_FAILURE              # op.status==DONE, op.error present
NOT_FOUND_CORRELATED         # 404 + registry match + postcondition pass → success
NOT_FOUND_UNCORRELATED       # 404 + no registry match → unknown/failure
NOT_FOUND_SCOPE_MISMATCH     # 404 + registry scope != poll scope → unknown/failure
NOT_FOUND_NO_POSTCONDITION   # 404 + no postcondition callable → unknown
NOT_FOUND_POSTCONDITION_FAIL # 404 + postcondition returns False → failure
PERMISSION_DENIED            # 403/401
INVALID_REQUEST              # 400/bad-arg
RETRY_BUDGET_EXHAUSTED       # transient errors exceeded max_retries
TIMEOUT                      # wall-clock deadline exceeded
CANCELLED                    # asyncio CancelledError
SCOPE_CONTRACT_ERROR         # operation missing self_link + zone
```

### `OperationRecord`
```python
@dataclass
class OperationRecord:
    operation_id: str         # operation.name
    scope: OperationScope
    instance_name: str
    action: str               # "start" | "stop" | "create" | "delete" | "reset" | ...
    created_at: float         # time.time() when op returned from API
    first_seen_at: float      # when registry first registered it
    last_seen_at: float       # last successful poll timestamp
    poll_count: int           # total poll attempts
    terminal_state: Optional[str]   # None if still in-flight
    terminal_reason: Optional[TerminalReason]
    correlation_id: str       # UUID per caller invocation
    supervisor_epoch: int     # for split-brain fencing
    error_message: Optional[str]
```

### `OperationLifecycleRegistry`
- Singleton per process
- In-memory `Dict[str, OperationRecord]`, protected by `asyncio.Lock`
- Persisted to `~/.jarvis/gcp/operations.json` (best-effort; failure is non-fatal)
- Cross-repo shared state written to `~/.jarvis/cross_repo/gcp/operations.json`
- On load: prune records older than `max_record_age_s` (default 24h)
- On startup reconciliation: for orphaned in-flight records, query actual VM state and close stale entries with `TIMEOUT` or `OP_DONE_SUCCESS` based on current instance status

Split-brain fencing:
- `supervisor_epoch` type: `int`, sourced from `OperationLifecycleRegistry.supervisor_epoch` which is set at construction from `GCPVMManager._current_boot_session_id` parsed as a monotonically increasing integer (or `time.monotonic_ns() // 1_000_000` at construction if unavailable).
- Epoch comparison: `update_terminal()` rejects any update where `incoming_epoch < record.supervisor_epoch`. "Older" means strictly less than.
- On rejected update: the attempt is logged at `WARNING` with `[SplitBrainFence]` prefix and the `op_id`, `incoming_epoch`, `record_epoch`. The record is **not mutated**. A `stale_supervisor_fenced` event is emitted. The calling coroutine receives a `SplitBrainFenceError` exception so it can abort cleanly rather than silently proceeding.
- Note: `supervisor_epoch=0` is accepted by all records (used in tests and first-boot scenarios).

### `GCPOperationPoller`
- Accepts `operations_client` (ZoneOperationsClient), `registry`, `project`
- Optional `postcondition: Callable[[], Awaitable[bool]]` per call
- Class-level `_active_pollers: Dict[str, "_PollerState"]` for deduplication (not futures)
- `_PollerState` holds: `asyncio.Task` (the primary poll loop), `asyncio.Future` (result), `waiter_count: int`
- Concurrent waiters for the same `operation_id` increment `waiter_count` and `await` the shared future
- **Cancellation cascade fix:** If the primary waiter is cancelled, `_PollerState` holds the poll `Task` separately from the `Future`. The poll task is NOT cancelled when one waiter cancels. The poll loop runs to completion and sets the result on the shared future. All remaining waiters receive the result. The cancelled waiter receives `CancelledError` only for itself.
  - The poll task is cancelled only when `waiter_count` drops to 0 (all waiters are gone).
- Structured concurrency: `asyncio.Task` owned by registry scope, `CancelledError` propagated only to the specific requesting coroutine, not all waiters
- No orphan tasks: `_PollerState` entry removed when poll task completes and all waiters have consumed the result (waiter_count == 0 and future resolved)
- `fallback_project`: used only to fill `OperationScope.project` when `self_link` does not include the project path. This is always set at poller construction from `config.project_id`. The `scope_type` inference (zonal/regional/global) still uses the operation's own metadata — `fallback_project` is never used for scope-type inference.

Backoff policy:
```
wait = min(base_backoff * (2 ** retry_count), max_backoff) * (1 + jitter_factor * random())
base_backoff = 1.0s, max_backoff = 30.0s, jitter_factor = 0.25, max_retries = 10
```

### Postcondition Factories (in `gcp_vm_manager.py`)
```python
def _postcondition_instance_running(self, name, zone) -> Callable:
    async def check() -> bool:
        status, _, _ = await self._describe_instance_full(name, zone=zone)
        return status == "RUNNING"
    return check

def _postcondition_instance_gone(self, name, zone) -> Callable:
    async def check() -> bool:
        status, _, _ = await self._describe_instance_full(name, zone=zone)
        return status == "NOT_FOUND"
    return check

def _postcondition_instance_stopped(self, name, zone) -> Callable:
    async def check() -> bool:
        status, _, _ = await self._describe_instance_full(name, zone=zone)
        return status in ("TERMINATED", "STOPPED")
    return check
```

---

## Integration Points

### `backend/core/gcp_vm_manager.py`
Replace `_wait_for_operation(self, operation, timeout)` with:
```python
async def _wait_for_operation(
    self,
    operation,
    timeout: int = 300,
    *,
    action: str = "unknown",
    instance_name: str = "",
    correlation_id: Optional[str] = None,
    postcondition: Optional[Callable[[], Awaitable[bool]]] = None,
) -> "OperationResult":
    poller = self._get_or_create_poller(timeout=timeout)
    return await poller.wait(
        operation,
        action=action,
        instance_name=instance_name,
        correlation_id=correlation_id or str(uuid.uuid4()),
        postcondition=postcondition,
    )
```
All callers of `_wait_for_operation` are updated to pass `action` and `instance_name`.

### `jarvis_prime/core/gcp_vm_manager.py`
Same replacement pattern. The `GCPOperationPoller` is imported with try/except fallback:
```python
try:
    import sys, os
    _jarvis_path = os.environ.get("JARVIS_REPO_PATH", "")
    if _jarvis_path and _jarvis_path not in sys.path:
        sys.path.insert(0, _jarvis_path)
    from backend.core.gcp_operation_poller import GCPOperationPoller, OperationLifecycleRegistry
except ImportError:
    from jarvis_prime.core.gcp_operation_poller import GCPOperationPoller, OperationLifecycleRegistry
```
A copy of `gcp_operation_poller.py` is placed in `jarvis_prime/core/` as the fallback.

**Hash mismatch policy:** At import time, if the JARVIS path is available and the local copy is used as fallback, the fallback module is fingerprinted (sha256 of first 8KB) and compared against the primary. If hashes differ: log `CRITICAL` with `[GCPOperationPoller] Local fallback copy differs from primary — using local; ensure sync`, disable the `_active_pollers` dedup registry (each call gets an independent poller — safe but not deduped), and emit a `poller_version_drift` event. The process continues; it does NOT raise because a version drift is survivable but must be made visible. The legacy `_wait_for_operation` behavior is NOT restored — the local copy's semantics are used.

---

## Emitted Events

| Event name | Trigger | Payload keys |
|------------|---------|--------------|
| `stale_operation_detected` | 404 with no registry match | op_id, scope, caller |
| `operation_gc_404_terminal` | 404 with registry match | op_id, reason, postcondition_result |
| `reconcile_success` | Orphan resolved at startup | op_id, inferred_state |
| `reconcile_fail` | Orphan could not be resolved | op_id, error |
| `orphan_recovered` | Stale in-flight record closed | op_id, action, age_s |
| `retry_budget_exhausted` | max_retries reached | op_id, retry_count, last_error |

All events are emitted via `metrics_emitter(event_name, payload)` callback. Cross-repo file write is best-effort, non-blocking, isolated behind `asyncio.shield` in a background task.

---

## Tests Required

| Test | Description |
|------|-------------|
| `test_scope_from_zone_url` | Zone extracted from op.zone URL; selfLink fallback works |
| `test_scope_missing_raises` | op with no zone/selfLink raises ScopeContractError |
| `test_zone_mismatch_regression` | Old `config.zone` path now impossible; scope comes from op only |
| `test_404_correlated_success` | Registry match + postcondition=True → terminal-success |
| `test_404_uncorrelated_failure` | No registry match → terminal-unknown |
| `test_404_scope_mismatch` | Registry scope != poll scope → terminal-unknown, not success |
| `test_404_postcondition_fail` | Registry match + postcondition=False → terminal-unknown |
| `test_transient_retry_bounded` | 503 retries with backoff; stops at max_retries |
| `test_permission_immediate_fail` | 403 raises immediately, no retry |
| `test_concurrent_dedup` | N concurrent waiters share 1 poll loop |
| `test_cancellation_no_leak` | CancelledError propagates; no orphan task in _active_futures |
| `test_stale_op_from_prior_session` | Load persisted orphan, reconcile, emit orphan_recovered |
| `test_crash_restart_recovery` | Simulate mid-op restart; startup reconciliation resolves per table below |
| `test_jarvis_prime_parity` | Same operation object produces same result in both code paths |

### Reconciliation outcome table (crash/restart recovery)

For each orphaned in-flight record loaded at startup, query `_describe_instance_full(instance_name, zone)` and apply:

| Instance status from describe | Action type | Reconciliation outcome | Event emitted |
|-------------------------------|-------------|----------------------|---------------|
| `RUNNING` | `start` | Close as `OP_DONE_SUCCESS` | `orphan_recovered` |
| `RUNNING` | `create` | Close as `OP_DONE_SUCCESS` | `orphan_recovered` |
| `RUNNING` | `stop` / `delete` | Close as `RECONCILE_FAIL` (expected STOPPED/gone) | `reconcile_fail` |
| `TERMINATED` / `STOPPED` | `stop` | Close as `OP_DONE_SUCCESS` | `orphan_recovered` |
| `TERMINATED` / `STOPPED` | `start` | Close as `RECONCILE_FAIL` | `reconcile_fail` |
| `NOT_FOUND` | `delete` | Close as `OP_DONE_SUCCESS` | `orphan_recovered` |
| `NOT_FOUND` | `create` / `start` / `stop` | Close as `RECONCILE_FAIL` | `reconcile_fail` |
| `STAGING` / `PROVISIONING` | any | Close as `orphan_recovered` (assumed in-progress success) | `orphan_recovered` |
| describe call throws exception | any | Close as `RECONCILE_FAIL` | `reconcile_fail` |

All tests use an injectable `persist_path` (tmp dir) so no filesystem side effects contaminate test runs.

---

## Registry Pruning Order

1. Remove completed records older than `max_record_age_s` (default 24h).
2. If still over 1000 entries, remove oldest **completed** records by `last_seen_at` until under limit.
3. Only if still over 1000 after removing all completed: remove oldest **in-flight** records with a `TIMEOUT` terminal classification and emit `orphan_recovered`.
4. Active, recently-seen in-flight records are never pruned under capacity.

---

## Risk Register

| Risk | Mitigation |
|------|------------|
| Postcondition API call fails | Classified as NOT_FOUND_POSTCONDITION_FAIL; caller handles gracefully |
| Persisted ops file corrupted | Load fails silently; registry starts empty; reconciliation skipped |
| JARVIS-Prime falls back to local copy and diverges | Enforce copy sync in CI; module hash checked at import time |
| Postcondition not provided for legacy callers | Default to NOT_FOUND_NO_POSTCONDITION (not success); log warning |
| Registry lock contention | asyncio.Lock; no cross-thread mutation |
| Very large number of inflight ops | Registry bounded at 1000 entries; oldest pruned on overflow |
