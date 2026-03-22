# Sequential Boot with Background Pre-Warming

**Date:** 2026-03-22
**Status:** Draft
**Scope:** PR 1 of 2 — boot stability + pre-warming. PR 2 (follow-up): `backend.main` import audit + refactor.

## Problem

The previous session replaced the sequential `_startup_impl` with a `ParallelBootOrchestrator` DAG (`parallel_boot.py`). This caused a cascade of misalignments: progress racing, heartbeat crashes, missing banners, redirect failures. Every fix introduced new edge cases. The root cause was maintaining two competing boot implementations with different progress/readiness semantics.

## Decision

**Keep the sequential `_startup_impl` as the sole authoritative boot pipeline.** Add an optional, advisory `StartupPreWarmer` that runs safe background work at T=0. The sequential phases find things already done and complete faster, but the pipeline order, gates, progress, CLI output, and loading page behavior are unchanged.

**Remove `ParallelBootOrchestrator` entirely.** One boot implementation, not two.

## Architecture

```
T=0 ──> StartupPreWarmer (background, advisory, supervisor-owned)
         |  fires safe probes in bounded thread pool + async tasks
         |  results stored with explicit states (PENDING | OK | FAILED | SKIPPED)
         |  MUST be created on the same event loop as _startup_impl
         |
T=0 ──> _startup_impl() (sequential, authoritative)
         |  same zone order, same gates, same progress
         |  at each phase: check pre-warm cache -> use if fresh OR run normally
         |
         └──> Single progress publisher -> heartbeat -> loading page -> CLI -> voice
```

### Key Invariants

1. **Sequential pipeline is source of truth.** Phases run in order. Nothing "finishes early" unless the real gate passes.
2. **Pre-warm is optional and non-mutating of authoritative boot config.** Background work may cache probes, import modules, and validate credentials. It must NOT set `os.environ` keys, `SystemKernelConfig` values, allocate ports, update dashboard progress, or advance readiness tier — those are owned exclusively by sequential phases.
3. **Same outcomes and gates with pre-warm off.** If the pre-warmer doesn't exist, crashes, or is disabled (`JARVIS_PREWARM_DISABLED=true`), the sequential pipeline produces identical gates, health states, and final readiness. Wall-clock timing will differ.
4. **No silent success.** If pre-warm fails, the sequential phase runs and surfaces the real error. Pre-warm never hides failures.
5. **No duplicate progress paths.** Heartbeat, loading page, CLI, and voice consume one progress/readiness stream from one atomic publisher. Pre-warmer never advances progress, readiness tier, or dashboard state.
6. **One boot implementation.** `ParallelBootOrchestrator` and `JARVIS_PARALLEL_BOOT` are removed.

### External Side Effects

Some pre-warm tasks have process-wide or cloud effects (importing C extensions sets BLAS globals; GCP VM start is idempotent provisioning, not read-only). The invariant is: **no mutation of authoritative boot configuration** — env vars, ports, config objects, readiness tier, dashboard progress, routing policy. External effects are acceptable if idempotent and re-verified by the sequential phase.

## Pre-Warm Task Inventory

### Safety Table

| # | Task | Type | Timeout | Deps | What It Does | Consumer Phase | Re-Check Rule | Cancel Behavior | Kernel State Mutations | Log Tag |
|---|------|------|---------|------|-------------|---------------|--------------|-----------------|----------------------|---------|
| 1 | Docker daemon probe | Thread (blocking socket) | 15s | None | Opens Docker socket, sends ping, caches `(alive, timestamp)` | `_phase_resources` | Re-ping if stale (>30s) or FAILED | Socket close; thread completes naturally | **None** | `prewarm.docker` |
| 2 | GCP credential validation | Thread (blocking I/O) | 10s | None | Loads service account JSON, validates structure, creates `compute_v1.InstancesClient`, caches ref | `_phase_resources` -> GCP manager | Manager validates client on use; drop cached client if FAILED | Client object is inert; safe to abandon | **None** | `prewarm.gcp_creds` |
| 3 | GCP VM proactive start | Async task (idempotent) | 300s | #2 optional | Calls `ensure_static_vm_ready()` ONLY — caches `(success, ip, status)` tuple. Does NOT write env vars, does NOT call `notify_gcp_vm_ready()`, does NOT update dashboard, does NOT call `_mark_startup_activity()`, does NOT wire `_deferred_prober`, does NOT call `acquire_gcp_lease()` or `signal_gcp_ready()`. All of those mutations remain in `_phase_trinity` after re-verification. | `_phase_trinity` (via handoff) | Trinity re-verifies VM health (HTTP ping) then performs all env var writes and routing notifications | Explicit handoff to Trinity; pre-warmer releases ownership | **None** — all env writes, routing, dashboard, activity markers deferred to `_phase_trinity` | `prewarm.gcp_vm` |
| 4 | Native library preload | Thread (imports) | 30s | None | Imports numpy, scipy, sounddevice, soundfile, webrtcvad, PIL in thread executor. Each callable wrapped in try/except producing a `PreWarmResult`. Does NOT set BLAS/OMP thread env vars. | Various (intelligence, audio) | No re-check needed — Python caches in `sys.modules`. First import may set BLAS globals (thread counts); sequential phases should set `OMP_NUM_THREADS` etc. before any compute. | Thread completes naturally; no interrupt | **None** (process-wide BLAS init is acceptable — documented, not authoritative config) | `prewarm.native_libs` |
| 5 | GGUF model file scan | Thread (disk I/O) | 10s | None | Scans `PRIME_MODELS_DIR` for `.gguf` files, caches `[(path, size, mtime)]` | `_phase_intelligence` | Re-scan if stale by TTL (>60s) OR if dir mtime changed since scan. Empty list is valid (remote-only mode) — NOT treated as stale. | Safe to abandon mid-scan | **None** | `prewarm.gguf_scan` |

### Side Effects Explicitly Deferred to Sequential Phases

The existing proactive GCP start in `_startup_impl` (~lines 73230-73365) performs these side effects. ALL of them remain in `_phase_trinity`, NOT in the pre-warmer:

| Side Effect | Current Location | Stays In |
|-------------|-----------------|----------|
| `os.environ["INVINCIBLE_NODE_IP"] = ip` | ~line 73315 | `_phase_trinity` after re-verification |
| `os.environ["INVINCIBLE_NODE_READY"] = "true"` | ~line 73316 | `_phase_trinity` after re-verification |
| `self._invincible_node_ip = ip` | ~line 73317 | `_phase_trinity` after re-verification |
| `notify_gcp_vm_ready(ip)` (routing layer) | ~line 73334 | `_phase_trinity` after re-verification |
| `_orch.acquire_gcp_lease()` | ~line 73322 | `_phase_trinity` after re-verification |
| `_orch._routing_policy.signal_gcp_ready()` | ~line 73330 | `_phase_trinity` after re-verification |
| `update_dashboard_gcp_progress()` | multiple lines | `_phase_trinity` (dashboard shows GCP progress only after Trinity starts) |
| `self._mark_startup_activity("gcp_verification")` | ~line 73307 | `_phase_trinity` (activity markers for ProgressController) |
| `self._deferred_prober` wiring | ~line 73258 | `_phase_trinity` after GCP manager ready |

### What Is NOT Pre-Warmed (and Why)

- **`backend.main` import** — Reads env vars set by `_phase_resources` (memory mode, ports). Import-time side effects not yet audited. Deferred to PR 2.
- **Port allocation** — Authoritative config, must happen in `_phase_resources` deterministically.
- **Any `os.environ` writes** — Owned exclusively by sequential phases.
- **Signal handlers** — Must install in main thread, in order.
- **BLAS/OMP thread config** (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`) — Owned by `_phase_resources`; native preload must not set these.
- **Dashboard updates, activity markers, routing notifications** — Owned by sequential phases (Invariant #5).

## StartupPreWarmer API

```python
class PreWarmStatus(Enum):
    PENDING = "pending"
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"

@dataclass
class PreWarmResult:
    status: PreWarmStatus
    value: Any = None
    error: Optional[str] = None
    timestamp: float = 0.0  # monotonic

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.timestamp

class StartupPreWarmer:
    def __init__(self, config: SystemKernelConfig, logger: Logger):
        self._config = config
        self._log = logger
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prewarm"
        )
        # Thread safety: _results is written by thread-pool workers and read
        # by the async event loop. In CPython, single-key dict assignment is
        # atomic under the GIL. If porting to a non-GIL runtime, add a
        # threading.Lock around _results access.
        self._results: Dict[str, PreWarmResult] = {}
        self._futures: Dict[str, Future] = {}  # thread pool futures
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._released_tasks: set = set()  # task names handed off
        self._shutdown_event = asyncio.Event()
        self._started = False
        self._disabled = os.environ.get(
            "JARVIS_PREWARM_DISABLED", ""
        ).lower() in ("true", "1", "yes")

    def start(self) -> None:
        """Fire all background pre-warm tasks. Non-blocking.
        No-op if disabled via JARVIS_PREWARM_DISABLED.
        Must be called from the same event loop as _startup_impl.
        Each thread callable is wrapped in try/except to always
        produce a PreWarmResult (never swallows exceptions silently)."""

    def get_result(self, name: str, max_age_s: float = 30.0) -> Optional[PreWarmResult]:
        """Get a pre-warm result. Returns the PreWarmResult if status is OK
        and age <= max_age_s. Returns None if:
        - Task doesn't exist or was never registered
        - Status is PENDING (phase should NOT await; run own path)
        - Status is FAILED or SKIPPED
        - Result is stale (age > max_age_s)

        Consumers who need to distinguish PENDING from FAILED can call
        get_status(name) instead."""

    def get_status(self, name: str) -> PreWarmStatus:
        """Get the current status of a pre-warm task without age filtering.
        Returns SKIPPED if the task was never registered."""

    def release_task(self, name: str) -> Optional[asyncio.Task]:
        """Release ownership of an async task to the caller.
        After release, shutdown() will NOT cancel this task.
        Used for GCP VM handoff to _phase_trinity.

        Returns the asyncio.Task (which may be done or still running)
        if it was registered and not yet released.
        Returns None if the task name was never registered or was
        already released by a prior call.

        The caller becomes the sole owner and is responsible for
        awaiting or cancelling the task."""

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop scheduling new work.
        1. Cancel un-released async tasks (released tasks are not touched).
        2. Call executor.shutdown(wait=False, cancel_futures=True) (Python 3.9+).
        3. Wait up to `timeout` seconds for thread futures via
           concurrent.futures.wait(futures, timeout=timeout).
        4. Log any threads still alive after timeout (stragglers).
        Does NOT block indefinitely — bounded by timeout parameter."""
```

### Disable Gate

```python
# In startup(), before creating pre-warmer:
if os.environ.get("JARVIS_PREWARM_DISABLED", "").lower() in ("true", "1", "yes"):
    self._prewarm = None
    # Skip pre-warmer entirely — sequential pipeline runs unmodified
else:
    prewarm = StartupPreWarmer(config=self.config, logger=self.logger)
    ...
```

### Consumer Pattern

```python
# In _phase_resources():
docker_result = (
    self._prewarm.get_result("docker_probe", max_age_s=30.0)
    if self._prewarm else None
)
if docker_result and docker_result.status == PreWarmStatus.OK and docker_result.value is True:
    self.logger.info("[Resources] Docker pre-warmed (%.1fs ago)", docker_result.age_s)
    # Skip probe, proceed to Docker operations
else:
    # Normal path — probe Docker ourselves
    ...
```

### GCP VM Task Handoff

```python
# In _phase_trinity():
# REPLACES the existing ensure_static_vm_ready() call at ~line 77390.
gcp_task = self._prewarm.release_task("gcp_vm_start") if self._prewarm else None
if gcp_task and not gcp_task.done():
    # Adopt the task — await with Trinity's own timeout
    try:
        await asyncio.wait_for(asyncio.shield(gcp_task), timeout=trinity_vm_timeout)
    except asyncio.TimeoutError:
        self.logger.warning("[Trinity] Pre-warmed GCP task timed out; running own provisioning")
        gcp_task.cancel()
        # Fall through to normal provisioning below
    else:
        # Pre-warm completed — extract result
        try:
            success, ip, status = gcp_task.result()
            if success:
                self.logger.info("[Trinity] GCP VM pre-warmed at %s — re-verifying health", ip)
                # Re-verify VM health before trusting (cheap HTTP ping)
                # Then perform all env var writes, routing notifications, etc.
                # (same code as the current _phase_trinity success path)
            else:
                self.logger.warning("[Trinity] Pre-warmed GCP returned failure: %s", status)
                # Fall through to normal provisioning
        except Exception as e:
            self.logger.warning("[Trinity] Pre-warmed GCP raised: %s", e)
            # Fall through to normal provisioning
elif gcp_task and gcp_task.done():
    # Task already resolved — check result
    try:
        success, ip, status = gcp_task.result()
        if success:
            self.logger.info("[Trinity] GCP VM pre-warmed at %s — re-verifying health", ip)
            # Re-verify, then env writes + routing (same as above)
        else:
            self.logger.warning("[Trinity] Pre-warmed GCP failed: %s", status)
    except Exception as e:
        self.logger.warning("[Trinity] Pre-warmed GCP raised: %s; running own provisioning", e)
else:
    # No pre-warm — normal provisioning path (same ensure_static_vm_ready call)
    ...

# IMPORTANT: All env var writes, routing notifications, dashboard updates,
# and activity markers happen HERE in _phase_trinity, not in the pre-warmer.
```

**Critical:** After `release_task()`, the pre-warmer no longer cancels that task on `shutdown()`. Only one owner at a time. The existing `ensure_static_vm_ready()` call in `_phase_trinity` (~line 77390) is replaced by this handoff pattern — not duplicated.

**Orphan prevention:** `_phase_trinity` must wrap the entire handoff in `try/finally` so that if Trinity itself is cancelled (e.g., boot timeout), the adopted `gcp_task` is also cancelled. Otherwise there is an orphan window between `release_task()` and task completion where no owner will cancel it.

```python
# In _phase_trinity():
gcp_task = self._prewarm.release_task("gcp_vm_start") if self._prewarm else None
try:
    # ... handoff logic as above ...
finally:
    if gcp_task and not gcp_task.done():
        gcp_task.cancel()
```

**Implementation note:** The handoff code in the spec shows two branches (task running vs task done) with nearly identical result-processing logic. The implementation SHOULD consolidate the common "process gcp_task result" logic into a helper function to avoid duplication.

### Thread Task Exception Handling

Every thread-pool callable is wrapped to always produce a `PreWarmResult`:

```python
def _wrap_thread_task(self, name: str, fn: Callable) -> Callable:
    """Wrap a thread callable to catch all exceptions and store result."""
    def wrapper():
        try:
            value = fn()
            self._results[name] = PreWarmResult(
                status=PreWarmStatus.OK, value=value,
                timestamp=time.monotonic(),
            )
            self._log.info("[PreWarm] %s: OK", name)
        except Exception as exc:
            self._results[name] = PreWarmResult(
                status=PreWarmStatus.FAILED, error=str(exc)[:200],
                timestamp=time.monotonic(),
            )
            self._log.warning("[PreWarm] %s: FAILED: %s", name, exc)
    return wrapper
```

This prevents silent exception swallowing and "exception was never retrieved" GC warnings.

## Integration Points

### Hook Placement (unified_supervisor.py startup())

```python
# ~line 70625, REPLACE the JARVIS_PARALLEL_BOOT block:
self._prewarm: Optional["StartupPreWarmer"] = None  # typed, always exists

_prewarm_disabled = os.environ.get(
    "JARVIS_PREWARM_DISABLED", ""
).lower() in ("true", "1", "yes")

if not _prewarm_disabled:
    try:
        from backend.core.startup_prewarmer import StartupPreWarmer
        _prewarm_inst = StartupPreWarmer(config=self.config, logger=self.logger)
        _prewarm_inst.start()
        self._prewarm = _prewarm_inst
    except Exception as _pw_err:
        self.logger.warning("[Kernel] Pre-warmer init failed (non-fatal): %s", _pw_err)

try:
    result = await progress_controller.run_with_progress_aware_timeout(
        self._startup_impl(),
        get_progress_state,
    )
    return result
finally:
    # Always shutdown — success, failure, cancellation, timeout
    if self._prewarm is not None:
        self._prewarm.shutdown(timeout=5.0)
        self._prewarm = None
```

### Restart Idempotency

`_phase_clean_slate` checks for and shuts down any lingering pre-warmer from a previous boot:

```python
if self._prewarm is not None:
    self._prewarm.shutdown(timeout=2.0)
    self._prewarm = None
```

## ParallelBootOrchestrator Removal

### Files to Delete
- `backend/core/parallel_boot.py` (500 lines) — entire file

### Code to Remove from unified_supervisor.py
- Lines ~70622-70658: `JARVIS_PARALLEL_BOOT` gate, `ParallelBootOrchestrator` import/call/fallback block (37 lines)
- Lines ~93373-93389: Entire parallel boot heartbeat suppression block (not just the env-var read)

### Proactive GCP Start Consolidation
- Remove `_proactive_gcp_vm_start()` definition and call site (~lines 73230-73365, ~120 lines)
- Remove the `ensure_static_vm_ready()` call inside `_phase_trinity` (~line 77390) and replace with the handoff consumer pattern
- Only one code path calls `ensure_static_vm_ready()` during boot: the pre-warmer's task #3

### References to Grep and Clean
```bash
grep -r 'JARVIS_PARALLEL_BOOT' --include='*.py' --include='*.env*' --include='*.md' --include='*.yml'
grep -r 'parallel_boot' --include='*.py'
grep -r 'ParallelBootOrchestrator' --include='*.py'
grep -r '_BootCLINarrator' --include='*.py'
```
Verified: `_BootCLINarrator` is only referenced in `parallel_boot.py` (goes with deletion).

## Loading Page Redirect Fix

### Current Issue
`loading_server.py` line 7698: `frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')` — hardcoded fallback with no readiness probe.

### Fix
After final readiness tier, resolve redirect URL via ordered strategy:

1. **`FRONTEND_URL` env var** (if set) — explicit override, used directly
2. **`JARVIS_FRONTEND_PROBE_URLS` env var** (if set) — comma-separated list of URLs to probe
3. **Default probe list** derived from config: `http://localhost:{config.frontend_port}` (default 3000)
4. **HTTP readiness probe** — probe all candidate URLs concurrently (asyncio.gather), first 2xx wins, 5s aggregate timeout. This prevents sequential worst-case (N urls * 3s each).
5. **Fallback** — if no frontend responds, show degraded-exit message in loading page:
   `"JARVIS is online — API available at http://localhost:{config.backend_port}"`
   with a clickable link. Loading page stops spinning, shows success state with API-only message.

### Port Consistency
- Frontend port from `config.frontend_port` (default from `JARVIS_FRONTEND_PORT` or `FRONTEND_PORT`, fallback 3000)
- Backend port from `config.backend_port` (dynamic, allocated in `_phase_resources`)
- Redirect URL and fallback message use config values, not hardcoded numbers

## Single Progress Publisher Audit

### Current State (After Removal)
With `_BootCLINarrator` and parallel boot heartbeat suppression removed, the remaining progress chain is:

1. **Publisher:** `_startup_impl` updates `self._startup_state` at each phase boundary
2. **Heartbeat:** Background task reads `_startup_state` and broadcasts via WebSocket to loading server
3. **Loading server:** Polls `/health/readiness-tier` endpoint (derived from same phase/progress)
4. **CLI renderer:** Subscribes to event bus
5. **Voice narrator:** Subscribes to event bus

### Atomic Update Rule
Phase and progress must be set atomically to prevent heartbeat desync. Use a single tuple assignment instead of two separate attribute writes:

```python
# BEFORE (two separate assignments — NOT atomic if heartbeat reads between them):
# self._current_startup_phase = "backend"
# self._current_startup_progress = 35

# AFTER (single assignment — atomic under CPython GIL):
self._startup_state = ("backend", 35)

# Heartbeat reads:
phase, progress = self._startup_state
```

**Why this is safe:** In CPython, a single attribute assignment (`self._startup_state = (...)`) is atomic because the GIL prevents another coroutine from running between the `STORE_ATTR` bytecode and the reference update. Two separate assignments have an `await`-free gap that is technically safe under the GIL but fragile — a future refactor could insert an `await` between them.

### Heartbeat Relay-Only Rule
The heartbeat task must ONLY relay `_startup_state`. If it currently has any logic that adjusts progress values (interpolation, smoothing, clamping), that logic must be documented and audited to ensure it doesn't create a second publisher.

## Verification Plan

### Test 1: Cold Boot — Sequential Only
- `JARVIS_PARALLEL_BOOT` removed (no env var needed)
- Run `python3 unified_supervisor.py`
- Verify: zones progress in order, dashboard shows correct phases, no progress races
- Verify: loading page reaches 100% iff readiness tier matches final state
- Verify: redirect works to frontend, or shows API-only fallback message

### Test 2: Pre-Warmer Disabled
- Set `JARVIS_PREWARM_DISABLED=true`
- Boot should be identical to pre-change behavior (no pre-warming)
- Same gates, same readiness, same output — just slower

### Test 3: Pre-Warmer Crash Resilience
- Inject failure in pre-warmer (e.g., bad Docker socket path)
- Sequential phases must run normally and surface real errors
- No silent failures, no half-done state

### Test 4: Restart Idempotency
- Boot, then restart twice in a row
- No orphan threads/tasks from pre-warmer
- No orphan listeners/processes
- `_phase_clean_slate` cleans up previous pre-warmer

### Test 5: GCP VM Handoff
- Boot with GCP enabled
- Verify: pre-warmer starts VM, Trinity adopts the task
- Verify: only one `ensure_static_vm_ready()` call happens (not two)
- Verify: if pre-warm VM fails, Trinity retries independently
- Verify: env var writes and routing notifications happen in `_phase_trinity`, not pre-warmer

### Test 6: Loading Page Redirect
- Boot with frontend running on default port -> redirect works
- Boot without frontend -> loading page shows API-only fallback (doesn't spin forever)
- Set `FRONTEND_URL` env var -> redirect uses that URL

## File Inventory

| File | Action | Lines Changed (est.) |
|------|--------|---------------------|
| `backend/core/startup_prewarmer.py` | **New** | ~350-400 |
| `backend/core/parallel_boot.py` | **Delete** | -500 |
| `unified_supervisor.py` | Edit: remove parallel boot gate (~37), remove proactive GCP start (~120), remove heartbeat suppression (~17), add pre-warmer hook (~20), add consumer checks in 3 phases (~30), update startup_state pattern (~15) | ~120 net removed |
| `loading_server.py` | Edit: fix redirect with concurrent probe + fallback | ~50 |
| `.env.example` (and any `.env`, `.env.template`) | Edit: remove `JARVIS_PARALLEL_BOOT`, add `JARVIS_PREWARM_DISABLED` | ~3 |

## Follow-Up (PR 2)

- **`backend.main` import audit:** Document all import-time side effects. Decision: defer heavy init behind explicit `initialize_app(config)` or confirm import is safe for pre-warming.
- **If safe:** Add `backend.main` pre-import as task #6 in the safety table.
- **If not safe:** Refactor `backend.main` to separate import from initialization. Then pre-import becomes safe.

## Out of Scope

- New features in Prime/Reactor repos (unless health endpoint contract)
- Frontend changes beyond redirect fix
- Changes to readiness tier definitions
- Performance benchmarking (that's validation, not design)
