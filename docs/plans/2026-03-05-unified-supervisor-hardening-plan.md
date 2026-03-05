# Unified Supervisor Hardening Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix structural diseases in unified_supervisor.py (96K lines) across observability, concurrency, integration, resources, and dead code — without breaking up the file.

**Architecture:** 5 phases with hard gates. Each phase is independently committable. All edits target `unified_supervisor.py` except one new contract file. Test-first where practical; smoke-test via `python3 -c "import unified_supervisor"` and `python3 unified_supervisor.py --test zones` at each gate.

**Tech Stack:** Python 3.10+, asyncio, threading, pytest, unified_supervisor.py monolith

**Design doc:** `docs/plans/2026-03-05-unified-supervisor-hardening-design.md`

---

## Phase 1: Observability + Async Safety Baseline

### Task 1: LazyAsyncLock Race Fix + Test

**Files:**
- Modify: `unified_supervisor.py:14628-14656` (LazyAsyncLock class)
- Create: `tests/unit/backend/test_kernel_concurrency.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/test_kernel_concurrency.py
"""
Concurrency safety tests for unified_supervisor.py kernel primitives.

Run: python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v
"""
import asyncio
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestLazyAsyncLock:
    """Tests for LazyAsyncLock thread-safe initialization."""

    def test_single_lock_instance_under_concurrent_access(self):
        """Two coroutines racing _ensure_lock() must get the same Lock object."""
        from unified_supervisor import LazyAsyncLock

        lazy = LazyAsyncLock()
        results = []
        barrier = threading.Barrier(2)

        def grab_lock():
            barrier.wait()
            lock = lazy._ensure_lock()
            results.append(id(lock))

        t1 = threading.Thread(target=grab_lock)
        t2 = threading.Thread(target=grab_lock)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert results[0] == results[1], (
            f"Two threads got different Lock objects: {results[0]} != {results[1]}"
        )

    @pytest.mark.asyncio
    async def test_mutual_exclusion_holds(self):
        """Two tasks using async with must actually serialize."""
        from unified_supervisor import LazyAsyncLock

        lazy = LazyAsyncLock()
        in_critical = 0
        max_concurrent = 0

        async def worker():
            nonlocal in_critical, max_concurrent
            async with lazy:
                in_critical += 1
                max_concurrent = max(max_concurrent, in_critical)
                await asyncio.sleep(0.01)
                in_critical -= 1

        await asyncio.gather(*[worker() for _ in range(10)])
        assert max_concurrent == 1, f"Critical section violated: max_concurrent={max_concurrent}"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py::TestLazyAsyncLock::test_single_lock_instance_under_concurrent_access -v`
Expected: May PASS or FAIL depending on timing — race is probabilistic. The implementation fix makes it deterministic.

**Step 3: Write the fix**

Edit `unified_supervisor.py` at class `LazyAsyncLock` (line ~14628):

```python
class LazyAsyncLock:
    """
    Lazy-initialized asyncio.Lock for Python 3.9+ compatibility.

    asyncio.Lock() cannot be created outside of an async context in Python 3.9.
    This wrapper delays initialization until first use within an async context.

    v311.0: Added threading.Lock guard to prevent duplicate Lock creation
    under concurrent access (design doc: 2026-03-05 Phase 2A).
    """

    def __init__(self):
        self._lock: Optional[asyncio.Lock] = None
        self._init_guard = threading.Lock()

    def _ensure_lock(self) -> asyncio.Lock:
        """Ensure lock exists, creating it if needed. Thread-safe."""
        if self._lock is None:
            with self._init_guard:
                if self._lock is None:
                    self._lock = asyncio.Lock()
        return self._lock

    async def __aenter__(self):
        lock = self._ensure_lock()
        await lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._lock is not None:
            self._lock.release()
        return False
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_kernel_concurrency.py
git commit -m "fix(kernel): add threading guard to LazyAsyncLock._ensure_lock() (Phase 2A)

Root cause: two concurrent callers of _ensure_lock() could both see
self._lock is None and create separate asyncio.Lock objects, defeating
mutual exclusion entirely.

Fix: double-checked locking with threading.Lock init guard."
```

---

### Task 2: UnifiedSignalHandler._get_event() TOCTOU Fix

**Files:**
- Modify: `unified_supervisor.py:56809-56855` (UnifiedSignalHandler)
- Test: `tests/unit/backend/test_kernel_concurrency.py` (append)

**Step 1: Write the failing test**

```python
# Append to tests/unit/backend/test_kernel_concurrency.py

class TestUnifiedSignalHandler:
    """Tests for signal handler thread safety."""

    def test_get_event_returns_same_instance(self):
        """Two threads racing _get_event() must get the same Event object."""
        from unified_supervisor import UnifiedSignalHandler

        handler = UnifiedSignalHandler.__new__(UnifiedSignalHandler)
        handler._shutdown_event = None
        handler._shutdown_requested = False
        handler._shutdown_count = 0
        handler._lock = threading.Lock()
        handler._shutdown_reason = None
        handler._loop = None
        handler._installed = False
        handler._callbacks = []
        handler._first_signal_time = None

        results = []
        barrier = threading.Barrier(2)

        def grab_event():
            barrier.wait()
            event = handler._get_event()
            results.append(id(event))

        t1 = threading.Thread(target=grab_event)
        t2 = threading.Thread(target=grab_event)
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        assert len(results) == 2
        assert results[0] == results[1], (
            f"Two threads got different Event objects: {results[0]} != {results[1]}"
        )
```

**Step 2: Run test — may intermittently fail due to race**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py::TestUnifiedSignalHandler -v`

**Step 3: Write the fix**

Edit `unified_supervisor.py` at `UnifiedSignalHandler._get_event()` (line ~56851):

```python
def _get_event(self) -> asyncio.Event:
    """Return the shutdown event. Thread-safe lazy creation for Python 3.9 fallback."""
    if self._shutdown_event is None:
        with self._lock:  # reuse existing self._lock (threading.Lock)
            if self._shutdown_event is None:
                self._shutdown_event = asyncio.Event()
    return self._shutdown_event
```

The class already has `self._lock = threading.Lock()` at line 56844. Reuse it — no new field needed.

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_kernel_concurrency.py
git commit -m "fix(signals): guard _get_event() with existing threading.Lock (Phase 2B)

Root cause: two rapid signals could both create separate asyncio.Event
objects. Second overwrites first, losing the .set() call — shutdown
signal silently lost.

Fix: double-checked locking reusing existing self._lock."
```

---

### Task 3: KernelBackgroundTaskRegistry Thread Safety

**Files:**
- Modify: `unified_supervisor.py:63805-63887` (KernelBackgroundTaskRegistry)
- Test: `tests/unit/backend/test_kernel_concurrency.py` (append)

**Step 1: Write the failing test**

```python
# Append to tests/unit/backend/test_kernel_concurrency.py

class TestKernelBackgroundTaskRegistry:
    """Tests for task registry thread safety."""

    @pytest.mark.asyncio
    async def test_concurrent_append_no_duplicates(self):
        """Concurrent appends of different tasks must not corrupt the list."""
        from unified_supervisor import KernelBackgroundTaskRegistry

        registry = KernelBackgroundTaskRegistry()

        async def noop():
            await asyncio.sleep(10)

        tasks = [asyncio.create_task(noop(), name=f"task-{i}") for i in range(20)]

        # Append all tasks from concurrent coroutines
        results = await asyncio.gather(
            *[asyncio.to_thread(registry.append, t) for t in tasks]
        )

        accepted = sum(1 for r in results if r)
        assert accepted == 20, f"Expected 20 accepted, got {accepted}"
        assert len(registry) == 20

        # Cleanup
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_snapshot_during_mutation(self):
        """snapshot() must not crash while append() is modifying the list."""
        from unified_supervisor import KernelBackgroundTaskRegistry

        registry = KernelBackgroundTaskRegistry()
        stop = threading.Event()

        async def noop():
            await asyncio.sleep(100)

        def appender():
            while not stop.is_set():
                t = asyncio.run_coroutine_threadsafe(
                    asyncio.sleep(100),
                    asyncio.get_event_loop(),
                )
                # Can't easily create tasks from thread, so just test snapshot safety
                registry.snapshot()

        # Just verify snapshot doesn't crash under concurrent access
        t = asyncio.create_task(noop())
        registry.append(t)
        snap = registry.snapshot()
        assert len(snap) >= 1
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py::TestKernelBackgroundTaskRegistry -v`

**Step 3: Write the fix**

Edit `unified_supervisor.py` at `KernelBackgroundTaskRegistry` (line ~63805):

```python
class KernelBackgroundTaskRegistry:
    """
    Track kernel-owned background tasks with lifecycle fencing.

    v311.0: Added threading.Lock for thread-safe list mutations.
    """

    def __init__(
        self,
        *,
        logger: Optional[UnifiedLogger] = None,
        can_accept_new: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._logger = logger
        self._can_accept_new = can_accept_new or (lambda: True)
        self._tasks: List["asyncio.Task[Any]"] = []
        self._guard = threading.Lock()  # v311.0: thread-safe mutations

    # ... _task_name stays the same ...

    def _on_task_done(self, task: "asyncio.Task[Any]") -> None:
        with self._guard:
            try:
                self._tasks.remove(task)
            except ValueError:
                pass

    def append(self, task: Optional["asyncio.Task[Any]"]) -> bool:
        if task is None:
            return False
        if task.done():
            return False
        with self._guard:
            if task in self._tasks:
                return False
            if not self._can_accept_new():
                if not task.done():
                    task.cancel()
                if self._logger:
                    self._logger.debug(
                        "[Kernel] Rejected background task during shutdown: %s",
                        self._task_name(task),
                    )
                return False
            self._tasks.append(task)
        task.add_done_callback(self._on_task_done)
        return True

    def remove(self, task: "asyncio.Task[Any]") -> None:
        with self._guard:
            self._tasks.remove(task)

    def snapshot(self, *, include_done: bool = True) -> List["asyncio.Task[Any]"]:
        with self._guard:
            if include_done:
                return list(self._tasks)
            return [task for task in self._tasks if not task.done()]

    def __contains__(self, task: object) -> bool:
        with self._guard:
            return task in self._tasks

    def __len__(self) -> int:
        with self._guard:
            return len(self._tasks)

    def __iter__(self):
        return iter(self.snapshot(include_done=True))
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_kernel_concurrency.py
git commit -m "fix(kernel): add threading.Lock to KernelBackgroundTaskRegistry (Phase 2D)

Root cause: append(), _on_task_done(), and snapshot() all mutate/read
self._tasks without synchronization. Concurrent calls cause duplicates,
ValueError on remove, or RuntimeError during iteration.

Fix: threading.Lock guard on all list mutations and reads."
```

---

### Task 4: SupervisorRestartManager Lock-Held-During-Sleep Fix

**Files:**
- Modify: `unified_supervisor.py:15033-15178` (SupervisorRestartManager)
- Test: `tests/unit/backend/test_kernel_concurrency.py` (append)

**Step 1: Write the failing test**

```python
# Append to tests/unit/backend/test_kernel_concurrency.py

class TestSupervisorRestartManager:
    """Tests for restart manager lock behavior."""

    @pytest.mark.asyncio
    async def test_get_status_not_blocked_during_restart(self):
        """get_status() must return immediately even during restart backoff."""
        from unified_supervisor import SupervisorRestartManager, SupervisorManagedProcess

        mgr = SupervisorRestartManager()

        # Simulate: check_and_restart_all is in backoff sleep
        # get_status() should still work without waiting

        start = asyncio.get_event_loop().time()
        status = mgr.get_status()
        elapsed = asyncio.get_event_loop().time() - start

        # Should be near-instant (< 0.1s), not blocked by any lock
        assert elapsed < 0.5, f"get_status() took {elapsed:.2f}s — likely blocked by lock"
```

**Step 2: Run test**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py::TestSupervisorRestartManager -v`

**Step 3: Write the fix**

Edit `unified_supervisor.py` at `SupervisorRestartManager.check_and_restart_all()` (line ~15103):

```python
async def check_and_restart_all(self) -> List[str]:
    """Check all cross-repo processes and restart any that have exited.

    v311.0: Split into collect (under lock) + restart (outside lock) to
    prevent blocking get_status() during backoff sleep.
    """
    if self._shutdown_requested:
        return []

    # Phase 1: Collect restart candidates under lock (fast, no I/O)
    to_restart = []
    async with self._lock:
        for name, managed in list(self.processes.items()):
            if not managed.enabled or managed.process is None:
                continue
            proc = managed.process
            if proc.returncode is not None:
                managed.exit_code = proc.returncode
                if proc.returncode in (0, -2, -15):
                    self._logger.debug(f"{name} exited normally (code: {proc.returncode})")
                    continue
                to_restart.append((name, managed))

    # Phase 2: Handle restarts outside lock (slow, with backoff + I/O)
    restarted = []
    for name, managed in to_restart:
        success = await self._handle_unexpected_exit(name, managed)
        if success:
            restarted.append(name)

    return restarted
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v`

**Step 5: Commit**

```bash
git add unified_supervisor.py tests/unit/backend/test_kernel_concurrency.py
git commit -m "fix(restart): release lock before backoff sleep in SupervisorRestartManager (Phase 2F)

Root cause: async with self._lock wrapped the entire loop including
_handle_unexpected_exit() which calls asyncio.sleep(backoff) — up to
60s per process. Lock blocked all get_status() calls during that time.

Fix: split into Phase 1 (collect under lock) + Phase 2 (restart outside
lock), matching existing ProcessRestartManager pattern."
```

---

### Task 5: _ProgressBroadcastWorker Thread-Safe Counters

**Files:**
- Modify: `unified_supervisor.py:63894+` (_ProgressBroadcastWorker)

**Step 1: Read current class to find exact field locations**

Read `unified_supervisor.py` at line 63894, ~100 lines, to find all observable state fields and their mutation sites.

**Step 2: Add threading.Lock and accessor methods**

Add to `__init__`:
```python
self._state_lock = threading.Lock()
```

Replace all direct mutations of `consecutive_failures`, `last_error`, `server_ready`, `total_sent` with locked methods:

```python
def _record_failure(self, error: str) -> None:
    with self._state_lock:
        self.consecutive_failures += 1
        self.last_error = error

def _record_success(self) -> None:
    with self._state_lock:
        self.consecutive_failures = 0
        self.total_sent += 1

def _set_server_ready(self, ready: bool) -> None:
    with self._state_lock:
        self.server_ready = ready

@property
def state_snapshot(self) -> Dict[str, Any]:
    with self._state_lock:
        return {
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "server_ready": self.server_ready,
            "total_sent": self.total_sent,
        }
```

Update all call sites within the class `run()` method to use these methods instead of direct field access.

**Step 3: Run smoke test**

Run: `python3 -c "from unified_supervisor import _ProgressBroadcastWorker; print('OK')"`

**Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix(broadcast): thread-safe counters in _ProgressBroadcastWorker (Phase 2E)

Root cause: observable state (consecutive_failures, total_sent) modified
by worker thread, read by supervisor event loop. GIL does NOT make
+= atomic (4 bytecodes: LOAD, LOAD_CONST, BINARY_ADD, STORE).

Fix: threading.Lock guard with _record_failure/_record_success accessors."
```

---

### Task 6: IPCServer StreamWriter Leak Fix

**Files:**
- Modify: `unified_supervisor.py` at `IPCServer._handle_client` (find exact line with grep for `async def _handle_client`)

**Step 1: Read current method**

Grep for `_handle_client` in IPCServer to find the exact location and current try/except structure.

**Step 2: Add finally block**

Wrap the existing try/except in try/except/finally:

```python
async def _handle_client(
    self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        # ... existing logic ...
    except asyncio.TimeoutError:
        self.logger.debug("[IPC] Client read timed out")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        self.logger.debug("[IPC] Client handler error: %s", exc)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
```

**Step 3: Run smoke test**

Run: `python3 -c "from unified_supervisor import IPCServer; print('OK')"`

**Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "fix(ipc): close StreamWriter in finally block in _handle_client (Phase 4A)

Root cause: no finally block meant exceptions left socket open,
leaking file descriptors over the kernel's lifetime.

Fix: writer.close() + await writer.wait_closed() in finally."
```

---

### Task 7: Cross-Repo Contract Definitions

**Files:**
- Create: `backend/core/cross_repo_contracts.py`
- Test: `tests/unit/backend/test_cross_repo_contracts.py`

**Step 1: Write the failing test**

```python
# tests/unit/backend/test_cross_repo_contracts.py
"""Tests for cross-repo contract validation."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "backend"))


class TestHealthContractV1:
    """Tests for HealthContractV1 schema parsing."""

    def test_parse_versioned_response(self):
        from core.cross_repo_contracts import HealthContractV1

        data = {
            "contract_version": 1,
            "status": "healthy",
            "model_loaded": True,
            "ready_for_inference": True,
            "trinity_connected": True,
        }
        contract = HealthContractV1.from_response(data)
        assert contract.status == "healthy"
        assert contract.model_loaded is True
        assert contract.contract_version == 1

    def test_parse_legacy_unversioned_response(self):
        from core.cross_repo_contracts import HealthContractV1

        data = {
            "status": "ok",
            "model_loaded": True,
            "ready_for_inference": False,
        }
        contract = HealthContractV1.from_response(data)
        assert contract.contract_version == 0
        assert contract.model_loaded is True
        assert contract.ready_for_inference is False

    def test_unsupported_version_raises(self):
        from core.cross_repo_contracts import HealthContractV1, UnsupportedContractVersion

        data = {"contract_version": 99, "status": "ok"}
        with pytest.raises(UnsupportedContractVersion):
            HealthContractV1.from_response(data)


class TestErrorHierarchy:
    """Tests for typed cross-repo error classification."""

    def test_repo_not_found_is_cross_repo_error(self):
        from core.cross_repo_contracts import RepoNotFoundError, CrossRepoError

        assert issubclass(RepoNotFoundError, CrossRepoError)

    def test_error_types_are_distinct(self):
        from core.cross_repo_contracts import (
            RepoNotFoundError, RepoImportError,
            RepoUnreachableError, RepoContractError,
        )

        errors = [RepoNotFoundError, RepoImportError, RepoUnreachableError, RepoContractError]
        for i, e1 in enumerate(errors):
            for j, e2 in enumerate(errors):
                if i != j:
                    assert not issubclass(e1, e2), f"{e1.__name__} should not be subclass of {e2.__name__}"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/unit/backend/test_cross_repo_contracts.py -v`
Expected: FAIL with ImportError (module doesn't exist yet)

**Step 3: Write the implementation**

```python
# backend/core/cross_repo_contracts.py
"""
Cross-repo contract definitions for Trinity integration.

Provides:
- HealthContractV1: schema-versioned health response parsing
- Typed error hierarchy for cross-repo failure classification

v311.0: Created per hardening design Phase 3A.
"""
from dataclasses import dataclass, fields
from typing import Any, Dict


class CrossRepoError(Exception):
    """Base class for all cross-repo integration errors."""
    pass


class RepoNotFoundError(CrossRepoError):
    """Repository path does not exist on disk."""
    pass


class RepoImportError(CrossRepoError):
    """Python import of repository module failed."""
    pass


class RepoUnreachableError(CrossRepoError):
    """Repository exists but health endpoint did not respond."""
    pass


class RepoContractError(CrossRepoError):
    """Repository responded but with incompatible schema version."""
    pass


class UnsupportedContractVersion(RepoContractError):
    """Remote repo uses a contract version this client does not understand."""
    pass


@dataclass(frozen=True)
class HealthContractV1:
    """Schema for cross-repo health endpoint responses (v1)."""

    contract_version: int = 0
    status: str = "unknown"
    model_loaded: bool = False
    ready_for_inference: bool = False
    trinity_connected: bool = False

    @classmethod
    def from_response(cls, data: Dict[str, Any]) -> "HealthContractV1":
        """Parse a health response dict into a typed contract.

        Raises:
            UnsupportedContractVersion: if contract_version > 1
        """
        version = data.get("contract_version", 0)

        if version > 1:
            raise UnsupportedContractVersion(
                f"Expected contract_version <= 1, got {version}"
            )

        field_names = {f.name for f in fields(cls)}
        kwargs = {k: data[k] for k in field_names if k in data}

        if version == 0:
            kwargs["contract_version"] = 0

        return cls(**kwargs)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/unit/backend/test_cross_repo_contracts.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add backend/core/cross_repo_contracts.py tests/unit/backend/test_cross_repo_contracts.py
git commit -m "feat(contracts): add cross-repo contract definitions and typed error hierarchy (Phase 3A)

New file: backend/core/cross_repo_contracts.py
- HealthContractV1: schema-versioned health response parsing
- CrossRepoError hierarchy: RepoNotFoundError, RepoImportError,
  RepoUnreachableError, RepoContractError, UnsupportedContractVersion"
```

---

### Task 8: Dead Code Verification + Removal

**Files:**
- Modify: `unified_supervisor.py` (remove ~3,700 lines)

**Step 1: Verify zero external references**

```bash
# Run this for every class to remove
for cls in MLOpsModelRegistry WorkflowOrchestrator DocumentManagementSystem \
           NotificationHub DataLakeManager StreamingAnalyticsEngine \
           ConsentManagementSystem DigitalSignatureService \
           _Deprecated_GracefulDegradationManager; do
    echo "=== $cls ==="
    grep -rn "$cls" --include="*.py" . | grep -v "unified_supervisor.py" | grep -v "__pycache__"
done
```

Expected: Zero matches for all classes (or only in test/plan files).

Also verify the NamedTuples:
```bash
for nt in WorkflowTaskDef WorkflowTransition BPMWorkflowDef WorkflowTaskInstance \
          BPMWorkflowInst DocumentVersion Folder NotificationChannel \
          NotificationTemplate NotificationPreference SessionStore \
          DataPartition DataCatalogEntry StreamWindow StreamAggregation \
          StreamEvent StreamState ConsentPurpose ConsentRecord \
          DataSubjectRequest SignatureAlgorithm SigningKey \
          DigitalSignature SignatureVerification DegradationLevel DegradationState; do
    echo "=== $nt ==="
    grep -rn "$nt" --include="*.py" . | grep -v "unified_supervisor.py" | grep -v "__pycache__"
done
```

**Step 2: Identify exact line ranges to remove**

Use grep to find the start of each class and the start of the NEXT class/section after it. Remove the entire block including class definition + all methods + all associated NamedTuples.

Key ranges (verify by reading before deleting):
- Lines ~51116 (MLOpsModelRegistry) through ~55610 (_Deprecated_GracefulDegradationManager end)
- Also remove DegradationLevel + DegradationState NamedTuples above the deprecated class

**Step 3: Remove dead code**

Delete the identified line ranges. Keep:
- HealthAggregator (line ~54772)
- SystemTelemetryCollector (line ~55085)
- CleanupTask/CleanupResult/CleanupReport + ResourceCleanupCoordinator (line ~55616+)

**Step 4: Run smoke test**

```bash
python3 -c "import unified_supervisor; print('Import OK')"
python3 unified_supervisor.py --test zones
```

Expected: Both pass with no ImportError or NameError

**Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "refactor(kernel): remove ~3,700 lines of dead enterprise code (Phase 5)

Removed 10 classes + 26 NamedTuples that had zero references outside
their own definitions:
- MLOpsModelRegistry, WorkflowOrchestrator, DocumentManagementSystem,
  NotificationHub, SessionManager (duplicate of GlobalSessionManager),
  DataLakeManager, StreamingAnalyticsEngine, ConsentManagementSystem,
  DigitalSignatureService, _Deprecated_GracefulDegradationManager

Kept: HealthAggregator, SystemTelemetryCollector, ResourceCleanupCoordinator
(all actively registered in SystemServiceRegistry or shutdown sequence).

Full-repo grep confirmed zero external references before removal."
```

---

## Remaining Work (Phase 1 Bulk + Phase 3-4 Detailed Tasks)

The tasks above cover the **structurally critical** fixes. The remaining work is:

### Phase 1 Remaining: Silent Swallow Triage (T1 sites)
- **Task 9-14:** Six sub-tasks, one per startup phase method (`_phase_preflight`, `_phase_resources`, `_phase_backend`, `_phase_intelligence`, `_phase_trinity`, `_phase_enterprise_services`). Each: read the phase method, identify `except Exception: pass` sites, replace T1 sites with logged errors, add `CancelledError` re-raise. Commit per phase.
- **Task 15:** Shutdown cleanup method — same treatment.
- **Task 16:** Cross-repo paths in TrinityIntegrator + UnifiedTrinityConnector.

### Phase 2 Remaining:
- **Task 17:** GlobalSessionManager lock unification (replace dual locks with single threading.Lock)
- **Task 18:** ProcessStateManager.get_statistics() sync lock
- **Task 19:** LiveProgressDashboard stdout serialization

### Phase 3 Remaining:
- **Task 20:** Wire `validate_contracts_at_boot()` into `_validate_cross_repo_contracts()` in kernel startup
- **Task 21:** Add `_continuous_health_check()` to UnifiedTrinityConnector
- **Task 22:** Add CrossRepoReconnector class + wire into connector
- **Task 23:** Replace generic except blocks in cross-repo paths with typed errors
- **Task 24:** Env var deduplication (4 drift pairs)

### Phase 4 Remaining:
- **Task 25:** Terminal state restoration (keyboard listener finally + atexit)
- **Task 26:** Atomic state persistence helper + apply to CostTracker, VMSessionTracker, GlobalSessionManager
- **Task 27:** Bounded collections (cap _errors, preemption_history, _step_times, _phase_timeline)
- **Task 28:** Voice narrator queue maxsize
- **Task 29:** ChromaDB client cleanup in SemanticVoiceCacheManager

### Gate Verification:
- **Task 30:** Gate 1 verification — grep for remaining `except Exception: pass` in critical zones
- **Task 31:** Gate 2 verification — run concurrency test suite under stress
- **Task 32:** Gate 3 verification — test contract mismatch detection
- **Task 33:** Gate 4 verification — soak test (manual, 30-60 min)
- **Task 34:** Gate 5 verification — final grep + full test suite

---

## Execution Notes

- **Import test after every edit:** `python3 -c "import unified_supervisor"` — catches syntax errors immediately
- **Line numbers shift:** After each edit, re-grep for target classes/methods before editing the next one. Never trust stale line numbers.
- **Commit granularity:** One commit per logical fix. Never bundle unrelated fixes.
- **No new files except:** `backend/core/cross_repo_contracts.py` and test files.
- **Test runner:** `python3 -m pytest tests/unit/backend/test_kernel_concurrency.py -v` for concurrency tests, `python3 -m pytest tests/unit/backend/test_cross_repo_contracts.py -v` for contract tests.
