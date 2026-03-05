# Unified Supervisor Hardening Design

**Date:** 2026-03-05
**Scope:** Enterprise-grade hardening of `unified_supervisor.py` (96,058 lines)
**Constraint:** No file breakup. All changes are edits to existing code + one new contract file.

---

## Problem Statement

Deep architectural analysis of `unified_supervisor.py` identified systemic defects across 6 domains:

| Category | Count | Severity |
|----------|-------|----------|
| Silent exception swallowing (`except Exception: pass`) | 474 | CRITICAL |
| Missing `finally` blocks (try:finally ratio 30:1) | ~1,839 missing | CRITICAL |
| `asyncio.wait_for` without `shield` (13:1 imbalance) | 326 unshielded | HIGH |
| Race conditions identified | 23 | CRITICAL-HIGH |
| Dead code lines (unused enterprise + deprecated) | ~3,700 | MEDIUM |
| Hardcoded values needing configuration | 50+ | HIGH |
| Resource leak paths | 12 | HIGH |
| Missing contract/schema validation | 12 | HIGH |
| Configuration drift (same concept, different env vars) | 4 pairs | HIGH |
| `asyncio.gather` without `return_exceptions` | 26 | MEDIUM |
| Untracked `asyncio.create_task` calls | 6 | MEDIUM |
| Event loop blocking (`time.sleep` in async) | 4 | MEDIUM |

These are **structural diseases**, not surface symptoms. Each must be fixed at its root.

---

## Phased Approach

### Phase 1: Observability + Async Safety Baseline

**Goal:** See errors before fixing them. Prevent cancellation-induced state corruption.

#### 1A. Silent Swallow Triage (Targeted)

Classify 474 `except Exception: pass` sites into 3 tiers:

- **T1 (Critical Path, ~60-80 sites):** Startup phases, shutdown, lock acquire/release, state persistence, cross-repo calls. Replace with logged + classified error handling. Emit `SupervisorEvent` for critical failures.
- **T2 (Resource Boundary, ~120-150 sites):** cleanup(), health_check(), process management. Replace with logged warning (non-fatal).
- **T3 (Cosmetic/Import Guard, ~250-300 sites):** Import fallbacks, optional feature detection, UI decoration. Leave as-is or add debug-level log.

Target zones for T1:
- `_phase_preflight` (line 71411), `_phase_resources` (72023), `_phase_backend` (73149), `_phase_intelligence` (74162), `_phase_trinity` (79606), `_phase_enterprise_services` (81937)
- Shutdown cleanup (~line 87298+)
- `StartupLock` (5207), `LazyAsyncLock` (14195)
- `UnifiedTrinityConnector` (61980), `TrinityIntegrator` (59323)
- `CostTracker`, `VMSessionTracker`, `GlobalSessionManager`

#### 1B. Cancellation Safety

Audit 353 `asyncio.wait_for` calls. Classify by whether wrapped coroutine must complete on timeout:

- **Must-complete** (state writes, lock release, commit): Wrap in `asyncio.shield()`
- **Best-effort** (health checks, optional probes): Leave as-is
- **Ambiguous** (process management): Add `finally` cleanup block

Target the ~27 most critical `wait_for` calls in:
- `_phase_*` methods
- Shutdown cleanup
- Cross-repo initialization
- Lock acquisition paths
- State persistence

Explicit `CancelledError` re-raise everywhere it currently falls through to `except Exception`.

#### 1C. `finally` Block Discipline

Target ~40-60 critical `try` blocks that manage:
- File handles / StreamWriters
- Locks (asyncio.Lock, threading.Lock, file locks)
- Subprocess handles
- Database connections
- Terminal state (tcsetattr)

**Gate 1:** No silent swallows in critical paths. `CancelledError` explicitly re-raised where required.

---

### Phase 2: Race Condition Hot Spots (Targeted)

**Goal:** Fix known critical races that corrupt state even with improved logging.

#### 2A. LazyAsyncLock (Line 14195)
Add `threading.Lock` guard around `_ensure_lock()` — double-checked locking pattern.

#### 2B. UnifiedSignalHandler._get_event() (Line 55975)
Add `threading.Lock` guard around `asyncio.Event` creation — same double-checked locking.

#### 2C. GlobalSessionManager (Line 14256)
Unify to single `threading.Lock` for both sync and async paths. No `await` points inside critical sections.

#### 2D. KernelBackgroundTaskRegistry (Line 62947)
Add `threading.Lock` to `append()`, `_on_task_done()`, `snapshot()`.

#### 2E. _ProgressBroadcastWorker (Line 63094)
Add `threading.Lock` for observable state. Replace direct field access with `_record_failure()`, `_record_success()`, `state_snapshot` property.

#### 2F. SupervisorRestartManager (Line 14643)
Split into Phase 1 (collect under lock, no I/O) and Phase 2 (restart outside lock, with backoff). Matches existing `ProcessRestartManager` pattern.

#### 2G. ProcessStateManager.get_statistics() (Line 56959)
Add `threading.Lock` for synchronous dict reads. Take snapshot under lock.

#### 2H. LiveProgressDashboard stdout (Line 7679)
Class-level `threading.Lock` for all `sys.stdout.write()` calls. Route all terminal output through `_write_stdout()`.

**Gate 2:** Provably single-instance locks. No mixed-domain lock pairs. stdout serialized.

---

### Phase 3: Cross-Repo Contract + Health Propagation

**Goal:** Make Trinity integration deterministic with schema-validated contracts, continuous health, and reconnection.

#### 3A. Contract Validation at Boot
- New file: `backend/core/cross_repo_contracts.py` — `HealthContractV1`, typed error hierarchy (`RepoNotFoundError`, `RepoImportError`, `RepoUnreachableError`, `RepoContractError`)
- Wire `validate_contracts_at_boot()` into startup (currently never called)
- Schema-versioned health response parsing with `contract_version` field

#### 3B. Continuous Health Refresh
- Add `_continuous_health_check()` background task to `UnifiedTrinityConnector`
- Upgrade `_validate_repositories()` from ".git exists?" to actual HTTP health check
- Emit `SupervisorEvent` on health transitions (healthy -> unhealthy, unhealthy -> healthy)

#### 3C. Reconnection Logic
- `CrossRepoReconnector` class with exponential backoff (configurable base/max/budget)
- Repos that fail at boot get reconnection attempts instead of permanent failure
- Connected to health monitor — detected recovery triggers reconnect

#### 3D. Error Classification
- Typed error hierarchy at cross-repo boundaries
- Callers distinguish permanent (not installed) vs transient (timeout) vs contract (version mismatch)
- Intelligent retry decisions based on error type

#### 3E. Env Var Deduplication
- Canonical name per concept with alias deprecation logging
- 4 drift pairs resolved: spot cost, backend port, GCP project ID, reactor core path

**Gate 3:** Schema mismatch fails fast with reason. Health refreshes every 10s. Reconnection works. No duplicate env vars.

---

### Phase 4: Resource Leak and Cleanup Discipline

**Goal:** Zero FD/memory growth over extended operation.

#### 4A. IPCServer StreamWriter (Line 62856)
Add `finally` block with `writer.close()` + `await writer.wait_closed()`.

#### 4B. Terminal State Restoration (Line 7186)
Wrap `_keyboard_listener` in `try/finally` for `tcsetattr`. Add `atexit` handler as safety net.

#### 4C. Subprocess Handle Cleanup
Pattern for all subprocess-managing code: timeout -> terminate with 5s grace -> kill. CancelledError -> kill immediately, re-raise. Track PIDs in `_protected_pids`.

#### 4D. ChromaDB Client (Line 12295)
Explicit close/release in `SemanticVoiceCacheManager.cleanup()`.

#### 4E. Lock Release Guarantee
Verify `StartupLock.release()` is in `finally` block at call site, not just happy path.

#### 4F. Atomic State Persistence
`tempfile.mkstemp()` + `os.fsync()` + `Path.replace()` for all state files: `CostTracker`, `VMSessionTracker`, `GlobalSessionManager`.

#### 4G. Bounded Collections
Cap all unbounded `List` growth: `IntelligentCacheManager._errors`, `SpotInstanceResilienceHandler.preemption_history`, `AnimatedProgressBar._step_times`, `CliRenderer._phase_timeline`.

#### 4H. Voice Narrator Queue
Add `maxsize=50` (configurable) to `AsyncVoiceNarrator._queue`. Drop on full.

**Gate 4:** Zero FD growth over 100 IPC queries. Terminal never left in cbreak. No orphan subprocesses. State files survive `kill -9`. No memory growth from unbounded lists. 30-60 min soak passes.

---

### Phase 5: Dead Code Removal

**Goal:** Remove ~3,700 lines of unused enterprise classes.

#### Classes to Remove

| Class | Lines | Rationale |
|-------|-------|-----------|
| MLOpsModelRegistry | 51116-51400 | Reactor Core owns model registry |
| WorkflowOrchestrator + 5 NamedTuples | 51510-52000 | AGI OS handles orchestration |
| DocumentManagementSystem + 3 NamedTuples | 51966-52430 | Storage is /tmp; stub |
| NotificationHub + 4 NamedTuples | 52432-52950 | All delivery methods are stubs |
| SessionManager + 2 NamedTuples | 52932-53250 | GlobalSessionManager is the real one |
| DataLakeManager + 3 NamedTuples | 53259-53720 | Storage is /tmp; stub |
| StreamingAnalyticsEngine + 4 NamedTuples | 53689-54020 | No stream sources; pure stub |
| ConsentManagementSystem + 3 NamedTuples | 54027-54400 | GDPR claims unenforceable |
| DigitalSignatureService + 4 NamedTuples | 54413-54750 | Crypto operations are stubs |
| _Deprecated_GracefulDegradationManager + 2 NamedTuples | 55367-55610 | Explicitly superseded |

#### Classes to Keep
- HealthAggregator (registered in SSR Phase 1)
- SystemTelemetryCollector (referenced by monitoring)
- ResourceCleanupCoordinator (used by shutdown)
- GracefulDegradationManager (active replacement)

#### Removal Process
1. Full-repo grep confirms zero external references
2. Single isolated commit with clear rationale
3. Zone 4.19 header updated or removed
4. Self-test function updated
5. `python3 unified_supervisor.py --test zones` passes

**Gate 5:** Zero external references. Tests pass. ~3,700 lines removed.

---

## Non-Negotiable Gates Between Phases

| Gate | Criteria | Blocks |
|------|----------|--------|
| **Gate 1** | No silent swallows in critical paths; CancelledError handled | Phase 2 |
| **Gate 2** | Targeted concurrency tests pass under stress | Phase 3 |
| **Gate 3** | Contract mismatch fails fast with reason-coded diagnostics | Phase 4 |
| **Gate 4** | 30-60 min soak without restart flapping, FD growth, or stale health | Phase 5 |
| **Gate 5** | Grep confirms zero references; tests pass after removal | Done |

---

## New Files

- `backend/core/cross_repo_contracts.py` — contract dataclasses + typed error hierarchy

## Modified Files

- `unified_supervisor.py` — all 5 phases of edits
- `backend/core/startup_contracts.py` — wire validation into boot path (if not already called)

## Estimated Impact

| Metric | Before | After |
|--------|--------|-------|
| File size | 96,058 lines | ~92,300 lines |
| Silent exception swallows in critical paths | ~80 | 0 |
| Unshielded must-complete wait_for | ~27 | 0 |
| Race conditions (known critical) | 8 | 0 |
| Cross-repo health refresh | Never | Every 10s |
| Contract validation | Never called | Every boot |
| Resource leak paths | 12 | 0 |
| Dead enterprise classes | 10 | 0 |
