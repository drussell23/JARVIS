# Sequential Boot Pre-Warming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore sequential boot as sole authority, add optional background pre-warming, remove ParallelBootOrchestrator, fix loading page redirect.

**Architecture:** Sequential `_startup_impl` stays unchanged. A new `StartupPreWarmer` fires safe background probes at T=0. Sequential phases check a result cache — if pre-warm succeeded and is fresh, skip redundant work; otherwise run normally. GCP VM task uses explicit ownership handoff. Loading page redirect resolves URL from config with concurrent HTTP probes.

**Tech Stack:** Python 3.10+, asyncio, concurrent.futures.ThreadPoolExecutor, aiohttp (for redirect probes)

**Spec:** `docs/superpowers/specs/2026-03-22-sequential-boot-prewarming-design.md`

**Important notes for implementers:**
- Tasks 4-6 are NOT independently deployable — between Task 4 (remove proactive GCP start) and Task 6 (add consumers), there is no GCP proactive start. These must ship together in one PR.
- `loading_server.py` already imports `aiohttp` at line 141 — no new import needed.
- The `.env` file (not `.env.example`) may contain `JARVIS_PARALLEL_BOOT=true` — remove it locally after Task 3.
- Cancelled async tasks appear as `FAILED` with `error="cancelled"` (no distinct CANCELLED status).

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `backend/core/startup_prewarmer.py` | **Create** | PreWarmStatus, PreWarmResult, StartupPreWarmer class (all 5 tasks, result cache, handoff, shutdown) |
| `tests/unit/core/test_startup_prewarmer.py` | **Create** | Unit tests for pre-warmer (result cache, staleness, handoff, shutdown, disable gate) |
| `unified_supervisor.py` | **Edit** | Remove parallel boot gate, remove proactive GCP start, remove heartbeat suppression, add pre-warmer hook + consumers |
| `backend/core/parallel_boot.py` | **Delete** | ParallelBootOrchestrator removal |
| `loading_server.py` | **Edit** | Fix redirect with JARVIS_FRONTEND_URL + concurrent probes + API-only fallback |
| `tests/unit/core/test_loading_redirect.py` | **Create** | Unit tests for redirect URL resolution |

---

### Task 1: Create StartupPreWarmer — Core API

**Files:**
- Create: `backend/core/startup_prewarmer.py`
- Create: `tests/unit/core/test_startup_prewarmer.py`

- [ ] **Step 1: Write failing tests for PreWarmResult and PreWarmStatus**

```python
# tests/unit/core/test_startup_prewarmer.py
"""Unit tests for StartupPreWarmer — result cache, staleness, handoff, shutdown."""
import asyncio
import time
import pytest
from unittest.mock import MagicMock

from backend.core.startup_prewarmer import (
    PreWarmStatus,
    PreWarmResult,
    StartupPreWarmer,
)


class TestPreWarmResult:
    def test_status_enum_values(self):
        assert PreWarmStatus.PENDING.value == "pending"
        assert PreWarmStatus.OK.value == "ok"
        assert PreWarmStatus.FAILED.value == "failed"
        assert PreWarmStatus.SKIPPED.value == "skipped"

    def test_result_age_monotonic(self):
        r = PreWarmResult(status=PreWarmStatus.OK, value=True, timestamp=time.monotonic())
        assert r.age_s < 1.0  # just created

    def test_result_default_timestamp_always_stale(self):
        r = PreWarmResult(status=PreWarmStatus.OK, value=True)
        # timestamp=0.0 means age_s is huge — always stale
        assert r.age_s > 100.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py -v --no-header 2>&1 | head -30`
Expected: ImportError — module doesn't exist yet

- [ ] **Step 3: Write PreWarmStatus, PreWarmResult, and StartupPreWarmer skeleton**

```python
# backend/core/startup_prewarmer.py
"""Background pre-warming for sequential boot pipeline.

Fires safe, non-mutating probes at T=0 before _startup_impl() runs.
Sequential phases check the result cache — if fresh and OK, skip
redundant work. If PENDING/FAILED/stale, run normally.

Invariants (from spec):
  - No mutation of authoritative boot config (env vars, ports, readiness)
  - No progress/dashboard updates
  - Same outcomes if pre-warmer is disabled or crashes
  - Single owner per async task (release_task for handoff)

See: docs/superpowers/specs/2026-03-22-sequential-boot-prewarming-design.md
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait as futures_wait
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


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
    timestamp: float = 0.0  # monotonic; 0.0 = sentinel "never set"

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.timestamp


class StartupPreWarmer:
    """Advisory background pre-warmer for the sequential boot pipeline.

    Thread safety: _results is written by thread-pool workers and read
    by the async event loop. In CPython, single-key dict assignment is
    atomic under the GIL. If porting to a non-GIL runtime, add a
    threading.Lock. If iterating _results, snapshot keys first.
    """

    def __init__(self, config: Any, log: Optional[logging.Logger] = None):
        self._config = config
        self._log = log or logger
        self._executor: Optional[ThreadPoolExecutor] = None
        self._results: Dict[str, PreWarmResult] = {}
        self._futures: Dict[str, Future] = {}
        self._async_tasks: Dict[str, asyncio.Task] = {}
        self._released_tasks: set = set()
        self._started = False
        self._disabled = os.environ.get(
            "JARVIS_PREWARM_DISABLED", ""
        ).lower() in ("true", "1", "yes")

    def start(self) -> None:
        """Fire all background pre-warm tasks. Non-blocking.
        No-op if disabled. Registers PENDING for each task before submission."""
        if self._disabled:
            self._log.info("[PreWarm] Disabled via JARVIS_PREWARM_DISABLED")
            return
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prewarm"
        )
        self._started = True
        self._log.info("[PreWarm] Starting background pre-warm tasks")
        # Task submissions added in Task 2

    def get_result(self, name: str, max_age_s: float = 30.0) -> Optional[PreWarmResult]:
        """Get a pre-warm result if OK and fresh. Returns None otherwise."""
        r = self._results.get(name)
        if r is None:
            return None
        if r.status != PreWarmStatus.OK:
            return None
        if r.age_s > max_age_s:
            return None
        return r

    def get_status(self, name: str) -> PreWarmStatus:
        """Get current status without age filtering. SKIPPED if unregistered."""
        r = self._results.get(name)
        return r.status if r else PreWarmStatus.SKIPPED

    def release_task(self, name: str) -> Optional[asyncio.Task]:
        """Release ownership of an async task to the caller.
        After release, shutdown() will NOT cancel this task.
        Returns None if never registered or already released."""
        if name in self._released_tasks or name not in self._async_tasks:
            return None
        self._released_tasks.add(name)
        return self._async_tasks[name]

    def shutdown(self, timeout: float = 5.0) -> None:
        """Bounded shutdown: cancel unreleased async tasks, stop executor."""
        if not self._started:
            return
        self._started = False

        # 1. Cancel un-released async tasks
        for name, task in self._async_tasks.items():
            if name not in self._released_tasks and not task.done():
                task.cancel()
                self._log.info("[PreWarm] Cancelled async task: %s", name)

        # 2. Shutdown thread executor (Python 3.9+)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

            # 3. Bounded wait for thread futures
            pending = [f for f in self._futures.values() if not f.done()]
            if pending:
                done, not_done = futures_wait(pending, timeout=timeout)
                if not_done:
                    self._log.warning(
                        "[PreWarm] %d thread tasks still running after %.1fs shutdown",
                        len(not_done), timeout,
                    )
            self._executor = None

        self._log.info("[PreWarm] Shutdown complete")

    def _register_pending(self, name: str) -> None:
        """Register a task as PENDING before submitting work."""
        self._results[name] = PreWarmResult(
            status=PreWarmStatus.PENDING,
            timestamp=time.monotonic(),
        )

    def _submit_thread(self, name: str, fn: Callable) -> None:
        """Submit a thread-pool task with exception wrapping."""
        self._register_pending(name)

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

        self._futures[name] = self._executor.submit(wrapper)

    def _submit_async(self, name: str, coro_fn: Callable) -> None:
        """Submit an async task with exception wrapping."""
        self._register_pending(name)

        async def wrapper():
            try:
                value = await coro_fn()
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.OK, value=value,
                    timestamp=time.monotonic(),
                )
                self._log.info("[PreWarm] %s: OK", name)
            except asyncio.CancelledError:
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.FAILED, error="cancelled",
                    timestamp=time.monotonic(),
                )
                raise
            except Exception as exc:
                self._results[name] = PreWarmResult(
                    status=PreWarmStatus.FAILED, error=str(exc)[:200],
                    timestamp=time.monotonic(),
                )
                self._log.warning("[PreWarm] %s: FAILED: %s", name, exc)

        self._async_tasks[name] = asyncio.create_task(
            wrapper(), name=f"prewarm_{name}"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py -v --no-header 2>&1 | head -30`
Expected: 3 tests PASS

- [ ] **Step 5: Add tests for get_result, get_status, disable gate**

```python
# Append to tests/unit/core/test_startup_prewarmer.py

class TestStartupPreWarmerAPI:
    def test_get_result_returns_none_for_unknown_task(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.get_result("nonexistent") is None

    def test_get_result_returns_none_for_pending(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.PENDING, timestamp=time.monotonic()
        )
        assert pw.get_result("test") is None

    def test_get_result_returns_none_for_failed(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.FAILED, error="boom", timestamp=time.monotonic()
        )
        assert pw.get_result("test") is None

    def test_get_result_returns_result_when_ok_and_fresh(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.OK, value=42, timestamp=time.monotonic()
        )
        r = pw.get_result("test", max_age_s=30.0)
        assert r is not None
        assert r.value == 42

    def test_get_result_returns_none_when_stale(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.OK, value=42, timestamp=time.monotonic() - 60
        )
        assert pw.get_result("test", max_age_s=30.0) is None

    def test_get_status_returns_skipped_for_unknown(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.get_status("nonexistent") == PreWarmStatus.SKIPPED

    def test_get_status_returns_pending(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._results["test"] = PreWarmResult(
            status=PreWarmStatus.PENDING, timestamp=time.monotonic()
        )
        assert pw.get_status("test") == PreWarmStatus.PENDING

    def test_disabled_start_is_noop(self):
        import os
        os.environ["JARVIS_PREWARM_DISABLED"] = "true"
        try:
            pw = StartupPreWarmer(config=MagicMock())
            pw.start()
            assert pw._started is False
            assert pw._executor is None
        finally:
            os.environ.pop("JARVIS_PREWARM_DISABLED", None)
```

- [ ] **Step 6: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py -v --no-header 2>&1 | head -30`
Expected: All tests PASS

- [ ] **Step 7: Add tests for release_task and shutdown**

```python
# Append to tests/unit/core/test_startup_prewarmer.py

class TestReleaseAndShutdown:
    @pytest.mark.asyncio
    async def test_release_task_returns_task(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["gcp_vm_start"] = task
        pw._register_pending("gcp_vm_start")

        released = pw.release_task("gcp_vm_start")
        assert released is task
        assert "gcp_vm_start" in pw._released_tasks

        # Second release returns None
        assert pw.release_task("gcp_vm_start") is None

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_release_task_unknown_returns_none(self):
        pw = StartupPreWarmer(config=MagicMock())
        assert pw.release_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_shutdown_cancels_unreleased_tasks(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["test_task"] = task
        pw._register_pending("test_task")

        pw.shutdown(timeout=1.0)
        assert task.cancelled()

    @pytest.mark.asyncio
    async def test_shutdown_skips_released_tasks(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def dummy():
            await asyncio.sleep(100)

        task = asyncio.create_task(dummy())
        pw._async_tasks["gcp"] = task
        pw._register_pending("gcp")
        pw.release_task("gcp")

        pw.shutdown(timeout=1.0)
        assert not task.cancelled()  # Released — not cancelled by shutdown

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    def test_shutdown_noop_when_not_started(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_register_pending_before_submit(self):
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        pw._submit_thread("docker_probe", lambda: True)
        # Immediately after submit, status should be at least PENDING
        status = pw.get_status("docker_probe")
        assert status in (PreWarmStatus.PENDING, PreWarmStatus.OK)

        pw.shutdown(timeout=2.0)
```

- [ ] **Step 8: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py -v --no-header 2>&1 | head -40`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
git add backend/core/startup_prewarmer.py tests/unit/core/test_startup_prewarmer.py
git commit -m "feat(boot): add StartupPreWarmer core API — result cache, handoff, shutdown"
```

---

### Task 2: Add Pre-Warm Task Implementations

**Files:**
- Modify: `backend/core/startup_prewarmer.py`
- Modify: `tests/unit/core/test_startup_prewarmer.py`

- [ ] **Step 1: Write failing tests for the 5 pre-warm tasks**

```python
# Append to tests/unit/core/test_startup_prewarmer.py
import tempfile
import pathlib
from unittest.mock import patch, AsyncMock


class TestPreWarmTasks:
    def test_docker_probe_success(self):
        """Docker probe returns True when socket responds."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        # Mock socket to simulate Docker daemon response
        with patch("backend.core.startup_prewarmer.socket") as mock_socket:
            mock_sock = MagicMock()
            mock_socket.socket.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_socket.socket.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.recv.return_value = b"HTTP/1.1 200 OK"

            pw._start_docker_probe()

            # Wait for thread to complete
            pw.shutdown(timeout=5.0)

            r = pw._results.get("docker_probe")
            assert r is not None
            assert r.status == PreWarmStatus.OK
            assert r.value is True

    def test_docker_probe_failure(self):
        """Docker probe returns FAILED when socket errors."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        with patch("backend.core.startup_prewarmer.socket") as mock_socket:
            mock_socket.socket.return_value.__enter__ = MagicMock(
                side_effect=ConnectionRefusedError("Docker not running")
            )

            pw._start_docker_probe()
            pw.shutdown(timeout=5.0)

            r = pw._results.get("docker_probe")
            assert r is not None
            assert r.status == PreWarmStatus.FAILED

    def test_gguf_scan_finds_models(self):
        """GGUF scan finds .gguf files in a temp directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create fake model files
            (pathlib.Path(tmpdir) / "model-7b.gguf").write_bytes(b"fake")
            (pathlib.Path(tmpdir) / "model-13b.gguf").write_bytes(b"fake2")
            (pathlib.Path(tmpdir) / "readme.txt").write_bytes(b"not a model")

            pw = StartupPreWarmer(config=MagicMock())
            pw._started = True
            pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

            pw._start_gguf_scan(models_dir=tmpdir)
            pw.shutdown(timeout=5.0)

            r = pw._results.get("gguf_scan")
            assert r is not None
            assert r.status == PreWarmStatus.OK
            assert len(r.value) == 2
            assert all(entry[0].endswith(".gguf") for entry in r.value)

    def test_gguf_scan_empty_dir_is_valid(self):
        """Empty GGUF directory is valid (remote-only mode)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            pw = StartupPreWarmer(config=MagicMock())
            pw._started = True
            pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

            pw._start_gguf_scan(models_dir=tmpdir)
            pw.shutdown(timeout=5.0)

            r = pw._results.get("gguf_scan")
            assert r is not None
            assert r.status == PreWarmStatus.OK
            assert r.value == []

    @pytest.mark.asyncio
    async def test_gcp_creds_mock(self):
        """GCP creds task stores client ref on success."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        with patch("backend.core.startup_prewarmer.compute_v1") as mock_compute:
            mock_compute.InstancesClient.return_value = MagicMock()
            pw._start_gcp_creds()
            pw.shutdown(timeout=5.0)

            r = pw._results.get("gcp_creds")
            assert r is not None
            assert r.status == PreWarmStatus.OK

    @pytest.mark.asyncio
    async def test_gcp_vm_async_submission(self):
        """GCP VM async task registers PENDING immediately."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        with patch("backend.core.startup_prewarmer.get_gcp_vm_manager") as mock_mgr:
            mock_instance = AsyncMock()
            mock_instance.is_static_vm_mode = False
            mock_mgr.return_value = mock_instance

            pw._start_gcp_vm()
            # Should be PENDING immediately after submission
            assert pw.get_status("gcp_vm_start") == PreWarmStatus.PENDING

            # Wait for async task to resolve
            await asyncio.sleep(0.1)
            assert pw.get_status("gcp_vm_start") == PreWarmStatus.OK

            pw.shutdown(timeout=2.0)

    @pytest.mark.asyncio
    async def test_submit_async_cancellation_stores_failed(self):
        """Cancelled async task transitions to FAILED with error='cancelled'."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True

        async def slow():
            await asyncio.sleep(100)
            return "should not reach"

        pw._submit_async("slow_task", slow)
        assert pw.get_status("slow_task") == PreWarmStatus.PENDING

        # Cancel the task
        pw._async_tasks["slow_task"].cancel()
        await asyncio.sleep(0.1)

        r = pw._results.get("slow_task")
        assert r is not None
        assert r.status == PreWarmStatus.FAILED
        assert r.error == "cancelled"

    def test_native_preload_succeeds(self):
        """Native preload imports standard library modules without error."""
        pw = StartupPreWarmer(config=MagicMock())
        pw._started = True
        pw._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test")

        # Use safe stdlib modules for test
        pw._start_native_preload(modules=["json", "os", "sys"])
        pw.shutdown(timeout=5.0)

        r = pw._results.get("native_libs")
        assert r is not None
        assert r.status == PreWarmStatus.OK
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py::TestPreWarmTasks -v --no-header 2>&1 | head -20`
Expected: AttributeError — `_start_docker_probe` etc. not defined

- [ ] **Step 3: Implement the 5 pre-warm task methods**

Add to `backend/core/startup_prewarmer.py` inside `StartupPreWarmer`:

```python
    # --- Pre-warm task implementations ---

    def _start_docker_probe(self) -> None:
        """Task #1: Probe Docker daemon via socket ping."""
        def probe():
            import socket as _socket
            # Check DOCKER_HOST first, then common socket paths
            import os as _os
            docker_host = _os.environ.get("DOCKER_HOST", "")
            if docker_host.startswith("unix://"):
                sock_path = docker_host[len("unix://"):]
            elif _os.path.exists("/var/run/docker.sock"):
                sock_path = "/var/run/docker.sock"
            else:
                sock_path = _os.path.expanduser("~/.docker/run/docker.sock")
            with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as sock:
                sock.settimeout(15.0)  # matches spec Safety Table timeout
                sock.connect(sock_path)
                sock.sendall(b"GET /_ping HTTP/1.1\r\nHost: localhost\r\n\r\n")
                resp = sock.recv(256)
                return b"200" in resp
        self._submit_thread("docker_probe", probe)

    def _start_gcp_creds(self) -> None:
        """Task #2: Validate GCP credentials and create API client."""
        def validate():
            from google.cloud import compute_v1
            client = compute_v1.InstancesClient()
            # Client creation validates credentials implicitly
            return client
        self._submit_thread("gcp_creds", validate)

    def _start_gcp_vm(self) -> None:
        """Task #3: Proactive GCP VM start (idempotent).
        Does NOT write env vars, dashboard, routing. Only caches result."""
        async def start_vm():
            from backend.core.gcp_vm_manager import get_gcp_vm_manager
            manager = await get_gcp_vm_manager()
            if not manager.is_static_vm_mode:
                self._log.info("[PreWarm] gcp_vm_start: not in static mode — skipping")
                return (False, None, "not_static_mode")
            # Call with NO progress_callback or activity_callback —
            # those are kernel mutations (Invariant #2). Port and timeout
            # use defaults; Trinity will re-verify with full params anyway.
            success, ip, status = await manager.ensure_static_vm_ready()
            return (success, ip, status)
        self._submit_async("gcp_vm_start", start_vm)

    def _start_native_preload(self, modules: Optional[list] = None) -> None:
        """Task #4: Import heavy native libraries in background thread."""
        if modules is None:
            modules = [
                "numpy", "scipy", "sounddevice", "soundfile",
                "webrtcvad", "PIL",
            ]

        def preload():
            imported = []
            for mod in modules:
                try:
                    __import__(mod)
                    imported.append(mod)
                except ImportError:
                    pass  # Optional dependency — skip silently
            return imported
        self._submit_thread("native_libs", preload)

    def _start_gguf_scan(self, models_dir: Optional[str] = None) -> None:
        """Task #5: Scan for GGUF model files on disk."""
        if models_dir is None:
            models_dir = os.environ.get(
                "PRIME_MODELS_DIR",
                os.path.expanduser("~/.jarvis/models"),
            )

        def scan():
            import pathlib
            p = pathlib.Path(models_dir)
            if not p.is_dir():
                return []
            results = []
            for f in p.glob("*.gguf"):
                stat = f.stat()
                results.append((str(f), stat.st_size, stat.st_mtime))
            return results
        self._submit_thread("gguf_scan", scan)
```

Also update `start()` to call them:

```python
    def start(self) -> None:
        if self._disabled:
            self._log.info("[PreWarm] Disabled via JARVIS_PREWARM_DISABLED")
            return
        self._executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="prewarm"
        )
        self._started = True
        self._log.info("[PreWarm] Starting background pre-warm tasks")

        # Task 1: Docker daemon probe
        try:
            self._start_docker_probe()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start docker_probe: %s", e)

        # Task 2: GCP credential validation
        if self._config and getattr(self._config, 'gcp_enabled', False):
            try:
                self._start_gcp_creds()
            except Exception as e:
                self._log.warning("[PreWarm] Failed to start gcp_creds: %s", e)

        # Task 3: GCP VM proactive start
        if self._config and getattr(self._config, 'gcp_enabled', False):
            try:
                self._start_gcp_vm()
            except Exception as e:
                self._log.warning("[PreWarm] Failed to start gcp_vm_start: %s", e)

        # Task 4: Native library preload
        try:
            self._start_native_preload()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start native_libs: %s", e)

        # Task 5: GGUF model file scan
        try:
            self._start_gguf_scan()
        except Exception as e:
            self._log.warning("[PreWarm] Failed to start gguf_scan: %s", e)

        self._log.info(
            "[PreWarm] Submitted %d thread tasks, %d async tasks",
            len(self._futures), len(self._async_tasks),
        )
```

Add `import socket` near the top of the file (but inside the probe function to avoid import at module level — it's already in stdlib).

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py -v --no-header 2>&1 | tail -20`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/core/startup_prewarmer.py tests/unit/core/test_startup_prewarmer.py
git commit -m "feat(boot): add 5 pre-warm task implementations — Docker, GCP, native libs, GGUF"
```

---

### Task 3: Remove ParallelBootOrchestrator

**Files:**
- Delete: `backend/core/parallel_boot.py`
- Modify: `unified_supervisor.py` (lines ~70622-70658, ~93373-93389)

- [ ] **Step 1: Grep for all references**

Run:
```bash
grep -rn 'JARVIS_PARALLEL_BOOT\|parallel_boot\|ParallelBootOrchestrator\|_BootCLINarrator' --include='*.py' --include='*.env*' --include='*.md' --include='*.yml' | grep -v '.pyc' | grep -v 'design.md' | grep -v 'plans/'
```
Expected: References only in `parallel_boot.py`, `unified_supervisor.py`, and possibly `.env` files.

- [ ] **Step 2: Delete `parallel_boot.py`**

```bash
git rm backend/core/parallel_boot.py
```

- [ ] **Step 3: Remove parallel boot gate from `unified_supervisor.py` (~lines 70622-70658)**

In `unified_supervisor.py`, find the block starting with:
```python
        _use_parallel_boot = os.environ.get(
```
and ending with:
```python
            os.environ.pop("JARVIS_STARTUP_COMPLETE", None)
```

Replace the entire block (lines 70622-70658) with nothing — just let it fall through to the sequential `try:` block on line 70660.

- [ ] **Step 4: Remove heartbeat suppression block (~lines 93373-93389)**

In `unified_supervisor.py`, find the block:
```python
                # v350.4: During parallel boot, the ProgressiveReadiness DAG
```
through:
```python
                    continue  # Skip progress broadcast — DAG is authoritative
```

Remove lines 93373-93389 entirely. Verify the heartbeat loop now only relays `_startup_state`.

- [ ] **Step 5: Remove any `.env` references to `JARVIS_PARALLEL_BOOT`**

Run: `grep -rn 'JARVIS_PARALLEL_BOOT' --include='*.env*'`

Remove any lines found.

- [ ] **Step 6: Verify boot still works without parallel boot**

Run: `python3 -c "from unified_supervisor import UnifiedSupervisor; print('Import OK')"`
Expected: No import errors

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(boot): remove ParallelBootOrchestrator — one boot path only"
```

---

### Task 4: Remove Proactive GCP Start from `_startup_impl`

**Files:**
- Modify: `unified_supervisor.py` (~lines 73242-73365)

- [ ] **Step 1: Read the proactive GCP start block**

Read `unified_supervisor.py` lines 73230-73370 to understand the full block including the `if self.config.gcp_enabled and self.config.gcp_proactive_start:` guard and the `create_safe_task` call.

- [ ] **Step 2: Remove the proactive GCP start block**

Find the block starting with the GCP proactive start comment/guard (around line 73242) through the `self._background_tasks.append(self._proactive_gcp_task)` (line 73365). Remove the entire block.

**Important:** Do NOT remove the `self._proactive_gcp_task` attribute initialization elsewhere (if it exists as `None` in `__init__`). Only remove the definition and call site inside `_startup_impl`.

- [ ] **Step 3: Verify no orphan references**

Run: `grep -n '_proactive_gcp_task\|_proactive_gcp_vm_start' unified_supervisor.py | head -10`

Any remaining references should be attribute initialization (`= None`) or cleanup, not execution.

- [ ] **Step 4: Commit**

```bash
git add unified_supervisor.py
git commit -m "refactor(boot): move proactive GCP VM start to StartupPreWarmer"
```

---

### Task 5: Hook StartupPreWarmer into `startup()`

**Files:**
- Modify: `unified_supervisor.py` (~line 70622, replacing removed parallel boot gate)

- [ ] **Step 1: Add the pre-warmer hook**

At the location where the `JARVIS_PARALLEL_BOOT` gate was removed (now just before the `try: return await progress_controller.run_with_progress_aware_timeout(...)` block), add:

```python
        # v360.0: Optional background pre-warming (advisory, non-mutating)
        # Pre-warmer fires safe probes at T=0. Sequential phases check the
        # result cache — if fresh, skip redundant work. See spec:
        # docs/superpowers/specs/2026-03-22-sequential-boot-prewarming-design.md
        self._prewarm: Optional[Any] = None  # type: StartupPreWarmer

        _prewarm_disabled = os.environ.get(
            "JARVIS_PREWARM_DISABLED", ""
        ).lower() in ("true", "1", "yes")

        if not _prewarm_disabled:
            try:
                from backend.core.startup_prewarmer import StartupPreWarmer
                _prewarm_inst = StartupPreWarmer(
                    config=self.config, log=self.logger
                )
                _prewarm_inst.start()
                self._prewarm = _prewarm_inst
                self.logger.info("[Kernel] StartupPreWarmer started")
            except Exception as _pw_err:
                self.logger.warning(
                    "[Kernel] Pre-warmer init failed (non-fatal): %s", _pw_err
                )
```

- [ ] **Step 2: Wrap the progress_controller call in try/finally for shutdown**

```python
        try:
            return await progress_controller.run_with_progress_aware_timeout(
                self._startup_impl(),
                get_progress_state,
            )
        except asyncio.TimeoutError as e:
            # ... existing timeout handling ...
        finally:
            # v360.0: Always shutdown pre-warmer
            if self._prewarm is not None:
                try:
                    self._prewarm.shutdown(timeout=5.0)
                except Exception as _pw_sd_err:
                    self.logger.warning(
                        "[Kernel] Pre-warmer shutdown error: %s", _pw_sd_err
                    )
                self._prewarm = None
```

- [ ] **Step 3: Add cleanup in `_phase_clean_slate`**

In `_phase_clean_slate` (~line 88958), add near the top:

```python
        # v360.0: Clean up any lingering pre-warmer from previous boot
        if self._prewarm is not None:
            try:
                self._prewarm.shutdown(timeout=2.0)
            except Exception:
                pass
            self._prewarm = None
```

- [ ] **Step 4: Verify import works**

Run: `python3 -c "from unified_supervisor import UnifiedSupervisor; print('Import OK')"`
Expected: No errors

- [ ] **Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(boot): hook StartupPreWarmer into startup() with try/finally shutdown"
```

---

### Task 6: Add Pre-Warm Consumers in Sequential Phases

**Files:**
- Modify: `unified_supervisor.py` — `_phase_resources` (~line 77005), `_phase_intelligence` (~line 79223), `_phase_trinity` (~line 84803)

- [ ] **Step 1: Add Docker probe consumer in `_phase_resources`**

Near the top of `_phase_resources()` (~line 77005), before the existing Docker probe logic, add:

```python
        # v360.0: Check pre-warm cache for Docker probe
        _docker_prewarmed = False
        if self._prewarm:
            _docker_result = self._prewarm.get_result("docker_probe", max_age_s=30.0)
            if _docker_result and _docker_result.value is True:
                self.logger.info(
                    "[Resources] Docker daemon pre-warmed (%.1fs ago)",
                    _docker_result.age_s,
                )
                _docker_prewarmed = True
```

Then wrap the existing Docker probe in `if not _docker_prewarmed:`.

- [ ] **Step 2: Add GGUF scan consumer in `_phase_intelligence`**

Near the start of `_phase_intelligence()` (~line 79223), before any GGUF model discovery logic, add:

```python
        # v360.0: Check pre-warm cache for GGUF model scan
        # Per spec: re-scan if stale by TTL OR if dir mtime changed since scan
        _gguf_prewarmed = None
        if self._prewarm:
            _gguf_result = self._prewarm.get_result("gguf_scan", max_age_s=60.0)
            if _gguf_result:
                # Verify dir hasn't changed since scan (mtime check)
                import pathlib
                _models_dir = os.environ.get("PRIME_MODELS_DIR", os.path.expanduser("~/.jarvis/models"))
                _dir_path = pathlib.Path(_models_dir)
                _dir_mtime_ok = True
                if _dir_path.is_dir():
                    _current_mtime = _dir_path.stat().st_mtime
                    # Check if any cached entry has older mtime than dir
                    if _gguf_result.value and any(
                        entry[2] < _current_mtime for entry in _gguf_result.value
                    ):
                        _dir_mtime_ok = False
                if _dir_mtime_ok:
                    _gguf_prewarmed = _gguf_result.value
                    self.logger.info(
                        "[Intelligence] GGUF scan pre-warmed: %d models (%.1fs ago)",
                        len(_gguf_prewarmed), _gguf_result.age_s,
                    )
                else:
                    self.logger.info("[Intelligence] GGUF dir modified since pre-warm — re-scanning")
```

Then pass `_gguf_prewarmed` to the model discovery code where applicable.

- [ ] **Step 3: Add GCP VM handoff consumer in `_phase_trinity`**

In `_phase_trinity()` (~line 84803), find the existing `ensure_static_vm_ready()` call (~line 77390 area within the method). Replace it with the handoff pattern from the spec:

```python
        # v360.0: GCP VM handoff from pre-warmer
        gcp_task = self._prewarm.release_task("gcp_vm_start") if self._prewarm else None
        gcp_handoff_consumed = False
        try:
            if gcp_task:
                if gcp_task.done():
                    try:
                        _vm_result = gcp_task.result()
                        if _vm_result and _vm_result[0]:  # (success, ip, status)
                            self.logger.info(
                                "[Trinity] GCP VM pre-warmed at %s — re-verifying",
                                _vm_result[1],
                            )
                            # Re-verify health, then do env writes + routing
                            # ... existing success path with _vm_result[1] as ip ...
                            gcp_handoff_consumed = True
                        else:
                            self.logger.warning(
                                "[Trinity] Pre-warmed GCP returned: %s",
                                _vm_result[2] if _vm_result else "no result",
                            )
                    except Exception as e:
                        self.logger.warning("[Trinity] Pre-warmed GCP raised: %s", e)
                else:
                    # Task still running — await with Trinity's timeout
                    try:
                        _vm_result = await asyncio.wait_for(
                            asyncio.shield(gcp_task),
                            timeout=_cap_phase_timeout(300.0, context="gcp_vm_handoff"),
                        )
                        if _vm_result and _vm_result[0]:
                            self.logger.info(
                                "[Trinity] GCP VM ready at %s — re-verifying",
                                _vm_result[1],
                            )
                            gcp_handoff_consumed = True
                        else:
                            self.logger.warning("[Trinity] Pre-warmed GCP: %s", _vm_result)
                    except asyncio.TimeoutError:
                        self.logger.warning("[Trinity] Pre-warmed GCP timed out — provisioning fresh")
                        gcp_task.cancel()

            if not gcp_handoff_consumed:
                # Normal provisioning path — no pre-warm or pre-warm failed
                success, ip, status = await manager.ensure_static_vm_ready()
                # ... existing provisioning success/failure handling ...
        finally:
            if gcp_task and not gcp_task.done() and not gcp_handoff_consumed:
                gcp_task.cancel()
```

**Note:** The exact integration depends on the structure around the `ensure_static_vm_ready` call. Read lines 77380-77400 and the surrounding context before editing.

- [ ] **Step 4: Verify no duplicate `ensure_static_vm_ready` calls**

Run: `grep -n 'ensure_static_vm_ready' unified_supervisor.py`
Expected: Only the one in the handoff pattern (task 6) and the fallback inside the `if not gcp_handoff_consumed:` block. Lines 73303 (old proactive) and 77390 (old Trinity) should be gone. Lines 85604 and 85868 may remain if they're in different contexts (non-boot paths).

- [ ] **Step 5: Commit**

```bash
git add unified_supervisor.py
git commit -m "feat(boot): add pre-warm consumers in _phase_resources, _intelligence, _trinity"
```

---

### Task 7: Fix Loading Page Redirect

**Files:**
- Modify: `loading_server.py` (~lines 7098-7110 and 7691-7705)
- Create: `tests/unit/core/test_loading_redirect.py`

- [ ] **Step 1: Write failing test for redirect URL resolution**

```python
# tests/unit/core/test_loading_redirect.py
"""Tests for loading page redirect URL resolution."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


class TestResolveRedirectUrl:
    @pytest.mark.asyncio
    async def test_explicit_env_var_wins(self):
        """JARVIS_FRONTEND_URL takes precedence over everything."""
        import os
        os.environ["JARVIS_FRONTEND_URL"] = "http://custom:4000"
        try:
            from loading_server import _resolve_redirect_url
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert url == "http://custom:4000"
        finally:
            os.environ.pop("JARVIS_FRONTEND_URL", None)

    @pytest.mark.asyncio
    async def test_legacy_env_var_fallback(self):
        """FRONTEND_URL is checked after JARVIS_FRONTEND_URL."""
        import os
        os.environ.pop("JARVIS_FRONTEND_URL", None)
        os.environ["FRONTEND_URL"] = "http://legacy:5000"
        try:
            from loading_server import _resolve_redirect_url
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert url == "http://legacy:5000"
        finally:
            os.environ.pop("FRONTEND_URL", None)

    @pytest.mark.asyncio
    async def test_fallback_to_api_when_no_frontend(self):
        """When no frontend responds, return API-only URL."""
        import os
        os.environ.pop("JARVIS_FRONTEND_URL", None)
        os.environ.pop("FRONTEND_URL", None)
        os.environ.pop("JARVIS_FRONTEND_PROBE_URLS", None)

        with patch("loading_server.aiohttp.ClientSession") as mock_session:
            # All probes fail
            mock_resp = AsyncMock()
            mock_resp.status = 503
            mock_session.return_value.__aenter__ = AsyncMock(return_value=mock_session.return_value)
            mock_session.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_session.return_value.get = AsyncMock(side_effect=ConnectionRefusedError)

            from loading_server import _resolve_redirect_url
            url = await _resolve_redirect_url(frontend_port=3000, backend_port=8010)
            assert "8010" in url  # Falls back to API URL
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/unit/core/test_loading_redirect.py -v --no-header 2>&1 | head -15`
Expected: ImportError — `_resolve_redirect_url` not defined yet

- [ ] **Step 3: Implement `_resolve_redirect_url` in `loading_server.py`**

Add near the `complete()` method (~line 7690):

```python
async def _resolve_redirect_url(
    frontend_port: int = 3000,
    backend_port: int = 8010,
    timeout: float = 5.0,
) -> str:
    """Resolve the redirect URL after boot completion.

    Precedence:
    1. JARVIS_FRONTEND_URL env var
    2. FRONTEND_URL env var (legacy)
    3. JARVIS_FRONTEND_PROBE_URLS (comma-separated, concurrent probe)
    4. Default probe: http://localhost:{frontend_port}
    5. Fallback: API-only URL
    """
    # 1. Explicit override
    url = os.getenv("JARVIS_FRONTEND_URL", "").strip()
    if url:
        return url

    # 2. Legacy fallback
    url = os.getenv("FRONTEND_URL", "").strip()
    if url:
        return url

    # 3+4. Probe candidate URLs concurrently
    probe_env = os.getenv("JARVIS_FRONTEND_PROBE_URLS", "").strip()
    if probe_env:
        candidates = [u.strip() for u in probe_env.split(",") if u.strip()]
    else:
        candidates = [f"http://localhost:{frontend_port}"]

    async def _probe(session, url):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3.0)) as resp:
                if resp.status < 400:
                    return url
        except Exception:
            pass
        return None

    try:
        async with aiohttp.ClientSession() as session:
            tasks = [_probe(session, u) for u in candidates]
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=timeout,
            )
            for r in results:
                if isinstance(r, str):
                    return r
    except Exception:
        pass

    # 5. Fallback — no frontend responded
    return f"http://localhost:{backend_port}"
```

- [ ] **Step 4: Update `complete()` method to use `_resolve_redirect_url`**

In the `complete()` method (~line 7691), replace:
```python
        frontend_url = os.getenv('FRONTEND_URL', 'http://localhost:3000')
```
with:
```python
        # Note: `config` is the module-level ServerConfig instance.
        # complete() is on StartupProgressReporter which does not hold config,
        # so access the module-level variable directly.
        frontend_url = await _resolve_redirect_url(
            frontend_port=config.frontend_port,
            backend_port=config.backend_port,
        )
```
Verify that `config` is accessible at this scope — it's a module-level `ServerConfig` instance in `loading_server.py`. If `complete()` doesn't have module scope access (e.g., it's in a class that shadows the name), pass `frontend_port` and `backend_port` as parameters to `complete()` from the caller.

Also update the watchdog redirect at ~line 7105:
```python
                            "redirect_url": f"http://localhost:{config.frontend_port}",
```
to:
```python
                            "redirect_url": await _resolve_redirect_url(
                                frontend_port=config.frontend_port,
                                backend_port=config.backend_port,
                            ),
```

- [ ] **Step 5: Run tests**

Run: `python3 -m pytest tests/unit/core/test_loading_redirect.py -v --no-header 2>&1 | tail -15`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add loading_server.py tests/unit/core/test_loading_redirect.py
git commit -m "fix(loading): resolve redirect URL from config with concurrent probes and API fallback"
```

---

### Task 8: Atomic Progress State + Env Cleanup

**Files:**
- Modify: `unified_supervisor.py` — startup state pattern, `.env.example`

- [ ] **Step 1: Audit heartbeat relay-only rule**

Read the heartbeat task in `unified_supervisor.py` around lines 93370-93420 (after the removed suppression block). Verify:
- The heartbeat reads `_startup_state` (or `_current_startup_phase` + `_current_startup_progress`)
- It does NOT have `_calculate_dynamic_progress()` or smoothing logic that creates a SECOND progress source
- If it does, document the behavior and ensure it doesn't conflict with the single-publisher invariant

Also grep for `_proactive_gcp_task` and clean up any remaining references:
```bash
grep -n '_proactive_gcp_task' unified_supervisor.py
```
Any remaining `self._proactive_gcp_task` attribute references (init, cleanup, checks) should either be removed or replaced with the pre-warmer pattern.

- [ ] **Step 2: Update startup state to use tuple**

Search for the pattern `self._current_startup_phase = ` followed by `self._current_startup_progress = ` within `_startup_impl`. There are approximately 8-10 instances. For each pair, replace with:

```python
self._startup_state = ("phase_name", progress_value)
```

Also add at the top of `_startup_impl`:

```python
        self._startup_state = ("initializing", 0)
```

And update the heartbeat reader to use:
```python
        phase, progress = getattr(self, '_startup_state', ('unknown', 0))
```

- [ ] **Step 3: Update `.env.example`**

Remove any `JARVIS_PARALLEL_BOOT` entries. Add:
```
# Pre-warming: set to true to disable background pre-warming during boot
# JARVIS_PREWARM_DISABLED=false
```

- [ ] **Step 4: Commit**

```bash
git add unified_supervisor.py .env.example
git commit -m "refactor(boot): atomic startup_state tuple, heartbeat audit, remove JARVIS_PARALLEL_BOOT from env"
```

---

### Task 9: Final Verification

- [ ] **Step 1: Run all pre-warmer tests**

Run: `python3 -m pytest tests/unit/core/test_startup_prewarmer.py tests/unit/core/test_loading_redirect.py -v`
Expected: All PASS

- [ ] **Step 2: Verify no parallel boot references remain**

Run:
```bash
grep -rn 'JARVIS_PARALLEL_BOOT\|ParallelBootOrchestrator\|_BootCLINarrator\|parallel_boot' --include='*.py' --include='*.env*' | grep -v '.pyc' | grep -v 'design.md' | grep -v 'plans/'
```
Expected: No matches

- [ ] **Step 3: Verify import chain**

Run: `python3 -c "from backend.core.startup_prewarmer import StartupPreWarmer; print('OK')"`
Expected: OK

- [ ] **Step 4: Verify `parallel_boot.py` is deleted**

Run: `ls backend/core/parallel_boot.py 2>&1`
Expected: No such file

- [ ] **Step 5: Commit any remaining cleanup**

```bash
git add -A
git commit -m "chore(boot): final cleanup — verify no parallel boot remnants"
```
